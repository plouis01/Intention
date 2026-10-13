"""Parser for parliamentary debate transcripts."""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from defusedxml import ElementTree as ET

logger = logging.getLogger(__name__)


def _extract_date_from_reference(reference: str | None = None, path: Path | None = None) -> datetime | None:
    """Try to extract a date from a debate reference string or file path.

    AN references like "CRAN20191115" or filenames like "20191115_seance.xml"
    contain embedded dates in YYYYMMDD format.
    """
    candidates = []
    if reference:
        candidates.append(str(reference))
    if path:
        candidates.append(path.stem)
        candidates.append(str(path.name))

    date_pattern = re.compile(r'(\d{4})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])')
    for candidate in candidates:
        match = date_pattern.search(candidate)
        if match:
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
            if 2000 <= year <= 2030:
                try:
                    return datetime(year, month, day)
                except ValueError:
                    continue
    return None


@dataclass
class Intervention:
    """A single speaker intervention in a debate."""

    orateur: str
    fonction: str | None
    contenu: str
    articles_mentionnes: list[str]
    ordre: int
    timestamp: datetime | None = None


@dataclass
class ParsedDebat:
    """A parsed debate session."""

    chambre: str  # AN or SENAT
    date: datetime
    etape: str  # commission or seance_publique
    url_source: str | None
    reference: str | None
    interventions: list[Intervention]
    contenu_integral: str


def detect_speaker_function(text: str, speaker: str) -> str:
    """
    Detect the function/role of a speaker from context.

    Args:
        text: The text surrounding the speaker mention
        speaker: The speaker's name

    Returns:
        One of: rapporteur, ministre, depute, senateur, president, autre
    """
    text_lower = text.lower()

    if "rapporteur" in text_lower:
        return "rapporteur"
    if "ministre" in text_lower or "secrétaire d'état" in text_lower:
        return "ministre"
    if "président" in text_lower or "présidente" in text_lower:
        return "president"
    if "sénat" in text_lower or "sénateur" in text_lower or "sénatrice" in text_lower:
        return "senateur"
    if "député" in text_lower or "députée" in text_lower:
        return "depute"

    return "autre"


def extract_article_mentions(text: str) -> list[str]:
    """
    Extract article numbers mentioned in text.

    Handles French legislative article references including:
    - Simple: article 12, article 1er
    - Latin suffixes: article 1 bis, article 3 quinquies
    - CGI compound: article 150-0 B ter, article 199 terdecies-0 A
    - Special: article premier, article liminaire
    - Ranges: articles 1 à 12, articles 3 et 5
    - Contextual: avant l'article 21, après l'article 3

    Args:
        text: Text to search

    Returns:
        List of article numbers found (lowercase, sorted)
    """
    LATIN_SUFFIXES = (
        r"bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies"
        r"|undecies|duodecies|terdecies|quaterdecies|quindecies|sexdecies"
    )

    # Article suffix handles both orderings:
    #   150-0 B ter  (number → dash-number → letter → latin)
    #   199 terdecies-0 A  (number → latin → dash-number → letter)
    LATIN_PART = rf"(?:{LATIN_SUFFIXES})"
    ARTICLE_SUFFIX = (
        rf"(?:"
        rf"(?:-\d+)?\s*[a-z]{{0,3}}\s*{LATIN_PART}"  # -0 B ter
        rf"(?:-\d+\s*[a-z]{{0,3}})?"                  # optional trailing -0 A
        rf")?"
    )

    # Full article number pattern
    ARTICLE_NUM = rf"\d+{ARTICLE_SUFFIX}"

    mentions = set()
    text_lower = text.lower()

    # 1. "article premier" → "1"
    for _ in re.finditer(r"articles?\s+premier\b", text_lower):
        mentions.add("1")

    # 2. "article 1er" → "1"
    for _ in re.finditer(r"articles?\s+(\d+)\s*er\b", text_lower):
        mentions.add("1")

    # 3. "article liminaire"
    for _ in re.finditer(r"articles?\s+liminaire\b", text_lower):
        mentions.add("liminaire")

    # 4. "articles 1 à 12" / "articles 1 et 2" (range/pair - must come before single)
    range_pattern = rf"articles?\s+({ARTICLE_NUM})\s+(?:à|et)\s+({ARTICLE_NUM})"
    for match in re.finditer(range_pattern, text_lower):
        for g in match.groups():
            if g:
                mentions.add(re.sub(r"\s+", " ", g.strip()))

    # 5. Single article: "l'article 12 bis A", "article 150-0 B ter",
    #    "avant l'article 21", "après l'article 3"
    single_pattern = rf"(?:avant\s+|après\s+|apres\s+)?(?:l['']\s*)?articles?\s+({ARTICLE_NUM})"
    for match in re.finditer(single_pattern, text_lower):
        val = match.group(1)
        if val:
            cleaned = re.sub(r"\s+", " ", val.strip())
            # Skip if it's just "premier" or "liminaire" (already handled)
            if cleaned not in ("premier", "liminaire"):
                mentions.add(cleaned)

    return sorted(mentions)


