#!/usr/bin/env python3
"""Fix ~172 debates with 0 interventions.

Two categories:
  1. AN debates (5): OCR files exist but parser truncated at "QUESTIONS ORALES"
     header. Re-parse without that truncation.
  2. SENAT debates (167): No OCR files on disk, empty shells.
     - 160 are "questions écrites" (q prefix) with no debate content → delete
     - 7 are séance/votes with no source → delete
"""

import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from src.db.models import Debat, Intervention, get_session
from src.parsers.archive_debates import (
    _HOMOGLYPH_MAP,
    _JO_HEADER_RE,
    _SPEAKER_PATTERN,
    FRENCH_MONTHS,
    detect_archive_speaker_function,
)
from src.parsers.debates import extract_article_mentions

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

OCR_BASE = Path("data/ocr_output/google-docai")

LEGISLATURES = [
    (1, 1958, 1962), (2, 1962, 1967), (3, 1967, 1968),
    (4, 1968, 1973), (5, 1973, 1978), (6, 1978, 1981),
    (7, 1981, 1986), (8, 1986, 1988), (9, 1988, 1993),
    (10, 1993, 1997), (11, 1997, 2002),
]


def get_legislature(session_name: str) -> int | None:
    match = re.match(r"(\d{4})-(\d{4})", session_name)
    if not match:
        return None
    start_year = int(match.group(1))
    for leg_num, leg_start, leg_end in LEGISLATURES:
        if leg_start <= start_year < leg_end:
            return leg_num
    return None


def find_an_ocr_file(reference_externe: str) -> Path | None:
    """Find OCR text file from an archive_an reference."""
    match = re.match(r"archive_an:(.+)/(\d+)$", reference_externe)
    if not match:
        return None
    session_name, page_num = match.group(1), match.group(2)
    leg = get_legislature(session_name)
    if leg is None:
        return None
    path = OCR_BASE / "an" / f"leg{leg}" / session_name / f"{page_num}.txt"
    return path if path.exists() else None


def clean_text_keep_questions(raw_text: str) -> str:
    """Clean OCR text WITHOUT truncating at QUESTIONS sections.

    The original clean_archive_text() truncates at "QUESTIONS..." which
    incorrectly drops entire "Questions orales sans débat" sessions.
    """
    text = raw_text
    text = text.translate(_HOMOGLYPH_MAP)
    text = text.replace("\f", "\n")

    # Only truncate at "QUESTIONS ÉCRITES" (written questions, never debates)
    # but keep "QUESTIONS ORALES" and "QUESTIONS PRÉALABLES"
    questions_match = re.search(
        r"(?:^|\n)\s*QUESTIONS\s+[ÉEée]CRITES",
        text,
    )
    if questions_match:
        text = text[:questions_match.start()]

    text = _JO_HEADER_RE.sub("", text)

    # Remove JO footnotes
    text = re.sub(
        r"\n\(\d+\)\s+Cette question.*?(?=\nM[.me]|\n[A-Z]{3,}|\Z)",
        "", text, flags=re.DOTALL,
    )

    # Fix OCR spacing
    text = re.sub(r"N\s+°\s*", "N° ", text)
    text = re.sub(r"-\n([a-zà-ÿ])", r"\1", text)
    text = re.sub(r"-\n([A-ZÀ-Ý])", r"-\1", text)
    text = re.sub(r"(M(?:me|lle)?\.)\s*\n\s*", r"\1 ", text)

    # Normalize ALL-CAPS names to Title Case
    from src.parsers.archive_debates import _ALLCAPS_NAME_RE, _allcaps_to_titlecase
    text = _ALLCAPS_NAME_RE.sub(_allcaps_to_titlecase, text)

    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    return text.strip()


def reparse_an_debate(debat: Debat, session) -> int:
    """Re-parse an AN debate from OCR, returning number of interventions created."""
    ocr_file = find_an_ocr_file(debat.reference_externe)
    if not ocr_file:
        logger.warning(f"  No OCR file for debate {debat.id} ({debat.reference_externe})")
        return 0

    raw_text = ocr_file.read_text(encoding="utf-8", errors="replace")
    if len(raw_text) < 200:
        logger.warning(f"  OCR file too short ({len(raw_text)} chars)")
        return 0

    cleaned = clean_text_keep_questions(raw_text)

    # Split by speakers
    raw_parts = _SPEAKER_PATTERN.split(cleaned)
    interventions = []

    for i in range(1, len(raw_parts) - 2, 3):
        orateur = (raw_parts[i] or raw_parts[i + 1] or "").strip()
        contenu = raw_parts[i + 2].strip() if i + 2 < len(raw_parts) else ""

        if not contenu or len(contenu) < 10:
            continue

        intro_line = contenu[:80]
        fonction = detect_archive_speaker_function(intro_line, orateur)
        articles = extract_article_mentions(contenu)

        interventions.append(Intervention(
            debat_id=debat.id,
            orateur=orateur,
            fonction=fonction,
            contenu=contenu,
            articles_mentionnes=articles,
            ordre=len(interventions) + 1,
        ))

    if interventions:
        for iv in interventions:
            session.add(iv)
        # Update contenu_integral
        debat.contenu_integral = "\n\n".join(
            f"{iv.orateur}: {iv.contenu}" for iv in interventions
        )
        logger.info(f"  Re-parsed debate {debat.id}: {len(interventions)} interventions")
    else:
        logger.warning(f"  Re-parsed debate {debat.id}: still 0 interventions ({len(cleaned)} chars cleaned)")

    return len(interventions)


def main():
    session = get_session()

    # Find all empty debates
    empty_ids = session.execute(text("""
        SELECT d.id, d.chambre, d.reference_externe
        FROM debats d
        LEFT JOIN interventions i ON d.id = i.debat_id
        GROUP BY d.id
        HAVING COUNT(i.id) = 0
        ORDER BY d.chambre, d.date
    """)).fetchall()

    logger.info(f"Found {len(empty_ids)} empty debates")

    an_debates = [r for r in empty_ids if r[1] == "AN"]
    senat_debates = [r for r in empty_ids if r[1] == "SENAT"]

    # --- Step 1: Re-parse AN debates with fixed cleaner ---
    logger.info(f"\n=== Step 1: Re-parse {len(an_debates)} AN debates ===")
    an_fixed = 0
    for row in an_debates:
        debat = session.query(Debat).get(row[0])
        n = reparse_an_debate(debat, session)
        if n > 0:
            an_fixed += 1

    # --- Step 2: Delete empty SENAT debates (no OCR source available) ---
    logger.info(f"\n=== Step 2: Delete {len(senat_debates)} empty SENAT debates ===")
    senat_deleted = 0
    for row in senat_debates:
        debat = session.query(Debat).get(row[0])
        if debat:
            session.delete(debat)
            senat_deleted += 1

    session.commit()
    session.close()

    logger.info(f"\nResults:")
    logger.info(f"  AN re-parsed with interventions: {an_fixed}/{len(an_debates)}")
    logger.info(f"  SENAT empty debates deleted: {senat_deleted}")


if __name__ == "__main__":
    main()
