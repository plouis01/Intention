"""Parser for historical parliamentary archive PDFs (AN 1958-2002, Sénat 1958-1996).

Extracts text from pre-OCR'd PDFs via PyMuPDF, cleans Journal Officiel artifacts,
splits by speaker, and returns ParsedDebat/Intervention dataclasses.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

from src.parsers.debates import (
    Intervention,
    ParsedDebat,
    extract_article_mentions,
)

logger = logging.getLogger(__name__)

# French month names → number
FRENCH_MONTHS = {
    "janvier": 1, "février": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
    # Handle common OCR variants
    "fevrier": 2, "aout": 8, "decembre": 12,
}

# Journal Officiel header patterns to remove
JO_HEADER_PATTERNS = [
    # Full header lines
    r"^JOURNAL\s+OFFICIEL.*$",
    r"^ASSEMBLÉE\s+NATIONALE.*$",
    r"^SÉNAT.*$",
    r"^COMPTE\s+RENDU\s+INTÉGRAL.*$",
    r"^COMPTE\s+RENDU\s+ANALYTIQUE.*$",
    r"^DÉBATS\s+PARLEMENTAIRES.*$",
    r"^QUESTIONS\s+ÉCRITES.*$",
    # Page numbers (standalone number lines)
    r"^\s*\d{1,5}\s*$",
    # Session headers
    r"^[-–-]\s*\d+\s*[-–-]\s*$",
    r"^\d+e\s+SÉANCE\s+DU.*$",
    r"^\d+re\s+SÉANCE\s+DU.*$",
    r"^SÉANCE\s+DU\s+.*$",
    r"^PRÉSIDENCE\s+DE\s+.*$",
]

# Compiled header patterns
_JO_HEADER_RE = re.compile(
    "|".join(JO_HEADER_PATTERNS),
    re.MULTILINE | re.IGNORECASE,
)

# ALL-CAPS speaker name pattern: M. DUPONT, Mme MARTIN, M. CHABAN-DELMAS
# NOTE: Does NOT match role-only forms like "M. LE PRÉSIDENT" - those are handled separately
_ALLCAPS_NAME_RE = re.compile(
    r"(M(?:me|lle)?\.?\s+)"  # Title: M., Mme, Mlle
    r"([A-ZÀ-Ý]{2,}(?:[-\s][A-ZÀ-Ý]{2,})*)"  # ALL-CAPS name parts (with hyphens)
)

# Date extraction pattern: Mercredi 29 Avril 1959
_DATE_PATTERN = re.compile(
    r"(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)?\s*"
    r"(\d{1,2})\s+"
    r"(janvier|février|fevrier|mars|avril|mai|juin|juillet|"
    r"août|aout|septembre|octobre|novembre|décembre|decembre)\s+"
    r"(\d{4})",
    re.IGNORECASE,
)

# Speaker pattern (works after ALL-CAPS normalization)
# Matches at line start: "M. Dupont." "M. Chaban-Delmas -" "Mme Martin :"
# Uses \n (line boundary) because PyMuPDF produces single \n between lines,
# never \n\n. The line-start requirement filters out inline references like
# "la parole est à M. Dupont" which appear mid-line.
#
# Two forms are matched:
# 1. Named speakers: "M. Dupont.", "Mme Martin," etc.
# 2. Role-based: "M. le président." - appears ~60x per PDF and is the
#    standard form for the presiding officer in Journal Officiel format.
_SPEAKER_PATTERN = re.compile(
    r"(?:^|\n)"
    r"("
    # Named speakers: "M. Dupont.", "Mme Chaban-Delmas,", etc.
    r"M(?:me|lle)?\.?\s+[A-ZÀ-Ý][a-zà-ÿ]+(?:-[A-ZÀ-Ý][a-zà-ÿ]+)*(?:\s+[A-ZÀ-Ý][a-zà-ÿ]+(?:-[A-ZÀ-Ý][a-zà-ÿ]+)*)*"
    r")\s*[:\.\-–-,]"
    r"|(?:^|\n)("
    # Role-based: "M. le président.", "M. le ministre de l'économie.", "M. le secrétaire d'État."
    # Allows optional qualifying text (e.g., "de l'économie et des finances") after the role word.
    r"M(?:me)?\.?\s+l[ea]\s+(?:président(?:e)?|ministre|secrétaire\s+d'[ÉEée]tat)"
    r"(?:\s+[a-zà-ÿA-ZÀ-Ý'',()\-\s]+?)?"  # optional qualifier like "de l'économie et des finances"
    r")\s*[\.\:–-]"
)

# Speaker intro pattern for function detection (the text between speaker name and first sentence)
_SPEAKER_INTRO_KEYWORDS = {
    "président": "president",
    "présidente": "president",
    "ministre": "ministre",
    "secrétaire d'état": "ministre",
    "secrétaire d'etat": "ministre",
    "rapporteur": "rapporteur",
    "rapporteure": "rapporteur",
}


def extract_text_from_pdf(pdf_path: Path) -> str:
    """Extract text from a PDF using PyMuPDF's pre-existing OCR layer.

    Args:
        pdf_path: Path to the PDF file.

    Returns:
        Full concatenated text from all pages.
    """
    import fitz

    doc = fitz.open(pdf_path)
    try:
        pages_text = []
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text()
            if len(text) < 100:
                logger.warning(
                    f"{pdf_path.name} page {page_num + 1}: only {len(text)} chars extracted"
                )
            pages_text.append(text)
        return "\n".join(pages_text)
    finally:
        doc.close()


def _allcaps_to_titlecase(match: re.Match) -> str:
    """Convert ALL-CAPS name to Title Case, preserving the title prefix and hyphens."""
    title = match.group(1)  # "M. ", "Mme ", etc.
    name_parts = match.group(2)  # "DUPONT", "LE PRÉSIDENT", "CHABAN-DELMAS"

    def capitalize_word(w: str) -> str:
        # Handle hyphenated words: CHABAN-DELMAS → Chaban-Delmas
        if "-" in w:
            return "-".join(part.capitalize() for part in w.split("-"))
        return w.capitalize()

    title_cased = " ".join(capitalize_word(w) for w in name_parts.split())
    return title + title_cased


def clean_archive_text(raw_text: str) -> str:
    """Clean OCR text from archive PDFs.

    - Remove JO headers/footers and page numbers
    - Fix common OCR spacing issues
    - Normalize ALL-CAPS speaker names to Title Case
    - Collapse excessive whitespace
    """
    text = raw_text

    # Remove form feeds / page breaks
    text = text.replace("\f", "\n")

    # Remove JO header lines
    text = _JO_HEADER_RE.sub("", text)

    # Fix common OCR spacing: "N ° " → "N° "
    text = re.sub(r"N\s+°\s*", "N° ", text)

    # Rejoin words split across lines by OCR hyphenation: "Chaban-\nDelmas" → "Chaban-Delmas"
    text = re.sub(r"-\n(\S)", r"-\1", text)

    # Rejoin speaker titles split across lines by OCR: "M.\nle" → "M. le", "M.\nDupont" → "M. Dupont"
    text = re.sub(r"(M(?:me|lle)?\.)\s*\n\s*", r"\1 ", text)

    # Normalize ALL-CAPS speaker names to Title Case
    text = _ALLCAPS_NAME_RE.sub(_allcaps_to_titlecase, text)

    # Collapse multiple blank lines to max 2
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Strip trailing spaces per line
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)

    # Strip leading/trailing whitespace
    text = text.strip()

    return text


def extract_date_from_text(text: str) -> datetime | None:
    """Extract session date from debate text.

    Looks for patterns like "Mercredi 29 Avril 1959".
    """
    match = _DATE_PATTERN.search(text[:3000])  # Only search first ~3000 chars
    if not match:
        return None

    day = int(match.group(1))
    month_name = match.group(2).lower()
    year = int(match.group(3))

    month = FRENCH_MONTHS.get(month_name)
    if not month:
        return None

    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def extract_date_from_session_name(session_name: str) -> datetime | None:
    """Fallback date extraction from session/URL pattern.

    For AN: "1988-1989-ordinaire1" → datetime(1988, 10, 1)
    For Sénat URLs containing sYYYYMMDD: extract date directly.
    """
    if not session_name:
        return None

    # Sénat pattern: s19881004 or just 19881004
    senat_match = re.search(r"s?(\d{4})(\d{2})(\d{2})", session_name)
    if senat_match:
        try:
            return datetime(
                int(senat_match.group(1)),
                int(senat_match.group(2)),
                int(senat_match.group(3)),
            )
        except ValueError:
            pass

    # AN pattern: 1988-1989-ordinaire1
    an_match = re.search(r"(\d{4})-(\d{4})", session_name)
    if an_match:
        year = int(an_match.group(1))
        try:
            return datetime(year, 10, 1)  # Parliamentary session starts in October
        except ValueError:
            pass

    return None


def detect_archive_speaker_function(speaker_intro: str, orateur: str) -> str:
    """Detect speaker function from the introduction line only.

    Unlike detect_speaker_function() which scans full content (causing false positives),
    this only examines the speaker name and the intro text describing their role.

    Vocative forms like "Monsieur le ministre, je voudrais..." are NOT role indicators -
    they mean the speaker is ADDRESSING the minister, not that they ARE the minister.

    Args:
        speaker_intro: The text between the speaker name and first sentence.
        orateur: The speaker's name.

    Returns:
        One of: president, ministre, rapporteur, autre.
    """
    orateur_lower = orateur.lower()

    # Check the speaker name itself for role indicators (e.g., "M. Le Président")
    for keyword, role in _SPEAKER_INTRO_KEYWORDS.items():
        if keyword in orateur_lower:
            return role

    # Check the intro for role description patterns (e.g., "ministre de l'économie")
    # But EXCLUDE vocative forms like "Monsieur le ministre" or "monsieur le président"
    intro_lower = speaker_intro.lower().strip()

    # Skip vocative forms: starts with "monsieur", "madame", or addressing someone
    vocative_prefixes = ("monsieur", "madame", "mesdames", "messieurs", "cher", "je ")
    if any(intro_lower.startswith(p) for p in vocative_prefixes):
        return "autre"

    for keyword, role in _SPEAKER_INTRO_KEYWORDS.items():
        if keyword in intro_lower:
            return role

    return "autre"


def parse_archive_debate(
    pdf_path: Path,
    chambre: str,
    url_source: str | None = None,
    session_name: str | None = None,
) -> ParsedDebat | None:
    """Parse an archive PDF into a structured debate.

    Args:
        pdf_path: Path to the PDF file.
        chambre: "AN" or "SENAT".
        url_source: Original URL of the PDF.
        session_name: Session name for fallback date extraction.

    Returns:
        ParsedDebat or None if parsing fails (e.g., no date found).
    """
    # 1. Extract text
    try:
        raw_text = extract_text_from_pdf(pdf_path)
    except Exception as e:
        logger.error(f"Failed to extract text from {pdf_path}: {e}")
        return None

    if not raw_text or len(raw_text) < 200:
        logger.warning(f"Very little text in {pdf_path}: {len(raw_text or '')} chars")
        return None

    # 2. Extract date BEFORE cleaning (headers contain the date)
    date = extract_date_from_text(raw_text)

    # 3. Clean text
    cleaned = clean_archive_text(raw_text)
    if date is None:
        date = extract_date_from_session_name(session_name)
    if date is None:
        logger.warning(f"No date found in {pdf_path.name}, skipping")
        return None

    # 4. Build reference key
    if chambre == "AN" and session_name:
        # archive_an:{leg}/{session}/{number}
        filename = pdf_path.stem  # e.g., "045"
        reference = f"archive_an:{session_name}/{filename}"
    elif chambre == "SENAT":
        # archive_senat:{YYYYMMDD}_{pages}
        filename = pdf_path.stem  # e.g., "s19881004_0001_0120"
        reference = f"archive_senat:{filename}"
    else:
        reference = f"archive_{chambre.lower()}:{pdf_path.stem}"

    # 5. Split by speakers
    # _SPEAKER_PATTERN has 2 capture groups (named speaker, role speaker).
    # re.split produces: preamble, grp1, grp2, content, grp1, grp2, content, ...
    # The speaker is whichever group is not None.
    raw_parts = _SPEAKER_PATTERN.split(cleaned)
    interventions = []

    for i in range(1, len(raw_parts) - 2, 3):
        orateur = (raw_parts[i] or raw_parts[i + 1] or "").strip()
        contenu = raw_parts[i + 2].strip() if i + 2 < len(raw_parts) else ""

        if not contenu or len(contenu) < 10:
            continue

        # Get the intro line (first ~80 chars) for function detection
        intro_line = contenu[:80]
        fonction = detect_archive_speaker_function(intro_line, orateur)
        articles = extract_article_mentions(contenu)

        interventions.append(Intervention(
            orateur=orateur,
            fonction=fonction,
            contenu=contenu,
            articles_mentionnes=articles,
            ordre=len(interventions) + 1,
        ))

    # 6. Return ParsedDebat
    return ParsedDebat(
        chambre=chambre,
        date=date,
        etape="seance_publique",
        url_source=url_source,
        reference=reference,
        interventions=interventions,
        contenu_integral="",  # Empty - archive PDFs are too large to store full text
    )
