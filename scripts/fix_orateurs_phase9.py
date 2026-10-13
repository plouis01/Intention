"""Phase 9: Fix stage directions (didascalies) — individual + group.

Handles: "M. X applaudit", "M. X et Y applaudissent", etc.
These are stage directions like (M. X applaudit) that the parser split into
fake interventions. The content starts with ')' — it's the continuation of
the previous speaker's discourse.

Logic:
- If content starts with ')' → merge with previous intervention, delete this one
- Otherwise (rare: actual short speech) → extract first name, keep intervention
"""
import re
import sys
from sqlalchemy import create_engine, text

engine = create_engine('postgresql://localhost/legislateur_local')

EXTRACT_NAME = re.compile(
    r'^((M\.|Mme|Mlle|MM\.?)\s+(?:le\s+|la\s+)?'
    r'[A-ZÀ-Ý][a-zà-ÿ-]+(?:\s+[A-ZÀ-Ý][a-zà-ÿ-]+)*)'
)

DRY_RUN = '--dry-run' in sys.argv

with engine.begin() as conn:
    # Count before
    before = conn.execute(text("SELECT COUNT(DISTINCT orateur) FROM interventions")).scalar()
    print(f"Orateurs distincts avant: {before}")

    rows = conn.execute(text("""
        SELECT i.id, i.orateur, i.contenu,
               prev.id as prev_id, prev.contenu as prev_contenu
        FROM interventions i
        JOIN interventions prev ON prev.debat_id = i.debat_id
            AND prev.ordre = (
                SELECT MAX(p2.ordre) FROM interventions p2
                WHERE p2.debat_id = i.debat_id AND p2.ordre < i.ordre
            )
        WHERE i.orateur ~ '(applaudit|acquiesce|opine|proteste|sourit|exclame|manifeste|esclaffe|approuve|renchérit|applaudissent)' OR i.orateur ~ '\\mrit$'
        ORDER BY i.debat_id, i.ordre
    """)).fetchall()

    print(f"Interventions didascalies trouvées: {len(rows)}")

    merged = 0
    cleaned = 0
    skipped = 0
    for fid, orateur, contenu, prev_id, prev_contenu in rows:
        if not contenu:
            skipped += 1
            continue

        if contenu.lstrip().startswith(')'):
            if DRY_RUN:
                merged += 1
                continue
            # Didascalie → merge with previous
            new_prev = prev_contenu + " (" + orateur + contenu
            conn.execute(text(
                "UPDATE interventions SET contenu = :c WHERE id = :pid"
            ), {"c": new_prev.strip(), "pid": prev_id})
            conn.execute(text(
                "DELETE FROM interventions WHERE id = :fid"
            ), {"fid": fid})
            merged += 1
        else:
            # Actual short speech → extract first name
            m = EXTRACT_NAME.match(orateur)
            if m:
                if DRY_RUN:
                    cleaned += 1
                    continue
                conn.execute(text(
                    "UPDATE interventions SET orateur = :name WHERE id = :fid"
                ), {"name": m.group(1).strip(), "fid": fid})
                cleaned += 1
            else:
                skipped += 1

    prefix = "[DRY RUN] " if DRY_RUN else ""
    print(f"{prefix}Merged (deleted): {merged}")
    print(f"{prefix}Cleaned name: {cleaned}")
    print(f"{prefix}Skipped: {skipped}")

    if not DRY_RUN:
        after = conn.execute(text("SELECT COUNT(DISTINCT orateur) FROM interventions")).scalar()
        print(f"Orateurs distincts après: {after} (delta: {before - after})")
