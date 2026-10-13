"""Parser for AN HTML debate transcripts (legislatures 12-13, 2002-2012).

Handles 4 HTML formats: LEG13, SEMANTIC, FONT/B, STRONG.
Downloads are in data/raw/an_html/leg{N}/{session}/*.html.
"""

import logging
import re
from datetime import datetime
from enum import Enum
from pathlib import Path

from bs4 import BeautifulSoup, Comment, Tag

from src.parsers.debates import (
    Intervention,
    ParsedDebat,
    extract_article_mentions,
)

logger = logging.getLogger(__name__)

# French month names → number
FRENCH_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}

_DATE_PATTERN = re.compile(
    r"(?:(?:premiere|deuxieme|troisieme|seconde|1re|2e|3e)\s+)?s[ée]ance"
    r"\s+(?:unique\s+)?du\s+"
    r"(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+"
    r"(\d{1,2})(?:er)?\s+"
    r"(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|"
    r"septembre|octobre|novembre|décembre|decembre)\s+"
    r"(\d{4,5})",  # Allow 4-5 digits to catch OCR errors like "21003"
    re.IGNORECASE,
)

# Fallback date pattern for JO-style titles:
# "Journaux Officiels - 1re seance du mardi 1er octobre 2002"
_JO_DATE_PATTERN = re.compile(
    r"(\d{1,2})(?:er)?\s+"
    r"(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|"
    r"septembre|octobre|novembre|décembre|decembre)\s+"
    r"(\d{4,5})",  # Allow 4-5 digits to catch OCR errors like "21003"
    re.IGNORECASE,
)

# Date pattern without year (e.g. "séance du jeudi 9 décembre")
_DATE_NO_YEAR_PATTERN = re.compile(
    r"(\d{1,2})(?:er)?\s+"
    r"(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|"
    r"septembre|octobre|novembre|décembre|decembre)"
    r"(?!\s*\d)",  # negative lookahead: no year follows
    re.IGNORECASE,
)


class HtmlFormat(Enum):
    LEG13 = "LEG13"
    SEMANTIC = "SEMANTIC"
    FONT_B = "FONT_B"
    STRONG = "STRONG"


def detect_format(html_content: str) -> HtmlFormat:
    """Detect which HTML format a debate page uses.

    Detection order is strict — earlier rules take priority.
    """
    # Step 1: LEG13
    if "RSPEAK_START" in html_content:
        return HtmlFormat.LEG13
    # Fallback: meta LEGISLATURE_ID = "13"
    if 'content="13"' in html_content and "LEGISLATURE_ID" in html_content:
        return HtmlFormat.LEG13

    # Step 2: SEMANTIC
    if '<div id="englobe">' in html_content:
        has_h2_titre = ('class="titre1"' in html_content or 'class="titre2"' in html_content)
        has_h1_seance = 'class="seance"' in html_content
        if has_h2_titre and has_h1_seance:
            return HtmlFormat.SEMANTIC

    # Step 3: FONT/B
    if '<font face="Arial"' in html_content:
        return HtmlFormat.FONT_B

    # Step 4: STRONG (fallback)
    return HtmlFormat.STRONG


def _parse_french_date(day_str: str, month_str: str, year_str: str) -> datetime | None:
    """Parse French date components into datetime."""
    month = FRENCH_MONTHS.get(month_str.lower())
    if not month:
        return None
    try:
        year = int(year_str)
        # Fix common OCR/typo errors: 21003 -> 2003, 21004 -> 2004, etc.
        if year > 2100:
            year = year - 19000
        return datetime(year, month, int(day_str))
    except ValueError:
        return None


def _infer_year_from_path(html_path: Path | None, month: int) -> int | None:
    """Infer year from the session directory name in the file path.

    Session names like "2004-2005" or "2004-2005-extra":
    - Months sept-dec → first year (e.g. 2004)
    - Months jan-aug  → second year (e.g. 2005)
    - Extra sessions   → always second year
    """
    if not html_path:
        return None
    parts = html_path.parts
    for part in parts:
        m = re.match(r"(\d{4})-(\d{4})(?:-extra\d*)?$", part)
        if m:
            year1, year2 = int(m.group(1)), int(m.group(2))
            if "extra" in part:
                return year2
            return year1 if month >= 9 else year2
    return None


