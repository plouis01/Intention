"""Create trial links in DB for each row in a CSV and update the LIEN_ESSAI column."""

import csv
import sys
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.admin.routes import _create_single_trial_link  # noqa: E402
from src.db.models import User, get_session  # noqa: E402
from src.db.models import get_engine  # noqa: E402


def main():
    csv_path = Path(__file__).resolve().parent.parent / "data" / "batch1_clean.csv"
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        sys.exit(1)

    # Init DB
    get_engine()
    db = get_session()

    # Find admin user
    admin = db.query(User).filter(User.role == "admin").first()
    if not admin:
        print("No admin user found in DB. Cannot create trial links.")
        db.close()
        sys.exit(1)
    print(f"Using admin: {admin.email} (id={admin.id})")

    # Read CSV
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    created = 0
    existing = 0
    errors = 0

    for row in rows:
        email = row.get("EMAIL", "").strip()
        name = row.get("NOM_COMPLET", "").strip() or None
        if not email:
            errors += 1
            continue

        result = _create_single_trial_link(db, email, name, admin.id)
        if result is None:
            print(f"  SKIP {email} (existing non-trial account)")
            errors += 1
            continue

        link_url = result["link_url"]
        row["LIEN_ESSAI"] = link_url

        # Detect if it was newly created or already existed
        if result.get("visit_count", 0) == 0 and result["status"] == "pending":
            created += 1
        else:
            existing += 1
        print(f"  OK   {email} -> {link_url[:60]}...")

    # Write updated CSV
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    db.close()

    print(f"\nDone! Created: {created}, Already existed: {existing}, Errors: {errors}")
    print(f"CSV updated: {csv_path}")


if __name__ == "__main__":
    main()
