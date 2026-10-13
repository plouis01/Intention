"""Phase 11: Fix OCR typos by fuzzy matching (distance=1 only, safe).

Matches unique orateurs (1 occurrence, name-like pattern) against known
orateurs (6+ occurrences) using Levenshtein distance. Only accepts
distance=1 to avoid false positives.

Outputs a log file for manual review before committing.
"""
import sys
from Levenshtein import distance
from sqlalchemy import create_engine, text

engine = create_engine('postgresql://localhost/legislateur_local')

DRY_RUN = '--dry-run' in sys.argv

with engine.begin() as conn:
    before = conn.execute(text("SELECT COUNT(DISTINCT orateur) FROM interventions")).scalar()
    print(f"Orateurs distincts avant: {before}")

    # Get known orateurs (6+ occurrences)
    known_rows = conn.execute(text(
        "SELECT orateur, COUNT(*) FROM interventions GROUP BY orateur HAVING COUNT(*) >= 6"
    )).fetchall()
    known = {r[0]: r[1] for r in known_rows}
    print(f"Known orateurs (6+ occ): {len(known)}")

    # Get unique orateurs (1 occurrence, name-like pattern)
    uniques = conn.execute(text("""
        SELECT orateur, id FROM interventions
        WHERE orateur IN (
            SELECT orateur FROM interventions GROUP BY orateur HAVING COUNT(*) = 1
        )
        AND LENGTH(orateur) BETWEEN 8 AND 35
        AND orateur ~ '^(M\\.|Mme|Mlle)\\s+[A-ZÀ-Ý]'
        AND orateur !~ '\\s+(et |applaudit|a |est |sur |pour |dans |les |des |un |fait |propose|nous |vous |que |qui |sont |ont |tout )'
    """)).fetchall()
    print(f"Unique candidates: {len(uniques)}")

    fixed = 0
    log = []
    for orateur, fid in uniques:
        best_match = None
        best_dist = 2  # only accept distance 1
        for known_name in known:
            if abs(len(known_name) - len(orateur)) > 1:
                continue
            d = distance(orateur, known_name)
            if d < best_dist:
                best_dist = d
                best_match = known_name

        if best_match and best_dist == 1:
            log.append(f"{orateur} -> {best_match} ({known[best_match]}x)")
            if not DRY_RUN:
                conn.execute(text(
                    "UPDATE interventions SET orateur = :name WHERE id = :fid"
                ), {"name": best_match, "fid": fid})
            fixed += 1

    prefix = "[DRY RUN] " if DRY_RUN else ""
    print(f"{prefix}Fixed OCR typos (dist=1): {fixed}")

    # Write log for review
    log_path = "data/ocr_corrections.log"
    with open(log_path, "w") as f:
        f.write("\n".join(sorted(log)))
    print(f"Log written to {log_path} ({len(log)} entries)")

    if not DRY_RUN:
        after = conn.execute(text("SELECT COUNT(DISTINCT orateur) FROM interventions")).scalar()
        print(f"Orateurs: {before} -> {after} (delta: {before - after})")
