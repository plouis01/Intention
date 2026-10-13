"""Relink mislinked debates to their correct dossiers.

Three actions based on content analysis:
  A. RELINK: debate clearly discusses the destination law → move to correct dossier
  B. LEAVE: debate still discusses the source law → keep in place
  C. UNLINK: generic debate (questions, other texts) → set dossier_id = NULL

Usage:
    # Dry run (no changes, just report)
    python scripts/relink_debates.py

    # Apply changes
    python scripts/relink_debates.py --apply
"""

import argparse
import json
import logging
from datetime import date
from pathlib import Path

from sqlalchemy import func

from src.db.models import Debat, DossierLegislatif, Intervention, Loi, get_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DEFINITIVE_PATH = Path("data/relink_definitive.json")


def load_definitive_plan() -> dict:
    """Load the pre-computed definitive plan."""
    if not DEFINITIVE_PATH.exists():
        raise FileNotFoundError(
            f"{DEFINITIVE_PATH} not found. Run the analysis script first."
        )
    with open(DEFINITIVE_PATH) as f:
        return json.load(f)


def verify_relink(db, entry: dict) -> str | None:
    """Verify a single relink action. Returns error string or None if OK."""
    did = entry["debate_id"]
    from_dos = entry["from_dossier_id"]
    to_dos = entry["to_dossier_id"]

    deb = db.query(Debat).filter(Debat.id == did).first()
    if not deb:
        return f"debate {did}: NOT FOUND"
    if deb.dossier_id != from_dos:
        return f"debate {did}: current dossier is {deb.dossier_id}, expected {from_dos}"

    dst_dos = db.query(DossierLegislatif).filter(DossierLegislatif.id == to_dos).first()
    if not dst_dos:
        return f"debate {did}: destination dossier {to_dos} NOT FOUND"

    # Chamber match
    deb_ch = (deb.chambre or "").upper().replace("SÉNAT", "SENAT")
    dos_ch = dst_dos.chambre.upper().replace("SÉNAT", "SENAT")
    if deb_ch != dos_ch:
        return f"debate {did}: chambre mismatch deb={deb_ch} dos={dos_ch}"

    return None


def verify_unlink(db, entry: dict) -> str | None:
    """Verify a single unlink action. Returns error string or None if OK."""
    did = entry["debate_id"]
    dos_id = entry["dossier_id"]

    deb = db.query(Debat).filter(Debat.id == did).first()
    if not deb:
        return f"debate {did}: NOT FOUND"
    if deb.dossier_id != dos_id:
        return f"debate {did}: current dossier is {deb.dossier_id}, expected {dos_id}"

    # Verify it's really a generic debate (no finance year reference for this law)
    loi = db.query(Loi).filter(Loi.numero == entry["law"]).first()
    if not loi:
        return None  # Can't verify further, proceed

    all_intervs = db.query(Intervention.contenu).filter(
        Intervention.debat_id == did
    ).all()
    full_text = " ".join(iv[0][:500] for iv in all_intervs if iv[0])
    full_lower = full_text.lower()

    year = loi.annee
    # If it mentions the source law year in a finance context, it's NOT generic
    src_patterns = [
        f"finances pour {year}",
        f"finances pour {year + 1}" if loi.type == "LF" else "",
    ]
    if any(p and p in full_lower for p in src_patterns):
        return f"debate {did}: mentions source law year {year} - should NOT unlink"

    return None