def parse_an_compte_rendu_xml(xml_path: Path) -> ParsedDebat | None:
    """
    Parse an Assemblée Nationale compte rendu XML file.

    Args:
        xml_path: Path to the XML file

    Returns:
        ParsedDebat or None if parsing fails
    """
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        # Extract metadata
        date_str = root.findtext(".//dateSeance") or root.findtext(".//date")
        if date_str:
            try:
                date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except ValueError:
                date = _extract_date_from_reference(path=xml_path) or datetime(1970, 1, 1)
                if date.year == 1970:
                    logger.warning(f"Could not parse date from {xml_path}, using sentinel 1970-01-01")
        else:
            date = _extract_date_from_reference(path=xml_path) or datetime(1970, 1, 1)
            if date.year == 1970:
                logger.warning(f"No date found in {xml_path}, using sentinel 1970-01-01")

        # Determine etape
        etape = "seance_publique"
        titre = root.findtext(".//titre", "").lower()
        if "commission" in titre:
            etape = "commission"

        # Extract interventions
        interventions = []
        full_text_parts = []

        # Look for intervention elements
        for idx, interv in enumerate(root.iter("intervention")):
            orateur_elem = interv.find("orateur") or interv.find("intervenant")
            if orateur_elem is not None:
                orateur = orateur_elem.findtext("nom") or orateur_elem.text or "Inconnu"
            else:
                orateur = "Inconnu"

            # Get intervention text
            texte_elem = interv.find("texte") or interv.find("contenu")
            if texte_elem is not None:
                contenu = "".join(texte_elem.itertext())
            else:
                contenu = "".join(interv.itertext())

            contenu = contenu.strip()
            if not contenu:
                continue

            full_text_parts.append(f"{orateur}: {contenu}")

            fonction = detect_speaker_function(contenu, orateur)
            articles = extract_article_mentions(contenu)

            interventions.append(Intervention(
                orateur=orateur,
                fonction=fonction,
                contenu=contenu,
                articles_mentionnes=articles,
                ordre=idx + 1,
            ))

        if not interventions:
            # Fallback: try to parse as plain text structure
            all_text = "".join(root.itertext())
            return _parse_plain_text_debate(all_text, "AN", date, etape)

        return ParsedDebat(
            chambre="AN",
            date=date,
            etape=etape,
            url_source=None,
            reference=xml_path.stem,
            interventions=interventions,
            contenu_integral="\n\n".join(full_text_parts),
        )

    except ET.ParseError as e:
        logger.warning(f"XML parse error for {xml_path}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error parsing {xml_path}: {e}")
        return None


def parse_an_compte_rendu_json(json_path: Path) -> ParsedDebat | None:
    """
    Parse an Assemblée Nationale compte rendu JSON file.

    Args:
        json_path: Path to the JSON file

    Returns:
        ParsedDebat or None if parsing fails
    """
    try:
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)

        cr = data.get("compteRendu", data)

        # Extract date
        date_str = cr.get("dateSeance") or cr.get("date")
        if date_str:
            try:
                date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except ValueError:
                date = _extract_date_from_reference(path=json_path) or datetime(1970, 1, 1)
                if date.year == 1970:
                    logger.warning(f"Could not parse date from {json_path}, using sentinel 1970-01-01")
        else:
            date = _extract_date_from_reference(path=json_path) or datetime(1970, 1, 1)
            if date.year == 1970:
                logger.warning(f"No date found in {json_path}, using sentinel 1970-01-01")

        # Determine etape
        etape = "seance_publique"
        if cr.get("typeSeance") == "COMMISSION":
            etape = "commission"

        # Extract interventions
        interventions = []
        full_text_parts = []

        intervs = cr.get("interventions", cr.get("pointsODJ", []))
        if isinstance(intervs, dict):
            intervs = intervs.get("intervention", [])

        for idx, interv in enumerate(intervs):
            if isinstance(interv, str):
                continue

            orateur = interv.get("orateur", {})
            if isinstance(orateur, dict):
                orateur_nom = orateur.get("nom", orateur.get("acteurRef", "Inconnu"))
            else:
                orateur_nom = str(orateur) if orateur else "Inconnu"

            contenu = interv.get("texte", interv.get("contenu", ""))
            if isinstance(contenu, dict):
                contenu = contenu.get("texte", "")
            contenu = str(contenu).strip()

            if not contenu:
                continue

            full_text_parts.append(f"{orateur_nom}: {contenu}")

            fonction = detect_speaker_function(contenu, orateur_nom)
            articles = extract_article_mentions(contenu)

            interventions.append(Intervention(
                orateur=orateur_nom,
                fonction=fonction,
                contenu=contenu,
                articles_mentionnes=articles,
                ordre=idx + 1,
            ))

        return ParsedDebat(
            chambre="AN",
            date=date,
            etape=etape,
            url_source=cr.get("url"),
            reference=cr.get("uid", json_path.stem),
            interventions=interventions,
            contenu_integral="\n\n".join(full_text_parts),
        )

    except Exception as e:
        logger.error(f"Error parsing {json_path}: {e}")
        return None


