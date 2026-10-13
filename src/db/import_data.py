"""Data import module for loading parsed data into the database."""

import html
import json
import logging
import re
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from defusedxml.ElementTree import parse as _xml_parse
from sqlalchemy.orm import Session

from src.db.models import (
    Amendement,
    Article,
    Debat,
    DossierLegislatif,
    Intervention,
    Loi,
    get_session,
    init_db,
)
from src.parsers.amendments import ParsedAmendement, parse_amendment_file
from src.parsers.debates import (
    ParsedDebat,
    detect_speaker_function,
    extract_article_mentions,
)

logger = logging.getLogger(__name__)

# Keywords for identifying finance law debates
FINANCE_LAW_KEYWORDS = [
    "loi de finances",
    "projet de loi de finances",
    "budget",
    "plf",
    "plfss",
    "plfr",
    "financement de la sécurité sociale",
    "finances rectificative",
]

# Patterns for identifying finance law amendments by text reference
FINANCE_LAW_REFS = ["BTC", "PLF", "PLFSS", "PLFR"]

# Regex patterns for extracting specific finance law references from debate content
FINANCE_LAW_PATTERNS = [
    # "PLF 2024", "PLF pour 2024", "projet de loi de finances pour 2024"
    (r"(?:plf|projet de loi de finances)\s*(?:pour\s*)?(\d{4})", "LF"),
    # "PLFSS 2024", "projet de loi de financement de la sécurité sociale pour 2024"
    (r"(?:plfss|projet de loi de financement de la sécurité sociale)\s*(?:pour\s*)?(\d{4})", "LFSS"),
    # "PLFR 2024", "projet de loi de finances rectificative pour 2024"
    (r"(?:plfr|projet de loi de finances rectificative)\s*(?:pour\s*)?(\d{4})", "LFR"),
    # "loi de finances pour 2024"
    (r"loi de finances\s+(?:initiale\s+)?pour\s+(\d{4})", "LF"),
    # "loi de financement de la sécurité sociale pour 2024"
    (r"loi de financement de la sécurité sociale\s+pour\s+(\d{4})", "LFSS"),
    # "budget 2024", "budget pour 2024"
    (r"budget\s+(?:pour\s+)?(\d{4})", "LF"),
]


def normalize_article_ref(ref: str) -> str:
    """
    Normalize an article reference for comparison.

    Strips "Art."/"article" prefix, normalizes whitespace, lowercases.

    Args:
        ref: Raw article reference string

    Returns:
        Normalized article reference
    """
    normalized = ref.strip()
    # Strip common prefixes
    normalized = re.sub(r"^(?:art(?:icle)?\.?\s*)", "", normalized, flags=re.IGNORECASE)
    # Normalize whitespace
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.lower()


def article_refs_match(amend_ref: str, article_ref: str) -> bool:
    """
    Check if an amendment article reference matches an article number.

    Uses exact match after normalization instead of substring matching.

    Args:
        amend_ref: Amendment's article_ref
        article_ref: Article's numero_article

    Returns:
        True if they refer to the same article
    """
    return normalize_article_ref(amend_ref) == normalize_article_ref(article_ref)


# Additional regex patterns for finance law amendment text references
_FINANCE_AMEND_PATTERNS = [
    # Law numero pattern: "2023-1322", "2012-1510"
    re.compile(r"\d{4}-\d+"),
    # PJL/PRJL/PRJLFIN prefixes
    re.compile(r"(?:PJL|PRJL|PRJLFIN)", re.IGNORECASE),
    # NOR codes (French official ID format): ECOX2300001L
    re.compile(r"[A-Z]{4}\d{7}[A-Z]"),
]


def is_cgi_law_amendment(
    text_ref: str,
    known_law_numeros: set[str] | None = None,
) -> bool:
    """
    Check if an amendment text reference relates to a CGI-modifying law.

    Checks in order:
    1. Existing BTC/PLF/PLFSS/PLFR substring matches
    2. Regex patterns for law numeros, PJL/PRJL prefixes, NOR codes
    3. Known law numeros from the database (all CGI laws)

    Args:
        text_ref: The texteLegislatifRef value from the amendment
        known_law_numeros: Set of law numero strings from the DB (all CGI laws)

    Returns:
        True if this amendment appears to be for a CGI-modifying law
    """
    if not text_ref:
        return False

    # Check existing simple refs (BTC, PLF, PLFSS, PLFR)
    if any(ref in text_ref for ref in FINANCE_LAW_REFS):
        return True

    # Check regex patterns
    for pattern in _FINANCE_AMEND_PATTERNS:
        if pattern.search(text_ref):
            return True

    # Check against known law numeros from DB
    if known_law_numeros:
        for numero in known_law_numeros:
            if numero in text_ref:
                return True

    return False


# Backward compatibility alias
is_finance_law_amendment = is_cgi_law_amendment


def extract_jorf_id_from_url(url: str | None) -> str | None:
    """
    Safely extract a JORF text ID from a Légifrance URL.

    Handles None, empty strings, and trailing slashes. Validates that
    the extracted ID starts with "JORFTEXT".

    Args:
        url: Légifrance URL (may be None)

    Returns:
        JORF text ID or None if invalid/missing
    """
    if not url or not url.strip():
        return None

    cleaned = url.strip().rstrip("/")
    candidate = cleaned.split("/")[-1]

    if candidate.startswith("JORFTEXT"):
        return candidate

    return None


def deduplicate_debates(session: Session) -> int:
    """
    Remove duplicate debates, keeping the record with the most interventions.

    Duplicates are identified by matching reference_externe values.

    Args:
        session: Database session

    Returns:
        Number of duplicate debates removed
    """
    from sqlalchemy import func as sa_func

    # Find reference_externe values that appear more than once
    dupes = (
        session.query(Debat.reference_externe)
        .filter(Debat.reference_externe.isnot(None))
        .group_by(Debat.reference_externe)
        .having(sa_func.count(Debat.id) > 1)
        .all()
    )

    removed = 0
    for (ref_ext,) in dupes:
        # Get all debates with this reference, ordered by intervention count desc
        debates = session.query(Debat).filter_by(reference_externe=ref_ext).all()

        # Count interventions for each
        debate_counts = []
        for d in debates:
            count = session.query(Intervention).filter_by(debat_id=d.id).count()
            debate_counts.append((d, count))

        # Sort by count descending, keep the first (most interventions)
        debate_counts.sort(key=lambda x: x[1], reverse=True)

        for d, _ in debate_counts[1:]:
            session.delete(d)
            removed += 1

    if removed:
        session.commit()
        logger.info(f"Removed {removed} duplicate debates")

    return removed


def extract_law_reference_from_content(content: str) -> tuple[str, int] | None:
    """
    Extract law type and year from debate content.

    Collects all matches and disambiguates LF vs LFR by checking for
    "rectificativ" context near PLF/LF matches.

    Args:
        content: Debate content text

    Returns:
        Tuple of (law_type, year) or None if no match found
    """
    content_lower = content.lower()

    # Collect all matches (not just the first)
    all_matches = []
    for pattern, law_type in FINANCE_LAW_PATTERNS:
        for match in re.finditer(pattern, content_lower):
            year = int(match.group(1))
            if 1958 <= year <= 2026:
                all_matches.append((law_type, year, match.start(), match.end()))

    if not all_matches:
        return None

    # Prefer explicit LFR/LFSS matches over generic LF
    for law_type, year, start, end in all_matches:
        if law_type in ("LFR", "LFSS"):
            return (law_type, year)

    # For LF matches, check if "rectificativ" appears nearby — indicates LFR
    for law_type, year, start, end in all_matches:
        if law_type == "LF":
            # Check ±200 chars around the match for "rectificativ" context
            window_start = max(0, start - 200)
            window_end = min(len(content_lower), end + 200)
            window = content_lower[window_start:window_end]
            if "rectificati" in window:
                return ("LFR", year)
            return (law_type, year)

    return None


def build_debate_lookup_cache(dossiers, lois):
    """Build lookup indexes for find_dossier_for_debate (call once, reuse)."""
    from collections import defaultdict

    # Multiple laws can share (type, year) — e.g. LFR 2014-891 and LFR 2014-1655
    lois_by_type_year = defaultdict(list)
    for loi in lois:
        lois_by_type_year[(loi.type, loi.annee)].append(loi)
    dossiers_by_loi_chambre = {}
    for dossier in dossiers:
        key = (dossier.loi_id, dossier.chambre)
        dossiers_by_loi_chambre[key] = dossier
    return lois_by_type_year, dossiers_by_loi_chambre


