#!/usr/bin/env python3
"""Enrich the `fonction` field of Sénat interventions.

Three passes, always scoped per debate (no cross-debate contamination):
  1. Name-based: orateur name implies a function (président, rapporteur, ministre…)
  2. Propagation: if an orateur has a function in one intervention, propagate to others
  3. President introductions: parse "La parole est à M. X, rapporteur…" patterns

Usage:
    python scripts/enrich_fonctions.py
    python scripts/enrich_fonctions.py --dry-run
"""

import argparse
import logging
import re
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.utils.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def get_connection():
    settings = get_settings()
    return psycopg2.connect(str(settings.database_url))


def pass1_name_based(conn, dry_run: bool = False) -> int:
    """Detect function from the orateur name itself."""
    c = conn.cursor()

    # 1a. Exact président/présidente matches
    c.execute("""
        SELECT COUNT(*) FROM interventions
        WHERE fonction IS NULL
          AND orateur IN ('M. le président', 'Mme la présidente')
          AND debat_id IN (SELECT id FROM debats WHERE chambre = 'SENAT')
    """)
    count_president = c.fetchone()[0]

    if not dry_run:
        c.execute("""
            UPDATE interventions SET fonction = 'président de séance'
            WHERE fonction IS NULL
              AND orateur IN ('M. le président', 'Mme la présidente')
              AND debat_id IN (SELECT id FROM debats WHERE chambre = 'SENAT')
        """)

    # 1b. Other président variants (vice-président, etc.)
    c.execute("""
        SELECT COUNT(*) FROM interventions
        WHERE fonction IS NULL
          AND orateur NOT IN ('M. le président', 'Mme la présidente')
          AND (LOWER(orateur) LIKE '%%président%%' OR LOWER(orateur) LIKE '%%présidente%%')
          AND debat_id IN (SELECT id FROM debats WHERE chambre = 'SENAT')
    """)
    count_vp = c.fetchone()[0]

    if not dry_run:
        c.execute("""
            UPDATE interventions SET fonction = 'président de séance'
            WHERE fonction IS NULL
              AND orateur NOT IN ('M. le président', 'Mme la présidente')
              AND (LOWER(orateur) LIKE '%%président%%' OR LOWER(orateur) LIKE '%%présidente%%')
              AND debat_id IN (SELECT id FROM debats WHERE chambre = 'SENAT')
        """)

    # 1c. Rapporteur — extract role from name
    # e.g. "M. le rapporteur général" -> "rapporteur général"
    c.execute("""
        SELECT DISTINCT orateur FROM interventions
        WHERE fonction IS NULL
          AND LOWER(orateur) LIKE '%%rapporteur%%'
          AND debat_id IN (SELECT id FROM debats WHERE chambre = 'SENAT')
    """)
    rapporteur_names = [r[0] for r in c.fetchall()]
    count_rapporteur = 0

    for name in rapporteur_names:
        # Extract everything from "rapporteur" onwards
        match = re.search(r"(rapporteur\w*(?:\s+.+)?)", name, re.IGNORECASE)
        if match:
            role = match.group(1).strip().rstrip(".")
        else:
            role = "rapporteur"

        c.execute("""
            SELECT COUNT(*) FROM interventions
            WHERE fonction IS NULL AND orateur = %s
              AND debat_id IN (SELECT id FROM debats WHERE chambre = 'SENAT')
        """, (name,))
        n = c.fetchone()[0]
        count_rapporteur += n

        if not dry_run:
            c.execute("""
                UPDATE interventions SET fonction = %s
                WHERE fonction IS NULL AND orateur = %s
                  AND debat_id IN (SELECT id FROM debats WHERE chambre = 'SENAT')
            """, (role, name))

    # 1d. Ministre / secrétaire d'État / garde des sceaux
    c.execute("""
        SELECT DISTINCT orateur FROM interventions
        WHERE fonction IS NULL
          AND (LOWER(orateur) LIKE '%%ministre%%'
               OR LOWER(orateur) LIKE '%%secrétaire d%%'
               OR LOWER(orateur) LIKE '%%garde des sceaux%%')
          AND debat_id IN (SELECT id FROM debats WHERE chambre = 'SENAT')
    """)
    ministre_names = [r[0] for r in c.fetchall()]
    count_ministre = 0

    for name in ministre_names:
        # Extract role: everything after "M./Mme le/la "
        match = re.search(
            r"(?:le|la)\s+(ministre.+|secrétaire\s+d.+|garde\s+des\s+sceaux.+)",
            name, re.IGNORECASE,
        )
        if match:
            role = match.group(1).strip().rstrip(".")
        else:
            role = "ministre"

        c.execute("""
            SELECT COUNT(*) FROM interventions
            WHERE fonction IS NULL AND orateur = %s
              AND debat_id IN (SELECT id FROM debats WHERE chambre = 'SENAT')
        """, (name,))
        n = c.fetchone()[0]
        count_ministre += n

        if not dry_run:
            c.execute("""
                UPDATE interventions SET fonction = %s
                WHERE fonction IS NULL AND orateur = %s
                  AND debat_id IN (SELECT id FROM debats WHERE chambre = 'SENAT')
            """, (role, name))

    total = count_president + count_vp + count_rapporteur + count_ministre
    logger.info(
        f"Passe 1 (nom): {total} interventions "
        f"(président={count_president}, vice-prés={count_vp}, "
        f"rapporteur={count_rapporteur}, ministre={count_ministre})"
    )

    if not dry_run:
        conn.commit()
    return total


