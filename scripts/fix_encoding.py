#!/usr/bin/env python3
"""Fix apostrophe and other Unicode encoding issues in the database.

Uses SQL REPLACE() for character substitutions (fast, no ORM overhead).
Falls back to Python only for rows containing HTML entities.
"""

import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from src.db.models import Intervention, SourceExterne, get_session


# Character replacements: (unicode_char, replacement)
REPLACEMENTS = [
    ("\u2019", "'"),   # RIGHT SINGLE QUOTATION MARK
    ("\u2018", "'"),   # LEFT SINGLE QUOTATION MARK
    ("\u201b", "'"),   # SINGLE HIGH-REVERSED-9 QUOTATION MARK
    ("\u201c", '"'),   # LEFT DOUBLE QUOTATION MARK
    ("\u201d", '"'),   # RIGHT DOUBLE QUOTATION MARK
    ("\u201e", '"'),   # DOUBLE LOW-9 QUOTATION MARK
    ("\u201f", '"'),   # DOUBLE HIGH-REVERSED-9 QUOTATION MARK
    ("\u2013", "-"),   # EN DASH
    ("\u2014", "-"),   # EM DASH
    ("\u2026", "..."), # HORIZONTAL ELLIPSIS
    ("\u00a0", " "),   # Non-breaking space
    ("\u202f", " "),   # Narrow no-break space
]


def _build_nested_replace(column: str) -> str:
    """Build nested REPLACE(REPLACE(...)) SQL expression for all substitutions."""
    expr = column
    for old, new in REPLACEMENTS:
        escaped_new = new.replace("'", "''")
        expr = f"REPLACE({expr}, '{old}', '{escaped_new}')"
    return expr


def _sql_fix_column(session, table: str, column: str, batch_size: int = 10000) -> int:
    """Fix Unicode characters in a single column using batched SQL. Returns rows updated."""
    conditions = " OR ".join(
        f"{column} LIKE '%{old}%'" for old, _ in REPLACEMENTS
    )
    nested_replace = _build_nested_replace(column)

    total_updated = 0
    while True:
        sql = f"""
            UPDATE {table} SET {column} = {nested_replace}
            WHERE id IN (
                SELECT id FROM {table} WHERE {conditions} LIMIT {batch_size}
            )
        """
        result = session.execute(text(sql))
        batch_count = result.rowcount
        session.commit()

        if batch_count == 0:
            break
        total_updated += batch_count
        print(f"    ... {total_updated} rows fixed in {table}.{column}")

    return total_updated


def _python_fix_html_entities(session, model_class, columns: list[str]) -> int:
    """Fix HTML entities in rows that contain them (Python fallback)."""
    # Check if any rows need HTML entity decoding
    # &#NNN; (numeric) + common named entities found in French legal texts
    _ENTITY_PATTERNS = ["&#", "&amp;", "&lt;", "&gt;", "&quot;", "&apos;",
                        "&eacute;", "&egrave;", "&agrave;", "&ccedil;", "&nbsp;"]
    conditions = []
    for col in columns:
        for pattern in _ENTITY_PATTERNS:
            conditions.append(getattr(model_class, col).contains(pattern))

    from sqlalchemy import or_
    rows = session.query(model_class).filter(or_(*conditions)).all()

    if not rows:
        return 0

    updated = 0
    for row in rows:
        changed = False
        for col in columns:
            val = getattr(row, col)
            if val:
                decoded = html.unescape(val)
                if decoded != val:
                    setattr(row, col, decoded)
                    changed = True
        if changed:
            updated += 1

    if updated > 0:
        session.commit()
    return updated


def fix_interventions():
    """Fix encoding in all interventions using SQL REPLACE()."""
    session = get_session()

    print("Fixing encoding in interventions...")

    try:
        # Phase 1: SQL-based character replacement (batched)
        updated_contenu = _sql_fix_column(session, "interventions", "contenu")
        updated_orateur = _sql_fix_column(session, "interventions", "orateur")
        print(f"  SQL replacements: {updated_contenu} contenu, {updated_orateur} orateur rows updated")

        # Phase 2: Python-based HTML entity decoding (only affected rows)
        session.expire_all()
        html_fixed = _python_fix_html_entities(session, Intervention, ["contenu", "orateur"])
        print(f"  HTML entities: {html_fixed} rows updated")
    finally:
        session.close()


def fix_sources_externes():
    """Fix encoding in external sources using SQL REPLACE()."""
    session = get_session()

    print("\nFixing encoding in sources_externes...")

    try:
        updated_contenu = _sql_fix_column(session, "sources_externes", "contenu")
        updated_resume = _sql_fix_column(session, "sources_externes", "resume")
        updated_titre = _sql_fix_column(session, "sources_externes", "titre")
        print(f"  SQL replacements: {updated_contenu} contenu, {updated_resume} resume, {updated_titre} titre rows updated")

        session.expire_all()
        html_fixed = _python_fix_html_entities(session, SourceExterne, ["contenu", "resume", "titre"])
        print(f"  HTML entities: {html_fixed} rows updated")
    finally:
        session.close()


def verify_fix():
    """Verify the fix worked."""
    session = get_session()

    print("\n=== VERIFICATION ===")

    # Check for remaining problematic characters
    for char, name in [("\u2019", "curly apostrophe"), ("\u201c", "curly quote left"), ("\u201d", "curly quote right")]:
        count = session.query(Intervention).filter(
            Intervention.contenu.contains(char)
        ).count()
        if count > 0:
            print(f"  WARNING: Still {count} interventions with {name}")
        else:
            print(f"  OK: No {name} remaining")

    # Test specific searches
    print("\n=== SEARCH TEST ===")
    terms = [
        "report d'imposition",
        "apport-cession",
        "150-0 B ter",
    ]
    for term in terms:
        count = session.query(Intervention).filter(
            Intervention.contenu.contains(term)
        ).count()
        print(f'  "{term}": {count} results')

    session.close()


if __name__ == "__main__":
    print("=" * 50)
    print("FIXING UNICODE ENCODING ISSUES")
    print("=" * 50)

    fix_interventions()
    fix_sources_externes()
    verify_fix()

    print("\nDone!")
