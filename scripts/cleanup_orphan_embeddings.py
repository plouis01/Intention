#!/usr/bin/env python3
"""Re-link and clean up orphaned embeddings in chunk_embeddings.

Modes:
  --relink   Re-link intervention embeddings with NULL source_id to their debates.
  --cleanup  Remove stale duplicate embeddings (amendment doublons + Type B stale hashes).

Both modes default to dry-run. Add --apply to execute changes.

Usage:
  python scripts/cleanup_orphan_embeddings.py --relink              # dry-run
  python scripts/cleanup_orphan_embeddings.py --relink --apply      # execute
  python scripts/cleanup_orphan_embeddings.py --cleanup             # dry-run
  python scripts/cleanup_orphan_embeddings.py --cleanup --apply     # execute
"""

import argparse
import hashlib
import json
import logging
import re
import sys
import time

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

logger = logging.getLogger(__name__)

PAGE_SIZE = 5000


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Relink: interventions with NULL source_id
# ---------------------------------------------------------------------------

def relink_interventions(apply=False):
    """Re-link intervention embeddings that have source_id=NULL.

    Strategy: build an in-memory lookup from intervention content to debat_id,
    then match each orphan embedding's core text against this lookup.
    Much faster than per-row LIKE queries.
    """
    session = get_session()
    total = session.execute(text(
        "SELECT COUNT(*) FROM chunk_embeddings "
        "WHERE chunk_type = 'intervention' AND source_id IS NULL"
    )).scalar()
    session.close()

    print(f"  Found {total:,} intervention embeddings with NULL source_id\n", flush=True)
    if total == 0:
        return

    # Step 1: Build lookup {contenu_prefix -> (debat_id, orateur, fonction, chambre, date, etape, articles)}
    # Use first 120 chars of contenu as key (enough to be unique in practice)
    print("  Building intervention content index...", flush=True)
    PREFIX_LEN = 120
    lookup = {}
    last_id = 0

    while True:
        page_session = get_session()
        try:
            rows = page_session.execute(text(
                "SELECT i.id, i.debat_id, i.orateur, i.fonction, i.contenu, i.articles_mentionnes, "
                "d.chambre, d.date, d.etape "
                "FROM interventions i "
                "JOIN debats d ON i.debat_id = d.id "
                "WHERE i.id > :last_id "
                "ORDER BY i.id LIMIT :limit"
            ), {"last_id": last_id, "limit": PAGE_SIZE}).fetchall()
        finally:
            page_session.close()

        if not rows:
            break
        last_id = rows[-1][0]

        for row in rows:
            i_id, debat_id, orateur, fonction, contenu, articles, chambre, date, etape = row
            if contenu:
                key = contenu[:PREFIX_LEN].strip()
                if key not in lookup:
                    lookup[key] = (debat_id, orateur, fonction, chambre, date, etape, articles)

    print(f"  Index built: {len(lookup):,} unique prefixes\n", flush=True)

    # Step 2: Match orphan embeddings against the index
    linked = 0
    unmatched = 0
    last_id = 0
    update_batch = []

    while True:
        session = get_session()
        try:
            rows = (
                session.query(ChunkEmbedding.id, ChunkEmbedding.content)
                .filter(
                    ChunkEmbedding.id > last_id,
                    ChunkEmbedding.chunk_type == "intervention",
                    ChunkEmbedding.source_id.is_(None),
                )
                .order_by(ChunkEmbedding.id)
                .limit(PAGE_SIZE)
                .all()
            )
        finally:
            session.close()

        if not rows:
            break
        last_id = rows[-1][0]

        for emb_id, content in rows:
            # Extract core text after speaker label
            # Format: "[context]\n\nOrateur (fonction): texte" or "Orateur (fonction): texte"
            match = re.search(r'(?:^|\n\n)([^[\n]+?)\s*:\s*(.+)', content, re.DOTALL)
            if match:
                core_text = match.group(2).strip()[:PREFIX_LEN]
            else:
                core_text = content[:PREFIX_LEN].strip()

            hit = lookup.get(core_text)
            if hit:
                debat_id, orateur, fonction, chambre, date, etape, articles = hit
                linked += 1
                if apply:
                    update_batch.append((emb_id, debat_id, orateur, fonction, chambre, date, etape, articles))
            else:
                unmatched += 1

        # Flush updates in batch
        if apply and update_batch:
            write_session = get_session()
            try:
                for emb_id, debat_id, orateur, fonction, chambre, date, etape, articles in update_batch:
                    metadata = {
                        "orateur": orateur,
                        "fonction": fonction,
                        "chambre": chambre or "AN",
                        "date": date.isoformat() if date else None,
                        "etape": etape,
                        "articles_mentionnes": articles or [],
                    }
                    write_session.execute(text(
                        "UPDATE chunk_embeddings "
                        "SET source_id = :sid, chunk_metadata = :meta "
                        "WHERE id = :eid"
                    ), {"sid": str(debat_id), "meta": json.dumps(metadata), "eid": emb_id})
                write_session.commit()
            finally:
                write_session.close()
            update_batch = []

        processed = linked + unmatched
        print(f"  [{processed:,}/{total:,}] linked={linked:,} unmatched={unmatched:,}", flush=True)

    # Flush remaining
    if apply and update_batch:
        write_session = get_session()
        try:
            for emb_id, debat_id, orateur, fonction, chambre, date, etape, articles in update_batch:
                metadata = {
                    "orateur": orateur, "fonction": fonction,
                    "chambre": chambre or "AN",
                    "date": date.isoformat() if date else None,
                    "etape": etape, "articles_mentionnes": articles or [],
                }
                write_session.execute(text(
                    "UPDATE chunk_embeddings SET source_id = :sid, chunk_metadata = :meta WHERE id = :eid"
                ), {"sid": str(debat_id), "meta": json.dumps(metadata), "eid": emb_id})
            write_session.commit()
        finally:
            write_session.close()

    print(f"\n  Result: {linked:,} linked, {unmatched:,} unmatched", flush=True)