def find_dossier_for_debate(
    dossiers: list[DossierLegislatif],
    lois: list[Loi],
    content: str,
    debate_date: datetime,
    chambre: str = "AN",
    _lookup_cache: tuple | None = None,
) -> DossierLegislatif | None:
    """
    Find the matching dossier for a debate based on content and date.

    Args:
        dossiers: List of all dossiers (not dict - avoids key collision)
        lois: List of all laws
        content: Debate content
        debate_date: Date of the debate
        chambre: Target chamber ("AN" or "SENAT")
        _lookup_cache: Optional pre-built (lois_by_type_year, dossiers_by_loi_chambre)
                       from build_debate_lookup_cache(). Avoids rebuilding per call.

    Returns:
        Matching DossierLegislatif or None
    """
    if _lookup_cache is not None:
        lois_by_type_year, dossiers_by_loi_chambre = _lookup_cache
    else:
        lois_by_type_year, dossiers_by_loi_chambre = build_debate_lookup_cache(dossiers, lois)

    # Try to extract explicit law reference from content
    law_ref = extract_law_reference_from_content(content)

    if law_ref:
        law_type, year = law_ref
        # Multiple laws may share (type, year) — iterate the list
        for loi in lois_by_type_year.get((law_type, year), []):
            dossier = dossiers_by_loi_chambre.get((loi.id, chambre))
            if dossier:
                return dossier

    # Fallback: match by date
    # PLF debates typically happen Oct-Dec of year N-1 for LF year N
    # LFSS debates typically happen Oct-Nov of year N-1 for LFSS year N
    debate_year = debate_date.year
    debate_month = debate_date.month

    # Determine target year based on debate date
    if debate_month >= 9:  # Sep-Dec: likely for next year's law
        target_year = debate_year + 1
    else:  # Jan-Aug: likely for current year's law (LFR) or ongoing debates
        target_year = debate_year

    # Try all law types in date fallback, not just LF
    for law_type in ("LF", "LFR", "LFSS"):
        for loi in lois_by_type_year.get((law_type, target_year), []):
            dossier = dossiers_by_loi_chambre.get((loi.id, chambre))
            if dossier:
                return dossier

    return None


def import_loi(session: Session, loi_data: dict[str, Any]) -> Loi:
    """
    Import a law from reference data.

    Args:
        session: Database session
        loi_data: Law data from reference file

    Returns:
        Loi model instance
    """
    # Check if already exists
    existing = session.query(Loi).filter_by(numero=loi_data["numero"]).first()
    if existing:
        logger.info(f"Law {loi_data['numero']} already exists, skipping")
        return existing

    # Parse date
    date_str = loi_data["date_promulgation"]
    try:
        date_promulgation = datetime.fromisoformat(date_str)
    except ValueError:
        date_promulgation = datetime.strptime(date_str, "%Y-%m-%d")

    loi = Loi(
        numero=loi_data["numero"],
        type=loi_data["type"],
        annee=loi_data["annee_budgetaire"],
        date_promulgation=date_promulgation,
        titre=loi_data["titre"],
        nor=loi_data.get("nor"),
        url_legifrance=loi_data.get("url_legifrance"),
    )

    session.add(loi)
    session.flush()  # Get the ID

    logger.info(f"Imported law: {loi.titre}")
    return loi


def import_articles(session: Session, loi: Loi, articles_data: list[dict]) -> list[Article]:
    """
    Import articles for a law.

    Args:
        session: Database session
        loi: Parent Loi instance
        articles_data: List of article data dicts

    Returns:
        List of Article model instances
    """
    articles = []

    for art_data in articles_data:
        # Check if exists
        existing = session.query(Article).filter_by(
            loi_id=loi.id,
            numero_article=art_data["numero_article"]
        ).first()

        if existing:
            articles.append(existing)
            continue

        article = Article(
            loi_id=loi.id,
            numero_article=art_data["numero_article"],
            contenu_texte=art_data["contenu_texte"],
            position_dans_loi=art_data.get("position_dans_loi"),
        )

        session.add(article)
        articles.append(article)

    session.flush()
    logger.info(f"Imported {len(articles)} articles for law {loi.numero}")
    return articles


def import_dossier(
    session: Session, loi: Loi, chambre: str, url: str, reference: str | None = None
) -> DossierLegislatif:
    """
    Import a legislative dossier.

    Args:
        session: Database session
        loi: Parent Loi instance
        chambre: Chamber (AN or SENAT)
        url: Source URL
        reference: External reference ID

    Returns:
        DossierLegislatif model instance
    """
    # Check if exists
    existing = session.query(DossierLegislatif).filter_by(
        loi_id=loi.id,
        chambre=chambre
    ).first()

    if existing:
        return existing

    dossier = DossierLegislatif(
        loi_id=loi.id,
        chambre=chambre,
        url_source=url,
        reference_externe=reference,
        etapes=[],  # Will be populated later
    )

    session.add(dossier)
    session.flush()

    logger.info(f"Imported dossier for {loi.numero} ({chambre})")
    return dossier


def import_dossiers_from_dosleg(session: Session, dosleg_path: Path) -> int:
    """Import Sénat dossiers from DOSLEG PostgreSQL dump.

    Parses the `loi` table from the DOSLEG dump, matches promulgated laws
    to existing Loi records in the DB via the `numero` field, and creates
    SENAT dossiers for matched laws that don't already have one.

    Args:
        session: Database session
        dosleg_path: Path to the dosleg.sql dump file

    Returns:
        Number of dossiers created
    """
    from src.parsers.dosleg_parser import get_senat_dossiers

    dosleg_dossiers = get_senat_dossiers(dosleg_path)
    if not dosleg_dossiers:
        logger.warning("No dossiers found in DOSLEG dump")
        return 0

    # Build lookup of existing Loi records by numero
    all_lois = {loi.numero: loi for loi in session.query(Loi).all() if loi.numero}

    created = 0
    matched = 0
    for dd in dosleg_dossiers:
        numero = dd["numero"]
        if numero not in all_lois:
            continue

        matched += 1
        loi = all_lois[numero]
        url = dd.get("url") or ""
        signet = dd.get("signet") or ""

        # Check if SENAT dossier already exists for this loi
        existing = session.query(DossierLegislatif).filter_by(
            loi_id=loi.id, chambre="SENAT"
        ).first()

        if existing:
            # Update URL if missing
            if not existing.url_source and url:
                existing.url_source = url
            continue

        dossier = DossierLegislatif(
            loi_id=loi.id,
            chambre="SENAT",
            url_source=url,
            reference_externe=f"dosleg_{signet}" if signet else None,
            etapes=[],
        )
        session.add(dossier)
        created += 1

        if created % 100 == 0:
            session.flush()

    session.commit()
    logger.info(
        f"DOSLEG dossiers: {matched} matched laws, {created} new SENAT dossiers created "
        f"(out of {len(dosleg_dossiers)} promulgated dossiers in dump)"
    )
    return created


def import_debat(session: Session, dossier: DossierLegislatif, debat: ParsedDebat) -> Debat:
    """
    Import a debate and its interventions.

    Uses upsert logic: if a debate with the same reference_externe already
    exists, returns the existing record instead of creating a duplicate.

    Args:
        session: Database session
        dossier: Parent DossierLegislatif instance
        debat: ParsedDebat data

    Returns:
        Debat model instance
    """
    # Upsert: check reference_externe before insert to prevent duplicates
    if debat.reference:
        existing = session.query(Debat).filter_by(
            reference_externe=debat.reference
        ).first()
        if existing:
            logger.debug(f"Debate already exists with reference {debat.reference}, skipping")
            return existing

    db_debat = Debat(
        dossier_id=dossier.id,
        chambre=debat.chambre,
        date=debat.date,
        etape=debat.etape,
        contenu_integral=debat.contenu_integral,
        url_source=debat.url_source,
        reference_externe=debat.reference,
    )

    session.add(db_debat)
    session.flush()

    # Import interventions
    for interv in debat.interventions:
        db_interv = Intervention(
            debat_id=db_debat.id,
            orateur=interv.orateur,
            fonction=interv.fonction,
            contenu=interv.contenu,
            articles_mentionnes=interv.articles_mentionnes,
            timestamp_debut=interv.timestamp,
            ordre=interv.ordre,
        )
        session.add(db_interv)

    session.flush()
    logger.info(f"Imported debate with {len(debat.interventions)} interventions")
    return db_debat


def import_orphan_debat(session: Session, debat: ParsedDebat) -> Debat:
    """Import a debate without a parent dossier (orphan).

    Used for historical archive debates that haven't been linked to a dossier yet.
    Idempotent: checks reference_externe before inserting.

    Args:
        session: Database session
        debat: ParsedDebat data

    Returns:
        Debat model instance (existing or newly created)
    """
    if debat.reference:
        existing = session.query(Debat).filter_by(
            reference_externe=debat.reference
        ).first()
        if existing:
            return existing

    db_debat = Debat(
        dossier_id=None,
        chambre=debat.chambre,
        date=debat.date,
        etape=debat.etape,
        contenu_integral=debat.contenu_integral,
        url_source=debat.url_source,
        reference_externe=debat.reference,
    )
    session.add(db_debat)
    session.flush()

    for interv in debat.interventions:
        db_interv = Intervention(
            debat_id=db_debat.id,
            orateur=interv.orateur,
            fonction=interv.fonction,
            contenu=interv.contenu,
            articles_mentionnes=interv.articles_mentionnes,
            timestamp_debut=interv.timestamp,
            ordre=interv.ordre,
        )
        session.add(db_interv)

    session.flush()
    logger.info(f"Imported orphan debate with {len(debat.interventions)} interventions")
    return db_debat


