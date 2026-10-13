"""Phase 6: Fix all remaining orateur parsing issues.

Categories:
  Cat 5 - Broken/duplicated president names (RENAME)
  Cat 1 - AN sommaire entries with concatenated names (DELETE safe only)
  Cat 2 - Amendment listing splits (MERGE+DELETE)
  Cat 4 - Question listing splits (MERGE+DELETE)
  Cat 3a - "X à M" question list headers (DELETE safe only)
  Cat 3c - "X remplace M" presidency changes (DELETE short only)

NOT touched:
  Cat 3b - "X et M/MM" co-authors — left as-is (real speeches)

Usage:
  python scripts/fix_orateurs_phase6_all.py                    # dry-run (default)
  python scripts/fix_orateurs_phase6_all.py --apply            # apply all
  python scripts/fix_orateurs_phase6_all.py --apply --only 5,2 # apply specific cats
"""
import argparse
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, text

ENGINE = create_engine("postgresql://localhost/legislateur_local")

SAMPLE_SIZE = 10


@dataclass
class Action:
    category: str  # "5", "1", "2", "4", "3a", "3c"
    action_type: str  # RENAME, MERGE_DELETE, DELETE
    intervention_id: int
    description: str
    old_orateur: str | None = None
    new_orateur: str | None = None
    prev_orateur: str | None = None


# ---------------------------------------------------------------------------
# Cat 5: Broken president names (RENAME)
# ---------------------------------------------------------------------------
PRESIDENT_NAME_MAP = {
    "M.le président. M. le président": "M. le président",
    "M. le président M. le président": "M. le président",
    "M.le président M. le président": "M. le président",
    "M.le président. M . le président": "M. le président",
    "M.le président. M. le président. M. le président": "M. le président",
    "M.le président. M. l e président": "M. le président",
    "M.le président": "M. le président",
    "M. la président": "M. le président",
    "M. la présidente": "Mme la présidente",
    "M. la président de la commission": "M. le président de la commission",
    "M. la président de le commission": "M. le président de la commission",
    "M. la président de la comraission": "M. le président de la commission",
    "M. la président de la commission spéciale": "M. le président de la commission spéciale",
    "Mmela présidente. Mme la présidente": "Mme la présidente",
    "Mmela présidente Mme la présidente": "Mme la présidente",
    "Mmela présidente. M me la présidente": "Mme la présidente",
    "Mmela présidente. Mme la présidente. Mme la présidente": "Mme la présidente",
    "Mmela présidente. Mme l a présidente": "Mme la présidente",
    "Mmela présidente. Mme la p réside nte": "Mme la présidente",
    "Mmela présidente. Mme la prés idente": "Mme la présidente",
    "Mmela présidente. Mme la préside nte": "Mme la présidente",
    "Mme la présidente Mme la présidente": "Mme la présidente",
    "Mme la présidente Michèle Tabarot": "Mme la présidente",
    "Mme le présidente": "Mme le président",
    "Mme. la présidente": "Mme la présidente",
    "Mmele président. Mme le président": "Mme le président",
    "Mmele président Mme le président": "Mme le président",
    "Mmele président. M me le président": "Mme le président",
}


def detect_cat5(conn) -> list[Action]:
    actions = []
    for bad, good in PRESIDENT_NAME_MAP.items():
        rows = conn.execute(
            text("SELECT id FROM interventions WHERE orateur = :bad"),
            {"bad": bad},
        ).fetchall()
        for (iid,) in rows:
            actions.append(Action(
                category="5", action_type="RENAME", intervention_id=iid,
                description=f"Rename '{bad}' → '{good}'",
                old_orateur=bad, new_orateur=good,
            ))
    return actions


# ---------------------------------------------------------------------------
# Cat 1: AN sommaire entries (DELETE safe only)
# Safety filter: exclude Sénat entries and long AN entries without "(p."
# ---------------------------------------------------------------------------
def detect_cat1(conn) -> list[Action]:
    rows = conn.execute(text("""
        SELECT i.id, i.orateur, d.chambre, LENGTH(i.contenu) as clen,
               i.contenu LIKE '%%(p.%%' as has_page_ref
        FROM interventions i
        JOIN debats d ON i.debat_id = d.id
        WHERE (i.orateur ~ E'[a-zà-ÿ]M[a-zà-ÿ. ]'
           OR (i.orateur ~ E'[a-zà-ÿ]M$'
               AND i.orateur NOT LIKE '%% et M'
               AND i.orateur NOT LIKE '%% à M'
               AND i.orateur NOT LIKE '%% remplace M'))
    """)).fetchall()

    actions = []
    skipped = 0
    for r in rows:
        iid, orateur, chambre, clen, has_page_ref = r
        # Safety: skip Sénat entries (may contain real speeches)
        if chambre == "SENAT":
            skipped += 1
            continue
        # Safety: skip long AN entries without page references (may be real content)
        if clen > 2000 and not has_page_ref:
            skipped += 1
            continue
        actions.append(Action(
            category="1", action_type="DELETE", intervention_id=iid,
            description=f"Delete AN sommaire ({clen} chars)",
            old_orateur=orateur,
        ))
    if skipped:
        print(f"  (Skipped {skipped} entries: Sénat or long AN without page refs)")
    return actions