def extract_date_from_html(soup: BeautifulSoup, fmt: HtmlFormat,
                           html_path: Path | None = None) -> datetime | None:
    """Extract session date from HTML content."""
    # Try <h1 class="seance"> first (SEMANTIC / LEG13)
    h1 = soup.find("h1", class_="seance")
    if h1:
        text = h1.get_text()
        m = _DATE_PATTERN.search(text)
        if m:
            return _parse_french_date(m.group(1), m.group(2), m.group(3))

    # Try meta QUANTIEME_SEANCE (LEG13)
    meta = soup.find("meta", attrs={"name": "QUANTIEME_SEANCE"})
    if meta and meta.get("content"):
        m = _DATE_PATTERN.search(meta["content"])
        if m:
            return _parse_french_date(m.group(1), m.group(2), m.group(3))

    # Try <title> (STRONG / FONT/B)
    title_tag = soup.find("title")
    if title_tag:
        title_text = title_tag.get_text()
        m = _DATE_PATTERN.search(title_text)
        if m:
            return _parse_french_date(m.group(1), m.group(2), m.group(3))
        # JO-style fallback
        m = _JO_DATE_PATTERN.search(title_text)
        if m:
            return _parse_french_date(m.group(1), m.group(2), m.group(3))

        # Fallback: title has day+month but no year — infer from path
        m = _DATE_NO_YEAR_PATTERN.search(title_text)
        if m:
            month = FRENCH_MONTHS.get(m.group(2).lower())
            if month:
                year = _infer_year_from_path(html_path, month)
                if year:
                    return _parse_french_date(m.group(1), m.group(2), str(year))

    return None



def _extract_function_from_html(bold_tag: Tag | None, italic_tag: Tag | None,
                                speaker_name: str) -> str:
    """Extract speaker function from HTML DOM elements.

    Uses the <i> tag after the speaker's <b> tag for role detection.
    Falls back to checking the speaker name for role keywords.

    Does NOT scan full content (avoids vocative false positives).
    """
    if italic_tag:
        italic_text = italic_tag.get_text().lower().strip()
        # Only check actual role descriptions, not vocative forms
        if not italic_text.startswith(("monsieur", "madame", "mesdames", "messieurs")):
            if (
                "ministre" in italic_text
                or "secrétaire d'état" in italic_text
                or "secrétaire d'etat" in italic_text
                or "secrétaire d\u2019état" in italic_text
                or "secrétaire d\u2019etat" in italic_text
            ):
                return "ministre"
            if "rapporteur" in italic_text:
                return "rapporteur"
            if "président" in italic_text or "présidente" in italic_text:
                return "president"

    # Check speaker name for role keywords
    name_lower = speaker_name.lower()
    if "président" in name_lower or "présidente" in name_lower or "president" in name_lower:
        return "president"

    return "autre"


def _clean_speaker_name(raw_name: str) -> str:
    """Clean up a speaker name extracted from HTML."""
    name = raw_name.strip()
    # Remove trailing punctuation
    name = name.rstrip(".,;: ")
    # Normalize non-breaking spaces
    name = name.replace("\xa0", " ")
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def _get_text_after_speaker(p_tag: Tag, bold_tag: Tag, italic_tag: Tag | None) -> str:
    """Extract the intervention text after the speaker name (and optional function) in a <p>."""
    # Get full text of the <p>
    full_text = p_tag.get_text()
    # Get speaker part text
    speaker_text = bold_tag.get_text()
    if italic_tag:
        speaker_text += italic_tag.get_text()

    # Find where speaker part ends in the full text
    idx = full_text.find(speaker_text)
    if idx >= 0:
        content = full_text[idx + len(speaker_text):]
    else:
        # Fallback: try to strip the speaker name from the start
        speaker_name_text = bold_tag.get_text()
        name_idx = full_text.find(speaker_name_text)
        if name_idx >= 0:
            content = full_text[name_idx + len(speaker_name_text):]
        else:
            content = full_text

    # Clean leading punctuation and whitespace
    content = content.lstrip(".,;: \xa0")
    return content.strip()