# ---------------------------------------------------------------------------
# Cleanup: amendment doublons + Type B stale intervention hashes
# ---------------------------------------------------------------------------

def cleanup_amendment_doublons(apply=False):
    """Remove old-format amendment embeddings that have been re-embedded in new format.

    Old embeddings (March 2026) use format "Amendement X\npar Y\nObjet:..."
    New embeddings (April 2026) use format "Auteur: Y\nObjet:..." (chunk_amendment output)

    For source_ids that have BOTH old and new embeddings, delete the old ones.
    Keep "only-old" source_ids (no new equivalent).
    """
    session = get_session()

    # Find source_ids present in both old and new
    duplicated = session.execute(text("""
        SELECT old_e.source_id, old_e.cnt AS old_count
        FROM (
            SELECT source_id, COUNT(*) AS cnt
            FROM chunk_embeddings
            WHERE chunk_type = 'amendement' AND created_at < '2026-04-01'
            GROUP BY source_id
        ) old_e
        WHERE EXISTS (
            SELECT 1 FROM chunk_embeddings new_e
            WHERE new_e.chunk_type = 'amendement'
              AND new_e.created_at >= '2026-04-01'
              AND new_e.source_id = old_e.source_id
        )
    """)).fetchall()

    dup_source_ids = {r[0] for r in duplicated}
    dup_old_count = sum(r[1] for r in duplicated)

    # Count only-old (to preserve)
    only_old = session.execute(text("""
        SELECT COUNT(DISTINCT source_id)
        FROM chunk_embeddings
        WHERE chunk_type = 'amendement' AND created_at < '2026-04-01'
          AND source_id NOT IN (
              SELECT DISTINCT source_id FROM chunk_embeddings
              WHERE chunk_type = 'amendement' AND created_at >= '2026-04-01'
          )
    """)).scalar()

    print(f"  Duplicated source_ids (old + new): {len(dup_source_ids):,} ({dup_old_count:,} old embeddings to remove)")
    print(f"  Only-old source_ids (preserved):   {only_old:,}")

    if not dup_source_ids:
        session.close()
        return 0

    if apply:
        # Delete old embeddings for duplicated source_ids
        deleted = 0
        dup_list = list(dup_source_ids)
        for i in range(0, len(dup_list), 500):
            batch = dup_list[i : i + 500]
            r = session.execute(text(
                "DELETE FROM chunk_embeddings "
                "WHERE chunk_type = 'amendement' "
                "AND created_at < '2026-04-01' "
                "AND source_id IN :sids"
            ), {"sids": tuple(batch)})
            deleted += r.rowcount
            session.commit()
            if (i + 500) % 5000 == 0 or i + 500 >= len(dup_list):
                print(f"  Deleted {deleted:,} old amendment embeddings...", flush=True)
        print(f"  Total deleted: {deleted:,}", flush=True)
        session.close()
        return deleted

    session.close()
    return dup_old_count


