#!/usr/bin/env python3
"""Embed missing interventions and add to vector store."""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Base, Debat, Intervention
from src.indexing.chunking import Chunk
from src.indexing.embeddings import EmbeddingService, VectorStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def get_session():
    engine = create_engine("sqlite:///data/legislateur.db")
    Session = sessionmaker(bind=engine)
    return Session()


def main():
    store_path = Path("data/embeddings/debate_store")

    # Load existing store to check what's already embedded
    logger.info("Loading existing debate store...")
    existing_store = VectorStore()
    if store_path.exists():
        existing_store.load(store_path)

    logger.info(f"Existing store has {len(existing_store)} chunks")

    # Get set of already embedded intervention IDs (using source_id + position as key)
    existing_keys = set()
    for chunk in existing_store.chunks:
        key = f"{chunk.source_id}_{chunk.position}"
        existing_keys.add(key)

    logger.info(f"Found {len(existing_keys)} unique chunk keys")

    # Get all interventions from DB
    session = get_session()

    total_interventions = session.query(Intervention).count()
    logger.info(f"Total interventions in DB: {total_interventions}")

    # Find missing interventions (those not in the store)
    logger.info("Finding missing interventions...")

    # Process in batches to avoid memory issues
    batch_size = 10000
    offset = 0
    missing_interventions = []

    while True:
        interventions = (
            session.query(Intervention)
            .join(Debat)
            .order_by(Intervention.id)
            .offset(offset)
            .limit(batch_size)
            .all()
        )

        if not interventions:
            break

        for interv in interventions:
            key = f"{interv.debat_id}_{interv.ordre or interv.id}"
            if key not in existing_keys:
                # Skip very short interventions
                if len(interv.contenu or "") >= 50:
                    missing_interventions.append(interv)

        offset += batch_size
        logger.info(f"Processed {offset} interventions, found {len(missing_interventions)} missing so far...")

        # Stop if we have enough (for incremental processing)
        if len(missing_interventions) >= 200000:
            logger.info("Capping at 200k missing for this run")
            break

    logger.info(f"Total missing interventions to embed: {len(missing_interventions)}")

    if not missing_interventions:
        logger.info("No missing interventions to embed!")
        session.close()
        return

    # Initialize embedding service
    logger.info("Initializing embedding service (local model)...")
    embedding_service = EmbeddingService()

    # Create chunks for missing interventions
    logger.info("Creating chunks...")
    new_chunks = []
    for interv in missing_interventions:
        debat = interv.debat
        chunk = Chunk(
            content=f"{interv.orateur} ({interv.fonction or 'député'}): {interv.contenu}",
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
            position=interv.ordre or interv.id,
        )
        new_chunks.append(chunk)

    logger.info(f"Created {len(new_chunks)} chunks")

    # Generate embeddings in batches
    logger.info("Generating embeddings...")
    embed_batch_size = 64

    for i in range(0, len(new_chunks), embed_batch_size):
        batch = new_chunks[i:i + embed_batch_size]
        texts = [c.content for c in batch]

        embeddings = embedding_service.generate_embeddings_batch(texts, batch_size=32, use_cache=True)

        for chunk, embedding in zip(batch, embeddings):
            existing_store.add(chunk, embedding)

        if (i + embed_batch_size) % 5000 == 0 or i + embed_batch_size >= len(new_chunks):
            logger.info(f"Embedded {min(i + embed_batch_size, len(new_chunks))}/{len(new_chunks)} chunks")

    # Save updated store
    logger.info(f"Saving updated store with {len(existing_store)} total chunks...")
    existing_store.save(store_path)

    logger.info("Done!")
    session.close()


if __name__ == "__main__":
    main()
