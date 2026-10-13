"""
Embed all interventions and amendments using Gemini Embedding 001 via Vertex AI.

Uses text-embedding-005 (= Gemini Embedding 001) with 768 dimensions.
Writes directly to chunk_embeddings table with pgvector.
Parallelized API calls for maximum throughput.

Prerequisites:
    pip install google-cloud-aiplatform
    gcloud auth application-default login
    gcloud auth application-default set-quota-project intention-490715

Usage:
    python scripts/embed_gemini.py              # Full embedding
    python scripts/embed_gemini.py --resume     # Resume from where we left off
    python scripts/embed_gemini.py --test       # Test with 500 interventions
"""
import hashlib
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import vertexai
from sqlalchemy import text
from vertexai.language_models import TextEmbeddingModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

sys.path.insert(0, ".")
from src.db.models import get_session
from src.indexing.chunking import strip_procedural_preamble

PROJECT_ID = "intention-490715"
LOCATION = "us-central1"
MODEL_NAME = "text-embedding-005"
OUTPUT_DIM = 768

PAGE_SIZE = 5000
EMBED_BATCH_SIZE = 20  # texts per API call (~20K token limit)
NUM_WORKERS = 10  # parallel API calls
INSERT_BATCH_SIZE = 1000
MAX_TEXT_CHARS = 5000

TEST_MODE = "--test" in sys.argv
RESUME_MODE = "--resume" in sys.argv


def content_hash(text_content: str) -> str:
    return hashlib.sha256(text_content.encode("utf-8")).hexdigest()


def embed_batch_safe(model, texts: list[str]) -> list[list[float]]:
    """Embed a batch with retry, auto-split on token limit, rate limit backoff."""
    max_retries = 8
    for attempt in range(max_retries):
        try:
            result = model.get_embeddings(texts, output_dimensionality=OUTPUT_DIM)
            return [e.values for e in result]
        except Exception as e:
            err = str(e)
            if "token count" in err or "20000" in err:
                if len(texts) <= 1:
                    texts = [t[:3000] for t in texts]
                    continue
                mid = len(texts) // 2
                left = embed_batch_safe(model, texts[:mid])
                right = embed_batch_safe(model, texts[mid:])
                return left + right
            elif "429" in err or "quota" in err.lower() or "resource" in err.lower():
                wait = min(2 ** attempt * 2, 60)
                logger.warning(f"Rate limited, waiting {wait}s")
                time.sleep(wait)
            elif attempt < max_retries - 1:
                logger.warning(f"Error: {err[:100]}, retry {attempt+1}")
                time.sleep(1)
            else:
                raise


def bulk_insert(rows: list[tuple]) -> int:
    """Bulk insert into chunk_embeddings."""
    if not rows:
        return 0
    from psycopg2.extras import execute_values

    session = get_session()
    raw_conn = session.connection().connection
    try:
        with raw_conn.cursor() as cur:
            execute_values(
                cur,
                """INSERT INTO chunk_embeddings
                (chunk_type, content, content_hash, chunk_metadata, source_id, position,
                 embedding_vector, embedding_model, embedding_dim, created_at,
                 intervention_id, debat_id)
                VALUES %s ON CONFLICT (content_hash) DO NOTHING""",
                rows,
                template="(%s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s, %s)",
            )
            inserted = cur.rowcount
        session.commit()
    except Exception:
        raw_conn.rollback()
        raise
    finally:
        session.close()
    return inserted


def process_embed_batch(model, batch_texts, batch_data, now):
    """Process one embedding batch: embed + prepare insert rows. Thread-safe."""
    vectors = embed_batch_safe(model, batch_texts)
    rows = []
    for (h, metadata, source_id, position, interv_id, debat_id, chunk_text), vector in zip(batch_data, vectors):
        vector_str = "[" + ",".join(str(x) for x in vector) + "]"
        rows.append((
            "intervention", chunk_text, h, json.dumps(metadata),
            source_id, position, vector_str, MODEL_NAME,
            OUTPUT_DIM, now, interv_id, debat_id,
        ))
    return rows