def cleanup_stale_intervention_hashes(apply=False):
    """Remove intervention embeddings whose content_hash no longer matches any current chunk.

    These are embeddings with a stale context_prefix (previous speaker changed).
    Only checks debates with more embeddings than valid interventions.
    """
    session = get_session()

    # Find debates with excess embeddings (excluding NULL source_ids)
    excess_debates = session.execute(text("""
        SELECT d.id, COUNT(i.id) AS n_interv, e.n_emb
        FROM debats d
        LEFT JOIN interventions i ON i.debat_id = d.id AND LENGTH(i.contenu) >= 50
        INNER JOIN (
            SELECT source_id, COUNT(*) AS n_emb
            FROM chunk_embeddings
            WHERE chunk_type = 'intervention' AND source_id IS NOT NULL
            GROUP BY source_id
        ) e ON CAST(d.id AS TEXT) = e.source_id
        GROUP BY d.id, e.n_emb
        HAVING e.n_emb > COUNT(i.id)
    """)).fetchall()
    session.close()

    debate_data = [(r[0], r[1], r[2]) for r in excess_debates]
    total_excess = sum(n_emb - n_interv for _, n_interv, n_emb in debate_data)
    print(f"  {len(debate_data):,} debates with excess embeddings (~{total_excess:,} stale expected)", flush=True)

    if not debate_data:
        return 0

    debate_ids = {d_id for d_id, _, _ in debate_data}

    # Rebuild hashes for these debates
    print(f"  Rebuilding intervention hashes for {len(debate_ids):,} debates...", flush=True)
    rebuilt_hashes_by_debate = {}
    prev_by_debate = {}
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
                    Intervention.debat_id.in_(debate_ids),
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
            speaker_label = f"{interv.orateur} ({interv.fonction or 'député'})"

            context_prefix = ""
            prev = prev_by_debate.get(interv.debat_id)
            if prev:
                context_prefix = f"[{prev[0]}: {prev[1]}...]\n\n"

            cleaned = strip_procedural_preamble(interv.contenu)
            content = f"{context_prefix}{speaker_label}: {cleaned}"

            h = _content_hash(content)
            prev_by_debate[interv.debat_id] = (speaker_label, interv.contenu[:150])

            if interv.debat_id not in rebuilt_hashes_by_debate:
                rebuilt_hashes_by_debate[interv.debat_id] = set()
            rebuilt_hashes_by_debate[interv.debat_id].add(h)

    # Find orphan hashes per debate
    orphan_hashes = set()
    session = get_session()
    for debate_id in debate_ids:
        existing = set(
            r[0] for r in session.query(ChunkEmbedding.content_hash).filter(
                ChunkEmbedding.chunk_type == "intervention",
                ChunkEmbedding.source_id == str(debate_id),
            ).all()
        )
        valid = rebuilt_hashes_by_debate.get(debate_id, set())
        orphan_hashes |= existing - valid
    session.close()

    print(f"  Stale intervention embeddings found: {len(orphan_hashes):,}", flush=True)

    if not orphan_hashes or not apply:
        return len(orphan_hashes)

    # Delete
    session = get_session()
    deleted = 0
    hash_list = list(orphan_hashes)
    for i in range(0, len(hash_list), 1000):
        batch = hash_list[i : i + 1000]
        n = session.query(ChunkEmbedding).filter(
            ChunkEmbedding.content_hash.in_(batch)
        ).delete(synchronize_session=False)
        session.commit()
        deleted += n
        if (i + 1000) % 10000 == 0 or i + 1000 >= len(hash_list):
            print(f"  Deleted {deleted:,} / {len(hash_list):,}", flush=True)
    session.close()
    return deleted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Re-link and clean up orphaned embeddings.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--relink", action="store_true",
                      help="Re-link intervention embeddings with NULL source_id")
    mode.add_argument("--cleanup", action="store_true",
                      help="Remove stale/duplicate embeddings")

    parser.add_argument("--apply", action="store_true",
                        help="Execute changes (default: dry-run)")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    mode_label = "APPLY" if args.apply else "DRY RUN"

    if args.relink:
        print(f"=== Re-link Intervention Embeddings [{mode_label}] ===\n", flush=True)
        t0 = time.time()
        relink_interventions(apply=args.apply)
        print(f"\nCompleted in {time.time() - t0:.1f}s", flush=True)

    elif args.cleanup:
        print(f"=== Cleanup Stale Embeddings [{mode_label}] ===\n", flush=True)
        t0 = time.time()

        print("Step 1: Amendment doublons (old format vs new format)...", flush=True)
        n_amend = cleanup_amendment_doublons(apply=args.apply)
        print()

        print("Step 2: Stale intervention hashes (context_prefix changed)...", flush=True)
        n_interv = cleanup_stale_intervention_hashes(apply=args.apply)
        print()

        total = n_amend + n_interv
        print(f"Total: {total:,} stale embeddings {'removed' if args.apply else 'found'}", flush=True)
        if not args.apply and total > 0:
            print(f"Run with --cleanup --apply to delete them.", flush=True)
        print(f"Completed in {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