def parse_leg13(soup: BeautifulSoup) -> ParsedDebat | None:
    """Parse a LEG13 format HTML debate."""
    # Find content between RSPEAK_START and RSPEAK_STOP
    rspeak_start = None
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        text = comment.strip()
        if "RSPEAK_START" in text:
            rspeak_start = comment
            break

    # Get the container — typically <div id="englobe">
    container = soup.find("div", id="englobe")
    if not rspeak_start and not container:
        return None
    if not container:
        container = rspeak_start.parent

    date = extract_date_from_html(soup, HtmlFormat.LEG13)
    if not date:
        return None

    interventions = []
    full_text_parts = []

    # Find all <p> elements that contain speaker interventions
    for p_tag in container.find_all("p"):
        bold = p_tag.find("b")
        if not bold:
            continue

        # Check if this is a speaker paragraph (has <b> with a name)
        bold_text = bold.get_text().strip()
        if not bold_text:
            continue

        # Skip presidency headers, section titles, didascalies
        parent_h5 = p_tag.find_parent("h5")
        if parent_h5:
            continue
        # Skip table-of-contents entries (e.g. <p class="somtitre">)
        if "somtitre" in (p_tag.get("class") or []):
            continue

        # Must look like a speaker name: starts with M./Mme/Mlle or contains "président"
        if not re.match(r"^(?:M(?:me|lle)?[\.\s]|Plusieurs|Un )", bold_text, re.IGNORECASE) and \
           "président" not in bold_text.lower() and "présidente" not in bold_text.lower():
            continue

        # Extract speaker name from the <b> tag
        # May contain an <a> link
        link = bold.find("a")
        if link and link.get_text().strip():
            speaker_name = _clean_speaker_name(link.get_text())
        else:
            speaker_name = _clean_speaker_name(bold_text)

        if not speaker_name:
            continue

        # Look for <i> tag after <b> for function
        italic = None
        next_sib = bold.next_sibling
        while next_sib:
            if isinstance(next_sib, Tag) and next_sib.name == "i":
                italic = next_sib
                break
            if isinstance(next_sib, Tag) and next_sib.name not in ("a", "b"):
                break
            next_sib = next_sib.next_sibling

        fonction = _extract_function_from_html(bold, italic, speaker_name)
        content = _get_text_after_speaker(p_tag, bold, italic)

        if not content:
            continue

        articles = extract_article_mentions(content)
        full_text_parts.append(f"{speaker_name}: {content}")

        interventions.append(Intervention(
            orateur=speaker_name,
            fonction=fonction,
            contenu=content,
            articles_mentionnes=articles,
            ordre=len(interventions) + 1,
        ))

    if not interventions:
        return None

    # Determine etape
    etape = "seance_publique"
    title_h2 = soup.find("h2", class_="entete")
    if title_h2 and "commission" in title_h2.get_text().lower():
        etape = "commission"

    return ParsedDebat(
        chambre="AN",
        date=date,
        etape=etape,
        url_source=None,
        reference=None,  # Set by parse_an_html_debate()
        interventions=interventions,
        contenu_integral="\n\n".join(full_text_parts),
    )


def parse_semantic(soup: BeautifulSoup) -> ParsedDebat | None:
    """Parse a SEMANTIC format HTML debate (legislature 12, 2005-2007)."""
    container = soup.find("div", id="englobe")
    if not container:
        return None

    date = extract_date_from_html(soup, HtmlFormat.SEMANTIC)
    if not date:
        return None

    interventions = []
    full_text_parts = []

    for p_tag in container.find_all("p"):
        bold = p_tag.find("b")
        if not bold:
            continue

        bold_text = bold.get_text().strip()
        if not bold_text:
            continue

        # Skip if not a speaker name pattern
        if not re.match(r"^(?:M(?:me|lle)?[\.\s]|Plusieurs|Un )", bold_text, re.IGNORECASE) and \
           "président" not in bold_text.lower() and "présidente" not in bold_text.lower():
            continue

        # Extract speaker name
        link = bold.find("a")
        if link and link.get_text().strip():
            speaker_name = _clean_speaker_name(link.get_text())
        else:
            speaker_name = _clean_speaker_name(bold_text)

        if not speaker_name:
            continue

        # Look for <i> function tag
        italic = None
        next_sib = bold.next_sibling
        while next_sib:
            if isinstance(next_sib, Tag) and next_sib.name == "i":
                italic = next_sib
                break
            if isinstance(next_sib, Tag) and next_sib.name not in ("a", "b"):
                break
            next_sib = next_sib.next_sibling

        fonction = _extract_function_from_html(bold, italic, speaker_name)
        content = _get_text_after_speaker(p_tag, bold, italic)

        if not content:
            continue

        articles = extract_article_mentions(content)
        full_text_parts.append(f"{speaker_name}: {content}")

        interventions.append(Intervention(
            orateur=speaker_name,
            fonction=fonction,
            contenu=content,
            articles_mentionnes=articles,
            ordre=len(interventions) + 1,
        ))

    if not interventions:
        return None

    etape = "seance_publique"

    return ParsedDebat(
        chambre="AN",
        date=date,
        etape=etape,
        url_source=None,
        reference=None,
        interventions=interventions,
        contenu_integral="\n\n".join(full_text_parts),
    )