# ---------------------------------------------------------------------------
# Cat 2: Amendment listing splits (MERGE+DELETE)
# ---------------------------------------------------------------------------
def detect_cat2(conn) -> list[Action]:
    rows = conn.execute(text("""
        SELECT i.id, i.orateur, prev.id as prev_id, prev.orateur as prev_orateur
        FROM interventions i
        JOIN LATERAL (
            SELECT p.id, p.orateur, p.contenu FROM interventions p
            WHERE p.debat_id = i.debat_id AND p.ordre < i.ordre
            ORDER BY p.ordre DESC LIMIT 1
        ) prev ON true
        WHERE i.orateur NOT LIKE 'M. le %%'
          AND i.orateur NOT LIKE 'Mme la %%'
          AND i.orateur NOT LIKE 'Mme le %%'
          AND LENGTH(i.contenu) < 200
          AND (
              i.contenu ~ '^(le rapporteur|Rejet|Retrait|Non soutenu|Adoption)'
              OR i.contenu ~ '^[A-ZÀ-Ý][a-zà-ÿ-]+,'
          )
          AND (
              RIGHT(prev.contenu, 3) IN (' de', ' du', ' au')
              OR RIGHT(prev.contenu, 4) IN (' par', ' des', ' aux')
          )
    """)).fetchall()

    actions = []
    for r in rows:
        iid, orateur, prev_id, prev_orateur = r
        actions.append(Action(
            category="2", action_type="MERGE_DELETE", intervention_id=iid,
            description=f"Merge amendment fragment into prev (id={prev_id})",
            old_orateur=orateur, prev_orateur=prev_orateur,
        ))
    return actions


# ---------------------------------------------------------------------------
# Cat 4: Question listing splits (MERGE+DELETE)
# ---------------------------------------------------------------------------
def detect_cat4(conn) -> list[Action]:
    rows = conn.execute(text("""
        SELECT i.id, i.orateur, prev.id as prev_id, prev.orateur as prev_orateur
        FROM interventions i
        JOIN LATERAL (
            SELECT p.id, p.orateur, p.contenu FROM interventions p
            WHERE p.debat_id = i.debat_id AND p.ordre < i.ordre
            ORDER BY p.ordre DESC LIMIT 1
        ) prev ON true
        WHERE i.orateur NOT LIKE 'M. le %%'
          AND i.orateur NOT LIKE 'Mme la %%'
          AND i.orateur NOT LIKE 'Mme le %%'
          AND LENGTH(i.contenu) < 250
          AND i.contenu LIKE '%%Question n°%%'
          AND (
              RIGHT(prev.contenu, 3) IN (' de', ' du')
              OR RIGHT(prev.contenu, 4) IN (' par', ' des')
              OR prev.contenu LIKE '%%Question n°%%'
          )
    """)).fetchall()

    actions = []
    for r in rows:
        iid, orateur, prev_id, prev_orateur = r
        actions.append(Action(
            category="4", action_type="MERGE_DELETE", intervention_id=iid,
            description=f"Merge question listing into prev (id={prev_id})",
            old_orateur=orateur, prev_orateur=prev_orateur,
        ))
    return actions


