"""Parser for parliamentary amendments."""

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Patterns indicating a function/role after a comma (not a co-author)
_FUNCTION_STARTS = (
    "rapporteur", "au nom", "pour avis", "ministre", "secrétaire",
    "président", "commissaire", "garde", "membre de", "à titre",
)

# Title prefixes that indicate a co-author
_TITLE_RE = re.compile(r"^(?:M\.|Mme|MM\.|Mlle|Mmes)\s", re.IGNORECASE)


def normalize_amendment_author(raw: str | None) -> str | None:
    """Normalize amendment author to first author only.

    Handles:
    - Multi-author comma lists: "M. Dupont, M. Martin, ..." → "M. Dupont"
    - Name + function: "M. Hamel, rapporteur au nom de..." → "M. Hamel, rapporteur au nom de..."
    - Gouvernement variants: "le Gouvernement" → "Gouvernement"
    - MM. plural: "MM. Dupont, Martin, ..." → "M. Dupont"
    - Mixed: "M. Jacob, rapporteur ... et M. Poignant" → "M. Jacob, rapporteur ..."
    - "et les membres du groupe ..." → stripped
    """
    if not raw or not isinstance(raw, str):
        return None

    raw = raw.strip()
    # Strip leading commas/punctuation (dirty data: ", M. Demilly")
    raw = raw.lstrip(",;. ").strip()
    if not raw:
        return None

    # Normalize Gouvernement
    if re.match(r"^(?:le\s+)?gouvernement\b", raw, re.IGNORECASE):
        return "Gouvernement"

    # Strip trailing junk like " --------"
    raw = re.sub(r"\s*-{3,}\s*$", "", raw)

    # Handle "avec l'accord du Gouvernement" suffix
    raw = re.sub(r"\s+avec l'accord du Gouvernement\s*$", "", raw, flags=re.IGNORECASE)

    # Handle MM. plural prefix → extract first author name
    mm_match = re.match(r"^MM\.\s+(.+)", raw)
    if mm_match:
        rest = mm_match.group(1)
        # First name is before the first comma
        first_name = rest.split(",")[0].strip()
        return f"M. {first_name}"

    # Handle Mmes plural prefix
    mmes_match = re.match(r"^Mmes\s+(.+)", raw)
    if mmes_match:
        rest = mmes_match.group(1)
        first_name = rest.split(",")[0].strip()
        return f"Mme {first_name}"

    # Strip "et les membres du groupe ..." / "et les commissaires ..." suffix
    raw = re.sub(r"\s+et les (?:membres|commissaires)\b.*$", "", raw, flags=re.IGNORECASE)

    # Split on comma
    parts = raw.split(",")
    if len(parts) <= 1:
        # No comma - check for "et M./Mme" co-author
        et_match = re.search(r"\s+et\s+(?:M\.|Mme|MM\.)\s", raw)
        if et_match:
            return raw[: et_match.start()].strip()
        return raw.strip()

    # Check if second segment is a function (rapporteur, au nom de, etc.)
    # or a co-author (starts with M., Mme, etc.)
    second = parts[1].strip()
    second_lower = second.lower()

    is_function = any(second_lower.startswith(f) for f in _FUNCTION_STARTS)
    is_coauthor = bool(_TITLE_RE.match(second))

    if is_function and not is_coauthor:
        # "M. Hamel, rapporteur au nom de la commission des affaires économiques et M. Poignant"
        # → keep name + function, strip co-authors after function
        name_and_func = ",".join(parts[:2]).strip()
        # Remove trailing "et M./Mme..." co-author after function
        et_match = re.search(r"\s+et\s+(?:M\.|Mme|MM\.)\s", name_and_func)
        if et_match:
            name_and_func = name_and_func[: et_match.start()].strip()
        # Remove bare co-author title in the function part (without "et")
        # e.g. "saisie pour avis M. Muzeau" or "économiques MM. Charié"
        if "," not in name_and_func:
            return name_and_func
        comma_pos = name_and_func.index(",")
        func_part = name_and_func[comma_pos + 1:]
        bare_title = re.search(r"\s+(?:M\.|Mme|MM\.|Mmes|Mlle)\s+[A-ZÀ-Ú]", func_part)
        if bare_title:
            name_and_func = (name_and_func[: comma_pos + 1] + func_part[: bare_title.start()]).strip()
        return name_and_func
    elif is_coauthor:
        # "M. Dupont, M. Martin, Mme Durand" → first author only
        return parts[0].strip()
    else:
        # Ambiguous - keep first segment
        return parts[0].strip()


@dataclass
class ParsedAmendement:
    """A parsed amendment."""

    numero: str
    auteur: str | None
    objet: str | None
    contenu: str | None
    expose_motifs: str | None
    sort: str | None  # adopte, rejete, retire, tombe, non_soutenu
    article_ref: str | None  # Article number this amendment targets
    date_depot: datetime | None
    chambre: str  # AN or SENAT
    dossier_ref: str | None
    reference_externe: str | None