def parse_font_b(soup: BeautifulSoup) -> ParsedDebat | None:
    """Parse a FONT/B format HTML debate (legislature 12, 2003-2005)."""
    date = extract_date_from_html(soup, HtmlFormat.FONT_B)

    interventions = []
    full_text_parts = []

    for p_tag in soup.find_all("p"):
        # FONT/B speakers are in <font face="Arial" size="2"><b>Name</b>. text</font>
        font = p_tag.find("font", attrs={"face": "Arial"})
        if not font:
            continue

        bold = font.find("b")
        if not bold:
            continue

        bold_text = bold.get_text().strip()
        # Normalize non-breaking spaces (\xa0 from &#160;)
        bold_text = bold_text.replace("\xa0", " ")

        if not bold_text:
            continue

        # Skip section headers (centered, all bold)
        align = p_tag.get("align", "").lower()
        if align == "center":
            continue

        # Skip didascalies: <p><i><font ...>(...)</font></i></p>
        if p_tag.find("i") and not bold:
            continue

        # Must look like a speaker name
        if not re.match(r"^(?:M(?:me|lle)?[\.\s]|Plusieurs|Un )", bold_text, re.IGNORECASE) and \
           "président" not in bold_text.lower() and "présidente" not in bold_text.lower():
            continue

        speaker_name = _clean_speaker_name(bold_text)
        if not speaker_name:
            continue

        # Look for <i> function tag
        italic = None
        next_sib = bold.next_sibling
        while next_sib:
            if isinstance(next_sib, Tag) and next_sib.name == "i":
                italic = next_sib
                break
            if isinstance(next_sib, Tag) and next_sib.name not in ("a", "b", "font"):
                break
            next_sib = next_sib.next_sibling

        fonction = _extract_function_from_html(bold, italic, speaker_name)

        # Get text content — everything in the <font> after the <b> (and optional <i>)
        font_text = font.get_text()
        speaker_part = bold.get_text()
        if italic:
            speaker_part += italic.get_text()
        idx = font_text.find(speaker_part)
        if idx >= 0:
            content = font_text[idx + len(speaker_part):]
        else:
            content = font_text
        content = content.lstrip(".,;: \xa0").strip()

        if not content:
            continue

        articles = extract_article_mentions(content)
        full_text_parts.append(f"{speaker_name}: {content}")

        interventions.append(Intervention(
            orateur=speaker_name,
            fonction=fonction,
            contenu=content,
            articles_mentionnes=articles,
            ordre=len(interventions) + 1,
        ))

    if not interventions:
        return None

    etape = "seance_publique"

    return ParsedDebat(
        chambre="AN",
        date=date,
        etape=etape,
        url_source=None,
        reference=None,
        interventions=interventions,
        contenu_integral="\n\n".join(full_text_parts),
    )


# Speaker pattern for STRONG format: <strong>&nbsp;&nbsp;&nbsp;&nbsp;M. Nom.</strong>
_STRONG_SPEAKER_RE = re.compile(
    r"^[\s\xa0]*(?:M(?:me|lle)?[\.\s].+?|.*?pr[ée]sident(?:e)?.*?)$",
    re.IGNORECASE,
)