def import_amendement(
    session: Session,
    dossier: DossierLegislatif,
    amend: ParsedAmendement,
    articles: list[Article],
) -> Amendement:
    """
    Import an amendment.

    Args:
        session: Database session
        dossier: Parent DossierLegislatif instance
        amend: ParsedAmendement data
        articles: List of articles to link to

    Returns:
        Amendement model instance
    """
    # Try to find matching article
    # Note: For batch operations, callers should pre-build an article lookup dict
    # for O(1) access instead of passing a list
    article_id = None
    if amend.article_ref:
        # Check if articles is a dict (optimized lookup) or list (legacy)
        if isinstance(articles, dict):
            norm_ref = normalize_article_ref(amend.article_ref)
            matched_articles = articles.get(norm_ref, [])
            if matched_articles:
                article_id = matched_articles[0].id
        else:
            # Legacy list-based lookup (slower)
            for art in articles:
                if article_refs_match(amend.article_ref, art.numero_article):
                    article_id = art.id
                    break

    db_amend = Amendement(
        dossier_id=dossier.id,
        article_id=article_id,
        numero=amend.numero,
        auteur=amend.auteur,
        objet=amend.objet,
        contenu=amend.contenu,
        expose_motifs=amend.expose_motifs,
        sort=amend.sort,
        date_depot=amend.date_depot,
        url_source=None,
        reference_externe=amend.reference_externe,
    )

    session.add(db_amend)
    return db_amend


def extract_articles_from_legifrance_json(json_data: dict) -> list[dict]:
    """
    Extract articles from a Légifrance JORF JSON structure.

    The JSON has nested sections with articles. We need to recursively
    find all articles and extract their content.

    Args:
        json_data: Légifrance JSON response

    Returns:
        List of article dicts with numero_article, contenu_texte, position_dans_loi
    """
    # Detect rectificatif texts — these are errata, not the actual law
    title = json_data.get("title", "")
    if "(rectificatif)" in title.lower():
        logger.warning(f"JORF text is a rectificatif, not the main law text: {title}")
        return []

    articles = []
    position = 0

    def extract_from_sections(sections: list, path_titles: list = None):
        nonlocal position
        path_titles = path_titles or []

        for section in sections:
            section_title = section.get("title", "")
            current_path = path_titles + [section_title] if section_title else path_titles

            # Extract articles in this section
            for article in section.get("articles", []):
                num = article.get("num")
                content = article.get("content", "")

                # Clean HTML from content
                if content:
                    content = re.sub(r"<[^>]+>", " ", content)
                    content = re.sub(r"\s+", " ", content).strip()

                if num and content:
                    position += 1
                    articles.append({
                        "numero_article": num,
                        "contenu_texte": content,
                        "position_dans_loi": position,
                        "section_path": " > ".join(current_path) if current_path else None,
                    })

            # Recurse into nested sections
            if section.get("sections"):
                extract_from_sections(section["sections"], current_path)

    # Start extraction from top-level sections
    if "sections" in json_data:
        extract_from_sections(json_data["sections"])

    # Fallback: some JORF structures store articles at root level
    if not articles and "articles" in json_data:
        for article in json_data["articles"]:
            num = article.get("num")
            content = article.get("content", "")
            if content:
                content = re.sub(r"<[^>]+>", " ", content)
                content = re.sub(r"\s+", " ", content).strip()
            if num and content:
                position += 1
                articles.append({
                    "numero_article": num,
                    "contenu_texte": content,
                    "position_dans_loi": position,
                    "section_path": None,
                })

    return articles


