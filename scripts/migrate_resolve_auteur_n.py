#!/usr/bin/env python3
"""Migration script: resolve or nullify 'N' author in amendements.

1,677 amendments from the Sénat have auteur='N' - a placeholder from the parser.
This script attempts to resolve them from the Sénat HTML pages, or nullifies them.

Usage:
    python scripts/migrate_resolve_auteur_n.py                  # Dry run
    python scripts/migrate_resolve_auteur_n.py --apply           # Nullify auteur='N'
    python scripts/migrate_resolve_auteur_n.py --fetch --apply   # Try fetch first, then apply
"""

import argparse
import re
import time

import psycopg2
import urllib.request


def fetch_senat_author(url: str) -> str | None:
    """Try to extract the author from a Sénat amendment page."""
    if not url:
        return None
    # Ensure URL has scheme
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith("http"):
        url = "https://" + url

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        # Look for author patterns in the page
        # Sénat amendment pages usually have "Présenté par M./Mme XXX"
        author_match = re.search(
            r"(?:Présenté|Déposé)\s+par\s+(M\.|Mme|MM\.)\s+([^<\n]+)",
            html,
        )
        if author_match:
            return (author_match.group(1) + " " + author_match.group(2)).strip()
        # Try "Auteur : XXX" pattern
        author_match2 = re.search(r"Auteur\s*:\s*([^<\n]+)", html)
        if author_match2:
            return author_match2.group(1).strip()
    except Exception as e:
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description="Resolve or nullify auteur='N'")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    parser.add_argument("--fetch", action="store_true", help="Try fetching from Sénat pages")
    parser.add_argument("--db", default="postgresql://localhost/legislateur_local", help="DB URL")
    args = parser.parse_args()

    conn = psycopg2.connect(args.db)
    cur = conn.cursor()

    # --- Phase 1: Analyze ---
    print("=" * 70)
    print("PHASE 1: ANALYSE - Amendements avec auteur = 'N'")
    print("=" * 70)

    cur.execute("SELECT COUNT(*) FROM amendements WHERE auteur = 'N'")
    total = cur.fetchone()[0]
    print(f"Total: {total:,}")

    cur.execute("""
        SELECT dossier_id, COUNT(*) FROM amendements
        WHERE auteur = 'N' GROUP BY dossier_id ORDER BY COUNT(*) DESC LIMIT 10
    """)
    print("\nPar dossier:")
    for did, count in cur.fetchall():
        print(f"  dossier_id={did}: {count:,}")

    cur.execute("""
        SELECT id, url_source, numero, sort FROM amendements
        WHERE auteur = 'N' AND url_source IS NOT NULL LIMIT 10
    """)
    sample_rows = cur.fetchall()
    print(f"\nÉchantillons avec URL:")
    for row_id, url, num, sort in sample_rows:
        print(f"  id={row_id} | num={num} | sort={sort} | url={url[:80]}")

    # --- Phase 2: Try fetching (optional) ---
    resolved_count = 0
    resolved_map = {}  # id → new_auteur

    if args.fetch and sample_rows:
        print(f"\n{'=' * 70}")
        print("PHASE 2: TENTATIVE DE RÉSOLUTION VIA SÉNAT")
        print("=" * 70)

        # Test on first 10 URLs
        cur.execute("""
            SELECT id, url_source FROM amendements
            WHERE auteur = 'N' AND url_source IS NOT NULL LIMIT 20
        """)
        test_rows = cur.fetchall()

        for row_id, url in test_rows:
            author = fetch_senat_author(url)
            if author:
                resolved_map[row_id] = author
                resolved_count += 1
                print(f"  id={row_id}: {url[:60]} → {author}")
            else:
                print(f"  id={row_id}: {url[:60]} → ÉCHEC")
            time.sleep(0.5)

        print(f"\nRésolus: {resolved_count} / {len(test_rows)}")

        if resolved_count == 0:
            print("Aucune résolution réussie - on passe en mode nullification")

    # --- Phase 3: Plan ---
    print(f"\n{'=' * 70}")
    print("PHASE 3: PLAN D'ACTION")
    print("=" * 70)

    if resolved_map:
        print(f"  {len(resolved_map)} amendements seront mis à jour avec l'auteur résolu")
        print(f"  {total - len(resolved_map)} amendements auront auteur = NULL")
    else:
        print(f"  {total:,} amendements auront auteur = NULL (le 'N' n'a pas de valeur)")

    # --- Phase 4: Verifications ---
    print(f"\n{'=' * 70}")
    print("PHASE 4: VÉRIFICATIONS")
    print("=" * 70)

    # Pre-count
    cur.execute("SELECT COUNT(*) FROM amendements WHERE auteur IS NULL")
    null_before = cur.fetchone()[0]
    expected_null = null_before + total - len(resolved_map)
    print(f"  auteur IS NULL avant: {null_before:,}")
    print(f"  auteur IS NULL après (attendu): {expected_null:,}")

    # Check these amendments have other useful fields
    cur.execute("""
        SELECT COUNT(*) FROM amendements
        WHERE auteur = 'N' AND objet IS NOT NULL AND LENGTH(objet) > 10
    """)
    with_objet = cur.fetchone()[0]
    print(f"  Avec objet exploitable: {with_objet:,} / {total:,}")

    print("  Aucune donnée perdue (le 'N' n'a pas de valeur informative)")

    # --- Phase 5: Apply or not ---
    if not args.apply:
        print(f"\n{'=' * 70}")
        print("MODE DRY RUN")
        print(f"Relancer avec --apply pour modifier {total:,} amendements")
        print("=" * 70)
        conn.close()
        return

    print(f"\n{'=' * 70}")
    print(f"PHASE 5: APPLICATION")
    print("=" * 70)

    # Apply resolved names
    for row_id, new_auteur in resolved_map.items():
        cur.execute("UPDATE amendements SET auteur = %s WHERE id = %s", (new_auteur, row_id))

    # Nullify the rest
    cur.execute("UPDATE amendements SET auteur = NULL WHERE auteur = 'N'")
    nullified = cur.rowcount
    conn.commit()

    print(f"  {len(resolved_map)} amendements avec auteur résolu")
    print(f"  {nullified:,} amendements nullifiés")

    # --- Phase 6: Post-apply verification ---
    print(f"\n{'=' * 70}")
    print("PHASE 6: VÉRIFICATION POST-APPLICATION")
    print("=" * 70)

    cur.execute("SELECT COUNT(*) FROM amendements WHERE auteur = 'N'")
    remaining = cur.fetchone()[0]
    print(f"  auteur = 'N' restants: {remaining}")

    cur.execute("SELECT COUNT(*) FROM amendements WHERE auteur IS NULL")
    null_after = cur.fetchone()[0]
    print(f"  auteur IS NULL: {null_after:,}")

    conn.close()
    print("\n  Migration terminée")


if __name__ == "__main__":
    main()