# Mapping of sort values to normalized form
SORT_MAPPING = {
    # French
    "adopté": "adopte",
    "adoptée": "adopte",
    "adopte": "adopte",
    "rejeté": "rejete",
    "rejetée": "rejete",
    "rejete": "rejete",
    "retiré": "retire",
    "retirée": "retire",
    "retire": "retire",
    "tombé": "tombe",
    "tombée": "tombe",
    "tombe": "tombe",
    "non soutenu": "non_soutenu",
    "non-soutenu": "non_soutenu",
    "non_soutenu": "non_soutenu",
    # English (in case data has English values)
    "adopted": "adopte",
    "rejected": "rejete",
    "withdrawn": "retire",
    "lapsed": "tombe",
}


def normalize_sort(sort_value: str | None) -> str | None:
    """
    Normalize amendment sort (outcome) value.

    Args:
        sort_value: Raw sort value

    Returns:
        Normalized sort value or None
    """
    if not sort_value:
        return None

    sort_lower = sort_value.lower().strip()
    return SORT_MAPPING.get(sort_lower, sort_lower)


def extract_article_from_text(text: str) -> str | None:
    """
    Extract article reference from amendment text.

    Args:
        text: Text to search

    Returns:
        Article number or None
    """
    # Common patterns for article references in amendments
    patterns = [
        r"(?:à l')?article\s+(\d+(?:\s*(?:bis|ter|quater|quinquies))?(?:\s*[A-Z])?)",
        r"art(?:icle)?\.?\s*(\d+(?:\s*(?:bis|ter|quater|quinquies))?(?:\s*[A-Z])?)",
        r"après l'article\s+(\d+)",
        r"avant l'article\s+(\d+)",
    ]

    text_lower = text.lower()

    for pattern in patterns:
        match = re.search(pattern, text_lower, re.IGNORECASE)
        if match:
            return match.group(1).strip()

    return None


def parse_an_amendement(data: dict[str, Any]) -> ParsedAmendement:
    """
    Parse an amendment from Assemblée Nationale JSON format.

    Args:
        data: Raw amendment data

    Returns:
        ParsedAmendement
    """
    amend = data.get("amendement", data)

    # Extract sort (outcome) - lives inside cycleDeVie in modern AN JSON
    cycle = amend.get("cycleDeVie", {}) or {}
    sort_data = cycle.get("sort", amend.get("sort", {}))
    if isinstance(sort_data, dict):
        sort_raw = sort_data.get("libelle", sort_data.get("sortEnSeance"))
    else:
        sort_raw = str(sort_data) if sort_data else None
    # Fallback: etatDesTraitements.sousEtat.libelle
    if not sort_raw or (isinstance(sort_data, dict) and "@xsi:nil" in sort_data):
        traitements = cycle.get("etatDesTraitements", {}) or {}
        sous_etat = traitements.get("sousEtat", {}) or {}
        if isinstance(sous_etat, dict):
            sort_raw = sous_etat.get("libelle") or sort_raw

    sort = normalize_sort(sort_raw)

    # Extract author
    signataires = amend.get("signataires", {})
    auteur = None
    if isinstance(signataires, dict):
        auteur_data = signataires.get("auteur", {})
        if isinstance(auteur_data, dict):
            auteur = auteur_data.get("nom") or auteur_data.get("acteurRef")
            # Also try libelle as fallback
            if not auteur:
                auteur = signataires.get("libelle")
        else:
            auteur = str(auteur_data) if auteur_data else None
    else:
        auteur = str(signataires) if signataires else None
    # Ensure auteur is a string or None (handle nil dicts)
    if auteur is not None and not isinstance(auteur, str):
        auteur = None
    auteur = normalize_amendment_author(auteur)

    # Extract content
    corps = amend.get("corps", {})
    if isinstance(corps, dict):
        contenu_auteur = corps.get("contenuAuteur", {})
        if isinstance(contenu_auteur, dict):
            objet = contenu_auteur.get("exposeSommaire") or corps.get("exposeSommaire")
            contenu = contenu_auteur.get("dispositif") or corps.get("dispositif")
        else:
            objet = corps.get("exposeSommaire")
            contenu = corps.get("dispositif")
    else:
        objet = None
        contenu = None

    # Decode HTML entities (&#233; → é, etc.)
    if objet:
        objet = html.unescape(objet)
    if contenu:
        contenu = html.unescape(contenu)

    # Extract article reference
    pointeur = amend.get("pointeurFragmentTexte", {})
    if isinstance(pointeur, dict):
        division = pointeur.get("division", pointeur.get("divisionArticle", {}))
        if isinstance(division, dict):
            article_ref = division.get("articleDesignation") or division.get("numeroArticle")
        else:
            article_ref = None
    else:
        article_ref = None

    # If no article ref found, try to extract from objet or contenu
    if not article_ref and objet:
        article_ref = extract_article_from_text(objet)
    if not article_ref and contenu:
        article_ref = extract_article_from_text(contenu)

    # Parse date - lives inside cycleDeVie in modern AN JSON
    date_str = cycle.get("dateDepot") or amend.get("dateDepot")
    if isinstance(date_str, dict):
        date_str = None  # nil placeholder
    if date_str:
        try:
            date_depot = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            date_depot = None
    else:
        date_depot = None

    return ParsedAmendement(
        numero=amend.get("numeroLong") or amend.get("numero") or (amend.get("identification", {}) or {}).get("numeroLong") or "?",
        auteur=auteur,
        objet=objet,
        contenu=contenu,
        expose_motifs=objet,  # AN uses exposeSommaire for both
        sort=sort,
        article_ref=article_ref,
        date_depot=date_depot,
        chambre="AN",
        dossier_ref=amend.get("dossierRef") or amend.get("texteLegislatifRef"),
        reference_externe=amend.get("uid"),
    )