def run():
    logger.info(f"Initializing Vertex AI (project={PROJECT_ID})")
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    model = TextEmbeddingModel.from_pretrained(MODEL_NAME)
    logger.info(f"Model: {MODEL_NAME}, dim={OUTPUT_DIM}, workers={NUM_WORKERS}")

    # Load existing hashes for dedup/resume
    session = get_session()
    existing_hashes = set()
    if RESUME_MODE:
        logger.info("Loading existing content hashes for resume...")
        existing_hashes = set(
            row[0] for row in session.execute(
                text("SELECT content_hash FROM chunk_embeddings")
            ).fetchall()
        )
        logger.info(f"Found {len(existing_hashes):,} existing chunks to skip")

    total = session.execute(
        text("SELECT COUNT(*) FROM interventions WHERE LENGTH(contenu) >= 50")
    ).scalar()
    session.close()
    logger.info(f"Total interventions (>=50 chars): {total:,}")

    if TEST_MODE:
        total = min(total, 500)
        logger.info(f"TEST MODE: {total} interventions")

    now = datetime.now(timezone.utc)
    prev_by_debate = {}
    hash_lock = threading.Lock()
    last_id = 0
    total_inserted = 0
    total_skipped = 0
    total_processed = 0
    start_time = time.time()

    while True:
        page_session = get_session()
        try:
            limit = PAGE_SIZE if not TEST_MODE else 500
            rows = page_session.execute(text("""
                SELECT i.id, i.debat_id, i.orateur, i.fonction, i.contenu, i.ordre,
                       d.chambre, d.date, d.etape
                FROM interventions i
                LEFT JOIN debats d ON d.id = i.debat_id
                WHERE i.id > :last_id AND LENGTH(i.contenu) >= 50
                ORDER BY i.id
                LIMIT :limit
            """), {"last_id": last_id, "limit": limit}).fetchall()
        finally:
            page_session.close()

        if not rows:
            break
        last_id = rows[-1][0]

        # Build chunks (sequential — needs prev_by_debate context)
        all_texts = []
        all_data = []

        for row in rows:
            interv_id, debat_id, orateur, fonction, contenu, ordre, chambre, date, etape = row

            speaker_label = f"{orateur} ({fonction or 'député'})"
            context_prefix = ""
            prev = prev_by_debate.get(debat_id)
            if prev:
                context_prefix = f"[{prev[0]}: {prev[1]}...]\n\n"

            cleaned = strip_procedural_preamble(contenu)
            chunk_text = f"{context_prefix}{speaker_label}: {cleaned}"
            if len(chunk_text) > MAX_TEXT_CHARS:
                chunk_text = chunk_text[:MAX_TEXT_CHARS]

            h = content_hash(chunk_text)
            if h in existing_hashes:
                total_skipped += 1
                prev_by_debate[debat_id] = (speaker_label, contenu[:150])
                continue

            with hash_lock:
                existing_hashes.add(h)

            metadata = {
                "orateur": orateur,
                "fonction": fonction,
                "chambre": chambre or "AN",
                "date": date.isoformat() if date else None,
                "etape": etape,
            }

            all_texts.append(chunk_text)
            all_data.append((h, metadata, str(debat_id), ordre or 0, interv_id, debat_id, chunk_text))
            prev_by_debate[debat_id] = (speaker_label, contenu[:150])

        # Split into EMBED_BATCH_SIZE sub-batches and embed in parallel
        sub_batches = []
        for i in range(0, len(all_texts), EMBED_BATCH_SIZE):
            sub_batches.append((
                all_texts[i:i + EMBED_BATCH_SIZE],
                all_data[i:i + EMBED_BATCH_SIZE],
            ))

        all_insert_rows = []
        with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
            futures = {
                executor.submit(process_embed_batch, model, texts, data, now): idx
                for idx, (texts, data) in enumerate(sub_batches)
            }
            for future in as_completed(futures):
                rows_to_insert = future.result()
                all_insert_rows.extend(rows_to_insert)

        # Bulk insert
        if all_insert_rows:
            for i in range(0, len(all_insert_rows), INSERT_BATCH_SIZE):
                batch = all_insert_rows[i:i + INSERT_BATCH_SIZE]
                inserted = bulk_insert(batch)
                total_inserted += inserted

        total_processed += len(rows)

        elapsed = time.time() - start_time
        rate = total_processed / elapsed if elapsed > 0 else 0
        remaining = total - total_processed
        eta = remaining / rate if rate > 0 else 0
        logger.info(
            f"{total_processed:,}/{total:,} ({total_processed/total*100:.1f}%) | "
            f"Ins: {total_inserted:,} Skip: {total_skipped:,} | "
            f"{rate:.0f}/s | ETA: {eta/60:.0f}min"
        )

        if TEST_MODE:
            break

    # Flush
    elapsed = time.time() - start_time
    logger.info(f"=== INTERVENTIONS DONE: {total_inserted:,} in {elapsed/60:.1f}min ===")

    if TEST_MODE:
        return

    # === AMENDMENTS ===
    logger.info("Processing amendments...")
    session = get_session()
    amend_total = session.execute(
        text("SELECT COUNT(*) FROM amendements WHERE LENGTH(contenu) >= 20")
    ).scalar()
    session.close()
    logger.info(f"Total amendments: {amend_total:,}")

    last_amend_id = 0
    amend_inserted = 0

    while True:
        page_session = get_session()
        try:
            rows = page_session.execute(text("""
                SELECT a.id, a.dossier_id, a.numero, a.auteur, a.objet,
                       a.contenu, a.expose_motifs, a.sort
                FROM amendements a WHERE a.id > :last_id AND LENGTH(a.contenu) >= 20
                ORDER BY a.id LIMIT :limit
            """), {"last_id": last_amend_id, "limit": PAGE_SIZE}).fetchall()
        finally:
            page_session.close()

        if not rows:
            break
        last_amend_id = rows[-1][0]

        all_texts = []
        all_data = []

        for row in rows:
            amend_id, dossier_id, numero, auteur, objet, contenu, expose, sort = row

            parts = [f"Amendement {numero or ''}"]
            if auteur:
                parts.append(f"par {auteur}")
            if objet:
                parts.append(f"Objet: {objet}")
            if contenu:
                parts.append(f"Contenu: {contenu}")
            if expose:
                parts.append(f"Exposé: {expose[:500]}")
            if sort:
                parts.append(f"Sort: {sort}")
            chunk_text = "\n".join(parts)
            if len(chunk_text) > MAX_TEXT_CHARS:
                chunk_text = chunk_text[:MAX_TEXT_CHARS]

            h = content_hash(chunk_text)
            if h in existing_hashes:
                continue
            existing_hashes.add(h)

            metadata = {"auteur": auteur, "numero": numero, "sort": sort}
            source_id = f"{dossier_id or 'unknown'}-{numero}"
            all_texts.append(chunk_text)
            all_data.append((h, metadata, source_id, amend_id, chunk_text))

        # Parallel embed
        sub_batches = []
        for i in range(0, len(all_texts), EMBED_BATCH_SIZE):
            sub_batches.append((all_texts[i:i+EMBED_BATCH_SIZE], all_data[i:i+EMBED_BATCH_SIZE]))

        all_rows = []
        with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
            def _embed_amend(texts, data):
                vectors = embed_batch_safe(model, texts)
                rows = []
                for (h, metadata, source_id, amend_id, chunk_text), vector in zip(data, vectors):
                    vector_str = "[" + ",".join(str(x) for x in vector) + "]"
                    rows.append((
                        "amendement", chunk_text, h, json.dumps(metadata),
                        source_id, 0, vector_str, MODEL_NAME,
                        OUTPUT_DIM, now, None, None,
                    ))
                return rows

            futures = {executor.submit(_embed_amend, t, d): i for i, (t, d) in enumerate(sub_batches)}
            for f in as_completed(futures):
                all_rows.extend(f.result())

        if all_rows:
            for i in range(0, len(all_rows), INSERT_BATCH_SIZE):
                inserted = bulk_insert(all_rows[i:i+INSERT_BATCH_SIZE])
                amend_inserted += inserted

        logger.info(f"Amendments: last_id={last_amend_id}")

    total_elapsed = time.time() - start_time
    logger.info(f"=== ALL DONE: {total_inserted:,} interventions + {amend_inserted:,} amendments in {total_elapsed/60:.1f}min ===")


if __name__ == "__main__":
    run()
