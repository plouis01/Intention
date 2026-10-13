"""Phase 10c: Fix remaining false speakers (name + context/verb).

These are sentence fragments parsed as orateurs, like:
  "Mme Capdevielle avait rappelé que"
  "M. Goujon évoquer les déplacements"

Logic: merge with previous intervention (orateur text + contenu → appended to prev).
Exception: if the orateur part looks like a real name with a particle (de, du, del),
skip it.
"""
import re
from sqlalchemy import create_engine, text

engine = create_engine('postgresql://localhost/legislateur_local')

# Names with particles that look like "name + lowercase word"
PARTICLES = re.compile(
    r'^(M\.|Mme|Mlle)\s+[A-ZÀ-Ý][a-zà-ÿ-]+\s+(de|du|del|des|d\'|von|van|ben|di)\s'
)

with engine.begin() as conn:
    before = conn.execute(text("SELECT COUNT(DISTINCT orateur) FROM interventions")).scalar()

    rows = conn.execute(text("""
        SELECT i.id, i.orateur, i.contenu,
               prev.id as prev_id, prev.contenu as prev_contenu
        FROM interventions i
        JOIN interventions prev ON prev.debat_id = i.debat_id
            AND prev.ordre = (
                SELECT MAX(p2.ordre) FROM interventions p2
                WHERE p2.debat_id = i.debat_id AND p2.ordre < i.ordre
            )
        WHERE i.orateur ~ '^(M\\.|Mme|Mlle)\\s+[A-ZÀ-Ý][a-zà-ÿ-]+\\s+[a-zà-ÿ]{3,}'
        AND i.orateur NOT LIKE '%président%'
        AND i.orateur NOT LIKE '%ministre%'
        AND i.orateur NOT LIKE '%secrétaire%'
        AND i.orateur NOT LIKE '%rapporteur%'
        AND i.orateur NOT LIKE '%commissaire%'
        AND i.orateur NOT LIKE '%membre%'
        ORDER BY i.debat_id, i.ordre
    """)).fetchall()

    print(f"False speakers found: {len(rows)}")

    merged = 0
    skipped = 0
    for fid, orateur, contenu, prev_id, prev_contenu in rows:
        # Skip names with particles (de, du, del, etc.)
        if PARTICLES.match(orateur):
            skipped += 1
            continue

        # Merge: append "orateur + contenu" to previous
        new_content = prev_contenu + " " + orateur + " " + (contenu or "")
        conn.execute(text(
            "UPDATE interventions SET contenu = :c WHERE id = :pid"
        ), {"c": new_content.strip(), "pid": prev_id})
        conn.execute(text("DELETE FROM interventions WHERE id = :fid"), {"fid": fid})
        merged += 1

    after = conn.execute(text("SELECT COUNT(DISTINCT orateur) FROM interventions")).scalar()
    print(f"Merged: {merged}, Skipped (particles): {skipped}")
    print(f"Orateurs: {before} -> {after} (delta: {before - after})")