def parse_senat_amendement(data: dict[str, Any]) -> ParsedAmendement:
    """
    Parse an amendment from Sénat format.

    Args:
        data: Raw amendment data

    Returns:
        ParsedAmendement
    """
    # Handle different possible field names
    sort_raw = data.get("sort") or data.get("etat") or data.get("sortAmendement")
    sort = normalize_sort(sort_raw)

    # Extract article reference
    article_ref = (
        data.get("article")
        or data.get("subdivision")
        or data.get("divisionTexte")
    )

    # If article_ref is a dict, extract the number
    if isinstance(article_ref, dict):
        article_ref = article_ref.get("numero") or article_ref.get("libelle")

    # Try to extract from objet if not found
    objet = data.get("objet") or data.get("expose") or data.get("objetAmendement")
    if not article_ref and objet:
        article_ref = extract_article_from_text(str(objet))

    # Parse date
    date_str = data.get("dateDepot") or data.get("date_depot") or data.get("datePublication")
    if date_str:
        try:
            if isinstance(date_str, str):
                date_depot = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            else:
                date_depot = None
        except ValueError:
            date_depot = None
    else:
        date_depot = None

    return ParsedAmendement(
        numero=data.get("numero") or data.get("num") or data.get("numeroAmendement") or "?",
        auteur=normalize_amendment_author(data.get("auteur") or data.get("signataire") or data.get("auteurAmendement")),
        objet=objet,
        contenu=data.get("dispositif") or data.get("contenu") or data.get("texteAmendement"),
        expose_motifs=data.get("expose") or data.get("motifs") or objet,
        sort=sort,
        article_ref=article_ref,
        date_depot=date_depot,
        chambre="SENAT",
        dossier_ref=data.get("dossier") or data.get("textRef") or data.get("texteLegislatifRef"),
        reference_externe=data.get("id") or data.get("uid") or data.get("identifiant"),
    )


def parse_amendment_file(file_path: Path) -> list[ParsedAmendement]:
    """
    Parse amendments from a file, auto-detecting format.

    Args:
        file_path: Path to the amendment file (JSON or CSV)

    Returns:
        List of ParsedAmendement
    """
    suffix = file_path.suffix.lower()
    path_str = str(file_path).lower()

    # Determine chamber from path
    is_senat = "senat" in path_str

    amendments = []

    if suffix == ".json":
        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)

            # Handle different structures
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                # Could be a single amendment or a container
                if "amendement" in data:
                    items = [data]
                elif "amendements" in data:
                    items = data["amendements"]
                else:
                    items = [data]
            else:
                items = []

            for item in items:
                if is_senat:
                    amendments.append(parse_senat_amendement(item))
                else:
                    amendments.append(parse_an_amendement(item))

        except Exception as e:
            logger.error(f"Error parsing {file_path}: {e}")

    elif suffix == ".csv":
        import csv

        try:
            with open(file_path, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if is_senat:
                        amendments.append(parse_senat_amendement(dict(row)))
                    else:
                        amendments.append(parse_an_amendement(dict(row)))

        except Exception as e:
            logger.error(f"Error parsing {file_path}: {e}")

    return amendments


def filter_amendments_by_article(
    amendments: list[ParsedAmendement], article_numero: str
) -> list[ParsedAmendement]:
    """
    Filter amendments that target a specific article.

    Args:
        amendments: List of amendments
        article_numero: Article number to filter by

    Returns:
        Filtered list of amendments
    """
    # Normalize article number for comparison
    target = article_numero.lower().strip()

    return [
        a for a in amendments
        if a.article_ref and target in a.article_ref.lower()
    ]


def group_amendments_by_sort(
    amendments: list[ParsedAmendement],
) -> dict[str, list[ParsedAmendement]]:
    """
    Group amendments by their outcome (sort).

    Args:
        amendments: List of amendments

    Returns:
        Dict mapping sort value to list of amendments
    """
    groups: dict[str, list[ParsedAmendement]] = {}

    for a in amendments:
        sort = a.sort or "inconnu"
        if sort not in groups:
            groups[sort] = []
        groups[sort].append(a)

    return groups