def _parse_plain_text_debate(
    text: str, chambre: str, date: datetime, etape: str
) -> ParsedDebat:
    """
    Parse a debate from plain text (fallback method).

    Uses regex to split by speaker patterns like "M. Dupont :" or "Mme Martin :"
    """
    # Pattern to match speaker introductions
    speaker_pattern = r"(?:^|\n)(M(?:me|lle)?\.?\s+[A-ZÀ-Ý][a-zà-ÿ]+(?:\s+[A-ZÀ-Ý][a-zà-ÿ]+)*)\s*[:\.\-–]"

    parts = re.split(speaker_pattern, text)
    interventions = []

    # parts[0] is before first speaker, then alternates: speaker, content, speaker, content...
    for i in range(1, len(parts) - 1, 2):
        orateur = parts[i].strip()
        contenu = parts[i + 1].strip() if i + 1 < len(parts) else ""

        if not contenu:
            continue

        fonction = detect_speaker_function(contenu, orateur)
        articles = extract_article_mentions(contenu)

        interventions.append(Intervention(
            orateur=orateur,
            fonction=fonction,
            contenu=contenu,
            articles_mentionnes=articles,
            ordre=len(interventions) + 1,
        ))

    return ParsedDebat(
        chambre=chambre,
        date=date,
        etape=etape,
        url_source=None,
        reference=None,
        interventions=interventions,
        contenu_integral=text,
    )


def parse_senat_compte_rendu_xml(xml_path: Path) -> ParsedDebat | None:
    """
    Parse a Sénat compte rendu XML file (Akoma Ntoso format).

    Args:
        xml_path: Path to the XML file

    Returns:
        ParsedDebat or None if parsing fails
    """
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        # Akoma Ntoso namespace
        ns = {"akn": "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"}

        # Try to extract date
        date_elem = root.find(".//akn:FRBRdate", ns) or root.find(".//{*}FRBRdate")
        if date_elem is not None:
            date_str = date_elem.get("date", "")
            try:
                date = datetime.fromisoformat(date_str)
            except ValueError:
                date = _extract_date_from_reference(path=xml_path) or datetime(1970, 1, 1)
                if date.year == 1970:
                    logger.warning(f"Could not parse date from {xml_path}, using sentinel 1970-01-01")
        else:
            date = _extract_date_from_reference(path=xml_path) or datetime(1970, 1, 1)
            if date.year == 1970:
                logger.warning(f"No date found in {xml_path}, using sentinel 1970-01-01")

        # Extract speeches
        interventions = []
        full_text_parts = []

        speeches = root.findall(".//akn:speech", ns) or root.findall(".//{*}speech")

        for idx, speech in enumerate(speeches):
            # Get speaker
            by_attr = speech.get("by", "")
            orateur = by_attr.replace("#", "") if by_attr else "Inconnu"

            # Get content
            contenu = "".join(speech.itertext()).strip()
            if not contenu:
                continue

            full_text_parts.append(f"{orateur}: {contenu}")

            fonction = detect_speaker_function(contenu, orateur)
            articles = extract_article_mentions(contenu)

            interventions.append(Intervention(
                orateur=orateur,
                fonction=fonction,
                contenu=contenu,
                articles_mentionnes=articles,
                ordre=idx + 1,
            ))

        if not interventions:
            # Fallback to plain text
            all_text = "".join(root.itertext())
            return _parse_plain_text_debate(all_text, "SENAT", date, "seance_publique")

        return ParsedDebat(
            chambre="SENAT",
            date=date,
            etape="seance_publique",
            url_source=None,
            reference=xml_path.stem,
            interventions=interventions,
            contenu_integral="\n\n".join(full_text_parts),
        )

    except ET.ParseError as e:
        logger.warning(f"XML parse error for {xml_path}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error parsing {xml_path}: {e}")
        return None


def parse_debate_file(file_path: Path) -> ParsedDebat | None:
    """
    Parse a debate file, auto-detecting format and chamber.

    Args:
        file_path: Path to the debate file

    Returns:
        ParsedDebat or None if parsing fails
    """
    suffix = file_path.suffix.lower()

    # Determine chamber from path
    path_str = str(file_path).lower()
    if "senat" in path_str:
        if suffix == ".xml":
            return parse_senat_compte_rendu_xml(file_path)
    else:  # Assume AN
        if suffix == ".xml":
            return parse_an_compte_rendu_xml(file_path)
        elif suffix == ".json":
            return parse_an_compte_rendu_json(file_path)

    logger.warning(f"Unknown format for {file_path}")
    return None
