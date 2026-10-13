"""Regenerate embeddings for all interventions modified by phase 5 and phase 6.

Sources:
  - Phase 5: interventions whose contenu contains president name fragments
    (M. Macron, M. Sarkozy, M. Chirac, M. Hollande)
  - Phase 6: IDs from data/phase6_modified_ids.json (merged/renamed interventions)

Uses Vertex AI text-embedding-005 (dim=768) — same model as the rest of the corpus.
"""
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import vertexai
from psycopg2.extras import execute_values
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

PRESIDENT_PATTERNS = [
    ("M. Macron", "2017-05-14"),
    ("M. Sarkozy", "2007-05-16"),
    ("M. Chirac", "1995-05-17"),
    ("M. Hollande", "2012-05-15"),
]


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


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


def collect_all_ids(engine) -> list[int]:
    """Collect all intervention IDs modified by phase 5 and phase 6."""
    all_ids = set()

    # Phase 6 IDs from JSON
    phase6_path = Path("data/phase6_modified_ids.json")
    if phase6_path.exists():
        phase6_ids = json.loads(phase6_path.read_text())
        logger.info(f"Phase 6: loaded {len(phase6_ids)} IDs from {phase6_path}")
        all_ids.update(phase6_ids)

    # Phase 5 IDs (president name mentions in contenu)
    with engine.connect() as conn:
        for name, date_from in PRESIDENT_PATTERNS:
            rows = conn.execute(text("""
                SELECT i.id
                FROM interventions i
                JOIN debats d ON i.debat_id = d.id
                WHERE i.contenu LIKE :pattern
                  AND i.orateur != :name
                  AND d.date > :date_from
                  AND LENGTH(i.contenu) >= 50
            """), {"pattern": f"% {name} %", "name": name, "date_from": date_from}).fetchall()
            ids = {r[0] for r in rows}
            logger.info(f"Phase 5 '{name}': {len(ids)} IDs")
            all_ids.update(ids)

    # Filter: keep only IDs that still exist with >= 50 chars
    with engine.connect() as conn:
        existing = conn.execute(text("""
            SELECT id FROM interventions
            WHERE id = ANY(:ids) AND LENGTH(contenu) >= 50
        """), {"ids": sorted(all_ids)}).fetchall()
        valid_ids = sorted(r[0] for r in existing)

    logger.info(f"Total unique IDs: {len(all_ids)}, valid (exist + >= 50 chars): {len(valid_ids)}")
    return valid_ids


def main():
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    model = TextEmbeddingModel.from_pretrained(MODEL_NAME)
    engine = create_engine(DATABASE_URL)

    # Step 1: Collect all IDs
    intervention_ids = collect_all_ids(engine)
    if not intervention_ids:
        logger.info("Nothing to do.")
        return

    # Step 2: Delete stale embeddings
    logger.info("Deleting stale embeddings...")
    with engine.begin() as conn:
        result = conn.execute(text(
            "DELETE FROM chunk_embeddings WHERE intervention_id = ANY(:ids)"
        ), {"ids": intervention_ids})
        logger.info(f"Deleted {result.rowcount} stale embeddings")

    # Step 3: Load interventions
    logger.info("Loading interventions...")
    with engine.connect() as conn:
        interventions = conn.execute(text("""
            SELECT i.id, i.orateur, i.fonction, i.contenu, i.debat_id, i.ordre,
                   d.chambre, d.date, d.etape
            FROM interventions i
            JOIN debats d ON i.debat_id = d.id
            WHERE i.id = ANY(:ids)
            ORDER BY i.id
        """), {"ids": intervention_ids}).fetchall()

    logger.info(f"Loaded {len(interventions)} interventions")

    # Step 4: Build chunks and embed with Vertex AI
    items = []
    for iid, orateur, fonction, contenu, debat_id, ordre, chambre, date, etape in interventions:
        speaker = orateur or "Inconnu"
        if fonction:
            speaker += f" ({fonction})"
        chunk_text = f"{speaker}: {contenu}"
        if len(chunk_text) > MAX_TEXT_CHARS:
            chunk_text = chunk_text[:MAX_TEXT_CHARS]
        h = _content_hash(chunk_text)
        metadata = json.dumps({
            "orateur": orateur,
            "fonction": fonction,
            "chambre": chambre or "AN",
            "date": date.isoformat() if date else None,
            "etape": etape,
        })
        items.append((iid, debat_id, ordre or 0, chunk_text, h, metadata))

    total = len(items)
    inserted = 0
    errors = 0
    start = time.time()

    for offset in range(0, total, 5000):
        page = items[offset:offset + 5000]

        # Embed with parallel workers
        all_embeddings = [None] * len(page)
        sub_batches = []
        for i in range(0, len(page), BATCH_SIZE):
            sub_batches.append((i, [it[3] for it in page[i:i + BATCH_SIZE]]))

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
        for idx, (iid, debat_id, ordre, chunk_text, h, metadata) in enumerate(page):
            if all_embeddings[idx] is None:
                continue
            vec_str = "[" + ",".join(str(v) for v in all_embeddings[idx]) + "]"
            rows.append((
                "intervention", chunk_text, h, metadata, str(debat_id), ordre,
                vec_str, MODEL_NAME, DIMENSIONS, now,
                iid, debat_id,
            ))

        if rows:
            with engine.begin() as conn:
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
        logger.info(
            f"{offset + len(page):,}/{total:,} "
            f"| Inserted: {inserted:,} | Errors: {errors} | Rate: {rate:.0f}/s"
        )

    logger.info(f"=== DONE: {inserted:,} inserted, {errors} errors in {elapsed / 60:.1f}min ===")


if __name__ == "__main__":
    main()
