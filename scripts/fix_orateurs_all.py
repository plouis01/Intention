"""
Consolidated speaker name fix — all phases in one script.

Fixes applied in order:
  1. Newlines in orateur field
  2. Deduplication (CORRECTED regex — minimum 5 chars to avoid "M" bug)
  3. Missing space after Mme/M. before accented chars + re-dedup
  4. Multi-speaker extraction (first speaker only)
  5. Stage directions (didascalies) — merge with previous intervention
  6. Vice-presidency concatenations
  7. False speakers (name + verb fragments) — merge with previous
  8. OCR typo correction via fuzzy matching (Levenshtein distance=1)

Usage:
    python scripts/fix_orateurs_all.py --dry-run   # Preview without applying
    python scripts/fix_orateurs_all.py              # Apply all fixes
"""
import re
import sys

from Levenshtein import distance as levenshtein_distance
from sqlalchemy import create_engine, text

DRY_RUN = "--dry-run" in sys.argv
DB_URL = "postgresql://localhost/legislateur_local"

engine = create_engine(DB_URL)

PARTICLES = re.compile(
    r"^(M\.|Mme|Mlle)\s+[A-ZÀ-Ý][a-zà-ÿ-]+\s+"
    r"(de|du|del|des|d'|von|van|ben|di)\s"
)
EXTRACT_NAME = re.compile(
    r"^((M\.|Mme|Mlle|MM\.?)\s+(?:le\s+|la\s+)?"
    r"[A-ZÀ-Ý][a-zà-ÿ-]+(?:\s+[A-ZÀ-Ý][a-zà-ÿ-]+)*)"
)


def step(name, count):
    prefix = "[DRY RUN] " if DRY_RUN else ""
    print(f"  {prefix}{name}: {count:,}")


