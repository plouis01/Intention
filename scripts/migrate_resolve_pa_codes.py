#!/usr/bin/env python3
"""Migration script: resolve PA actor codes to real deputy names.

68,584 amendments have AN actor UIDs (PA719608, PA840809...) instead of names.
This script fetches the real name from assemblee-nationale.fr for each unique
PA code and updates the auteur field.

Usage:
    python scripts/migrate_resolve_pa_codes.py                  # Dry run (default)
    python scripts/migrate_resolve_pa_codes.py --apply           # Apply changes
    python scripts/migrate_resolve_pa_codes.py --fetch           # Fetch PA→name mapping
    python scripts/migrate_resolve_pa_codes.py --fetch --apply   # Fetch then apply
"""

import argparse
import json
import re
import time
from pathlib import Path

import psycopg2
import urllib.request

CACHE_FILE = Path(__file__).parent.parent / "data" / "pa_acteurs_mapping.json"


def fetch_deputy_name(pa_code: str) -> str | None:
    """Fetch deputy name from assemblee-nationale.fr page."""
    url = f"https://www.assemblee-nationale.fr/dyn/deputes/{pa_code}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        # Extract from <h1>
        h1 = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
        if h1:
            name = h1.group(1).strip()
            if name and len(name) > 3:
                return name
    except Exception as e:
        print(f"    ERREUR {pa_code}: {e}")
    return None


def load_cache() -> dict:
    """Load cached PA→name mapping."""
    if CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_cache(mapping: dict):
    """Save PA→name mapping to cache."""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)
    print(f"  Cache sauvegardé: {CACHE_FILE} ({len(mapping)} entrées)")


