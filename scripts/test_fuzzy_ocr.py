"""Test OCR fuzzy matching on a sample of 100 candidates."""
from Levenshtein import distance
from sqlalchemy import create_engine, text

engine = create_engine('postgresql://localhost/legislateur_local')

with engine.connect() as conn:
    # Get known orateurs (6+ occurrences)
    known_rows = conn.execute(text(
        "SELECT orateur, COUNT(*) FROM interventions GROUP BY orateur HAVING COUNT(*) >= 6"
    )).fetchall()
    known = {r[0]: r[1] for r in known_rows}
    print(f"Known orateurs: {len(known)}")

    # Get unique OCR typo candidates
    candidates = conn.execute(text("""
        SELECT orateur FROM interventions
        WHERE orateur IN (SELECT orateur FROM interventions GROUP BY orateur HAVING COUNT(*) = 1)
        AND LENGTH(orateur) BETWEEN 8 AND 30
        AND orateur ~ '^(M\\.|Mme)\\s+[A-ZÀ-Ý]'
        AND orateur !~ '\\s+(et |applaudit|a |est |sur |pour |dans |les |des |un |fait |propose|nous |vous |que |qui |sont |ont )'
        LIMIT 200
    """)).fetchall()
    cands = [r[0] for r in candidates]
    print(f"Testing {len(cands)} candidates...")

    # Test fuzzy matching
    results = []
    for cand in cands:
        best_match = None
        best_dist = 3
        for known_name in known:
            if abs(len(known_name) - len(cand)) > 3:
                continue
            d = distance(cand, known_name)
            if d < best_dist:
                best_dist = d
                best_match = known_name
        if best_match and best_dist <= 2:
            results.append((cand, best_match, best_dist, known[best_match]))

    print(f"\nMatched: {len(results)} / {len(cands)}")
    print(f"\n{'Typo':40} -> {'Correction':40} (dist, freq)")
    print("-" * 100)
    for typo, correction, dist, freq in sorted(results, key=lambda x: -x[3])[:30]:
        print(f"{typo:40} -> {correction:40} ({dist}, {freq}x)")

    # Show wrong matches (false positives) — distance 2 can be risky
    print(f"\n=== Distance 2 matches (check for false positives) ===")
    d2 = [r for r in results if r[2] == 2]
    for typo, correction, dist, freq in d2[:15]:
        print(f"{typo:40} -> {correction:40} ({dist}, {freq}x)")

    # Show unmatched
    matched_names = {r[0] for r in results}
    unmatched = [c for c in cands if c not in matched_names]
    print(f"\nUnmatched: {len(unmatched)} samples:")
    for u in unmatched[:15]:
        print(f"  {u}")
