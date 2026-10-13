"""Phase 4: Fix false speaker + multi-speaker interventions by merging or cleaning."""
from sqlalchemy import create_engine, text
import re

engine = create_engine('postgresql://localhost/legislateur_local')

PARTICLE_CLEAN_RE = re.compile(
    r"^(M\.|Mme|Mlle)\s+[A-ZÀ-Ý][a-zà-ÿ-]+\s+"
    r"(de|du|del|des|von|van|ben|d')[A-ZÀ-Ý ''\-]*$"
)
LE_LA_CLEAN_RE = re.compile(
    r"^(M\.|Mme|Mlle)\s+[A-ZÀ-Ý][a-zà-ÿ-]+\s+(le|la)\s+[A-ZÀ-Ý]"
)
EXTRACT_NAME_RE = re.compile(
    r"^((M\.|Mme|Mlle)\s+(?:le\s+|la\s+)?[A-ZÀ-Ý][a-zà-ÿ-]+(?:\s+[A-ZÀ-Ý][a-zà-ÿ-]+)*)"
)


def is_clean_particle_name(orateur):
    return bool(PARTICLE_CLEAN_RE.match(orateur) or LE_LA_CLEAN_RE.match(orateur))


def extract_name(orateur):
    m = EXTRACT_NAME_RE.match(orateur)
    return m.group(1).strip() if m else orateur


def prev_ends_complete(contenu):
    stripped = contenu.rstrip()
    return bool(re.search(r'[.!?)"]\s*$', stripped))


with engine.begin() as conn:
    bad_interventions = conn.execute(text("""
        SELECT i.id, i.debat_id, i.ordre, i.orateur, i.contenu,
               prev.id as prev_id, prev.contenu as prev_contenu
        FROM interventions i
        JOIN interventions prev ON prev.debat_id = i.debat_id AND prev.ordre = i.ordre - 1
        WHERE (
            (i.orateur ~ '^(M\\.|Mme|Mlle)\\s+[A-ZÀ-Ý][a-zà-ÿ-]+\\s+[a-zà-ÿ]{3,}'
             AND i.orateur NOT LIKE '%président%'
             AND i.orateur NOT LIKE '%ministre%'
             AND i.orateur NOT LIKE '%secrétaire%'
             AND i.orateur NOT LIKE '%rapporteur%')
            OR
            i.orateur ~ '^(M\\.|Mme|Mlle)\\s+[A-ZÀ-Ý][a-zà-ÿ-]+\\s+et\\s+'
        )
        ORDER BY i.debat_id, i.ordre
    """)).fetchall()

    merged_count = 0
    cleaned_count = 0
    skipped_particles = 0
    deleted_ids = set()
    updated_prevs = {}

    for row in bad_interventions:
        fid, debat_id, ordre, orateur, contenu, prev_id, prev_contenu = row

        if prev_id in deleted_ids:
            continue

        if ' et ' not in orateur and is_clean_particle_name(orateur):
            skipped_particles += 1
            continue

        actual_prev_contenu = updated_prevs.get(prev_id, prev_contenu)

        if prev_ends_complete(actual_prev_contenu):
            clean_name = extract_name(orateur)
            if clean_name != orateur:
                conn.execute(text(
                    "UPDATE interventions SET orateur = :name WHERE id = :fid"
                ), {"name": clean_name, "fid": fid})
                cleaned_count += 1
        else:
            merged_content = actual_prev_contenu + " " + orateur + " " + contenu
            updated_prevs[prev_id] = merged_content.strip()
            conn.execute(text(
                "UPDATE interventions SET contenu = :merged WHERE id = :pid"
            ), {"merged": merged_content.strip(), "pid": prev_id})
            deleted_ids.add(fid)
            merged_count += 1

    if deleted_ids:
        conn.execute(text(
            "DELETE FROM interventions WHERE id = ANY(:ids)"
        ), {"ids": list(deleted_ids)})

    print(f"Merged (prev cut): {merged_count}")
    print(f"Cleaned name only (prev complete): {cleaned_count}")
    print(f"Skipped (particle names): {skipped_particles}")
    print(f"Deleted: {len(deleted_ids)}")