def fix_all():
    with engine.begin() as conn:
        before = conn.execute(
            text("SELECT COUNT(DISTINCT orateur) FROM interventions")
        ).scalar()
        before_total = conn.execute(
            text("SELECT COUNT(*) FROM interventions")
        ).scalar()
        print(f"Avant: {before:,} orateurs, {before_total:,} interventions")
        print()

        # === 1. Fix newlines ===
        r = conn.execute(text("""
            UPDATE interventions
            SET orateur = REGEXP_REPLACE(orateur, E'[\\n\\r]+', ' ', 'g')
            WHERE orateur ~ E'[\\n\\r]'
        """))
        step("1. Newlines", r.rowcount)

        # === 2. Deduplication — CORRECTED regex (min 5 chars) ===
        # Point separator: "M. X. M. X" → "M. X"
        r = conn.execute(text("""
            UPDATE interventions
            SET orateur = TRIM(REGEXP_REPLACE(
                orateur, '^(.{5,}?)\\. ?\\1.*$', '\\1'
            ))
            WHERE orateur ~ '^(.{5,})\\. ?\\1'
        """))
        step("2a. Dedup (dot)", r.rowcount)

        # Comma separator: "M. X, M. X," → "M. X"
        r = conn.execute(text("""
            UPDATE interventions
            SET orateur = TRIM(REGEXP_REPLACE(
                orateur, '^(.{5,}?),\\s*\\1.*$', '\\1'
            ))
            WHERE orateur ~ '^(.{5,}),\\s*\\1'
        """))
        step("2b. Dedup (comma)", r.rowcount)

        # Safety check: "M" should NOT have 355K interventions
        m_count = conn.execute(
            text("SELECT COUNT(*) FROM interventions WHERE orateur = 'M'")
        ).scalar()
        print(f"  [CHECK] orateur='M' count: {m_count:,} (should be ~188)")
        if m_count > 1000:
            print("  [ERROR] Dedup regex is still bugged! Aborting.")
            raise RuntimeError("Dedup regex corrupted M. M... names")

        # === 3. Fix missing space after Mme/M. before accented chars ===
        r1 = conn.execute(text("""
            UPDATE interventions
            SET orateur = REGEXP_REPLACE(
                orateur, '^(Mme|Mlle)(É|È|Ê|Ë)', '\\1 \\2'
            )
            WHERE orateur ~ '^(Mme|Mlle)(É|È|Ê|Ë)'
        """))
        r2 = conn.execute(text("""
            UPDATE interventions
            SET orateur = REGEXP_REPLACE(
                orateur, '^(M\\.)(É|È|Ê|Ë)', '\\1 \\2'
            )
            WHERE orateur ~ '^M\\.(É|È|Ê|Ë)'
        """))
        step("3. Missing space (Mme/M.+accent)", r1.rowcount + r2.rowcount)

        # Re-dedup after space fix (new duplicates exposed)
        r = conn.execute(text("""
            UPDATE interventions
            SET orateur = TRIM(REGEXP_REPLACE(
                orateur, '^(.{5,}?)\\. ?\\1.*$', '\\1'
            ))
            WHERE orateur ~ '^(.{5,})\\. ?\\1'
        """))
        step("3b. Re-dedup (dot)", r.rowcount)

        r = conn.execute(text("""
            UPDATE interventions
            SET orateur = TRIM(REGEXP_REPLACE(
                orateur, '^(.{5,}?),\\s*\\1.*$', '\\1'
            ))
            WHERE orateur ~ '^(.{5,}),\\s*\\1'
        """))
        step("3c. Re-dedup (comma)", r.rowcount)

        # === 4. Multi-speaker: extract first name ===
        r = conn.execute(text("""
            UPDATE interventions
            SET orateur = TRIM(REGEXP_REPLACE(
                orateur,
                '^((M\\.|Mme|Mlle)\\s+[A-ZÀ-Ý][a-zà-ÿ-]+(?:\\s+[A-ZÀ-Ý][a-zà-ÿ-]+)*)\\s+et\\s+.*$',
                '\\1'
            ))
            WHERE orateur ~ '^(M\\.|Mme|Mlle)\\s+[A-ZÀ-Ý][a-zà-ÿ-]+\\s+et\\s+'
        """))
        step("4. Multi-speaker", r.rowcount)

        # === 5. Stage directions (didascalies) ===
        # Merge with previous intervention when content starts with ')'
        rows = conn.execute(text("""
            SELECT i.id, i.orateur, i.contenu,
                   prev.id as prev_id, prev.contenu as prev_contenu
            FROM interventions i
            JOIN interventions prev ON prev.debat_id = i.debat_id
                AND prev.ordre = (
                    SELECT MAX(p2.ordre) FROM interventions p2
                    WHERE p2.debat_id = i.debat_id AND p2.ordre < i.ordre
                )
            WHERE (
                i.orateur ~ '(applaudit|acquiesce|opine|proteste|sourit|exclame|manifeste|esclaffe|approuve|renchérit|applaudissent)'
                OR i.orateur ~ '\\mrit$'
            )
            ORDER BY i.debat_id, i.ordre
        """)).fetchall()

        merged_5 = 0
        cleaned_5 = 0
        delete_ids_5 = []
        for fid, orateur, contenu, prev_id, prev_contenu in rows:
            if not contenu:
                continue
            if contenu.lstrip().startswith(")"):
                new_prev = prev_contenu + " (" + orateur + contenu
                conn.execute(
                    text("UPDATE interventions SET contenu = :c WHERE id = :pid"),
                    {"c": new_prev.strip(), "pid": prev_id},
                )
                delete_ids_5.append(fid)
                merged_5 += 1
            else:
                m = EXTRACT_NAME.match(orateur)
                if m:
                    conn.execute(
                        text("UPDATE interventions SET orateur = :n WHERE id = :fid"),
                        {"n": m.group(1).strip(), "fid": fid},
                    )
                    cleaned_5 += 1

        if delete_ids_5:
            conn.execute(
                text("DELETE FROM interventions WHERE id = ANY(:ids)"),
                {"ids": delete_ids_5},
            )
        step("5a. Didascalies merged", merged_5)
        step("5b. Didascalies cleaned", cleaned_5)

        # === 6. Vice-presidency concatenations ===
        r1 = conn.execute(text("""
            UPDATE interventions SET orateur = 'Mme la présidente'
            WHERE orateur ~ 'vice-présidente'
            AND orateur LIKE '%Mme la présidente%'
        """))
        r2 = conn.execute(text("""
            UPDATE interventions SET orateur = 'M. le président'
            WHERE orateur ~ 'vice-président[^e]'
            AND (orateur ~ ' M$' OR orateur ~ 'M$')
        """))
        r3 = conn.execute(text("""
            UPDATE interventions SET orateur = 'Mme le président'
            WHERE orateur LIKE '%Gruny vice-président%Mme le président%'
        """))
        step("6. Vice-presidencies", r1.rowcount + r2.rowcount + r3.rowcount)

        # === 7. False speakers (name + verb fragment) ===
        false_rows = conn.execute(text("""
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

        merged_7 = 0
        skipped_7 = 0
        delete_ids_7 = []
        for fid, orateur, contenu, prev_id, prev_contenu in false_rows:
            if PARTICLES.match(orateur):
                skipped_7 += 1
                continue
            new_content = prev_contenu + " " + orateur + " " + (contenu or "")
            conn.execute(
                text("UPDATE interventions SET contenu = :c WHERE id = :pid"),
                {"c": new_content.strip(), "pid": prev_id},
            )
            delete_ids_7.append(fid)
            merged_7 += 1

        if delete_ids_7:
            conn.execute(
                text("DELETE FROM interventions WHERE id = ANY(:ids)"),
                {"ids": delete_ids_7},
            )
        step("7a. False speakers merged", merged_7)
        step("7b. False speakers skipped (particles)", skipped_7)

        # === 8. OCR fuzzy matching (distance=1 only) ===
        known_rows = conn.execute(text(
            "SELECT orateur, COUNT(*) FROM interventions "
            "GROUP BY orateur HAVING COUNT(*) >= 6"
        )).fetchall()
        known = {r[0]: r[1] for r in known_rows}

        uniques = conn.execute(text("""
            SELECT orateur, id FROM interventions
            WHERE orateur IN (
                SELECT orateur FROM interventions
                GROUP BY orateur HAVING COUNT(*) = 1
            )
            AND LENGTH(orateur) BETWEEN 8 AND 35
            AND orateur ~ '^(M\\.|Mme|Mlle)\\s+[A-ZÀ-Ý]'
            AND orateur !~ '\\s+(et |applaudit|a |est |sur |pour |dans |les |des |un |fait |propose|nous |vous |que |qui |sont |ont |tout )'
        """)).fetchall()

        fixed_8 = 0
        ocr_log = []
        for orateur, fid in uniques:
            best_match = None
            best_dist = 2  # only accept distance 1
            for known_name in known:
                if abs(len(known_name) - len(orateur)) > 1:
                    continue
                d = levenshtein_distance(orateur, known_name)
                if d < best_dist:
                    best_dist = d
                    best_match = known_name

            if best_match and best_dist == 1:
                conn.execute(
                    text("UPDATE interventions SET orateur = :n WHERE id = :fid"),
                    {"n": best_match, "fid": fid},
                )
                fixed_8 += 1
                ocr_log.append(f"{orateur} -> {best_match}")

        step("8. OCR fuzzy (dist=1)", fixed_8)

        # Write OCR log
        with open("data/ocr_corrections.log", "w") as f:
            f.write("\n".join(sorted(ocr_log)))

        # === Final counts ===
        after = conn.execute(
            text("SELECT COUNT(DISTINCT orateur) FROM interventions")
        ).scalar()
        after_total = conn.execute(
            text("SELECT COUNT(*) FROM interventions")
        ).scalar()

        print()
        print("=" * 60)
        print(f"Orateurs:      {before:>10,} → {after:>10,} (delta: {after - before:+,})")
        print(f"Interventions: {before_total:>10,} → {after_total:>10,} (delta: {after_total - before_total:+,})")

        # === Safety checks ===
        print()
        checks = [
            ("orateur='M' (should be ~188)", "SELECT COUNT(*) FROM interventions WHERE orateur = 'M'"),
            ("M. Michel Dreyfus-Schmidt", "SELECT COUNT(*) FROM interventions WHERE orateur = 'M. Michel Dreyfus-Schmidt'"),
            ("M. Maxime Gremetz", "SELECT COUNT(*) FROM interventions WHERE orateur = 'M. Maxime Gremetz'"),
            ("M. Manuel Valls", "SELECT COUNT(*) FROM interventions WHERE orateur = 'M. Manuel Valls'"),
            ("applaudit remaining", "SELECT COUNT(*) FROM interventions WHERE orateur LIKE '%applaudit%'"),
            ("applaudissent remaining", "SELECT COUNT(*) FROM interventions WHERE orateur LIKE '%applaudissent%'"),
            ("dedup residual (dot)", "SELECT COUNT(*) FROM interventions WHERE orateur ~ '^(.{5,})\\. ?\\1'"),
            ("dedup residual (comma)", "SELECT COUNT(*) FROM interventions WHERE orateur ~ '^(.{5,}),\\s*\\1'"),
        ]
        print("Safety checks:")
        for label, query in checks:
            val = conn.execute(text(query)).scalar()
            status = "OK" if val < 1000 or "should" not in label else "WARN"
            print(f"  [{status}] {label}: {val:,}")

        if DRY_RUN:
            print("\n[DRY RUN] Rolling back all changes.")
            raise Exception("DRY_RUN_ROLLBACK")


if __name__ == "__main__":
    try:
        fix_all()
    except Exception as e:
        if "DRY_RUN_ROLLBACK" in str(e):
            pass
        else:
            raise