def pass2_propagation(conn, dry_run: bool = False) -> int:
    """Propagate function within a debate: if orateur X has a function somewhere, apply everywhere."""
    c = conn.cursor()

    c.execute("""
        SELECT COUNT(*) FROM interventions i
        WHERE i.fonction IS NULL
          AND EXISTS (
              SELECT 1 FROM interventions i2
              WHERE i2.debat_id = i.debat_id
                AND i2.orateur = i.orateur
                AND i2.fonction IS NOT NULL
          )
          AND i.debat_id IN (SELECT id FROM debats WHERE chambre = 'SENAT')
    """)
    count = c.fetchone()[0]

    if not dry_run and count > 0:
        c.execute("""
            UPDATE interventions i
            SET fonction = sub.fonction
            FROM (
                SELECT DISTINCT ON (debat_id, orateur) debat_id, orateur, fonction
                FROM interventions
                WHERE fonction IS NOT NULL
                  AND debat_id IN (SELECT id FROM debats WHERE chambre = 'SENAT')
                ORDER BY debat_id, orateur, ordre
            ) sub
            WHERE i.debat_id = sub.debat_id
              AND i.orateur = sub.orateur
              AND i.fonction IS NULL
        """)
        conn.commit()

    logger.info(f"Passe 2 (propagation): {count} interventions")
    return count