def parse_strong(soup: BeautifulSoup) -> ParsedDebat | None:
    """Parse a STRONG format HTML debate (legislature 12, 2002-2004).

    This is the most complex parser due to:
    - Unclosed <strong> tags that bleed across <p> boundaries
    - Interventions separated by <br> within <p> rather than one <p> per intervention
    """
    date = extract_date_from_html(soup, HtmlFormat.STRONG)

    interventions = []
    full_text_parts = []
    current_speaker = None
    current_function = None
    current_content_parts = []

    def _flush_intervention():
        nonlocal current_speaker, current_function, current_content_parts
        if current_speaker and current_content_parts:
            content = " ".join(current_content_parts).strip()
            if content:
                articles = extract_article_mentions(content)
                full_text_parts.append(f"{current_speaker}: {content}")
                interventions.append(Intervention(
                    orateur=current_speaker,
                    fonction=current_function or "autre",
                    contenu=content,
                    articles_mentionnes=articles,
                    ordre=len(interventions) + 1,
                ))
        current_speaker = None
        current_function = None
        current_content_parts = []

    for p_tag in soup.find_all("p"):
        # Skip page anchor tables and centered section headers
        align = p_tag.get("align", "").upper()
        if align == "CENTER":
            # Check if this is a section header — flush current speaker
            text = p_tag.get_text().strip()
            if text and not text.isdigit():
                _flush_intervention()
            continue

        # Skip tables inside <p> (page number anchors)
        if p_tag.find("table"):
            continue

        # Process <strong> tags within this <p>
        # Walk through the children of the <p> to find <strong> speaker markers
        for child in p_tag.children:
            if isinstance(child, Tag) and child.name == "strong":
                strong_text = child.get_text().strip()
                strong_text = strong_text.replace("\xa0", " ").strip()

                if not strong_text:
                    continue

                # Check if this looks like a speaker name
                is_speaker = (
                    re.match(r"^M(?:me|lle)?[\.\s]", strong_text, re.IGNORECASE)
                    or "président" in strong_text.lower()
                    or "présidente" in strong_text.lower()
                )

                if is_speaker:
                    # New speaker — flush previous
                    _flush_intervention()
                    current_speaker = _clean_speaker_name(strong_text)

                    # Check for function in speaker name
                    name_lower = current_speaker.lower()
                    if (
                        "président" in name_lower
                        or "présidente" in name_lower
                        or "president" in name_lower
                    ):
                        current_function = "president"
                    else:
                        current_function = "autre"

                    # Check for <i> tag right after this <strong> for function
                    next_sib = child.next_sibling
                    while next_sib:
                        if isinstance(next_sib, Tag) and next_sib.name == "i":
                            italic_text = next_sib.get_text().lower().strip()
                            if not italic_text.startswith(("monsieur", "madame")):
                                if "ministre" in italic_text:
                                    current_function = "ministre"
                                elif "rapporteur" in italic_text:
                                    current_function = "rapporteur"
                                elif "président" in italic_text:
                                    current_function = "president"
                            break
                        if isinstance(next_sib, Tag) and next_sib.name not in ("a", "b"):
                            break
                        next_sib = next_sib.next_sibling
                else:
                    # Not a speaker name — treat as content if we have a speaker
                    if current_speaker:
                        current_content_parts.append(strong_text)

            elif isinstance(child, Tag) and child.name == "i":
                # Didascalie or italic text — skip didascalies starting with (
                text = child.get_text().strip()
                if text.startswith("("):
                    continue
                # Could be function info if right after speaker or content
                if current_speaker:
                    current_content_parts.append(text)

            elif isinstance(child, Tag) and child.name == "br":
                # Line break — might separate interventions but we handle via <strong> detection
                continue

            elif isinstance(child, Tag):
                # Other tags (table, font, etc.)
                text = child.get_text().strip()
                if text and current_speaker:
                    current_content_parts.append(text)

            else:
                # NavigableString (text node)
                text = str(child).strip()
                if text and current_speaker:
                    current_content_parts.append(text)

    # Flush last intervention
    _flush_intervention()

    if not interventions:
        return None

    etape = "seance_publique"

    return ParsedDebat(
        chambre="AN",
        date=date,
        etape=etape,
        url_source=None,
        reference=None,
        interventions=interventions,
        contenu_integral="\n\n".join(full_text_parts),
    )


