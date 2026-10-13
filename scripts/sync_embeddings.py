#!/usr/bin/env python3
"""Analyze and fix embedding gaps in the chunk_embeddings table.

Modes:
  --analyze (default)  Show gap statistics between source tables and embeddings.
  --run                Generate embeddings for missing chunks (streams & deduplicates).
  --clean              Remove orphaned embeddings (requires --confirm).

Examples:
  python scripts/sync_embeddings.py                          # Quick analysis
  python scripts/sync_embeddings.py --run --batch-size 200   # Embed missing chunks
  python scripts/sync_embeddings.py --run --dry-run          # Preview what would be done
  python scripts/sync_embeddings.py --clean --confirm        # Remove stale embeddings
"""

import argparse
import hashlib
import json
import logging
import sys
import time
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import func

sys.path.insert(0, ".")

from src.db.models import (
    Amendement,
    ChunkEmbedding,
    Debat,
    Intervention,
    get_session,
)
from src.indexing.chunking import Chunk, chunk_amendment, strip_procedural_preamble
from src.utils.config import get_settings

logger = logging.getLogger(__name__)

PAGE_SIZE = 5000


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _clean_utf8(text: str) -> str:
    """Normalize Unicode and replace invalid UTF-8 sequences."""
    clean = text.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
    clean = clean.replace("\ufffd", "")
    return unicodedata.normalize("NFC", clean)


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _bulk_insert_embeddings(session, chunks, embeddings, existing_hashes, embedding_model):
    """Bulk-insert chunk embeddings via raw SQL (replicates run_pipeline logic)."""
    from psycopg2.extras import execute_values

    data = []
    now = datetime.now(timezone.utc)
    for chunk, embedding in zip(chunks, embeddings):
        h = _content_hash(chunk.content)
        if h in existing_hashes:
            continue
        existing_hashes.add(h)
        vector_str = "[" + ",".join(str(x) for x in embedding) + "]"
        data.append((
            chunk.chunk_type,
            chunk.content,
            h,
            json.dumps(chunk.metadata),
            chunk.source_id,
            chunk.position,
            vector_str,
            embedding_model,
            len(embedding),
            now,
        ))

    if not data:
        return 0

    raw_conn = session.connection().connection
    try:
        with raw_conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO chunk_embeddings
                (chunk_type, content, content_hash, chunk_metadata, source_id, position,
                 embedding_vector, embedding_model, embedding_dim, created_at)
                VALUES %s
                ON CONFLICT (content_hash, source_id) DO NOTHING
                """,
                data,
                template="(%s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s)",
            )
            inserted = cur.rowcount
        session.commit()
    except Exception:
        raw_conn.rollback()
        raise

    return inserted


def _load_existing_hashes(chunk_type=None):
    """Load all content_hash values from chunk_embeddings."""
    session = get_session()
    q = session.query(ChunkEmbedding.content_hash)
    if chunk_type:
        q = q.filter(ChunkEmbedding.chunk_type == chunk_type)
    hashes = set(row[0] for row in q.all())
    session.close()
    return hashes


def _load_existing_hash_source_pairs(chunk_type=None):
    """Load all (content_hash, source_id) pairs from chunk_embeddings."""
    session = get_session()
    q = session.query(ChunkEmbedding.content_hash, ChunkEmbedding.source_id)
    if chunk_type:
        q = q.filter(ChunkEmbedding.chunk_type == chunk_type)
    pairs = set((row[0], row[1]) for row in q.all())
    session.close()
    return pairs


def _copy_embeddings_batch(session, items):
    """Copy existing embeddings to new source_ids (no Vertex API call needed).

    Args:
        items: list of (content_hash, source_id, chunk_metadata_json) tuples
    """
    if not items:
        return 0
    now = datetime.now(timezone.utc)
    raw_conn = session.connection().connection
    inserted = 0
    try:
        with raw_conn.cursor() as cur:
            for h, source_id, meta_json in items:
                cur.execute("""
                    INSERT INTO chunk_embeddings
                    (chunk_type, content, content_hash, chunk_metadata, source_id, position,
                     embedding_vector, embedding_model, embedding_dim, created_at)
                    SELECT chunk_type, content, content_hash, %s::jsonb, %s, position,
                           embedding_vector, embedding_model, embedding_dim, %s
                    FROM chunk_embeddings
                    WHERE content_hash = %s
                    LIMIT 1
                    ON CONFLICT (content_hash, source_id) DO NOTHING
                """, (meta_json, source_id, now, h))
                inserted += cur.rowcount
        session.commit()
    except Exception:
        raw_conn.rollback()
        raise
    return inserted


# ---------------------------------------------------------------------------
# Analyze
# ---------------------------------------------------------------------------

def analyze(min_date=None, chunk_type="all"):
    """Show gap statistics using fast SQL counts + per-debate gap detection."""
    session = get_session()

    print("=== Embedding Gap Analysis ===\n")

    if chunk_type in ("all", "intervention"):
        # Count source interventions
        q = session.query(func.count(Intervention.id)).filter(
            func.length(Intervention.contenu) >= 50
        )
        if min_date:
            q = q.join(Debat).filter(Debat.date >= min_date)
        n_interv = q.scalar()

        n_emb_interv = session.query(func.count(ChunkEmbedding.id)).filter(
            ChunkEmbedding.chunk_type == "intervention"
        ).scalar()

        # Count orphaned intervention embeddings (source_id not in debats)
        n_orphan_interv = session.execute(text("""
            SELECT COUNT(*) FROM chunk_embeddings ce
            WHERE ce.chunk_type = 'intervention'
              AND NOT EXISTS (SELECT 1 FROM debats d WHERE CAST(d.id AS TEXT) = ce.source_id)
        """)).scalar()
        # Count excess embeddings per debate (more embeddings than valid interventions)
        n_excess_interv = session.execute(text("""
            SELECT COALESCE(SUM(excess), 0) FROM (
                SELECT COALESCE(e.n_emb, 0) - COUNT(i.id) AS excess
                FROM debats d
                LEFT JOIN interventions i ON i.debat_id = d.id AND LENGTH(i.contenu) >= 50
                LEFT JOIN (
                    SELECT source_id, COUNT(*) AS n_emb
                    FROM chunk_embeddings WHERE chunk_type = 'intervention'
                    GROUP BY source_id
                ) e ON CAST(d.id AS TEXT) = e.source_id
                GROUP BY d.id, e.n_emb
                HAVING COALESCE(e.n_emb, 0) > COUNT(i.id)
            ) sub
        """)).scalar()

        gap = n_interv - n_emb_interv
        orphans = n_orphan_interv + n_excess_interv
        real_gap = gap + orphans  # positive = truly missing
        print(f"  Interventions (>=50 chars):  {n_interv:>10,}")
        print(f"  Intervention embeddings:     {n_emb_interv:>10,}")
        print(f"  Orphaned embeddings:         {orphans:>10,}  (no-source={n_orphan_interv:,}, excess={n_excess_interv:,})")
        print(f"  Real gap (missing):          {max(0, real_gap):>10,}")
        print()

        # Per-debate gap: debates with more interventions than embeddings
        date_filter = f"AND d.date >= '{min_date}'" if min_date else ""
        gap_sql = text(f"""
            SELECT d.date, d.chambre,
                   COUNT(i.id) AS n_interv,
                   COALESCE(e.n_emb, 0) AS n_emb,
                   COUNT(i.id) - COALESCE(e.n_emb, 0) AS gap
            FROM interventions i
            JOIN debats d ON i.debat_id = d.id
            LEFT JOIN (
                SELECT source_id, COUNT(*) AS n_emb
                FROM chunk_embeddings
                WHERE chunk_type = 'intervention'
                GROUP BY source_id
            ) e ON CAST(d.id AS TEXT) = e.source_id
            WHERE LENGTH(i.contenu) >= 50 {date_filter}
            GROUP BY d.id, d.date, d.chambre, e.n_emb
            HAVING COUNT(i.id) > COALESCE(e.n_emb, 0)
            ORDER BY COUNT(i.id) - COALESCE(e.n_emb, 0) DESC
            LIMIT 20
        """)
        gaps = session.execute(gap_sql).fetchall()
        if gaps:
            total_debate_gap = sum(r[4] for r in gaps)
            print(f"  Top debates with missing embeddings ({len(gaps)} shown, ~{total_debate_gap:,} missing):")
            for date, chambre, n_i, n_e, g in gaps:
                print(f"    {str(date):>12} {chambre or '?':<6}  {n_i:>6,} interv, {n_e:>6,} emb, gap={g:,}")
            print()

    if chunk_type in ("all", "amendement"):
        q = session.query(func.count(Amendement.id))
        if min_date:
            q = q.filter(Amendement.date_depot >= min_date)
        n_amend = q.scalar()

        n_emb_amend = session.query(func.count(ChunkEmbedding.id)).filter(
            ChunkEmbedding.chunk_type == "amendement"
        ).scalar()

        # Amendment source_id format is "dossier_id-numero", match on SPLIT_PART
        n_orphan_amend = session.execute(text("""
            SELECT COUNT(*) FROM chunk_embeddings ce
            WHERE ce.chunk_type = 'amendement'
              AND NOT EXISTS (
                  SELECT 1 FROM dossiers_legislatifs d
                  WHERE CAST(d.id AS TEXT) = SPLIT_PART(ce.source_id, '-', 1)
              )
        """)).scalar()
        n_excess_amend = 0  # Amendment duplicates handled by cleanup script

        gap_a = n_amend - n_emb_amend
        orphans_a = n_orphan_amend + n_excess_amend
        real_gap_a = gap_a + orphans_a
        print(f"  Amendements:                 {n_amend:>10,}")
        print(f"  Amendement embeddings:       {n_emb_amend:>10,}")
        print(f"  Orphaned embeddings:         {orphans_a:>10,}  (no-source={n_orphan_amend:,}, excess={n_excess_amend:,})")
        print(f"  Real gap (missing):          {max(0, real_gap_a):>10,}")
        print()

    # Embedding model distribution
    model_counts = (
        session.query(ChunkEmbedding.embedding_model, func.count(ChunkEmbedding.id))
        .group_by(ChunkEmbedding.embedding_model)
        .all()
    )
    if model_counts:
        print("  Embedding models in use:")
        for model, count in model_counts:
            print(f"    {model}: {count:,}")
        print()

    # Null vectors check
    null_vectors = session.execute(text(
        "SELECT COUNT(*) FROM chunk_embeddings WHERE embedding_vector IS NULL"
    )).scalar()
    if null_vectors:
        print(f"  WARNING: {null_vectors:,} embeddings with NULL vector (need migration)\n")

    # Time estimate for gap
    total_gap = 0
    if chunk_type in ("all", "intervention"):
        total_gap += max(0, gap)
    if chunk_type in ("all", "amendement"):
        total_gap += max(0, gap_a)

    if total_gap > 0:
        settings = get_settings()
        if settings.embedding_provider == "local":
            rate, label = 50, "local model (~50/s)"
        elif settings.embedding_provider == "vertex":
            rate, label = 200, "Vertex AI (~200/s)"
        else:
            rate, label = 200, "OpenAI (~200/s)"
        est = total_gap / rate
        if est < 60:
            est_str = f"~{est:.0f}s"
        elif est < 3600:
            est_str = f"~{est / 60:.0f}m {est % 60:.0f}s"
        else:
            est_str = f"~{est / 3600:.1f}h"
        print(f"  Estimated embedding time ({label}): {est_str}")
        print(f"\n  Run with --run to generate missing embeddings.")

    session.close()
    print()


# ---------------------------------------------------------------------------
# Run sync - streams through all source records, embeds only missing ones
# ---------------------------------------------------------------------------

def _find_debate_ids_with_gaps(min_date=None):
    """Find debate IDs where embedding count != intervention count.

    Detects both missing embeddings (n_interv > n_emb) and stale embeddings
    (n_emb > n_interv), which can hide missing valid embeddings.
    """
    session = get_session()
    date_filter = f"AND d.date >= '{min_date}'" if min_date else ""
    rows = session.execute(text(f"""
        SELECT d.id
        FROM interventions i
        JOIN debats d ON i.debat_id = d.id
        LEFT JOIN (
            SELECT source_id, COUNT(*) AS n_emb
            FROM chunk_embeddings
            WHERE chunk_type = 'intervention'
            GROUP BY source_id
        ) e ON CAST(d.id AS TEXT) = e.source_id
        WHERE LENGTH(i.contenu) >= 50 {date_filter}
        GROUP BY d.id, e.n_emb
        HAVING COUNT(i.id) != COALESCE(e.n_emb, 0)
    """)).fetchall()
    session.close()
    return {r[0] for r in rows}


def _stream_and_embed_interventions(
    embedding_service, embedding_model, existing_pairs, existing_hashes,
    batch_size, min_date, dry_run, gap_debate_ids,
):
    """Stream interventions from debates with gaps, embed or copy missing ones.

    Uses (content_hash, source_id) pairs to detect what's truly missing.
    If a hash exists under a different source_id, copies the vector (no API call).
    If a hash is entirely new, embeds via Vertex AI.
    """
    count_session = get_session()
    total = count_session.query(func.count(Intervention.id)).filter(
        func.length(Intervention.contenu) >= 50,
        Intervention.debat_id.in_(gap_debate_ids),
    ).scalar()
    count_session.close()

    print(f"  {len(gap_debate_ids):,} debates with gaps, {total:,} interventions to scan", flush=True)

    prev_by_debate = {}
    embed_batch = []      # chunks needing Vertex AI embedding
    copy_batch = []       # chunks that can be copied from existing embedding
    processed = 0
    inserted_total = 0
    copied_total = 0
    skipped = 0
    t0 = time.time()
    last_id = 0

    while True:
        page_session = get_session()
        try:
            rows = (
                page_session.query(Intervention)
                .options(selectinload(Intervention.debat))
                .filter(
                    Intervention.id > last_id,
                    func.length(Intervention.contenu) >= 50,
                    Intervention.debat_id.in_(gap_debate_ids),
                )
                .order_by(Intervention.id)
                .limit(PAGE_SIZE)
                .all()
            )
        finally:
            page_session.close()

        if not rows:
            break
        last_id = rows[-1].id

        for interv in rows:
            debat = interv.debat
            speaker_label = f"{interv.orateur} ({interv.fonction or 'député'})"

            context_prefix = ""
            prev = prev_by_debate.get(interv.debat_id)
            if prev:
                context_prefix = f"[{prev[0]}: {prev[1]}...]\n\n"

            cleaned = strip_procedural_preamble(interv.contenu)
            content = _clean_utf8(f"{context_prefix}{speaker_label}: {cleaned}")

            h = _content_hash(content)
            source_id = str(interv.debat_id)
            prev_by_debate[interv.debat_id] = (speaker_label, interv.contenu[:150])
            processed += 1

            pair = (h, source_id)
            if pair in existing_pairs:
                skipped += 1
                continue

            metadata = {
                "orateur": interv.orateur,
                "fonction": interv.fonction,
                "chambre": debat.chambre if debat else "AN",
                "date": debat.date.isoformat() if debat and debat.date else None,
                "etape": debat.etape if debat else None,
                "articles_mentionnes": interv.articles_mentionnes or [],
            }

            if h in existing_hashes:
                # Hash exists under different source_id → copy vector
                copy_batch.append((h, source_id, json.dumps(metadata)))
                existing_pairs.add(pair)
            else:
                # Entirely new content → needs embedding
                chunk = Chunk(
                    content=content,
                    metadata=metadata,
                    chunk_type="intervention",
                    source_id=source_id,
                    position=interv.ordre or 0,
                )
                embed_batch.append(chunk)
                existing_pairs.add(pair)
                existing_hashes.add(h)

            # Flush copy batch
            if len(copy_batch) >= 1000:
                if not dry_run:
                    write_session = get_session()
                    try:
                        n = _copy_embeddings_batch(write_session, copy_batch)
                        copied_total += n
                    finally:
                        write_session.close()
                else:
                    copied_total += len(copy_batch)
                copy_batch = []

            # Flush embed batch
            if len(embed_batch) >= batch_size:
                if dry_run:
                    inserted_total += len(embed_batch)
                else:
                    try:
                        texts = [c.content for c in embed_batch]
                        embeddings = embedding_service.generate_embeddings_batch(
                            texts, batch_size=100, use_cache=False
                        )
                        write_session = get_session()
                        try:
                            batch_dedup = set()
                            inserted = _bulk_insert_embeddings(
                                write_session, embed_batch, embeddings, batch_dedup, embedding_model
                            )
                            inserted_total += inserted
                        finally:
                            write_session.close()
                    except Exception as e:
                        logger.error(f"Failed intervention batch (skipping): {e}")
                embed_batch = []

                elapsed = time.time() - t0
                rate = processed / elapsed if elapsed > 0 else 0
                eta = (total - processed) / rate if rate > 0 else 0
                print(
                    f"  interventions: [{processed:,}/{total:,}] "
                    f"new={inserted_total:,} copied={copied_total:,} skip={skipped:,} "
                    f"{rate:.0f}/s ETA={eta:.0f}s",
                    flush=True,
                )

    # Flush remaining
    if copy_batch:
        if not dry_run:
            write_session = get_session()
            try:
                n = _copy_embeddings_batch(write_session, copy_batch)
                copied_total += n
            finally:
                write_session.close()
        else:
            copied_total += len(copy_batch)

    if embed_batch:
        if dry_run:
            inserted_total += len(embed_batch)
        else:
            try:
                texts = [c.content for c in embed_batch]
                embeddings = embedding_service.generate_embeddings_batch(
                    texts, batch_size=100, use_cache=False
                )
                write_session = get_session()
                try:
                    batch_dedup = set()
                    inserted = _bulk_insert_embeddings(
                        write_session, embed_batch, embeddings, batch_dedup, embedding_model
                    )
                    inserted_total += inserted
                finally:
                    write_session.close()
            except Exception as e:
                logger.error(f"Failed final intervention batch (skipping): {e}")

    elapsed = time.time() - t0
    total_added = inserted_total + copied_total
    print(f"  interventions: {processed:,} scanned, {inserted_total:,} embedded, "
          f"{copied_total:,} copied, {skipped:,} skipped ({elapsed:.1f}s)", flush=True)
    return total_added


def _stream_and_embed_amendments(
    embedding_service, embedding_model, existing_pairs, existing_hashes,
    batch_size, min_date, dry_run,
):
    """Stream amendments, build chunks, embed or copy missing ones."""
    embed_batch = []
    copy_batch = []
    processed = 0
    inserted_total = 0
    copied_total = 0
    skipped = 0

    count_session = get_session()
    q = count_session.query(func.count(Amendement.id))
    if min_date:
        q = q.filter(Amendement.date_depot >= min_date)
    total = q.scalar()
    count_session.close()

    print(f"  Scanning {total:,} amendments...", flush=True)
    t0 = time.time()
    last_id = 0

    while True:
        page_session = get_session()
        try:
            q = page_session.query(Amendement).options(
                selectinload(Amendement.article),
                selectinload(Amendement.dossier),
            ).filter(Amendement.id > last_id)
            if min_date:
                q = q.filter(Amendement.date_depot >= min_date)
            rows = q.order_by(Amendement.id).limit(PAGE_SIZE).all()
        finally:
            page_session.close()

        if not rows:
            break
        last_id = rows[-1].id

        for amend in rows:
            article_ref = amend.article.numero_article if amend.article else None
            chambre = amend.dossier.chambre if amend.dossier and amend.dossier.chambre else "AN"

            chunk = chunk_amendment(
                numero=amend.numero,
                auteur=amend.auteur,
                objet=_clean_utf8(amend.objet or ""),
                contenu=_clean_utf8(amend.contenu or ""),
                expose_motifs=_clean_utf8(amend.expose_motifs or ""),
                sort=amend.sort,
                article_ref=article_ref,
                chambre=chambre,
                dossier_ref=str(amend.dossier_id) if amend.dossier_id else None,
                date=amend.date_depot.isoformat() if amend.date_depot else None,
            )
            processed += 1

            h = _content_hash(chunk.content)
            source_id = chunk.source_id
            pair = (h, source_id)

            if pair in existing_pairs:
                skipped += 1
                continue

            if h in existing_hashes:
                copy_batch.append((h, source_id, json.dumps(chunk.metadata)))
                existing_pairs.add(pair)
            else:
                embed_batch.append(chunk)
                existing_pairs.add(pair)
                existing_hashes.add(h)

            if len(copy_batch) >= 1000:
                if not dry_run:
                    ws = get_session()
                    try:
                        n = _copy_embeddings_batch(ws, copy_batch)
                        copied_total += n
                    finally:
                        ws.close()
                else:
                    copied_total += len(copy_batch)
                copy_batch = []

            if len(embed_batch) >= batch_size:
                if dry_run:
                    inserted_total += len(embed_batch)
                else:
                    try:
                        texts = [c.content for c in embed_batch]
                        embeddings = embedding_service.generate_embeddings_batch(
                            texts, batch_size=100, use_cache=False
                        )
                        ws = get_session()
                        try:
                            batch_dedup = set()
                            inserted = _bulk_insert_embeddings(
                                ws, embed_batch, embeddings, batch_dedup, embedding_model
                            )
                            inserted_total += inserted
                        finally:
                            ws.close()
                    except Exception as e:
                        logger.error(f"Failed amendment batch (skipping): {e}")
                embed_batch = []

                elapsed = time.time() - t0
                rate = processed / elapsed if elapsed > 0 else 0
                eta = (total - processed) / rate if rate > 0 else 0
                print(
                    f"  amendments: [{processed:,}/{total:,}] "
                    f"new={inserted_total:,} copied={copied_total:,} skip={skipped:,} "
                    f"{rate:.0f}/s ETA={eta:.0f}s",
                    flush=True,
                )

    # Flush remaining
    if copy_batch:
        if not dry_run:
            ws = get_session()
            try:
                n = _copy_embeddings_batch(ws, copy_batch)
                copied_total += n
            finally:
                ws.close()
        else:
            copied_total += len(copy_batch)

    if embed_batch:
        if dry_run:
            inserted_total += len(embed_batch)
        else:
            try:
                texts = [c.content for c in embed_batch]
                embeddings = embedding_service.generate_embeddings_batch(
                    texts, batch_size=100, use_cache=False
                )
                ws = get_session()
                try:
                    batch_dedup = set()
                    inserted = _bulk_insert_embeddings(
                        ws, embed_batch, embeddings, batch_dedup, embedding_model
                    )
                    inserted_total += inserted
                finally:
                    ws.close()
            except Exception as e:
                logger.error(f"Failed final amendment batch (skipping): {e}")

    elapsed = time.time() - t0
    total_added = inserted_total + copied_total
    print(f"  amendments: {processed:,} scanned, {inserted_total:,} embedded, "
          f"{copied_total:,} copied, {skipped:,} skipped ({elapsed:.1f}s)", flush=True)
    return total_added


def run_sync(min_date=None, chunk_type="all", batch_size=500, dry_run=False):
    """Generate embeddings for missing chunks by streaming through source tables."""
    from src.indexing.embeddings import EmbeddingService

    settings = get_settings()
    if settings.embedding_provider == "openai":
        embedding_model = settings.embedding_model
    elif settings.embedding_provider == "vertex":
        embedding_model = settings.vertex_embedding_model
    else:
        embedding_model = settings.local_embedding_model

    if dry_run:
        embedding_service = None
    else:
        embedding_service = EmbeddingService()

    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}Syncing embeddings (model={embedding_model}, batch={batch_size})...\n", flush=True)

    total_inserted = 0
    t0 = time.time()

    if chunk_type in ("all", "intervention"):
        gap_debate_ids = _find_debate_ids_with_gaps(min_date)
        if gap_debate_ids:
            print("  Loading intervention (hash, source_id) pairs...", flush=True)
            existing_pairs = _load_existing_hash_source_pairs("intervention")
            existing_hashes = {h for h, _ in existing_pairs}
            print(f"  Loaded {len(existing_pairs):,} pairs, {len(existing_hashes):,} unique hashes", flush=True)
            n = _stream_and_embed_interventions(
                embedding_service, embedding_model, existing_pairs, existing_hashes,
                batch_size, min_date, dry_run, gap_debate_ids,
            )
            total_inserted += n
        else:
            print("  No intervention embedding gaps found.", flush=True)

    if chunk_type in ("all", "amendement"):
        print("  Loading amendment (hash, source_id) pairs...", flush=True)
        existing_pairs_a = _load_existing_hash_source_pairs("amendement")
        existing_hashes_a = {h for h, _ in existing_pairs_a}
        print(f"  Loaded {len(existing_pairs_a):,} pairs, {len(existing_hashes_a):,} unique hashes", flush=True)

        n = _stream_and_embed_amendments(
            embedding_service, embedding_model, existing_pairs_a, existing_hashes_a,
            batch_size, min_date, dry_run,
        )
        total_inserted += n

    elapsed = time.time() - t0
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Done! {total_inserted:,} embeddings "
          f"{'would be ' if dry_run else ''}inserted in {elapsed:.1f}s", flush=True)


# ---------------------------------------------------------------------------
# Clean orphaned
# ---------------------------------------------------------------------------

def clean_orphaned(chunk_type="all", confirm=False):
    """Remove orphaned chunk_embeddings whose source_id has no matching debate/amendment."""
    session = get_session()
    orphaned_count = 0

    print("=== Orphaned Embedding Detection ===\n")

    if chunk_type in ("all", "intervention"):
        # Find intervention embeddings whose source_id (debat_id) doesn't exist in debats
        n = session.execute(text("""
            SELECT COUNT(*) FROM chunk_embeddings ce
            WHERE ce.chunk_type = 'intervention'
              AND NOT EXISTS (
                  SELECT 1 FROM debats d WHERE CAST(d.id AS TEXT) = ce.source_id
              )
        """)).scalar()
        print(f"  Orphaned intervention embeddings (no matching debate): {n:,}")
        orphaned_count += n

    if chunk_type in ("all", "amendement"):
        n = session.execute(text("""
            SELECT COUNT(*) FROM chunk_embeddings ce
            WHERE ce.chunk_type = 'amendement'
              AND NOT EXISTS (
                  SELECT 1 FROM dossiers_legislatifs d WHERE CAST(d.id AS TEXT) = ce.source_id
              )
        """)).scalar()
        print(f"  Orphaned amendment embeddings (no matching dossier):   {n:,}")
        orphaned_count += n

    if orphaned_count == 0:
        print("\n  No orphaned embeddings found.")
        session.close()
        return

    if not confirm:
        print(f"\n  Total orphaned: {orphaned_count:,}")
        print(f"  Re-run with --clean --confirm to delete them.")
        session.close()
        return

    deleted = 0
    if chunk_type in ("all", "intervention"):
        r = session.execute(text("""
            DELETE FROM chunk_embeddings
            WHERE chunk_type = 'intervention'
              AND NOT EXISTS (
                  SELECT 1 FROM debats d WHERE CAST(d.id AS TEXT) = source_id
              )
        """))
        deleted += r.rowcount

    if chunk_type in ("all", "amendement"):
        r = session.execute(text("""
            DELETE FROM chunk_embeddings
            WHERE chunk_type = 'amendement'
              AND NOT EXISTS (
                  SELECT 1 FROM dossiers_legislatifs d WHERE CAST(d.id AS TEXT) = source_id
              )
        """))
        deleted += r.rowcount

    session.commit()
    session.close()
    print(f"\n  Deleted {deleted:,} orphaned embeddings.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analyze and fix embedding gaps in chunk_embeddings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--analyze", action="store_true", default=True,
                       help="Show gap statistics (default)")
    mode.add_argument("--run", action="store_true",
                       help="Generate missing embeddings")
    mode.add_argument("--clean", action="store_true",
                       help="Remove orphaned embeddings")

    parser.add_argument("--min-date",
                        help="Only process debates from this date (YYYY-MM-DD)")
    parser.add_argument("--type", choices=["intervention", "amendement", "all"],
                        default="all", help="Chunk type to process (default: all)")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Embedding batch size (default: 500)")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --run: show what would be done")
    parser.add_argument("--confirm", action="store_true",
                        help="With --clean: confirm deletion")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.run:
        run_sync(
            min_date=args.min_date,
            chunk_type=args.type,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
    elif args.clean:
        clean_orphaned(chunk_type=args.type, confirm=args.confirm)
    else:
        analyze(min_date=args.min_date, chunk_type=args.type)


if __name__ == "__main__":
    main()