def pass3_president_introductions(conn, dry_run: bool = False) -> int:
    """Parse president interventions for speaker introductions with roles."""
    c = conn.cursor()

    # Get all Sénat debate IDs
    c.execute("""
        SELECT id FROM debats WHERE chambre = 'SENAT'
        ORDER BY id
    """)
    debate_ids = [r[0] for r in c.fetchall()]

    # Patterns for president introductions
    # "La parole est à M. Marini, rapporteur général de la commission"
    # Allow lowercase particles (de, le) and newlines in role
    intro_pattern = re.compile(
        r"La parole est à (M\.|Mme|Mlle)\s+"
        r"([A-Za-zÀ-ü][A-Za-zÀ-ü\s\-]+?)"
        r",\s*([^\.]{5,80})\.",
        re.UNICODE,
    )
    skip_phrases = [
        "pour défendre", "pour présenter", "pour explication",
        "contre l'", "pour l'", "sur l'", "sur l'",
        "dont c'est", "avec l'autorisation",
    ]

    total_updated = 0
    batch_size = 500

    for batch_start in range(0, len(debate_ids), batch_size):
        batch_ids = debate_ids[batch_start:batch_start + batch_size]

        for debat_id in batch_ids:
            # Get president interventions with potential introductions
            c.execute("""
                SELECT id, orateur, contenu FROM interventions
                WHERE debat_id = %s
                  AND orateur IN ('M. le président', 'Mme la présidente')
                  AND contenu LIKE '%%parole est à%%'
                ORDER BY ordre
            """, (debat_id,))
            president_intervs = c.fetchall()

            if not president_intervs:
                continue

            # Build speaker -> function map for this debate
            speaker_functions = {}
            for _, _, contenu in president_intervs:
                # Normalize whitespace for multi-line content
                normalized = re.sub(r"\s+", " ", contenu)
                for match in intro_pattern.finditer(normalized):
                    prefix = match.group(1)
                    name = match.group(2).strip()
                    role = match.group(3).strip().rstrip(",;")

                    # Skip if role looks like a procedural instruction
                    role_lower = role.lower()
                    if any(skip in role_lower for skip in skip_phrases):
                        continue

                    full_name = f"{prefix} {name}"
                    if full_name not in speaker_functions:
                        speaker_functions[full_name] = role

            if not speaker_functions:
                continue

            # Apply to interventions without function in this debate
            for full_name, role in speaker_functions.items():
                c.execute("""
                    SELECT COUNT(*) FROM interventions
                    WHERE debat_id = %s AND orateur = %s AND fonction IS NULL
                """, (debat_id, full_name))
                n = c.fetchone()[0]

                if n > 0 and not dry_run:
                    c.execute("""
                        UPDATE interventions SET fonction = %s
                        WHERE debat_id = %s AND orateur = %s AND fonction IS NULL
                    """, (role, debat_id, full_name))
                    total_updated += n
                elif n > 0:
                    total_updated += n

        if not dry_run:
            conn.commit()

        if (batch_start + batch_size) % 2000 == 0:
            logger.info(f"  Passe 3 progress: {batch_start + batch_size}/{len(debate_ids)} débats")

    logger.info(f"Passe 3 (introductions président): {total_updated} interventions")
    return total_updated


def show_stats(conn):
    """Show before/after stats."""
    c = conn.cursor()
    c.execute("""
        SELECT
            CASE
                WHEN d.date < '2002-01-01' THEN 'pré-2002'
                ELSE '2002+'
            END as period,
            COUNT(*) as total,
            COUNT(i.fonction) as with_fn,
            ROUND(100.0 * COUNT(i.fonction) / COUNT(*), 1) as pct
        FROM interventions i
        JOIN debats d ON d.id = i.debat_id
        WHERE d.chambre = 'SENAT'
        GROUP BY period
        ORDER BY period
    """)
    print(f"\n{'Période':>10} | {'Total':>8} | {'Avec fn':>8} | {'%':>6}")
    print("-" * 42)
    for r in c.fetchall():
        print(f"{r[0]:>10} | {r[1]:>8} | {r[2]:>8} | {r[3]:>5}%")


def main():
    parser = argparse.ArgumentParser(description="Enrich Sénat intervention fonctions")
    parser.add_argument("--dry-run", action="store_true", help="Count without updating")
    args = parser.parse_args()

    conn = get_connection()

    print("=== État avant enrichissement ===")
    show_stats(conn)

    print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Enrichissement en cours...\n")

    t1 = pass1_name_based(conn, dry_run=args.dry_run)
    t2 = pass2_propagation(conn, dry_run=args.dry_run)
    t3 = pass3_president_introductions(conn, dry_run=args.dry_run)

    print(f"\nTotal enrichi: {t1 + t2 + t3} interventions")

    if not args.dry_run:
        print("\n=== État après enrichissement ===")
        show_stats(conn)

    conn.close()


if __name__ == "__main__":
    main()