def run(apply: bool = False):
    """Run verification and optionally apply changes."""
    plan = load_definitive_plan()

    relinks = plan["relink"]
    leaves = plan["leave"]
    unlinks = plan["unlink"]

    print("=" * 80)
    print("RELINK DEBATES - DRY RUN" if not apply else "RELINK DEBATES - APPLYING")
    print("=" * 80)
    print(f"  A. Relinks: {len(relinks)}")
    print(f"  B. Leave in place: {len(leaves)}")
    print(f"  C. Unlink (set dossier_id=NULL): {len(unlinks)}")
    print(f"  Total: {len(relinks) + len(leaves) + len(unlinks)}")

    with get_session() as db:
        # --- Verify relinks ---
        print(f"\n--- Verifying {len(relinks)} relinks ---")
        relink_issues = []
        for entry in relinks:
            err = verify_relink(db, entry)
            if err:
                relink_issues.append(err)

        if relink_issues:
            print(f"  ⚠️  {len(relink_issues)} issues:")
            for issue in relink_issues:
                print(f"    {issue}")
        else:
            print(f"  ✅ All {len(relinks)} relinks verified")

        # Show relinks grouped by transfer
        from collections import defaultdict
        by_transfer = defaultdict(list)
        for e in relinks:
            by_transfer[(e["from_law"], e["to_law"])].append(e["debate_id"])
        print(f"\n  Transfers:")
        for (src, dst), ids in sorted(by_transfer.items(), key=lambda x: -len(x[1])):
            print(f"    {src} → {dst}: {len(ids)} debates")

        # --- Verify unlinks ---
        print(f"\n--- Verifying {len(unlinks)} unlinks ---")
        unlink_issues = []
        for entry in unlinks:
            err = verify_unlink(db, entry)
            if err:
                unlink_issues.append(err)

        if unlink_issues:
            print(f"  ⚠️  {len(unlink_issues)} issues:")
            for issue in unlink_issues:
                print(f"    {issue}")
        else:
            print(f"  ✅ All {len(unlinks)} unlinks verified")

        # Show unlinks grouped by source dossier
        by_source = defaultdict(list)
        for e in unlinks:
            by_source[e["law"]].append(e["debate_id"])
        print(f"\n  Unlinks by source law:")
        for law, ids in sorted(by_source.items(), key=lambda x: -len(x[1])):
            print(f"    {law}: {len(ids)} debates to unlink")

        # --- Summary ---
        total_issues = len(relink_issues) + len(unlink_issues)
        print(f"\n{'='*80}")
        print(f"VERIFICATION SUMMARY")
        print(f"{'='*80}")
        print(f"  Relinks OK: {len(relinks) - len(relink_issues)}/{len(relinks)}")
        print(f"  Unlinks OK: {len(unlinks) - len(unlink_issues)}/{len(unlinks)}")
        print(f"  Total issues: {total_issues}")

        if total_issues > 0 and apply:
            print(f"\n❌ NOT APPLYING: {total_issues} issues found. Fix them first.")
            return

        # --- Apply ---
        if apply:
            print(f"\n--- APPLYING ---")

            # A. Relinks
            relinked = 0
            for entry in relinks:
                db.query(Debat).filter(Debat.id == entry["debate_id"]).update(
                    {"dossier_id": entry["to_dossier_id"]}
                )
                relinked += 1
            print(f"  ✅ Relinked {relinked} debates")

            # C. Unlinks
            unlinked = 0
            for entry in unlinks:
                db.query(Debat).filter(Debat.id == entry["debate_id"]).update(
                    {"dossier_id": None}
                )
                unlinked += 1
            print(f"  ✅ Unlinked {unlinked} debates (dossier_id → NULL)")

            db.commit()
            print(f"\n  COMMITTED. Total changes: {relinked + unlinked}")

            # Save audit log
            audit = {
                "timestamp": str(date.today()),
                "relinked": relinked,
                "unlinked": unlinked,
                "left_in_place": len(leaves),
                "relink_details": relinks,
                "unlink_details": unlinks,
            }
            audit_path = Path("data/relink_audit.json")
            with open(audit_path, "w") as f:
                json.dump(audit, f, indent=2, ensure_ascii=False)
            print(f"  Audit log: {audit_path}")
        else:
            print(f"\n--- DRY RUN --- (use --apply to execute)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Relink mislinked debates")
    parser.add_argument("--apply", action="store_true", help="Apply changes")
    args = parser.parse_args()
    run(apply=args.apply)
