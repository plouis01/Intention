#!/usr/bin/env python3
"""Fix 426 AN debates incorrectly dated 1958-10-04.

These are OCR imports from AN archives where date extraction failed.
The reference_externe field contains the session info, and the OCR text
files contain the actual session date in the header.
"""

import logging
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

OCR_BASE = Path("data/ocr_output/google-docai/an")

# Legislature year ranges (start year of first session)
LEGISLATURES = [
    (1, 1958, 1962),
    (2, 1962, 1967),
    (3, 1967, 1968),
    (4, 1968, 1973),
    (5, 1973, 1978),
    (6, 1978, 1981),
    (7, 1981, 1986),
    (8, 1986, 1988),
    (9, 1988, 1993),
    (10, 1993, 1997),
    (11, 1997, 2002),
]

# French months for date parsing
FRENCH_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}

# Date pattern from archive_debates.py
DATE_PATTERN = re.compile(
    r"(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)?\s*"
    r"(\d{1,2})\s+"
    r"(janvier|février|fevrier|mars|avril|mai|juin|juillet|"
    r"août|aout|septembre|octobre|novembre|décembre|decembre)\s+"
    r"(\d{4})",
    re.IGNORECASE,
)

# Also match "Séance du ..." pattern specifically
SEANCE_DATE_PATTERN = re.compile(
    r"[Ss][ée]ance\s+du\s+(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)?\s*"
    r"(\d{1,2})\s*(?:er)?\s+"
    r"(janvier|février|fevrier|mars|avril|mai|juin|juillet|"
    r"août|aout|septembre|octobre|novembre|décembre|decembre)\s+"
    r"(\d{4})",
    re.IGNORECASE,
)


def get_legislature_for_session(session_name: str) -> int | None:
    """Map a session name like '1962-1963-ordinaire1' to a legislature number."""
    match = re.match(r"(\d{4})-(\d{4})", session_name)
    if not match:
        return None
    start_year = int(match.group(1))

    for leg_num, leg_start, leg_end in LEGISLATURES:
        if leg_start <= start_year < leg_end:
            return leg_num
    return None


def parse_reference_externe(ref: str) -> tuple[str, str] | None:
    """Parse reference_externe to get session_name and page_num.

    Input: 'archive_an:1962-1963-extraordinaire1/047'
    Returns: ('1962-1963-extraordinaire1', '047')
    """
    match = re.match(r"archive_an:(.+)/(\d+)$", ref)
    if match:
        return match.group(1), match.group(2)
    return None


def find_ocr_file(session_name: str, page_num: str) -> Path | None:
    """Find the OCR text file for a given session and page."""
    leg = get_legislature_for_session(session_name)
    if leg is None:
        return None

    path = OCR_BASE / f"leg{leg}" / session_name / f"{page_num}.txt"
    if path.exists():
        return path
    return None


def extract_date_from_ocr(filepath: Path) -> datetime | None:
    """Extract the session date from the first 3000 chars of an OCR text file."""
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")[:3000]
    except Exception:
        return None

    # Try "Séance du ..." pattern first (most reliable)
    match = SEANCE_DATE_PATTERN.search(text)
    if match:
        return _parse_date_match(match)

    # Fallback to general date pattern
    for match in DATE_PATTERN.finditer(text):
        result = _parse_date_match(match)
        if result and 1958 <= result.year <= 2002:
            return result

    return None


def _parse_date_match(match) -> datetime | None:
    """Parse a regex date match into a datetime."""
    try:
        day = int(match.group(1))
        month_name = match.group(2).lower()
        year = int(match.group(3))
        month = FRENCH_MONTHS.get(month_name)
        if month and 1 <= day <= 31 and 1958 <= year <= 2002:
            return datetime(year, month, day)
    except (ValueError, KeyError):
        pass
    return None


def session_midpoint_date(session_name: str) -> datetime | None:
    """Compute an approximate date from the session name.

    Used as fallback when OCR date extraction fails.
    """
    match = re.match(r"(\d{4})-(\d{4})-(\w+)", session_name)
    if not match:
        return None

    start_year = int(match.group(1))
    end_year = int(match.group(2))
    session_type = match.group(3).lower()

    # Map session types to approximate date ranges
    if session_type.startswith("ordinaire1") or session_type == "ordinaire":
        # October to June (main session)
        return datetime(start_year, 12, 15)  # midpoint
    elif session_type.startswith("ordinaire2"):
        # April to June (second ordinary session, pre-1995)
        return datetime(end_year, 5, 15)
    elif session_type.startswith("extraordinaire"):
        # July to September typically
        return datetime(end_year, 8, 1)
    elif session_type.startswith("const"):
        # Constitutional session 1958
        return datetime(start_year, 10, 15)
    elif session_type.startswith("droit"):
        # Opening session
        return datetime(start_year, 10, 2)
    else:
        return datetime(start_year, 12, 1)


def main():
    from src.db.models import Debat, get_session

    session = get_session()

    # Find all misdated debates
    misdated = (
        session.query(Debat)
        .filter(
            Debat.chambre == "AN",
            Debat.date == datetime(1958, 10, 4),
            Debat.reference_externe.like("archive_an:%"),
        )
        .all()
    )

    logger.info(f"Found {len(misdated)} misdated AN debates")

    fixed_ocr = 0
    fixed_approx = 0
    failed = 0

    for debat in misdated:
        parsed = parse_reference_externe(debat.reference_externe)
        if not parsed:
            logger.warning(f"  Cannot parse reference: {debat.reference_externe}")
            failed += 1
            continue

        session_name, page_num = parsed
        new_date = None

        # Try 1: Extract from OCR text
        ocr_file = find_ocr_file(session_name, page_num)
        if ocr_file:
            new_date = extract_date_from_ocr(ocr_file)
            if new_date:
                fixed_ocr += 1

        # Try 2: Approximate from session name
        if not new_date:
            new_date = session_midpoint_date(session_name)
            if new_date:
                fixed_approx += 1
            else:
                logger.warning(f"  Cannot determine date for {debat.reference_externe}")
                failed += 1
                continue

        debat.date = new_date

    session.commit()
    session.close()

    logger.info(f"\nResults:")
    logger.info(f"  Fixed from OCR text:    {fixed_ocr}")
    logger.info(f"  Fixed with approximate: {fixed_approx}")
    logger.info(f"  Failed:                 {failed}")
    logger.info(f"  Total:                  {fixed_ocr + fixed_approx + failed}")


if __name__ == "__main__":
    main()