def import_articles_from_legifrance(
    session: Session,
    loi: Loi,
    legifrance_dir: Path,
) -> list[Article]:
    """
    Import articles for a law from Légifrance JSON file.

    Args:
        session: Database session
        loi: Parent Loi instance
        legifrance_dir: Directory containing Légifrance JSON files

    Returns:
        List of Article instances
    """
    # Find the JSON file for this law (by JORF ID)
    jorf_id = extract_jorf_id_from_url(loi.url_legifrance)
    if not jorf_id:
        # Try NOR code
        jorf_id = loi.nor

    if not jorf_id:
        logger.warning(f"No JORF ID found for law {loi.numero}")
        return []

    json_file = legifrance_dir / f"{jorf_id}.json"
    if not json_file.exists():
        # Try pattern matching by JORF ID
        for f in legifrance_dir.glob("*.json"):
            if jorf_id in f.name:
                json_file = f
                break
        else:
            # Fallback: scan for files matching the law numero
            if loi.numero:
                for f in legifrance_dir.glob("*.json"):
                    if loi.numero in f.name:
                        json_file = f
                        break
                else:
                    logger.warning(f"No Légifrance file found for {jorf_id}")
                    return []
            else:
                logger.warning(f"No Légifrance file found for {jorf_id}")
                return []

    try:
        with open(json_file, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Error reading {json_file}: {e}")
        return []

    # Extract articles from JSON
    articles_data = extract_articles_from_legifrance_json(data)

    # Fallback: use LEGI dump articles if JORF articles are empty
    if not articles_data and data.get("legi_articles"):
        logger.info(f"Using LEGI dump articles for {loi.numero}")
        for idx, art in enumerate(data["legi_articles"]):
            num = art.get("num")
            content = art.get("content", "")
            if content:
                content = re.sub(r"<[^>]+>", " ", content)
                content = re.sub(r"\s+", " ", content).strip()
            if num and content:
                articles_data.append({
                    "numero_article": num,
                    "contenu_texte": content,
                    "position_dans_loi": idx + 1,
                    "section_path": None,
                })

    if not articles_data:
        logger.warning(f"No articles found in {json_file}")
        return []

    # Import articles
    articles = import_articles(session, loi, articles_data)
    logger.info(f"Imported {len(articles)} articles for law {loi.numero}")

    return articles


def is_finance_law_debate(xml_path: Path) -> bool:
    """
    Check if an XML file contains a debate about finance laws.

    Args:
        xml_path: Path to compte rendu XML

    Returns:
        True if this appears to be a finance law debate
    """
    try:
        # Read first 50KB to check content
        with open(xml_path, "r", encoding="utf-8") as f:
            content = f.read(50000).lower()

        return any(kw in content for kw in FINANCE_LAW_KEYWORDS)
    except Exception:
        return False


def parse_an_compte_rendu_enhanced(xml_path: Path) -> ParsedDebat | None:
    """
    Enhanced parser for AN compte rendu XML with better intervention extraction.

    Args:
        xml_path: Path to XML file

    Returns:
        ParsedDebat or None
    """
    try:
        tree = _xml_parse(xml_path)
        root = tree.getroot()

        # Handle namespace - the namespace is in the tag names
        ns_uri = "http://schemas.assemblee-nationale.fr/referentiel"
        ns = {"cr": ns_uri}

        # Helper to find elements with or without namespace
        def find_elem(parent, tag):
            result = parent.find(f".//{{{ns_uri}}}{tag}")
            if result is None:
                result = parent.find(f".//{tag}")
            return result

        def find_all_elem(parent, tag):
            result = list(parent.iter(f"{{{ns_uri}}}{tag}"))
            if not result:
                result = list(parent.iter(tag))
            return result

        # Extract metadata
        date_elem = find_elem(root, "dateSeance")
        if date_elem is not None and date_elem.text:
            date_str = date_elem.text.strip()
            try:
                # Handle format: 20230313160000000
                if len(date_str) >= 8 and date_str.isdigit():
                    date = datetime.strptime(date_str[:8], "%Y%m%d")
                else:
                    date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except ValueError:
                date = datetime.now()
        else:
            date = datetime.now()

        # Determine etape
        etape = "seance_publique"

        # Extract interventions from paragraphe elements
        from src.parsers.debates import (
            Intervention,
            detect_speaker_function,
            extract_article_mentions,
        )

        interventions = []
        full_text_parts = []
        ordre = 0

        # Helper for finding child elements (avoid `or` with Elements)
        def find_child(parent, tag):
            elem = parent.find(f"{{{ns_uri}}}{tag}")
            if elem is None:
                elem = parent.find(tag)
            return elem

        # Find all paragraphe elements with orateurs
        for para in find_all_elem(root, "paragraphe"):
            # Find orateur (nested under orateurs)
            orateurs_elem = find_child(para, "orateurs")
            if orateurs_elem is None:
                continue

            orateur_elem = find_child(orateurs_elem, "orateur")
            if orateur_elem is None:
                continue

            # Find texte
            texte_elem = find_child(para, "texte")
            if texte_elem is None:
                continue

            # Get name
            nom_elem = find_child(orateur_elem, "nom")
            if nom_elem is None or not nom_elem.text:
                continue

            orateur = nom_elem.text.strip()

            # Get qualite/fonction
            qualite_elem = find_child(orateur_elem, "qualite")
            qualite = qualite_elem.text.strip() if qualite_elem is not None and qualite_elem.text else None

            # Get text content
            contenu = "".join(texte_elem.itertext()).strip()

            if contenu and len(contenu) > 20:
                ordre += 1
                full_text_parts.append(f"{orateur}: {contenu}")

                # Determine function
                if qualite:
                    if "ministre" in qualite.lower():
                        fonction = "ministre"
                    elif "rapporteur" in qualite.lower():
                        fonction = "rapporteur"
                    elif "président" in qualite.lower():
                        fonction = "president"
                    else:
                        fonction = detect_speaker_function(contenu, orateur)
                else:
                    fonction = detect_speaker_function(contenu, orateur)

                articles = extract_article_mentions(contenu)

                interventions.append(Intervention(
                    orateur=orateur,
                    fonction=fonction,
                    contenu=contenu,
                    articles_mentionnes=articles,
                    ordre=ordre,
                ))

        if not interventions:
            return None

        uid_elem = find_elem(root, "uid")
        reference = uid_elem.text if uid_elem is not None and uid_elem.text else xml_path.stem

        return ParsedDebat(
            chambre="AN",
            date=date,
            etape=etape,
            url_source=None,
            reference=reference,
            interventions=interventions,
            contenu_integral="\n\n".join(full_text_parts),
        )

    except ET.ParseError as e:
        logger.warning(f"XML parse error for {xml_path}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error parsing {xml_path}: {e}")
        return None


def import_debates_from_an(
    session: Session,
    an_data_dir: Path,
    dossiers: dict[str, DossierLegislatif],
) -> tuple[int, int]:
    """
    Import debates from Assemblée Nationale compte rendu XML files.

    Args:
        session: Database session
        an_data_dir: Directory containing AN data (assemblee/)
        dossiers: Dict mapping reference to DossierLegislatif

    Returns:
        Tuple of (debates_imported, interventions_imported)
    """
    debats_count = 0
    interventions_count = 0

    # Get all lois for matching
    lois = session.query(Loi).all()
    dossiers_list = list(set(dossiers.values()))

    # Get first AN dossier as default fallback
    default_dossier = None
    for dossier in dossiers.values():
        if dossier.chambre == "AN":
            default_dossier = dossier
            break

    if not default_dossier:
        logger.warning("No AN dossier found, skipping debate import")
        return 0, 0

    # Track dossier assignments for logging
    dossier_counts: dict[int, int] = {}

    # Find compte rendu directories
    cr_dirs = list(an_data_dir.glob("comptes_rendus_leg*/xml/compteRendu"))

    if not cr_dirs:
        # Try alternate structure
        cr_dirs = list(an_data_dir.glob("comptes_rendus_leg*"))

    for cr_dir in cr_dirs:
        xml_files = list(cr_dir.glob("*.xml"))
        logger.info(f"Processing {len(xml_files)} compte rendu files in {cr_dir}")

        for xml_file in xml_files:

            # Check if this is a finance law debate
            if not is_finance_law_debate(xml_file):
                continue

            # Parse the debate
            debat = parse_an_compte_rendu_enhanced(xml_file)
            if not debat or not debat.interventions:
                continue

            # Check if debate already exists
            existing = session.query(Debat).filter_by(
                reference_externe=debat.reference
            ).first()
            if existing:
                continue

            # Find the matching dossier based on content and date
            content = debat.contenu_integral or ""
            matched_dossier = find_dossier_for_debate(
                dossiers_list, lois, content, debat.date
            )

            # Use matched dossier or fall back to default
            target_dossier = matched_dossier if matched_dossier else default_dossier

            # Import the debate
            db_debat = import_debat(session, target_dossier, debat)
            debats_count += 1
            interventions_count += len(debat.interventions)

            # Track dossier assignments
            dossier_counts[target_dossier.id] = dossier_counts.get(target_dossier.id, 0) + 1

            # Commit periodically
            if debats_count % 50 == 0:
                session.commit()
                logger.info(f"Imported {debats_count} debates, {interventions_count} interventions...")

    session.commit()

    # Log dossier distribution
    logger.info(f"Debate distribution across dossiers: {len(dossier_counts)} dossiers used")
    for dossier_id, count in sorted(dossier_counts.items(), key=lambda x: -x[1])[:10]:
        logger.info(f"  Dossier {dossier_id}: {count} debates")

    return debats_count, interventions_count


def import_debates_from_senat_xml(
    session: Session,
    senat_cr_dir: Path,
) -> tuple[int, int]:
    """
    Import debates from Sénat comptes rendus XML files.

    Scans session directories for comptes_rendus.xml files, parses them,
    filters for finance-law debates, and imports to the database.

    Args:
        session: Database session
        senat_cr_dir: Directory containing session subdirectories
                      (e.g., data/raw/senat/comptes_rendus/)

    Returns:
        Tuple of (debates_imported, interventions_imported)
    """
    debats_count = 0
    interventions_count = 0

    # Get lois and dossiers for matching
    lois = session.query(Loi).all()
    dossiers_list = session.query(DossierLegislatif).all()

    # Find a default SENAT dossier as fallback
    default_dossier = None
    for dossier in dossiers_list:
        if dossier.chambre == "SENAT":
            default_dossier = dossier
            break

    if not default_dossier:
        logger.warning("No SENAT dossier found, skipping Sénat XML import")
        return 0, 0

    # Scan all session directories
    session_dirs = sorted(senat_cr_dir.iterdir()) if senat_cr_dir.exists() else []

    for session_dir in session_dirs:
        if not session_dir.is_dir():
            continue

        xml_file = session_dir / "comptes_rendus.xml"
        if not xml_file.exists():
            continue

        # Check file size -- skip empty files
        if xml_file.stat().st_size < 100:
            logger.debug(f"Skipping empty XML: {xml_file}")
            continue

        logger.info(f"Processing Sénat CR XML: {session_dir.name}")

        try:
            tree = _xml_parse(xml_file)
            root = tree.getroot()
        except ET.ParseError as e:
            logger.warning(f"XML parse error for {xml_file}: {e}")
            continue
        except Exception as e:
            logger.warning(f"Error reading {xml_file}: {e}")
            continue

        # Extract all text content to check for finance-law relevance
        all_text = ET.tostring(root, encoding="unicode", method="text")
        # Decode HTML entities (&#233; → é, etc.)
        all_text = html.unescape(all_text)

        if not all_text or len(all_text) < 100:
            continue

        # Check if this contains finance law content
        if not any(kw in all_text.lower() for kw in FINANCE_LAW_KEYWORDS):
            continue

        # Reference for deduplication
        session_name = session_dir.name
        reference = f"senat_cr_xml_{session_name}"

        existing = session.query(Debat).filter_by(
            reference_externe=reference
        ).first()
        if existing:
            continue

        # Parse date from session name (e.g., "2020-2021")
        # Use November 15 of the first year as a reasonable midpoint for
        # finance law debates (LF/LFSS debates run Oct-Dec)
        try:
            year = int(session_name.split("-")[0])
            debate_date = datetime(year, 11, 15)
        except (ValueError, IndexError):
            logger.warning(f"Cannot parse session date from '{session_name}', skipping")
            continue

        # Extract interventions using text-based approach
        interventions_data = []

        # Split by speaker patterns — strict regex to avoid false positives
        speaker_pattern = re.compile(
            r"((?:M\.|Mme|Mlle)\s+"
            r"(?:l[ea]\s+(?:président(?:e)?|ministre|secrétaire|rapporteur)"
            r"|(?:[A-ZÀ-Ý][a-zà-ÿ]+|d'[A-ZÀ-Ý][a-zà-ÿ]+|(?:de|du|le|la|des)\s+[A-ZÀ-Ý])[a-zà-ÿ]*"
            r"(?:-[A-ZÀ-Ý][a-zà-ÿ]+)*"
            r"(?:\s+(?:[A-ZÀ-Ý][a-zà-ÿ]+(?:-[A-ZÀ-Ý][a-zà-ÿ]+)*|d'[A-ZÀ-Ý][a-zà-ÿ]+|(?:de|du|le|la|des)))*"
            r"))"
            r"(?:\s*\([^)]+\))?"
            r"(?:\s*,\s*([^\.]+))?"
            r"\.\s*[-\u2013\u2014]?\s*"
        )

        matches = list(speaker_pattern.finditer(all_text))

        for idx, match in enumerate(matches):
            speaker = match.group(1).strip()
            # Extract first speaker if multi ("M. X et M. Y")
            multi = re.match(
                r'^((?:M\.|Mme|Mlle)\s+[A-ZÀ-Ý][a-zà-ÿ-]+(?:\s+[A-ZÀ-Ý][a-zà-ÿ-]+)*)\s+et\s+',
                speaker
            )
            if multi:
                speaker = multi.group(1).strip()
            function_str = match.group(2).strip() if match.group(2) else None

            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else min(start + 5000, len(all_text))
            content = all_text[start:end].strip()
            content = re.sub(r"\s+", " ", content)

            if content and len(content) > 30:
                fonction = None
                if function_str:
                    fl = function_str.lower()
                    if "ministre" in fl:
                        fonction = "ministre"
                    elif "rapporteur" in fl:
                        fonction = "rapporteur"
                    elif "président" in fl:
                        fonction = "president"
                    else:
                        fonction = detect_speaker_function(content, speaker)
                else:
                    fonction = detect_speaker_function(content, speaker)

                interventions_data.append({
                    "orateur": speaker,
                    "fonction": fonction,
                    "contenu": content[:10000],
                    "articles_mentionnes": extract_article_mentions(content),
                })

        if not interventions_data:
            continue

        # Find matching dossier based on content
        matched_dossier = find_dossier_for_debate(
            dossiers_list, lois, all_text[:50000], debate_date, chambre="SENAT"
        )
        target_dossier = matched_dossier if matched_dossier else default_dossier

        # Create debate record
        debat = Debat(
            dossier_id=target_dossier.id,
            chambre="SENAT",
            date=debate_date,
            etape="seance_publique",
            contenu_integral=all_text[:50000],
            url_source=None,
            reference_externe=reference,
        )
        session.add(debat)
        session.flush()

        for ordre, interv in enumerate(interventions_data, 1):
            db_interv = Intervention(
                debat_id=debat.id,
                orateur=interv["orateur"],
                fonction=interv["fonction"],
                contenu=interv["contenu"],
                articles_mentionnes=interv.get("articles_mentionnes"),
                ordre=ordre,
            )
            session.add(db_interv)

        debats_count += 1
        interventions_count += len(interventions_data)

        if debats_count % 10 == 0:
            session.commit()
            logger.info(f"Imported {debats_count} Sénat XML debates...")

    session.commit()
    logger.info(f"Sénat XML import: {debats_count} debates, {interventions_count} interventions")
    return debats_count, interventions_count


def import_debates_from_senat_cri(
    session: Session,
    cri_dir: Path,
) -> tuple[int, int]:
    """Import debates from Sénat CRI (Comptes Rendus Intégraux) XML files.

    The CRI bulk archive (cri.zip) contains individual XML debate files
    named ``CRI_YYYYAAAA_NNN.xml`` (one per session sitting since Jan 2003).
    Each XML file holds a structured debate transcript with speaker
    interventions, chapters/sections, and references to legislative texts.

    The function filters for finance-law-related debates, deduplicates
    against existing records, and imports both debates and interventions.

    Args:
        session: Database session
        cri_dir: Directory containing extracted CRI_*.xml files
                 (e.g., ``data/raw/senat/cri/``)

    Returns:
        Tuple of (debates_imported, interventions_imported)
    """
    debats_count = 0
    interventions_count = 0

    if not cri_dir.exists():
        logger.warning(f"CRI directory not found: {cri_dir}")
        return 0, 0

    lois = session.query(Loi).all()
    dossiers_list = session.query(DossierLegislatif).all()

    default_dossier = None
    for dossier in dossiers_list:
        if dossier.chambre == "SENAT":
            default_dossier = dossier
            break

    if not default_dossier:
        logger.warning("No SENAT dossier found, skipping CRI import")
        return 0, 0

    # CRI XML files are named d20YYMMDD.xml (e.g., d20130409.xml)
    # Also support legacy CRI_*.xml naming
    xml_files = sorted(cri_dir.glob("d*.xml")) + sorted(cri_dir.glob("CRI_*.xml"))
    logger.info(f"Found {len(xml_files)} CRI XML files to process")

    for xml_file in xml_files:
        if xml_file.stat().st_size < 200:
            continue

        # Reference for deduplication
        reference = f"senat_cri_{xml_file.stem}"

        existing = session.query(Debat).filter_by(
            reference_externe=reference
        ).first()
        if existing:
            continue

        try:
            raw_bytes = xml_file.read_bytes()
            # Files declare ISO-8859-1 but are actually UTF-8
            try:
                raw_text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raw_text = raw_bytes.decode("iso-8859-1")
        except Exception as e:
            logger.debug(f"Error reading {xml_file.name}: {e}")
            continue

        # Strip XML/HTML tags to get plain text
        all_text = re.sub(r"<[^>]+>", " ", raw_text)
        # Decode HTML entities (&#233; → é, etc.)
        all_text = html.unescape(all_text)
        all_text = re.sub(r"\s+", " ", all_text).strip()
        if len(all_text) < 200:
            continue

        # Only keep finance-law-related debates
        text_lower = all_text.lower()
        if not any(kw in text_lower for kw in FINANCE_LAW_KEYWORDS):
            continue

        # Parse date from filename:
        #   d20130409.xml  → 2013-04-09  (actual date)
        #   CRI_20232024_NNN.xml → approximate (session year, Nov 15)
        try:
            stem = xml_file.stem
            if stem.startswith("d") and len(stem) == 9:
                # d20YYMMDD format
                debate_date = datetime.strptime(stem[1:], "%Y%m%d")
            else:
                # Legacy CRI_YYYYAAAA_NNN format
                parts = stem.split("_")
                session_code = parts[1]
                year = int(session_code[:4])
                debate_date = datetime(year, 11, 15)
        except (ValueError, IndexError):
            logger.debug(f"Cannot parse date from '{xml_file.name}', skipping")
            continue

        # Extract interventions using speaker detection on plain text.
        # Sénat CRI XML embeds malformed HTML (97% fail XML parsing),
        # so we use regex-based speaker detection on stripped text.
        interventions_data = _extract_cri_interventions(None, all_text)

        if not interventions_data:
            continue

        # Match to a dossier based on content
        matched_dossier = find_dossier_for_debate(
            dossiers_list, lois, all_text[:50000], debate_date, chambre="SENAT"
        )
        target_dossier = matched_dossier if matched_dossier else default_dossier

        debat = Debat(
            dossier_id=target_dossier.id,
            chambre="SENAT",
            date=debate_date,
            etape="seance_publique",
            contenu_integral=all_text[:50000],
            url_source=None,
            reference_externe=reference,
        )
        session.add(debat)
        session.flush()

        for ordre, interv in enumerate(interventions_data, 1):
            db_interv = Intervention(
                debat_id=debat.id,
                orateur=interv["orateur"],
                fonction=interv["fonction"],
                contenu=interv["contenu"],
                articles_mentionnes=interv.get("articles_mentionnes"),
                ordre=ordre,
            )
            session.add(db_interv)

        debats_count += 1
        interventions_count += len(interventions_data)

        if debats_count % 50 == 0:
            session.commit()
            logger.info(f"Imported {debats_count} CRI debates, {interventions_count} interventions...")

    session.commit()
    logger.info(f"Sénat CRI import: {debats_count} debates, {interventions_count} interventions")
    return debats_count, interventions_count


def _extract_cri_interventions(root: ET.Element | None, all_text: str) -> list[dict]:
    """Extract speaker interventions from a CRI XML element tree or plain text.

    If *root* is provided, tries structured XML tags first
    (``<intervention>``, ``<orateur>``).  Always falls back to regex-based
    speaker detection on *all_text*.

    Returns:
        List of dicts with keys: orateur, fonction, contenu, articles_mentionnes
    """
    interventions = []

    # --- Attempt 1: structured XML tags (if root provided) ---
    if root is not None:
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        for interv_el in root.iter(f"{ns}intervention"):
            orateur_el = interv_el.find(f"{ns}orateur")
            if orateur_el is None:
                orateur_el = interv_el.find(f"{ns}speaker")

            orateur = None
            fonction = None

            if orateur_el is not None:
                nom_el = orateur_el.find(f"{ns}nom")
                if nom_el is not None and nom_el.text:
                    orateur = nom_el.text.strip()
                elif orateur_el.text:
                    orateur = orateur_el.text.strip()
                role_el = orateur_el.find(f"{ns}qualite")
                if role_el is None:
                    role_el = orateur_el.find(f"{ns}role")
                if role_el is not None and role_el.text:
                    fl = role_el.text.strip().lower()
                    if "ministre" in fl:
                        fonction = "ministre"
                    elif "rapporteur" in fl:
                        fonction = "rapporteur"
                    elif "président" in fl:
                        fonction = "president"
                    else:
                        fonction = "senateur"
            elif interv_el.get("orateur"):
                orateur = interv_el.get("orateur")

            # Decode HTML entities in orateur name
            if orateur:
                orateur = html.unescape(orateur)

            if not orateur:
                continue

            content = ET.tostring(interv_el, encoding="unicode", method="text").strip()
            # Decode HTML entities (&#233; → é, etc.)
            content = html.unescape(content)
            content = re.sub(r"\s+", " ", content)

            if content and len(content) > 30:
                if not fonction:
                    fonction = detect_speaker_function(content, orateur)
                interventions.append({
                    "orateur": orateur,
                    "fonction": fonction,
                    "contenu": content[:10000],
                    "articles_mentionnes": extract_article_mentions(content),
                })

        if interventions:
            return interventions

    # --- Attempt 2: text-based speaker detection (fallback) ---
    speaker_pattern = re.compile(
        r"((?:M\.|Mme|Mlle)\s+"
        r"(?:l[ea]\s+(?:président(?:e)?|ministre|secrétaire|rapporteur)"
        r"|(?:[A-ZÀ-Ý][a-zà-ÿ]+|d'[A-ZÀ-Ý][a-zà-ÿ]+|(?:de|du|le|la|des)\s+[A-ZÀ-Ý])[a-zà-ÿ]*"
        r"(?:-[A-ZÀ-Ý][a-zà-ÿ]+)*"
        r"(?:\s+(?:[A-ZÀ-Ý][a-zà-ÿ]+(?:-[A-ZÀ-Ý][a-zà-ÿ]+)*|d'[A-ZÀ-Ý][a-zà-ÿ]+|(?:de|du|le|la|des)))*"
        r"))"
        r"(?:\s*\([^)]+\))?"
        r"(?:\s*,\s*([^\.]+))?"
        r"\.\s*[-\u2013\u2014]?\s*"
    )

    matches = list(speaker_pattern.finditer(all_text))

    for idx, match in enumerate(matches):
        speaker = match.group(1).strip()
        # Extract first speaker if multi ("M. X et M. Y")
        multi = re.match(
            r'^((?:M\.|Mme|Mlle)\s+[A-ZÀ-Ý][a-zà-ÿ-]+(?:\s+[A-ZÀ-Ý][a-zà-ÿ-]+)*)\s+et\s+',
            speaker
        )
        if multi:
            speaker = multi.group(1).strip()
        function_str = match.group(2).strip() if match.group(2) else None

        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else min(start + 5000, len(all_text))
        content = all_text[start:end].strip()
        content = re.sub(r"\s+", " ", content)

        if content and len(content) > 30:
            fonction = None
            if function_str:
                fl = function_str.lower()
                if "ministre" in fl:
                    fonction = "ministre"
                elif "rapporteur" in fl:
                    fonction = "rapporteur"
                elif "président" in fl:
                    fonction = "president"
                else:
                    fonction = detect_speaker_function(content, speaker)
            else:
                fonction = detect_speaker_function(content, speaker)

            interventions.append({
                "orateur": speaker,
                "fonction": fonction,
                "contenu": content[:10000],
                "articles_mentionnes": extract_article_mentions(content),
            })

    return interventions


def import_amendments_from_nosdeputes(
    session: Session,
    nosdeputes_dir: Path,
) -> int:
    """Import amendments from NosDéputés.fr JSON files (legislature 13).

    NosDéputés.fr provides amendments for legislature 13 (2007-2012) as
    JSON files per text number. Each file contains an ``amendements`` array
    with individual amendment objects.

    Args:
        session: Database session
        nosdeputes_dir: Directory containing NosDéputés JSON files
                        (e.g., ``data/raw/nosdeputes/leg13/``)

    Returns:
        Number of amendments imported
    """
    if not nosdeputes_dir.exists():
        logger.warning(f"NosDéputés directory not found: {nosdeputes_dir}")
        return 0

    json_files = sorted(nosdeputes_dir.glob("text_*.json"))
    if not json_files:
        logger.warning(f"No NosDéputés JSON files found in {nosdeputes_dir}")
        return 0

    logger.info(f"Found {len(json_files)} NosDéputés amendment files")

    # Find AN dossier for legislature 13
    dossiers_list = session.query(DossierLegislatif).filter_by(chambre="AN").all()
    default_dossier = dossiers_list[0] if dossiers_list else None

    if not default_dossier:
        logger.warning("No AN dossier found, skipping NosDéputés import")
        return 0

    # Get articles for linking
    articles = session.query(Article).all()

    # Build article lookup index for O(1) access
    articles_by_normalized_num: dict[str, list[Article]] = {}
    for art in articles:
        norm_num = normalize_article_ref(art.numero_article)
        if norm_num not in articles_by_normalized_num:
            articles_by_normalized_num[norm_num] = []
        articles_by_normalized_num[norm_num].append(art)

    # Pre-load existing references for dedup
    existing_refs = set(
        row[0]
        for row in session.query(Amendement.reference_externe).filter(
            Amendement.reference_externe.isnot(None)
        ).all()
    )

    count = 0

    # NosDéputés sort value mapping
    sort_map = {
        "adopté": "adopte",
        "rejeté": "rejete",
        "retiré": "retire",
        "tombé": "tombe",
        "non soutenu": "non_soutenu",
        "irrecevable": "rejete",
    }

    for json_file in json_files:
        try:
            with open(json_file, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Error reading {json_file.name}: {e}")
            continue

        metadata = data.get("_metadata", {})
        text_number = metadata.get("text_number", json_file.stem.replace("text_", ""))
        label = metadata.get("label", f"text_{text_number}")
        law_type = metadata.get("law_type", "")

        amendements = data.get("amendements", [])
        if not amendements:
            continue

        for amend_wrapper in amendements:
            # NosDéputés wraps each amendment in {"amendement": {...}}
            amend = amend_wrapper.get("amendement", amend_wrapper)

            numero = amend.get("numero", "")
            ref_externe = f"nosdeputes_leg13_{text_number}_{numero}"

            if ref_externe in existing_refs:
                continue

            # Parse sort
            sort_raw = amend.get("sort", "")
            sort_val = sort_map.get(sort_raw.lower().strip(), sort_raw.lower() if sort_raw else None)

            # Parse date
            date_depot = None
            date_str = amend.get("date", "")
            if date_str:
                try:
                    date_depot = datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    pass

            # Match article using O(1) dict lookup
            article_id = None
            article_ref = amend.get("sujet", "") or amend.get("article", "")
            if article_ref:
                norm_ref = normalize_article_ref(article_ref)
                matched_articles = articles_by_normalized_num.get(norm_ref, [])
                if matched_articles:
                    article_id = matched_articles[0].id

            signataires = amend.get("signataires", "") or amend.get("auteur", "")
            objet = amend.get("expose", "") or amend.get("objet", "")
            contenu = amend.get("texte", "") or amend.get("contenu", "")

            db_amend = Amendement(
                dossier_id=default_dossier.id,
                article_id=article_id,
                numero=str(numero),
                auteur=signataires[:500] if signataires else None,
                objet=objet,
                contenu=contenu,
                expose_motifs=amend.get("expose", ""),
                sort=sort_val,
                date_depot=date_depot,
                url_source=amend.get("url_nosdeputes"),
                reference_externe=ref_externe,
            )
            session.add(db_amend)
            existing_refs.add(ref_externe)
            count += 1

        if count % 500 == 0 and count > 0:
            session.commit()
            logger.info(f"Imported {count} NosDéputés amendments...")

    session.commit()
    logger.info(f"NosDéputés import complete: {count} amendments from {len(json_files)} files")
    return count


def import_amendments_from_ameli(
    session: Session,
    ameli_dir: Path,
) -> int:
    """Import amendments from AMELI parsed JSON files (Sénat, 2001-2026).

    The AMELI parser (``src/parsers/ameli_parser.py``) extracts amendments
    from the Sénat PostgreSQL dump into per-text JSON files.

    Args:
        session: Database session
        ameli_dir: Directory containing parsed AMELI JSON files
                   (e.g., ``data/raw/senat/ameli/amendments/``)

    Returns:
        Number of amendments imported
    """
    if not ameli_dir.exists():
        logger.warning(f"AMELI amendments directory not found: {ameli_dir}")
        return 0

    json_files = sorted(ameli_dir.glob("text_*.json"))
    if not json_files:
        logger.warning(f"No AMELI JSON files found in {ameli_dir}")
        return 0

    logger.info(f"Found {len(json_files)} AMELI amendment files")

    # Find SENAT dossier
    dossiers_list = session.query(DossierLegislatif).filter_by(chambre="SENAT").all()
    default_dossier = dossiers_list[0] if dossiers_list else None

    if not default_dossier:
        # Fallback to any dossier
        default_dossier = session.query(DossierLegislatif).first()
        if not default_dossier:
            logger.warning("No dossier found, skipping AMELI import")
            return 0

    # Get articles for linking
    articles = session.query(Article).all()

    # Build article lookup index for O(1) access
    articles_by_normalized_num: dict[str, list[Article]] = {}
    for art in articles:
        norm_num = normalize_article_ref(art.numero_article)
        if norm_num not in articles_by_normalized_num:
            articles_by_normalized_num[norm_num] = []
        articles_by_normalized_num[norm_num].append(art)

    # Pre-load existing references for dedup
    existing_refs = set(
        row[0]
        for row in session.query(Amendement.reference_externe).filter(
            Amendement.reference_externe.isnot(None)
        ).all()
    )

    sort_map = {
        "adopte": "adopte",
        "rejete": "rejete",
        "retire": "retire",
        "tombe": "tombe",
        "non_soutenu": "non_soutenu",
    }

    count = 0

    for json_file in json_files:
        try:
            with open(json_file, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Error reading {json_file.name}: {e}")
            continue

        amendments = data.get("amendments", [])
        if not amendments:
            continue

        for amend in amendments:
            ref_externe = amend.get("reference_externe", "")
            if ref_externe in existing_refs:
                continue

            sort_raw = amend.get("sort", "")
            sort_val = sort_map.get(sort_raw, sort_raw if sort_raw else None)

            date_depot = None
            date_str = amend.get("date_depot", "")
            if date_str:
                try:
                    date_depot = datetime.strptime(date_str[:10], "%Y-%m-%d")
                except (ValueError, IndexError):
                    pass

            # Match article using O(1) dict lookup
            article_id = None
            article_ref = amend.get("article_ref", "")
            if article_ref:
                norm_ref = normalize_article_ref(article_ref)
                matched_articles = articles_by_normalized_num.get(norm_ref, [])
                if matched_articles:
                    article_id = matched_articles[0].id

            auteur = amend.get("auteur", "")
            objet = amend.get("objet", "")
            dispositif = amend.get("dispositif", "")

            db_amend = Amendement(
                dossier_id=default_dossier.id,
                article_id=article_id,
                numero=str(amend.get("numero", "")),
                auteur=auteur[:500] if auteur else None,
                objet=objet,
                contenu=dispositif,
                expose_motifs=objet,
                sort=sort_val,
                date_depot=date_depot,
                url_source=None,
                reference_externe=ref_externe,
            )
            session.add(db_amend)
            if ref_externe:
                existing_refs.add(ref_externe)
            count += 1

        if count % 1000 == 0 and count > 0:
            session.commit()
            logger.info(f"Imported {count} AMELI amendments...")

    session.commit()
    logger.info(f"AMELI import complete: {count} amendments from {len(json_files)} files")
    return count


def import_amendments_from_an_html(
    session: Session,
    an_html_dir: Path,
) -> int:
    """Import amendments from AN HTML scraper output (legislatures 12-13).

    Reads JSON files produced by ``src/collectors/an_html_amendments.py``.
    Scans ``an_html_dir/leg{12,13}/text_*.json``.

    Args:
        session: Database session
        an_html_dir: Base directory (e.g., ``data/raw/an_html_amendments/``)

    Returns:
        Number of amendments imported
    """
    if not an_html_dir.exists():
        logger.warning(f"AN HTML amendments directory not found: {an_html_dir}")
        return 0

    # Collect JSON files from all legislature subdirectories
    json_files = []
    for leg_dir in sorted(an_html_dir.iterdir()):
        if leg_dir.is_dir() and leg_dir.name.startswith("leg"):
            json_files.extend(sorted(leg_dir.glob("text_*.json")))

    if not json_files:
        logger.warning(f"No AN HTML JSON files found in {an_html_dir}")
        return 0

    logger.info(f"Found {len(json_files)} AN HTML amendment files")

    # Find AN dossier
    dossiers_list = session.query(DossierLegislatif).filter_by(chambre="AN").all()
    default_dossier = dossiers_list[0] if dossiers_list else None

    if not default_dossier:
        default_dossier = session.query(DossierLegislatif).first()
        if not default_dossier:
            logger.warning("No dossier found, skipping AN HTML import")
            return 0

    # Get articles for linking
    articles = session.query(Article).all()

    # Build article lookup index for O(1) access
    articles_by_normalized_num: dict[str, list[Article]] = {}
    for art in articles:
        norm_num = normalize_article_ref(art.numero_article)
        if norm_num not in articles_by_normalized_num:
            articles_by_normalized_num[norm_num] = []
        articles_by_normalized_num[norm_num].append(art)

    # Pre-load existing references for dedup
    existing_refs = set(
        row[0]
        for row in session.query(Amendement.reference_externe).filter(
            Amendement.reference_externe.isnot(None)
        ).all()
    )

    count = 0

    for json_file in json_files:
        try:
            with open(json_file, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Error reading {json_file.name}: {e}")
            continue

        amendments = data.get("amendments", [])
        if not amendments:
            continue

        for amend in amendments:
            ref_externe = amend.get("reference_externe", "")
            if not ref_externe or ref_externe in existing_refs:
                continue

            sort_val = amend.get("sort")  # Already normalized by collector

            # Parse date (dd/mm/yyyy from search results)
            date_depot = None
            date_str = amend.get("date_depot", "")
            if date_str:
                for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                    try:
                        date_depot = datetime.strptime(date_str[:10], fmt)
                        break
                    except (ValueError, IndexError):
                        continue

            # Match article using O(1) dict lookup
            article_id = None
            article_ref = amend.get("article_ref", "")
            if article_ref:
                norm_ref = normalize_article_ref(article_ref)
                matched_articles = articles_by_normalized_num.get(norm_ref, [])
                if matched_articles:
                    article_id = matched_articles[0].id

            auteur = amend.get("auteur", "")
            objet = amend.get("objet", "") or amend.get("expose_sommaire", "")
            contenu = amend.get("dispositif", "")

            db_amend = Amendement(
                dossier_id=default_dossier.id,
                article_id=article_id,
                numero=str(amend.get("numero", "")),
                auteur=auteur[:500] if auteur else None,
                objet=objet,
                contenu=contenu,
                expose_motifs=objet,
                sort=sort_val,
                date_depot=date_depot,
                url_source=amend.get("detail_url"),
                reference_externe=ref_externe,
            )
            session.add(db_amend)
            existing_refs.add(ref_externe)
            count += 1

        if count % 1000 == 0 and count > 0:
            session.commit()
            logger.info(f"Imported {count} AN HTML amendments...")

    session.commit()
    logger.info(f"AN HTML import complete: {count} amendments from {len(json_files)} files")
    return count


# Backward compatibility alias
import_amendments_from_an_leg12 = import_amendments_from_an_html


def _prefilter_amendment_file(args: tuple) -> list[dict] | None:
    """Worker function: read JSON, filter, parse amendment in a subprocess.

    Returns a list of serialised amendment dicts or None if not relevant.
    """
    json_file_str, known_law_numeros_list, is_senat = args
    json_file = Path(json_file_str)

    try:
        with open(json_file, encoding="utf-8") as f:
            raw_data = json.load(f)

        text_ref = ""
        if "amendement" in raw_data:
            text_ref = raw_data["amendement"].get("texteLegislatifRef", "")

        # Inline filter logic (avoid importing module-level objects in subprocess)
        finance_refs = ["BTC", "PLF", "PLFSS", "PLFR"]
        if not text_ref:
            return None
        matched = any(ref in text_ref for ref in finance_refs)
        if not matched:
            # Check regex patterns
            import re as _re
            _patterns = [
                _re.compile(r"\b\d{4}-\d{3,4}\b"),
                _re.compile(r"\b(?:PJL|PRJL)", _re.IGNORECASE),
                _re.compile(r"\bNOR\w+", _re.IGNORECASE),
            ]
            matched = any(p.search(text_ref) for p in _patterns)
        if not matched and known_law_numeros_list:
            matched = any(n in text_ref for n in known_law_numeros_list)
        if not matched:
            return None

        # Parse the amendment file
        from src.parsers.amendments import parse_amendment_file as _parse
        amendments = _parse(json_file)

        results = []
        for amend in amendments:
            results.append({
                "reference_externe": amend.reference_externe,
                "numero": amend.numero,
                "auteur": amend.auteur,
                "objet": amend.objet,
                "contenu": amend.contenu,
                "expose_motifs": amend.expose_motifs,
                "sort": amend.sort,
                "date_depot": amend.date_depot.isoformat() if amend.date_depot else None,
                "article_ref": amend.article_ref,
                "texte_legislatif_ref": text_ref,
            })
        return results if results else None
    except Exception:
        return None


def import_amendments_from_files(
    session: Session,
    raw_data_dir: Path,
    dossiers: dict[str, DossierLegislatif],
    articles: list[Article],
    num_workers: int = 8,
) -> int:
    """
    Import amendments from AN and Sénat data files.

    Only imports amendments related to finance law texts.
    Uses parallel workers for reading/filtering JSON files.

    Args:
        session: Database session
        raw_data_dir: Root raw data directory
        dossiers: Dict mapping reference to DossierLegislatif
        articles: List of all articles for linking
        num_workers: Number of parallel workers for file reading

    Returns:
        Number of amendments imported
    """
    count = 0

    # Build article lookup index for O(1) access instead of O(n) iteration
    # This optimization reduces amendment-article linking from ~6 minutes to <1 second
    articles_by_normalized_num: dict[str, list[Article]] = {}
    for art in articles:
        norm_num = normalize_article_ref(art.numero_article)
        if norm_num not in articles_by_normalized_num:
            articles_by_normalized_num[norm_num] = []
        articles_by_normalized_num[norm_num].append(art)

    # Get default dossiers per chamber
    default_dossiers = {}
    for dossier in dossiers.values():
        if dossier.chambre not in default_dossiers:
            default_dossiers[dossier.chambre] = dossier

    if not default_dossiers:
        logger.warning("No dossiers found, skipping amendment import")
        return 0

    # Build texteLegislatifRef → dossier mapping for precise dossier assignment
    # AN refs like PIONANR5L16BTC0617 contain the text number; we group
    # amendments by ref and try to find the matching dossier for each ref.
    _text_ref_to_dossier: dict[str, DossierLegislatif] = {}

    # Pre-load all lois to avoid N+1 query problem (3.7M potential queries!)
    # This single query replaces millions of individual session.query(Loi).get() calls
    all_lois = session.query(Loi).all()
    all_lois_by_id = {loi.id: loi for loi in all_lois}

    def _resolve_dossier_for_text_ref(
        text_ref: str, chambre: str
    ) -> DossierLegislatif:
        """Find the correct dossier for a texteLegislatifRef, with caching."""
        if text_ref in _text_ref_to_dossier:
            return _text_ref_to_dossier[text_ref]

        # Try matching via existing dossier reference_externe
        for dossier in dossiers.values():
            if dossier.reference_externe and text_ref in dossier.reference_externe:
                _text_ref_to_dossier[text_ref] = dossier
                return dossier

        # Try extracting law year from text_ref to match loi
        # Patterns: PIONANR5L16BTC0617, PRJLANR5L14BTC3904
        import re as _re
        year_match = _re.search(r"L(\d{2})", text_ref)
        if year_match:
            leg_num = int(year_match.group(1))
            # Legislature → approximate year mapping
            leg_start = {12: 2002, 13: 2007, 14: 2012, 15: 2017, 16: 2022, 17: 2024}
            start_year = leg_start.get(leg_num)
            if start_year:
                # Find lois in this legislature's timeframe
                for dossier in dossiers.values():
                    if dossier.chambre != chambre:
                        continue
                    # O(1) dict lookup instead of SQL query - fixes N+1 problem
                    loi = all_lois_by_id.get(dossier.loi_id)
                    if loi and loi.date_promulgation:
                        loi_year = loi.date_promulgation.year
                        # Check if text_ref contains a text number that matches
                        # the dossier's reference pattern
                        if dossier.reference_externe and text_ref and (
                            text_ref in (dossier.reference_externe or "")
                        ):
                            _text_ref_to_dossier[text_ref] = dossier
                            return dossier

        # Fallback to default
        fallback = default_dossiers.get(chambre) or list(default_dossiers.values())[0]
        _text_ref_to_dossier[text_ref] = fallback
        return fallback

    # Reuse already loaded lois instead of querying again
    known_law_numeros = {loi.numero for loi in all_lois if loi.numero}
    known_law_numeros_list = list(known_law_numeros)

    # Pre-load existing reference_externe set for fast dedup
    logger.info("Loading existing amendment references for deduplication...")
    existing_refs = set(
        row[0]
        for row in session.query(Amendement.reference_externe).filter(
            Amendement.reference_externe.isnot(None)
        ).all()
    )
    logger.info(f"Loaded {len(existing_refs)} existing amendment references")

    # Find amendment directories
    amend_dirs = []

    # AN amendments
    for d in raw_data_dir.glob("assemblee/amendements_leg*"):
        amend_dirs.append(("AN", d))

    # Sénat amendments (AMELI)
    senat_ameli = raw_data_dir / "senat" / "ameli"
    if senat_ameli.exists():
        amend_dirs.append(("SENAT", senat_ameli))

    for chambre, amend_dir in amend_dirs:
        default_dossier = default_dossiers.get(chambre) or list(default_dossiers.values())[0]
        is_senat = chambre == "SENAT"

        json_files = [
            f for f in amend_dir.rglob("*.json")
            if f.stat().st_size < 50_000_000  # Skip monolithic files (>50MB)
        ]
        logger.info(
            f"Processing {len(json_files)} amendment files in {amend_dir} "
            f"with {num_workers} workers"
        )

        # Prepare worker args
        worker_args = [
            (str(f), known_law_numeros_list, is_senat)
            for f in json_files
        ]

        # Process files in parallel, insert sequentially
        processed = 0
        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            for result in executor.map(
                _prefilter_amendment_file, worker_args, chunksize=256
            ):
                processed += 1

                if result is None:
                    if processed % 10000 == 0:
                        logger.info(
                            f"Processed {processed}/{len(json_files)} files, "
                            f"{count} amendments..."
                        )
                    continue

                for amend_data in result:
                    ref = amend_data["reference_externe"]
                    if ref and ref in existing_refs:
                        continue

                    # Resolve the correct dossier from texteLegislatifRef
                    text_leg_ref = amend_data.get("texte_legislatif_ref", "")
                    if text_leg_ref:
                        resolved_dossier = _resolve_dossier_for_text_ref(text_leg_ref, chambre)
                    else:
                        resolved_dossier = default_dossier

                    # Find matching article using O(1) dict lookup instead of O(n) iteration
                    article_id = None
                    if amend_data["article_ref"]:
                        norm_ref = normalize_article_ref(amend_data["article_ref"])
                        matched_articles = articles_by_normalized_num.get(norm_ref, [])
                        if matched_articles:
                            article_id = matched_articles[0].id

                    db_amend = Amendement(
                        dossier_id=resolved_dossier.id,
                        article_id=article_id,
                        numero=amend_data["numero"],
                        auteur=amend_data["auteur"],
                        objet=amend_data["objet"],
                        contenu=amend_data["contenu"],
                        expose_motifs=amend_data["expose_motifs"],
                        sort=amend_data["sort"],
                        date_depot=(
                            datetime.fromisoformat(amend_data["date_depot"])
                            if amend_data["date_depot"]
                            else None
                        ),
                        url_source=None,
                        reference_externe=ref,
                    )
                    session.add(db_amend)
                    if ref:
                        existing_refs.add(ref)
                    count += 1

                # Commit periodically
                if processed % 5000 == 0:
                    session.commit()
                    logger.info(
                        f"Processed {processed}/{len(json_files)} files, "
                        f"{count} amendments..."
                    )

        session.commit()
        logger.info(
            f"Finished {amend_dir.name}: {processed} files processed, "
            f"{count} amendments imported"
        )

    return count


def import_laws_from_reference(
    session: Session,
    reference_file: Path | None = None,
    laws_data: list[dict] | None = None,
) -> list[Loi]:
    """
    Import all laws from a reference file or a pre-loaded list.

    Args:
        session: Database session
        reference_file: Path to laws reference JSON (used if laws_data is None)
        laws_data: Pre-loaded list of law dicts (takes priority over file)

    Returns:
        List of imported Loi instances
    """
    if laws_data is None:
        with open(reference_file, encoding="utf-8") as f:
            data = json.load(f)
        laws_data = data["lois"]

    laws = []
    for loi_data in laws_data:
        loi = import_loi(session, loi_data)
        laws.append(loi)

        # Create dossiers from reference URLs
        if loi_data.get("dossier_an_url"):
            import_dossier(
                session, loi, "AN",
                loi_data["dossier_an_url"],
                loi_data.get("jorf")
            )

        if loi_data.get("dossier_senat_url"):
            import_dossier(
                session, loi, "SENAT",
                loi_data["dossier_senat_url"],
                loi_data.get("jorf")
            )

    session.commit()
    return laws


def run_import(
    reference_file: Path,
    raw_data_dir: Path,
    reset_db: bool = False,
    laws_data: list[dict] | None = None,
    skip_existing: bool = False,
) -> dict[str, int]:
    """
    Run the full data import pipeline.

    Args:
        reference_file: Path to laws reference JSON (used if laws_data is None)
        raw_data_dir: Directory containing raw collected data
        reset_db: If True, drop and recreate tables
        laws_data: Pre-loaded list of law dicts (takes priority over file)

    Returns:
        Dict with counts of imported items
    """
    from src.db.models import reset_db as do_reset_db

    if reset_db:
        logger.warning("Resetting database...")
        do_reset_db()
    else:
        init_db()

    counts = {
        "lois": 0,
        "articles": 0,
        "dossiers": 0,
        "debats": 0,
        "interventions": 0,
        "amendements": 0,
    }

    with get_session() as session:
        try:
            existing_law_numeros_before: set[str] = set()
            if skip_existing:
                existing_law_numeros_before = {
                    row[0] for row in session.query(Loi.numero).all()
                }

            # Step 1: Import laws from reference
            logger.info("Step 1: Importing laws from reference file...")
            laws = import_laws_from_reference(session, reference_file, laws_data=laws_data)
            counts["lois"] = len(laws)
            counts["dossiers"] = session.query(DossierLegislatif).count()
            session.commit()
            new_laws = (
                [loi for loi in laws if loi.numero not in existing_law_numeros_before]
                if skip_existing else []
            )
            if skip_existing:
                logger.info(
                    f"Skip-existing mode: {len(new_laws)} new laws, "
                    f"{len(laws) - len(new_laws)} already present"
                )

            # Step 2: Import articles from Légifrance JSON
            logger.info("Step 2: Importing articles from Légifrance...")
            legifrance_dir = raw_data_dir / "legifrance"
            all_articles = []
            if legifrance_dir.exists():
                laws_to_import = laws
                if skip_existing:
                    existing_article_loi_ids = {
                        row[0] for row in session.query(Article.loi_id).distinct().all()
                    }
                    laws_to_import = [
                        loi for loi in laws if loi.id not in existing_article_loi_ids
                    ]
                    logger.info(
                        f"Step 2 fast-skip: {len(laws) - len(laws_to_import)} laws already "
                        f"have articles, importing {len(laws_to_import)} missing laws"
                    )

                for loi in laws_to_import:
                    articles = import_articles_from_legifrance(session, loi, legifrance_dir)
                    all_articles.extend(articles)
                session.commit()

            if skip_existing:
                # Ensure article lookup remains complete for downstream steps.
                all_articles = session.query(Article).all()

            counts["articles"] = len(all_articles)
            logger.info(f"Imported {counts['articles']} articles")

            # Step 3: Build dossier lookup
            logger.info("Step 3: Building dossier lookup...")
            dossiers = {}
            for dossier in session.query(DossierLegislatif).all():
                if dossier.reference_externe:
                    dossiers[dossier.reference_externe] = dossier
                dossiers[f"{dossier.chambre}_{dossier.loi_id}"] = dossier

            # Step 4: Import debates from AN
            logger.info("Step 4: Importing debates from AN...")
            an_data_dir = raw_data_dir / "assemblee"
            if an_data_dir.exists():
                has_existing_debates = session.query(Debat.id).limit(1).first() is not None
                if skip_existing and not new_laws and has_existing_debates:
                    logger.info(
                        "Step 4 fast-skip: debates already present and no new laws added; "
                        "skipping AN debate import"
                    )
                else:
                    debats, intervs = import_debates_from_an(session, an_data_dir, dossiers)
                    counts["debats"] = debats
                    counts["interventions"] = intervs
                    logger.info(f"Imported {debats} debates with {intervs} interventions")

            # Step 5: Import amendments
            logger.info("Step 5: Importing amendments...")
            if raw_data_dir.exists():
                has_existing_amendements = session.query(Amendement.id).limit(1).first() is not None
                if skip_existing and not new_laws and has_existing_amendements:
                    logger.info(
                        "Step 5 fast-skip: amendments already present and no new laws added; "
                        "skipping amendment import"
                    )
                else:
                    counts["amendements"] = import_amendments_from_files(
                        session, raw_data_dir, dossiers, all_articles
                    )
                    logger.info(f"Imported {counts['amendements']} amendments")

            session.commit()
            logger.info(f"Import complete: {counts}")

        except Exception as e:
            session.rollback()
            logger.error(f"Import failed: {e}")
            raise

    return counts