def _detect_encoding(file_path: Path) -> str:
    """Detect the encoding of an HTML file.

    Priority:
    1. UTF-8 BOM
    2. <meta charset="utf-8"> or <meta http-equiv="Content-Type" content="...charset=utf-8">
    3. Default: iso-8859-1 (always succeeds)
    """
    with open(file_path, "rb") as f:
        head = f.read(1024)

    # BOM check
    if head[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig"

    # Meta charset check (case-insensitive)
    head_lower = head.lower()
    if b'charset="utf-8"' in head_lower or b"charset=utf-8" in head_lower:
        return "utf-8"

    return "iso-8859-1"


def _reconstruct_url(file_path: Path) -> str | None:
    """Reconstruct the original URL from the file path.

    data/raw/an_html/leg{leg}/{session}/{filename}.html
    → https://www.assemblee-nationale.fr/{leg}/cri/{session}/{filename}.asp

    Commission élargie:
    data/raw/an_html/leg13/commission_elargie/plf{year}/{filename}.html
    → https://www.assemblee-nationale.fr/13/budget/plf{year}/commissions_elargies/cr/{filename}.asp
    """
    parts = file_path.parts
    try:
        # Find "an_html" in the path
        idx = list(parts).index("an_html")
    except ValueError:
        return None

    remaining = parts[idx + 1:]  # e.g., ("leg13", "2011-2012", "20120140.html")

    if len(remaining) < 3:
        return None

    leg_dir = remaining[0]  # "leg13"
    leg = leg_dir.replace("leg", "")

    filename = Path(remaining[-1]).stem  # "20120140"

    # Commission élargie path
    if "commission_elargie" in remaining:
        # remaining: ("leg13", "commission_elargie", "plf2010", "filename.html")
        plf_dir = remaining[2]  # "plf2010"
        year = plf_dir.replace("plf", "")
        return f"https://www.assemblee-nationale.fr/13/budget/plf{year}/commissions_elargies/cr/{filename}.asp"

    session = remaining[1]  # "2011-2012"
    return f"https://www.assemblee-nationale.fr/{leg}/cri/{session}/{filename}.asp"


def _build_reference_key(file_path: Path) -> str | None:
    """Build reference key from file path.

    Format: archive_an_html:leg{legislature}:{session}:{filename}
    """
    parts = file_path.parts
    try:
        idx = list(parts).index("an_html")
    except ValueError:
        return None

    remaining = parts[idx + 1:]

    if len(remaining) < 3:
        return None

    leg_dir = remaining[0]  # "leg13"
    leg = leg_dir.replace("leg", "")
    filename = Path(remaining[-1]).stem

    # Commission élargie
    if "commission_elargie" in remaining:
        plf_dir = remaining[2]  # "plf2010"
        return f"archive_an_html:leg{leg}:commission_elargie/{plf_dir}:{filename}"

    session = remaining[1]
    return f"archive_an_html:leg{leg}:{session}:{filename}"


def parse_an_html_debate(html_path: Path) -> ParsedDebat | None:
    """Parse an AN HTML debate file into a ParsedDebat.

    Detects encoding and format, dispatches to the appropriate parser.
    Returns None for non-debate pages or files with < 2 interventions.
    """
    if not html_path.exists():
        logger.warning(f"File not found: {html_path}")
        return None

    # Detect encoding and read file
    encoding = _detect_encoding(html_path)
    with open(html_path, encoding=encoding) as f:
        html_content = f.read()

    if not html_content or len(html_content) < 100:
        return None

    soup = BeautifulSoup(html_content, "html.parser")
    fmt = detect_format(html_content)
    logger.debug(f"{html_path.name}: detected format {fmt.value}")

    # Check for commission élargie path → override etape
    is_commission = "commission_elargie" in str(html_path)

    # Dispatch to format parser
    if fmt == HtmlFormat.LEG13:
        result = parse_leg13(soup)
    elif fmt == HtmlFormat.SEMANTIC:
        result = parse_semantic(soup)
    elif fmt == HtmlFormat.FONT_B:
        result = parse_font_b(soup)
    elif fmt == HtmlFormat.STRONG:
        result = parse_strong(soup)
    else:
        return None

    if result is None:
        return None

    # Recover missing date from path (some leg12 titles lack the year)
    if result.date is None:
        result.date = extract_date_from_html(soup, fmt, html_path=html_path)
    if result.date is None:
        logger.warning(f"No date found in {html_path.name}, skipping")
        return None

    # Minimum intervention threshold
    if len(result.interventions) < 2:
        logger.warning(
            f"Only {len(result.interventions)} interventions "
            f"in {html_path.name}, skipping"
        )
        return None

    # Set reference key and URL
    result.reference = _build_reference_key(html_path)
    result.url_source = _reconstruct_url(html_path)

    # Override etape for commission élargie
    if is_commission:
        result.etape = "commission"

    return result