# ---------------------------------------------------------------------------
# Cat 3a: "X à M" question list headers (DELETE safe only)
# Safety: only delete if content contains procedural markers
# ---------------------------------------------------------------------------
def detect_cat3a(conn) -> list[Action]:
    rows = conn.execute(text("""
        SELECT i.id, i.orateur, LENGTH(i.contenu) as clen,
               (i.contenu LIKE '%%ministre%%'
                OR i.contenu LIKE '%%secrétaire%%'
                OR i.contenu LIKE '%%Question n°%%'
                OR i.contenu LIKE '%%Ordre du jour%%'
                OR i.contenu LIKE '%%nº%%'
                OR i.contenu LIKE '%%; -%%'
               ) as is_procedural
        FROM interventions i
        WHERE i.orateur LIKE '%% à M'
           OR i.orateur LIKE '%% à Mme'
    """)).fetchall()

    actions = []
    skipped = 0
    for r in rows:
        iid, orateur, clen, is_procedural = r
        if not is_procedural:
            skipped += 1
            continue
        actions.append(Action(
            category="3a", action_type="DELETE", intervention_id=iid,
            description=f"Delete question header ({clen} chars)",
            old_orateur=orateur,
        ))
    if skipped:
        print(f"  (Skipped {skipped} entries without procedural markers — may contain real speech)")
    return actions


# ---------------------------------------------------------------------------
# Cat 3c: "X remplace M" presidency changes (DELETE short only)
# Safety: only delete entries < 200 chars (purely procedural)
# Long ones contain real debate content that is NOT duplicated elsewhere.
# ---------------------------------------------------------------------------
def detect_cat3c(conn) -> list[Action]:
    rows = conn.execute(text("""
        SELECT i.id, i.orateur, LENGTH(i.contenu) as clen
        FROM interventions i
        WHERE (i.orateur LIKE '%% remplace M'
            OR i.orateur LIKE '%% remplace Mme')
          AND LENGTH(i.contenu) < 200
    """)).fetchall()

    # Count skipped for info
    skipped = conn.execute(text("""
        SELECT COUNT(*) FROM interventions
        WHERE (orateur LIKE '%% remplace M' OR orateur LIKE '%% remplace Mme')
          AND LENGTH(contenu) >= 200
    """)).scalar()

    actions = []
    for r in rows:
        iid, orateur, clen = r
        actions.append(Action(
            category="3c", action_type="DELETE", intervention_id=iid,
            description=f"Delete presidency change marker ({clen} chars)",
            old_orateur=orateur,
        ))
    if skipped:
        print(f"  (Skipped {skipped} entries >= 200 chars — contain real debate content)")
    return actions


# ---------------------------------------------------------------------------
# Dry-run display
# ---------------------------------------------------------------------------
def display_dry_run(actions: list[Action], category: str):
    cat_actions = [a for a in actions if a.category == category]
    if not cat_actions:
        print(f"  No actions found.\n")
        return

    by_type = {}
    for a in cat_actions:
        by_type.setdefault(a.action_type, []).append(a)

    print(f"  Total: {len(cat_actions)} interventions")
    for t, items in by_type.items():
        print(f"    {t}: {len(items)}")
    print()

    samples = random.sample(cat_actions, min(SAMPLE_SIZE, len(cat_actions)))
    for i, a in enumerate(samples, 1):
        print(f"  Sample {i}:")
        if a.action_type == "RENAME":
            print(f"    id={a.intervention_id}")
            print(f"    OLD: {repr(a.old_orateur)}")
            print(f"    NEW: {repr(a.new_orateur)}")
        elif a.action_type == "DELETE":
            print(f"    id={a.intervention_id} orateur={repr(a.old_orateur)}")
            print(f"    {a.description}")
        else:  # MERGE_DELETE
            print(f"    id={a.intervention_id} orateur={repr(a.old_orateur)}")
            print(f"    prev={repr(a.prev_orateur)}")
        print()


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
def apply_actions(conn, actions: list[Action]) -> tuple[dict, set]:
    stats = {"renamed": 0, "merged": 0, "deleted": 0, "embeddings_deleted": 0}
    modified_ids = set()
    deleted_ids = set()

    # RENAMEs in bulk
    rename_map: dict[str, str] = {}
    for a in actions:
        if a.action_type == "RENAME":
            rename_map.setdefault(a.old_orateur, a.new_orateur)

    for old, new in rename_map.items():
        result = conn.execute(
            text("UPDATE interventions SET orateur = :new WHERE orateur = :old"),
            {"old": old, "new": new},
        )
        stats["renamed"] += result.rowcount

    # MERGE_DELETEs
    for a in actions:
        if a.action_type != "MERGE_DELETE" or a.intervention_id in deleted_ids:
            continue

        prev_row = conn.execute(text("""
            SELECT p.id, p.contenu FROM interventions p
            WHERE p.debat_id = (SELECT debat_id FROM interventions WHERE id = :iid)
              AND p.ordre < (SELECT ordre FROM interventions WHERE id = :iid)
            ORDER BY p.ordre DESC LIMIT 1
        """), {"iid": a.intervention_id}).fetchone()

        if not prev_row:
            continue

        prev_id, prev_contenu = prev_row
        cur_row = conn.execute(text(
            "SELECT orateur, contenu FROM interventions WHERE id = :iid"
        ), {"iid": a.intervention_id}).fetchone()

        if not cur_row:
            continue

        orateur, contenu = cur_row
        merged = prev_contenu + " " + orateur + " " + contenu
        conn.execute(
            text("UPDATE interventions SET contenu = :merged WHERE id = :pid"),
            {"merged": merged.strip(), "pid": prev_id},
        )
        modified_ids.add(prev_id)
        deleted_ids.add(a.intervention_id)
        stats["merged"] += 1

    # DELETEs (pure delete, no merge)
    pure_deletes = {a.intervention_id for a in actions if a.action_type == "DELETE"}
    deleted_ids.update(pure_deletes)

    # Clean up embeddings + delete interventions
    if deleted_ids:
        deleted_list = list(deleted_ids)
        result = conn.execute(
            text("DELETE FROM chunk_embeddings WHERE intervention_id = ANY(:ids)"),
            {"ids": deleted_list},
        )
        stats["embeddings_deleted"] += result.rowcount

        conn.execute(
            text("DELETE FROM interventions WHERE id = ANY(:ids)"),
            {"ids": deleted_list},
        )
        stats["deleted"] = len(deleted_list)

    return stats, modified_ids


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
ALL_CATS = ["5", "1", "2", "4", "3a", "3c"]


