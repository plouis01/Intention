"""
Embed the ~117K missing interventions (duplicated content that was skipped by content_hash dedup).

Uses intervention_id in the hash so each intervention gets its own embedding,
even if the text content is identical to another intervention.
"""
import hashlib
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import vertexai
from sqlalchemy import create_engine, text
from vertexai.language_models import TextEmbeddingModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = "postgresql://localhost/legislateur_local"
PROJECT_ID = "intention-490715"
LOCATION = "us-central1"
MODEL_NAME = "text-embedding-005"
DIMENSIONS = 768
BATCH_SIZE = 20
WORKERS = 10
MAX_TEXT_CHARS = 2000


def unique_hash(intervention_id: int, text_content: str) -> str:
    """Hash that includes intervention_id to avoid dedup of identical content."""
    return hashlib.sha256(f"iid:{intervention_id}:{text_content}".encode("utf-8")).hexdigest()


def embed_batch_safe(model, texts: list[str]) -> list[list[float]]:
    max_retries = 8
    for attempt in range(max_retries):
        try:
            result = model.get_embeddings(texts, output_dimensionality=DIMENSIONS)
            return [e.values for e in result]
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err or "503" in err:
                wait = min(2 ** attempt, 30)
                time.sleep(wait)
                continue
            if "limit" in err.lower() and len(texts) > 1:
                mid = len(texts) // 2
                left = embed_batch_safe(model, texts[:mid])
                right = embed_batch_safe(model, texts[mid:])
                return left + right
            raise
    raise RuntimeError(f"Failed after {max_retries} retries")


def main():
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    model = TextEmbeddingModel.from_pretrained(MODEL_NAME)
    engine = create_engine(DATABASE_URL)

    # Find missing interventions
    logger.info("Finding missing interventions...")
    with engine.connect() as conn:
        missing = conn.execute(text("""
            SELECT i.id, i.orateur, i.fonction, i.contenu, i.debat_id
            FROM interventions i
            LEFT JOIN chunk_embeddings ce ON ce.intervention_id = i.id
            WHERE LENGTH(i.contenu) >= 50
            AND ce.id IS NULL
            ORDER BY i.id
        """)).fetchall()

    total = len(missing)
    logger.info(f"Missing interventions to embed: {total:,}")

    if total == 0:
        logger.info("Nothing to do!")
        return

    # Process in batches
    inserted = 0
    errors = 0
    start = time.time()

    for offset in range(0, total, 5000):
        page = missing[offset:offset + 5000]

        # Build texts
        items = []
        for iid, orateur, fonction, contenu, debat_id in page:
            speaker = orateur or "Inconnu"
            if fonction:
                speaker += f" ({fonction})"
            chunk_text = f"{speaker}: {contenu}"
            if len(chunk_text) > MAX_TEXT_CHARS:
                chunk_text = chunk_text[:MAX_TEXT_CHARS]
            h = unique_hash(iid, chunk_text)
            items.append((iid, debat_id, orateur, chunk_text, h))

        # Embed with parallel workers
        all_embeddings = [None] * len(items)
        sub_batches = []
        for i in range(0, len(items), BATCH_SIZE):
            sub_batches.append((i, [it[3] for it in items[i:i + BATCH_SIZE]]))

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {}
            for start_idx, texts in sub_batches:
                f = executor.submit(embed_batch_safe, model, texts)
                futures[f] = start_idx
            for f in as_completed(futures):
                start_idx = futures[f]
                try:
                    vecs = f.result()
                    for j, vec in enumerate(vecs):
                        all_embeddings[start_idx + j] = vec
                except Exception as e:
                    logger.error(f"Batch at {start_idx} failed: {e}")
                    errors += 1

        # Insert
        rows = []
        now = datetime.now(timezone.utc)
        for idx, (iid, debat_id, orateur, chunk_text, h) in enumerate(items):
            if all_embeddings[idx] is None:
                continue
            vec_str = "[" + ",".join(str(v) for v in all_embeddings[idx]) + "]"
            rows.append((
                "intervention", chunk_text, h, "{}", None, 0,
                vec_str, MODEL_NAME, DIMENSIONS, now,
                iid, debat_id
            ))

        if rows:
            with engine.begin() as conn:
                from psycopg2.extras import execute_values
                raw = conn.connection.connection
                cur = raw.cursor()
                execute_values(cur, """
                    INSERT INTO chunk_embeddings
                    (chunk_type, content, content_hash, chunk_metadata, source_id, position,
                     embedding_vector, embedding_model, embedding_dim, created_at,
                     intervention_id, debat_id)
                    VALUES %s ON CONFLICT (content_hash) DO NOTHING""",
                    rows,
                    template="(%s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s, %s)",
                    page_size=500,
                )
                inserted += cur.rowcount

        elapsed = time.time() - start
        rate = (offset + len(page)) / elapsed if elapsed > 0 else 0
        logger.info(f"{offset + len(page):,}/{total:,} ({(offset + len(page))/total*100:.1f}%) | Inserted: {inserted:,} | Errors: {errors} | Rate: {rate:.0f}/s")

    logger.info(f"=== DONE: {inserted:,} inserted, {errors} errors in {elapsed/60:.1f}min ===")


if __name__ == "__main__":
    main()
