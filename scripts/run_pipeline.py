#!/usr/bin/env python3
"""
Main data pipeline script for collecting and importing legislative data.

Usage:
    python scripts/run_pipeline.py [--collect] [--import] [--embed] [--reset]

Options:
    --collect   Run data collection from sources
    --import    Run database import (laws, articles, debates, amendments)
    --embed     Generate embeddings for search (Phase 2)
    --validate  Run data validation
    --reset     Reset the database before import
    --all       Run full pipeline (collect + import + validate)
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.utils.config import get_settings

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(project_root / "data" / "pipeline.log"),
    ],
)
logger = logging.getLogger(__name__)


def collect_data(laws_file: Path, output_dir: Path) -> None:
    """
    Run all data collectors.

    Args:
        laws_file: Path to laws reference JSON
        output_dir: Output directory for raw data
    """
    logger.info("Starting data collection...")

    # Collect from Légifrance
    logger.info("Collecting from Légifrance API...")
    try:
        from src.collectors.legifrance import collect_law_texts

        collect_law_texts(laws_file, output_dir / "legifrance")
        logger.info("Légifrance collection complete")
    except Exception as e:
        logger.error(f"Légifrance collection failed: {e}")
        logger.warning("Continuing with other sources...")

    # Collect from Assemblée Nationale
    logger.info("Collecting from Assemblée Nationale...")
    try:
        from src.collectors.assemblee import collect_an_data

        collect_an_data(laws_file, output_dir)
        logger.info("AN collection complete")
    except Exception as e:
        logger.error(f"AN collection failed: {e}")
        logger.warning("Continuing with other sources...")

    # Collect from Sénat
    logger.info("Collecting from Sénat...")
    try:
        from src.collectors.senat import collect_senat_data

        collect_senat_data(laws_file, output_dir)
        logger.info("Sénat collection complete")
    except Exception as e:
        logger.error(f"Sénat collection failed: {e}")

    logger.info("Data collection finished")


def import_data(laws_file: Path, raw_data_dir: Path, reset: bool = False) -> dict:
    """
    Run database import.

    Args:
        laws_file: Path to laws reference JSON
        raw_data_dir: Directory containing raw collected data
        reset: If True, reset database before import

    Returns:
        Dict with import counts
    """
    logger.info("Starting database import...")

    from src.db.import_data import run_import

    counts = run_import(laws_file, raw_data_dir, reset_db=reset)

    logger.info(f"Import complete. Counts: {counts}")
    return counts


def validate_data() -> dict:
    """
    Run data validation checks.

    Returns:
        Dict with validation results
    """
    logger.info("Running data validation...")

    from src.db.models import (
        Amendement,
        Article,
        Debat,
        DossierLegislatif,
        Intervention,
        Loi,
        get_session,
    )

    session = get_session()
    results = {}

    try:
        # Count records
        results["lois"] = session.query(Loi).count()
        results["articles"] = session.query(Article).count()
        results["dossiers"] = session.query(DossierLegislatif).count()
        results["debats"] = session.query(Debat).count()
        results["interventions"] = session.query(Intervention).count()
        results["amendements"] = session.query(Amendement).count()

        # Check completeness
        results["lois_with_articles"] = (
            session.query(Loi)
            .join(Article)
            .distinct()
            .count()
        )

        results["lois_with_dossiers"] = (
            session.query(Loi)
            .join(DossierLegislatif)
            .distinct()
            .count()
        )

        # Check by type
        for loi_type in ["LF", "LFR", "LFSS"]:
            results[f"lois_{loi_type}"] = (
                session.query(Loi)
                .filter(Loi.type == loi_type)
                .count()
            )

        logger.info("Validation Results:")
        for key, value in results.items():
            logger.info(f"  {key}: {value}")

    finally:
        session.close()

    return results


def _checkpoint_path(output_dir: Path, store_name: str) -> Path:
    """Get base path for checkpoint files (without extension)."""
    return output_dir / f"{store_name}_checkpoint"


def _save_checkpoint(store, output_dir: Path, store_name: str) -> None:
    """Save a checkpoint of the current store state."""
    output_dir.mkdir(parents=True, exist_ok=True)
    base = _checkpoint_path(output_dir, store_name)
    store.save(base)
    logger.info(f"Checkpoint saved for {store_name}: {len(store)} chunks")


def _load_checkpoint(output_dir: Path, store_name: str):
    """Load checkpoint if it exists, returns None otherwise."""
    from src.indexing.embeddings import VectorStore

    base = _checkpoint_path(output_dir, store_name)
    npz_path = base.with_suffix(".npz")
    if not npz_path.exists():
        return None

    logger.info(f"Found checkpoint for {store_name}, resuming...")
    store = VectorStore()
    try:
        store.load(base)
    except Exception as e:
        logger.warning(f"Corrupted checkpoint for {store_name}, starting fresh: {e}")
        _delete_checkpoint(output_dir, store_name)
        return None
    logger.info(f"Resumed {len(store)} chunks from checkpoint")
    return store


def _delete_checkpoint(output_dir: Path, store_name: str) -> None:
    """Delete checkpoint files after successful completion."""
    base = _checkpoint_path(output_dir, store_name)
    for suffix in (".npz", ".meta.json"):
        p = base.with_suffix(suffix)
        if p.exists():
            p.unlink()


def _content_hash(content: str) -> str:
    """Compute SHA256 hash for dedup."""
    import hashlib
    return hashlib.sha256(content.encode()).hexdigest()


def _filter_new_chunks(chunks: list, existing_hashes: set) -> list:
    """Filter out chunks whose content hash already exists. Returns only new chunks."""
    return [c for c in chunks if _content_hash(c.content) not in existing_hashes]


def _bulk_insert_embeddings(session, chunks: list, embeddings: list, existing_hashes: set,
                            embedding_model: str) -> int:
    """Bulk-insert chunk embeddings to embedding_vector via raw SQL."""
    import json
    from datetime import datetime, timezone
    from psycopg2.extras import execute_values

    data = []
    now = datetime.now(timezone.utc)
    for chunk, embedding in zip(chunks, embeddings):
        content_hash = _content_hash(chunk.content)
        if content_hash in existing_hashes:
            continue
        existing_hashes.add(content_hash)
        vector_str = '[' + ','.join(str(x) for x in embedding) + ']'
        data.append((
            chunk.chunk_type,
            chunk.content,
            content_hash,
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
                template="(%s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s)"
            )
            inserted = cur.rowcount
        session.commit()
    except Exception:
        raw_conn.rollback()
        raise

    return inserted


def _generate_embeddings_to_database(batch_size: int = 500, min_date: str | None = None) -> dict:
    """Generate embeddings and persist them directly in chunk_embeddings.

    Optimized with:
    - In-memory hash set for dedup (no per-row SELECT)
    - Bulk inserts instead of individual ORM adds
    - Offset-based pagination (no long-lived server cursors that Railway kills)
    - Pre-filter: skip embedding for already-known content hashes

    Args:
        batch_size: Number of chunks per embedding+insert batch
        min_date: If set, only embed debates/amendments from this date onwards (YYYY-MM-DD)
    """
    logger.info("Generating embeddings with database backend (chunk_embeddings table)...")
    if min_date:
        logger.info(f"Filtering to debates from {min_date} onwards")

    from sqlalchemy.orm import selectinload
    from sqlalchemy.sql import func

    from src.db.models import ChunkEmbedding, Amendement, Debat, Intervention, get_session
    from src.indexing.chunking import Chunk, chunk_amendment, strip_procedural_preamble
    from src.indexing.embeddings import EmbeddingService

    session = get_session()
    embedding_service = EmbeddingService()
    settings = get_settings()
    if settings.embedding_provider == "openai":
        embedding_model = settings.embedding_model
    elif settings.embedding_provider == "vertex":
        embedding_model = settings.vertex_embedding_model
    else:
        embedding_model = settings.local_embedding_model

    # Pre-load all existing content hashes into memory for fast dedup
    logger.info("Loading existing content hashes for dedup...")
    existing_hashes = set(
        row[0] for row in session.query(ChunkEmbedding.content_hash).all()
    )
    logger.info(f"Loaded {len(existing_hashes)} existing hashes")

    existing_debate = session.query(func.count(ChunkEmbedding.id)).filter(
        ChunkEmbedding.chunk_type == "intervention"
    ).scalar()
    existing_amend = session.query(func.count(ChunkEmbedding.id)).filter(
        ChunkEmbedding.chunk_type == "amendement"
    ).scalar()
    session.close()

    results = {
        "debate_chunks": existing_debate,
        "amendment_chunks": existing_amend,
        "debate_inserted": 0,
        "amendment_inserted": 0,
    }

    PAGE_SIZE = 5000  # Fetch this many rows per DB query (short-lived connections)

    # === DEBATES ===
    logger.info("Generating debate chunks from interventions...")

    # Get total count for progress
    count_session = get_session()
    interv_q = count_session.query(func.count(Intervention.id)).filter(
        func.length(Intervention.contenu) >= 50
    )
    if min_date:
        interv_q = interv_q.join(Debat).filter(Debat.date >= min_date)
    total_interventions = interv_q.scalar()
    count_session.close()
    logger.info(f"Total interventions to process: {total_interventions:,}")

    prev_by_debate = {}
    debate_batch: list[Chunk] = []
    processed_debate = 0
    offset = 0

    while offset < total_interventions:
        # Fresh session per page - no long-lived cursors
        page_session = get_session()
        try:
            q = (
                page_session.query(Intervention)
                .options(selectinload(Intervention.debat))
                .filter(func.length(Intervention.contenu) >= 50)
            )
            if min_date:
                q = q.join(Debat).filter(Debat.date >= min_date)
            rows = (
                q.order_by(Intervention.id)
                .offset(offset)
                .limit(PAGE_SIZE)
                .all()
            )
        finally:
            page_session.close()

        if not rows:
            break

        for interv in rows:
            debat = interv.debat
            speaker_label = f"{interv.orateur} ({interv.fonction or 'député'})"

            context_prefix = ""
            prev = prev_by_debate.get(interv.debat_id)
            if prev:
                context_prefix = f"[{prev[0]}: {prev[1]}...]\n\n"

            cleaned_contenu = strip_procedural_preamble(interv.contenu)
            chunk = Chunk(
                content=f"{context_prefix}{speaker_label}: {cleaned_contenu}",
                metadata={
                    "orateur": interv.orateur,
                    "fonction": interv.fonction,
                    "chambre": debat.chambre if debat else "AN",
                    "date": debat.date.isoformat() if debat and debat.date else None,
                    "etape": debat.etape if debat else None,
                    "articles_mentionnes": interv.articles_mentionnes or [],
                },
                chunk_type="intervention",
                source_id=str(interv.debat_id),
                position=interv.ordre or 0,
            )
            prev_by_debate[interv.debat_id] = (speaker_label, interv.contenu[:150])
            debate_batch.append(chunk)
            processed_debate += 1

            if len(debate_batch) >= batch_size:
                new_chunks = _filter_new_chunks(debate_batch, existing_hashes)
                if new_chunks:
                    write_session = get_session()
                    try:
                        texts = [c.content for c in new_chunks]
                        embeddings = embedding_service.generate_embeddings_batch(
                            texts, batch_size=50, use_cache=False
                        )
                        inserted = _bulk_insert_embeddings(
                            write_session, new_chunks, embeddings, existing_hashes, embedding_model
                        )
                        results["debate_inserted"] += inserted
                    except Exception as e:
                        logger.error(f"Failed to insert debate batch at offset {offset}: {e}")
                    finally:
                        write_session.close()
                logger.info(
                    f"Processed {processed_debate} debate chunks "
                    f"(inserted {results['debate_inserted']} new, "
                    f"skipped {processed_debate - results['debate_inserted']})"
                )
                debate_batch = []

        offset += PAGE_SIZE

    # Flush remaining debate batch
    if debate_batch:
        new_chunks = _filter_new_chunks(debate_batch, existing_hashes)
        if new_chunks:
            write_session = get_session()
            try:
                texts = [c.content for c in new_chunks]
                embeddings = embedding_service.generate_embeddings_batch(
                    texts, batch_size=50, use_cache=False
                )
                inserted = _bulk_insert_embeddings(
                    write_session, new_chunks, embeddings, existing_hashes, embedding_model
                )
                results["debate_inserted"] += inserted
            except Exception as e:
                logger.error(f"Failed to insert final debate batch: {e}")
            finally:
                write_session.close()

    results["debate_chunks"] = existing_debate + results["debate_inserted"]
    logger.info(
        f"Debate embedding complete: {processed_debate} processed, "
        f"{results['debate_inserted']} inserted, total {results['debate_chunks']}"
    )

    # === AMENDMENTS ===
    logger.info("Generating amendment chunks...")

    count_session = get_session()
    amend_count_q = count_session.query(func.count(Amendement.id))
    if min_date:
        amend_count_q = amend_count_q.filter(Amendement.date_depot >= min_date)
    total_amendments = amend_count_q.scalar()
    count_session.close()
    logger.info(f"Total amendments to process: {total_amendments:,}")

    amend_batch: list[Chunk] = []
    processed_amend = 0
    offset = 0

    while offset < total_amendments:
        page_session = get_session()
        try:
            q = (
                page_session.query(Amendement)
                .options(selectinload(Amendement.article), selectinload(Amendement.dossier))
            )
            if min_date:
                q = q.filter(Amendement.date_depot >= min_date)
            rows = (
                q.order_by(Amendement.id)
                .offset(offset)
                .limit(PAGE_SIZE)
                .all()
            )
        finally:
            page_session.close()

        if not rows:
            break

        for amend in rows:
            article_ref = amend.article.numero_article if amend.article else None
            chambre = amend.dossier.chambre if amend.dossier and amend.dossier.chambre else "AN"

            chunk = chunk_amendment(
                numero=amend.numero,
                auteur=amend.auteur,
                objet=amend.objet,
                contenu=amend.contenu,
                expose_motifs=amend.expose_motifs,
                sort=amend.sort,
                article_ref=article_ref,
                chambre=chambre,
                dossier_ref=str(amend.dossier_id) if amend.dossier_id else None,
                date=amend.date_depot.isoformat() if amend.date_depot else None,
            )
            amend_batch.append(chunk)
            processed_amend += 1

            if len(amend_batch) >= batch_size:
                new_chunks = _filter_new_chunks(amend_batch, existing_hashes)
                if new_chunks:
                    write_session = get_session()
                    try:
                        texts = [c.content for c in new_chunks]
                        embeddings = embedding_service.generate_embeddings_batch(
                            texts, batch_size=50, use_cache=False
                        )
                        inserted = _bulk_insert_embeddings(
                            write_session, new_chunks, embeddings, existing_hashes, embedding_model
                        )
                        results["amendment_inserted"] += inserted
                    except Exception as e:
                        logger.error(f"Failed to insert amendment batch at offset {offset}: {e}")
                    finally:
                        write_session.close()
                logger.info(
                    f"Processed {processed_amend} amendment chunks "
                    f"(inserted {results['amendment_inserted']} new, "
                    f"skipped {processed_amend - results['amendment_inserted']})"
                )
                amend_batch = []

        offset += PAGE_SIZE

    # Flush remaining amendment batch
    if amend_batch:
        new_chunks = _filter_new_chunks(amend_batch, existing_hashes)
        if new_chunks:
            write_session = get_session()
            try:
                texts = [c.content for c in new_chunks]
                embeddings = embedding_service.generate_embeddings_batch(
                    texts, batch_size=50, use_cache=False
                )
                inserted = _bulk_insert_embeddings(
                    write_session, new_chunks, embeddings, existing_hashes, embedding_model
                )
                results["amendment_inserted"] += inserted
            except Exception as e:
                logger.error(f"Failed to insert final amendment batch: {e}")
            finally:
                write_session.close()

    results["amendment_chunks"] = existing_amend + results["amendment_inserted"]
    logger.info(
        f"Amendment embedding complete: {processed_amend} processed, "
        f"{results['amendment_inserted']} inserted, total {results['amendment_chunks']}"
    )

    logger.info(f"Database embedding generation complete: {results}")
    return results


def generate_embeddings(output_dir: Path, checkpoint_every: int = 50000,
                        min_date: str | None = None) -> dict:
    """
    Generate embeddings for all debates and amendments (Phase 2).

    Supports resuming from checkpoints if interrupted.

    Args:
        output_dir: Directory to save vector stores
        checkpoint_every: Save a checkpoint every N chunks (default: 50000)
        min_date: If set, only embed debates/amendments from this date onwards (YYYY-MM-DD)

    Returns:
        Dict with embedding counts
    """
    BATCH_SIZE = 100
    DB_BATCH_SIZE = 1000  # Larger batches for DB path (fewer network round-trips)
    CHECKPOINT_EVERY = max(1, checkpoint_every // BATCH_SIZE)  # convert chunks to batches

    settings = get_settings()
    logger.info("Generating embeddings (Phase 2)...")

    if settings.vector_store_provider == "database":
        return _generate_embeddings_to_database(batch_size=DB_BATCH_SIZE, min_date=min_date)

    from sqlalchemy import func
    from sqlalchemy.orm import selectinload

    from src.db.models import Amendement, Debat, Intervention, get_session
    from src.indexing.chunking import Chunk, chunk_amendment, chunk_by_intervention, strip_procedural_preamble
    from src.indexing.embeddings import EmbeddingService, VectorStore

    session = get_session()

    # Quick check: if stores already exist and match DB counts, skip entirely
    debate_npz = output_dir / "debate_store.npz"
    debate_meta = output_dir / "debate_store.meta.json"
    amend_npz = output_dir / "amendment_store.npz"
    amend_meta = output_dir / "amendment_store.meta.json"

    if all(p.exists() for p in (debate_npz, debate_meta, amend_npz, amend_meta)):
        import numpy as np
        db_interv_count = session.query(func.count(Intervention.id)).filter(
            func.length(Intervention.contenu) >= 50
        ).scalar()
        db_amend_count = session.query(func.count(Amendement.id)).scalar()

        with np.load(debate_npz) as d:
            existing_debate_count = d["embeddings"].shape[0]
        with np.load(amend_npz) as d:
            existing_amend_count = d["embeddings"].shape[0]

        if existing_debate_count >= db_interv_count and existing_amend_count >= db_amend_count:
            logger.info(
                f"Embeddings already complete: "
                f"debates {existing_debate_count}/{db_interv_count}, "
                f"amendments {existing_amend_count}/{db_amend_count}. Skipping."
            )
            session.close()
            return {
                "debate_chunks": existing_debate_count,
                "amendment_chunks": existing_amend_count,
            }
        else:
            logger.info(
                f"Embeddings incomplete: "
                f"debates {existing_debate_count}/{db_interv_count}, "
                f"amendments {existing_amend_count}/{db_amend_count}. Resuming..."
            )

    embedding_service = EmbeddingService()

    # Try to resume from checkpoints
    debate_store = _load_checkpoint(output_dir, "debate_store") or VectorStore()
    amendment_store = _load_checkpoint(output_dir, "amendment_store") or VectorStore()

    results = {
        "debate_chunks": len(debate_store),
        "amendment_chunks": len(amendment_store),
    }

    try:
        # === DEBATES ===
        logger.info("Generating debate chunks from interventions...")
        interventions = session.query(Intervention).options(
            selectinload(Intervention.debat)
        ).filter(
            func.length(Intervention.contenu) >= 50
        ).order_by(Intervention.debat_id, Intervention.ordre).yield_per(1000)

        # Track previous speaker per debate for conversational context
        prev_by_debate = {}  # debat_id -> (speaker_label, content_preview)

        # Build set of already-processed keys to skip on resume
        processed_debate_keys = {
            f"{c.source_id}:{c.position}" for c in debate_store.chunks
        }
        if processed_debate_keys:
            logger.info(f"Skipping {len(processed_debate_keys)} already-processed debate chunks")

        # Stream chunks: build batch → embed → store, without accumulating all in memory
        logger.info("Streaming debate chunks and embedding in batches...")
        debate_batch: list[Chunk] = []
        total_debate_processed = 0
        batch_num = 0

        for interv in interventions:
            key = f"{interv.debat_id}:{interv.ordre or 0}"
            if key in processed_debate_keys:
                continue

            debat = interv.debat
            speaker_label = f"{interv.orateur} ({interv.fonction or 'député'})"

            # Add previous speaker context for conversational flow
            context_prefix = ""
            prev = prev_by_debate.get(interv.debat_id)
            if prev:
                context_prefix = f"[{prev[0]}: {prev[1]}...]\n\n"

            # Strip procedural preamble to focus embedding on substantive content
            cleaned_contenu = strip_procedural_preamble(interv.contenu)

            chunk = Chunk(
                content=f"{context_prefix}{speaker_label}: {cleaned_contenu}",
                metadata={
                    "orateur": interv.orateur,
                    "fonction": interv.fonction,
                    "chambre": debat.chambre if debat else "AN",
                    "date": debat.date.isoformat() if debat and debat.date else None,
                    "etape": debat.etape if debat else None,
                    "articles_mentionnes": interv.articles_mentionnes or [],
                },
                chunk_type="intervention",
                source_id=str(interv.debat_id),
                position=interv.ordre or 0,
            )
            prev_by_debate[interv.debat_id] = (speaker_label, interv.contenu[:150])
            debate_batch.append(chunk)

            if len(debate_batch) >= BATCH_SIZE:
                texts = [c.content for c in debate_batch]
                embeddings = embedding_service.generate_embeddings_batch(texts, batch_size=50, use_cache=False)
                debate_store.add_batch(list(zip(debate_batch, embeddings)))
                total_debate_processed += len(debate_batch)
                batch_num += 1
                logger.info(f"Processed {total_debate_processed} debate chunks so far")
                debate_batch = []

                if batch_num % CHECKPOINT_EVERY == 0:
                    _save_checkpoint(debate_store, output_dir, "debate_store")

        # Flush remaining
        if debate_batch:
            texts = [c.content for c in debate_batch]
            embeddings = embedding_service.generate_embeddings_batch(texts, batch_size=50, use_cache=False)
            debate_store.add_batch(list(zip(debate_batch, embeddings)))
            total_debate_processed += len(debate_batch)

        logger.info(f"Debate embedding complete: {total_debate_processed} new chunks")
        results["debate_chunks"] = len(debate_store)

        # === AMENDMENTS ===
        logger.info("Generating amendment chunks...")
        from src.db.models import Article

        amendments = session.query(Amendement).options(
            selectinload(Amendement.article),
            selectinload(Amendement.dossier)
        ).yield_per(1000)

        # Build set of already-processed source_ids to skip on resume
        processed_amend_keys = {c.source_id for c in amendment_store.chunks}
        if processed_amend_keys:
            logger.info(f"Skipping {len(processed_amend_keys)} already-processed amendment chunks")

        # Stream chunks: build batch → embed → store
        logger.info("Streaming amendment chunks and embedding in batches...")
        amend_batch: list[Chunk] = []
        total_amend_processed = 0
        batch_num = 0

        for amend in amendments:
            source_id = f"{amend.dossier_id or 'unknown'}-{amend.numero}"
            if source_id in processed_amend_keys:
                continue

            article_ref = None
            if amend.article:
                article_ref = amend.article.numero_article

            chambre = "AN"
            if amend.dossier and amend.dossier.chambre:
                chambre = amend.dossier.chambre

            chunk = chunk_amendment(
                numero=amend.numero,
                auteur=amend.auteur,
                objet=amend.objet,
                contenu=amend.contenu,
                expose_motifs=amend.expose_motifs,
                sort=amend.sort,
                article_ref=article_ref,
                chambre=chambre,
                dossier_ref=str(amend.dossier_id) if amend.dossier_id else None,
                date=amend.date_depot.isoformat() if amend.date_depot else None,
            )
            amend_batch.append(chunk)

            if len(amend_batch) >= BATCH_SIZE:
                texts = [c.content for c in amend_batch]
                embeddings = embedding_service.generate_embeddings_batch(texts, batch_size=50, use_cache=False)
                amendment_store.add_batch(list(zip(amend_batch, embeddings)))
                total_amend_processed += len(amend_batch)
                batch_num += 1
                logger.info(f"Processed {total_amend_processed} amendment chunks so far")
                amend_batch = []

                if batch_num % CHECKPOINT_EVERY == 0:
                    _save_checkpoint(amendment_store, output_dir, "amendment_store")

        # Flush remaining
        if amend_batch:
            texts = [c.content for c in amend_batch]
            embeddings = embedding_service.generate_embeddings_batch(texts, batch_size=50, use_cache=False)
            amendment_store.add_batch(list(zip(amend_batch, embeddings)))
            total_amend_processed += len(amend_batch)

        logger.info(f"Amendment embedding complete: {total_amend_processed} new chunks")
        results["amendment_chunks"] = len(amendment_store)

        # Save final stores (new binary format)
        output_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Saving final vector stores...")
        debate_store.save(output_dir / "debate_store")
        amendment_store.save(output_dir / "amendment_store")

        # Build BM25 indices
        if settings.bm25_enabled:
            from src.indexing.bm25 import BM25Index

            logger.info("Building BM25 indices...")
            debate_bm25 = BM25Index()
            debate_bm25.build(debate_store.chunks)
            debate_bm25.save(output_dir / "debate_bm25_tokens.json")

            amendment_bm25 = BM25Index()
            amendment_bm25.build(amendment_store.chunks)
            amendment_bm25.save(output_dir / "amendment_bm25_tokens.json")
            logger.info("BM25 indices built and saved")

        # Clean up checkpoints
        _delete_checkpoint(output_dir, "debate_store")
        _delete_checkpoint(output_dir, "amendment_store")

        logger.info(f"Embedding generation complete: {results}")

    except Exception as e:
        logger.error(f"Embedding generation failed: {e}")
        # Save checkpoint on failure so we can resume
        try:
            _save_checkpoint(debate_store, output_dir, "debate_store")
            _save_checkpoint(amendment_store, output_dir, "amendment_store")
            logger.info("Checkpoints saved. Run again to resume.")
        except Exception:
            pass
        raise

    finally:
        session.close()

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Run the legislative data pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--collect",
        action="store_true",
        help="Run data collection from sources",
    )
    parser.add_argument(
        "--import",
        dest="do_import",
        action="store_true",
        help="Run database import (laws, articles, debates, amendments)",
    )
    parser.add_argument(
        "--embed",
        action="store_true",
        help="Generate embeddings for search (Phase 2)",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=50000,
        help="Save checkpoint every N chunks during embedding (default: 50000)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run data validation",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset database before import",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run full pipeline (collect + import + validate)",
    )
    parser.add_argument(
        "--min-date",
        type=str,
        default=None,
        help="Only embed debates/amendments from this date onwards (YYYY-MM-DD)",
    )

    args = parser.parse_args()

    # Default paths
    laws_file = project_root / "data" / "lois_finances_2012_2025.json"
    raw_data_dir = project_root / "data" / "raw"
    embeddings_dir = project_root / "data" / "embeddings"

    # Ensure directories exist
    raw_data_dir.mkdir(parents=True, exist_ok=True)

    # Check reference file exists
    if not laws_file.exists():
        logger.error(f"Laws reference file not found: {laws_file}")
        sys.exit(1)

    # Run requested operations
    if args.all:
        args.collect = True
        args.do_import = True
        args.validate = True

    if not any([args.collect, args.do_import, args.embed, args.validate]):
        parser.print_help()
        sys.exit(0)

    try:
        if args.collect:
            collect_data(laws_file, raw_data_dir)

        if args.do_import:
            import_data(laws_file, raw_data_dir, reset=args.reset)

        if args.validate:
            validate_data()

        if args.embed:
            generate_embeddings(embeddings_dir, checkpoint_every=args.checkpoint_every,
                                min_date=args.min_date)

        logger.info("Pipeline completed successfully")

    except KeyboardInterrupt:
        logger.warning("Pipeline interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        raise


if __name__ == "__main__":
    main()