def main():
    parser = argparse.ArgumentParser(description="Fix orateur parsing issues (phase 6)")
    parser.add_argument("--apply", action="store_true", help="Apply corrections (default: dry-run)")
    parser.add_argument("--only", type=str, help="Comma-separated categories (e.g. 5,2,4)")
    args = parser.parse_args()

    cats = set(args.only.split(",")) if args.only else set(ALL_CATS)

    print("=" * 80)
    print(f"Phase 6 — Fix orateur parsing issues ({'APPLY' if args.apply else 'DRY-RUN'})")
    print(f"Categories: {sorted(cats)}")
    print("=" * 80)

    all_actions: list[Action] = []

    detectors = {
        "5":  ("Cat 5: Broken president names (RENAME)", detect_cat5),
        "1":  ("Cat 1: AN sommaire entries (DELETE safe)", detect_cat1),
        "2":  ("Cat 2: Amendment listing splits (MERGE+DELETE)", detect_cat2),
        "4":  ("Cat 4: Question listing splits (MERGE+DELETE)", detect_cat4),
        "3a": ("Cat 3a: 'à M' question headers (DELETE safe)", detect_cat3a),
        "3c": ("Cat 3c: 'remplace M' presidency changes (DELETE short)", detect_cat3c),
    }

    with ENGINE.connect() as conn:
        for cat_id in ALL_CATS:
            if cat_id not in cats:
                continue
            label, detector = detectors[cat_id]
            print(f"\n{'─' * 60}")
            print(f"  {label}")
            print(f"{'─' * 60}")
            actions = detector(conn)
            all_actions.extend(actions)
            display_dry_run(actions, cat_id)

    print(f"\n{'=' * 80}")
    print(f"TOTAL: {len(all_actions)} actions")

    by_cat = {}
    for a in all_actions:
        by_cat.setdefault(a.category, []).append(a)
    for c in sorted(by_cat):
        by_type = {}
        for a in by_cat[c]:
            by_type.setdefault(a.action_type, 0)
            by_type[a.action_type] += 1
        types_str = ", ".join(f"{v} {k.lower()}" for k, v in by_type.items())
        print(f"  Cat {c}: {len(by_cat[c])} ({types_str})")

    if not args.apply:
        print(f"\nDry-run complete. Use --apply to execute.")
        return

    print(f"\nApplying {len(all_actions)} actions...")
    with ENGINE.begin() as conn:
        stats, modified_ids = apply_actions(conn, all_actions)

    print(f"\nResults:")
    print(f"  Renamed: {stats['renamed']}")
    print(f"  Merged:  {stats['merged']}")
    print(f"  Deleted: {stats['deleted']}")
    print(f"  Embeddings deleted: {stats['embeddings_deleted']}")
    print(f"  Modified intervention IDs: {len(modified_ids)}")

    out_path = Path("data/phase6_modified_ids.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(sorted(modified_ids)))
    print(f"  Saved modified IDs to {out_path}")


if __name__ == "__main__":
    main()