def main():
    parser = argparse.ArgumentParser(description="Resolve PA codes to deputy names")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    parser.add_argument("--fetch", action="store_true", help="Fetch missing PA codes from AN website")
    parser.add_argument("--db", default="postgresql://localhost/legislateur_local", help="DB URL")
    parser.add_argument("--batch", type=int, default=5000, help="Batch size for updates")
    parser.add_argument("--delay", type=float, default=0.3, help="Delay between fetches (seconds)")
    args = parser.parse_args()

    conn = psycopg2.connect(args.db)
    cur = conn.cursor()

    # --- Phase 1: Identify PA codes ---
    print("=" * 70)
    print("PHASE 1: IDENTIFICATION DES CODES PA")
    print("=" * 70)

    cur.execute("""
        SELECT auteur, COUNT(*) FROM amendements
        WHERE auteur ~ '^PA[0-9]+'
        GROUP BY auteur ORDER BY COUNT(*) DESC
    """)
    pa_stats = cur.fetchall()
    total_amendments = sum(count for _, count in pa_stats)
    print(f"Codes PA uniques: {len(pa_stats)}")
    print(f"Amendements concernés: {total_amendments:,}")

    # --- Phase 2: Load/build mapping ---
    print(f"\n{'=' * 70}")
    print("PHASE 2: MAPPING PA → NOM")
    print("=" * 70)

    mapping = load_cache()
    print(f"Cache existant: {len(mapping)} entrées")

    # Find codes not yet in cache
    all_codes = [pa for pa, _ in pa_stats]
    missing = [pa for pa in all_codes if pa not in mapping]
    print(f"Codes manquants: {len(missing)}")

    if args.fetch and missing:
        print(f"\nFetching {len(missing)} codes depuis assemblee-nationale.fr...")
        resolved = 0
        failed = 0
        for i, pa_code in enumerate(missing):
            name = fetch_deputy_name(pa_code)
            if name:
                mapping[pa_code] = name
                resolved += 1
                if resolved <= 10 or resolved % 50 == 0:
                    print(f"  [{i+1}/{len(missing)}] {pa_code} → {name}")
            else:
                failed += 1
                print(f"  [{i+1}/{len(missing)}] {pa_code} → ÉCHEC")
            time.sleep(args.delay)

        print(f"\nRésolu: {resolved}, Échec: {failed}")
        save_cache(mapping)
    elif missing:
        print(f"\n⚠ {len(missing)} codes non résolus. Relancer avec --fetch pour les récupérer.")

    # --- Phase 3: Preview changes ---
    print(f"\n{'=' * 70}")
    print("PHASE 3: PRÉVISUALISATION DES CHANGEMENTS")
    print("=" * 70)

    resolvable = 0
    unresolvable = 0
    for pa_code, count in pa_stats:
        if pa_code in mapping:
            resolvable += count
        else:
            unresolvable += count

    print(f"Résolvables: {resolvable:,} amendements")
    print(f"Non résolvables: {unresolvable:,} amendements")

    # Show top 20 mappings
    print(f"\nTop 20 mappings:")
    for pa_code, count in pa_stats[:20]:
        name = mapping.get(pa_code, "???")
        print(f"  {pa_code} → {name:40s} (x{count:,})")

    # Distribution of resolved names
    if mapping:
        from collections import Counter
        name_counts = Counter()
        for pa_code, count in pa_stats:
            if pa_code in mapping:
                name_counts[mapping[pa_code]] += count

        print(f"\nTop 20 auteurs après résolution:")
        for name, count in name_counts.most_common(20):
            print(f"  x{count:5d}: {name}")

    # --- Phase 4: Verifications ---
    print(f"\n{'=' * 70}")
    print("PHASE 4: VÉRIFICATIONS")
    print("=" * 70)

    issues = []

    # Check for suspiciously short names
    short_names = {pa: name for pa, name in mapping.items() if len(name) < 5}
    if short_names:
        issues.append(f"⚠ {len(short_names)} noms très courts: {short_names}")

    # Check for HTML in names
    html_names = {pa: name for pa, name in mapping.items() if "<" in name or ">" in name}
    if html_names:
        issues.append(f"⚠ {len(html_names)} noms contiennent du HTML")

    # Check format (should start with M./Mme)
    bad_format = {pa: name for pa, name in mapping.items()
                  if not re.match(r"^(M\.|Mme|Mlle)\s", name)}
    if bad_format:
        print(f"  Noms sans civilité M./Mme ({len(bad_format)} cas):")
        for pa, name in list(bad_format.items())[:5]:
            print(f"    {pa} → {name}")

    if not issues:
        print("  Aucun problème détecté")
    else:
        for issue in issues:
            print(f"  {issue}")

    # --- Phase 5: Apply or not ---
    if not args.apply:
        print(f"\n{'=' * 70}")
        print("MODE DRY RUN")
        print(f"Relancer avec --apply pour modifier {resolvable:,} amendements")
        if missing:
            print(f"Relancer avec --fetch d'abord pour résoudre {len(missing)} codes manquants")
        print("=" * 70)
        conn.close()
        return

    if not mapping:
        print("\nAucun mapping disponible. Relancer avec --fetch.")
        conn.close()
        return

    print(f"\n{'=' * 70}")
    print(f"PHASE 5: APPLICATION ({resolvable:,} modifications)")
    print("=" * 70)

    applied = 0
    for pa_code, count in pa_stats:
        if pa_code not in mapping:
            continue
        name = mapping[pa_code]
        cur.execute("UPDATE amendements SET auteur = %s WHERE auteur = %s", (name, pa_code))
        applied += cur.rowcount
        if applied % 10000 < count:
            conn.commit()
            print(f"  {applied:,} / {resolvable:,} appliqués")

    conn.commit()
    print(f"\n  {applied:,} amendements mis à jour")

    # --- Phase 6: Post-apply verification ---
    print(f"\n{'=' * 70}")
    print("PHASE 6: VÉRIFICATION POST-APPLICATION")
    print("=" * 70)

    cur.execute("SELECT COUNT(*) FROM amendements WHERE auteur ~ '^PA[0-9]+'")
    remaining = cur.fetchone()[0]
    print(f"  Codes PA restants: {remaining:,}")

    cur.execute("""
        SELECT auteur, COUNT(*) FROM amendements
        WHERE dossier_id IN (5, 9) AND auteur IS NOT NULL
        GROUP BY auteur ORDER BY COUNT(*) DESC LIMIT 15
    """)
    print("\nTop auteurs dossiers 5+9 post-migration:")
    for auteur, count in cur.fetchall():
        print(f"  x{count:5d}: {auteur[:60]}")

    conn.close()
    print("\n  Migration terminée")


if __name__ == "__main__":
    main()
