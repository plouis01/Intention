#!/usr/bin/env python3
"""Fix amendement-to-article linkage by re-parsing raw files from ALL sources.

Supports 3 amendment data sources:
  - AMANR: Individual JSON files (legislatures 14-17)
  - AN-HTML: Aggregate JSON files per text (legislatures 12-13)
  - AMELI: Aggregate JSON files per text (Sénat, 2001-2026)

Strategy:
  1. Build a multi-source index: reference_externe → (article_ref, loi_id)
  2. Fix dossier_id FIRST (import assigned all to a single default dossier)
  3. Then match article_id against the correct loi's articles
  4. Batch SQL updates
"""

import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from src.db.models import Amendement, Article, DossierLegislatif, Loi, get_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

RAW_DIR = Path(__file__).parent.parent / "data" / "raw"
CONCORDANCE_PATH = Path(__file__).parent.parent / "data" / "concordance_articles.json"


# ---------------------------------------------------------------------------
# Article normalization & matching (kept from original)
# ---------------------------------------------------------------------------

def normalize_article_ref(ref: str) -> str | None:
    """Normalize article reference to match DB format.

    Returns None for "APRÈS/AVANT L'ARTICLE" patterns (insertion amendments)
    since they don't target the referenced article directly.

    Examples:
        'ARTICLE PREMIER' -> '1'
        'ARTICLE 1ER EB' -> '1 EB'
        'ART. 12' -> '12'
        'Article liminaire' -> 'liminaire'
        'Article 2' -> '2'
        "APRÈS L'ARTICLE 21, insérer..." -> None (insertion, not a target)
        "AVANT L'ARTICLE 16" -> None (insertion, not a target)
    """
    if not ref:
        return None

    ref = ref.strip()

    # Reject "APRÈS/AVANT L'ARTICLE X" - these are insertion amendments that
    # do NOT modify the referenced article. Linking them would be a false positive.
    if re.match(r"(?:APR[ÈE]S|AVANT)\s+L", ref, flags=re.IGNORECASE):
        return None

    ref = re.sub(r'\bPREMIER\b|\bPremier\b|\bpremier\b', '1', ref)
    ref = re.sub(r'(\d+)(ER|er|E|e|EME|eme|ème)\b', r'\1', ref)
    ref = re.sub(r'^(ARTICLE|Article|ART\.?)\s*', '', ref, flags=re.IGNORECASE)
    ref = re.sub(r'\s+', ' ', ref).strip()

    return ref if ref else None


def build_article_lookup(session) -> dict[int, dict[str, int]]:
    """Build lookup: loi_id -> {normalized_numero -> article_id}."""
    articles = session.query(Article.id, Article.loi_id, Article.numero_article).all()
    lookup: dict[int, dict[str, int]] = {}

    for art_id, loi_id, numero_article in articles:
        if loi_id not in lookup:
            lookup[loi_id] = {}

        key = numero_article.strip().lower()
        lookup[loi_id][key] = art_id

        simple_key = re.sub(r'[^a-z0-9]', '', key)
        if simple_key != key:
            lookup[loi_id][simple_key] = art_id

    return lookup


def find_matching_article(
    article_ref: str,
    loi_id: int,
    article_lookup: dict[int, dict[str, int]],
) -> int | None:
    """Find matching article_id for a given article_ref within a specific loi."""
    if not article_ref:
        return None

    normalized = normalize_article_ref(article_ref)
    if not normalized:
        return None

    key = normalized.lower()
    simple_key = re.sub(r'[^a-z0-9]', '', key)

    loi_articles = article_lookup.get(loi_id, {})

    # Exact match
    if key in loi_articles:
        return loi_articles[key]

    # Simplified match
    if simple_key in loi_articles:
        return loi_articles[simple_key]

    # Fuzzy match
    for art_key, art_id in loi_articles.items():
        if simple_key == re.sub(r'[^a-z0-9]', '', art_key):
            return art_id
        if art_key.startswith(key + " ") or art_key == key:
            return art_id

    return None


# ---------------------------------------------------------------------------
# Concordance table (PLF article → final law article)
# ---------------------------------------------------------------------------

def _load_concordance(all_lois: dict[int, object]) -> dict[tuple[int, str], str | None]:
    """Load concordance table and build lookup: (loi_id, normalized_navette) -> definitif_ref.

    Returns empty dict if concordance file doesn't exist.
    Value is None for suppressed articles.
    """
    if not CONCORDANCE_PATH.exists():
        logger.info("No concordance file found, skipping article translation")
        return {}

    with open(CONCORDANCE_PATH, encoding="utf-8") as f:
        data = json.load(f)

    # Build reverse lookup: loi_numero -> loi_id
    numero_to_id = {loi.numero: loi_id for loi_id, loi in all_lois.items()}

    lookup: dict[tuple[int, str], str | None] = {}
    loi_count = 0

    for loi_numero, entry in data.items():
        loi_id = numero_to_id.get(loi_numero)
        if not loi_id:
            continue

        loi_count += 1
        for mapping in entry.get("articles", []):
            navette = mapping.get("navette", "")
            definitif = mapping.get("definitif")  # None = suppressed

            if not navette:
                continue

            # Normalize the navette ref the same way we normalize amendment article_ref
            nav_normalized = normalize_article_ref(navette)
            if nav_normalized:
                key = (loi_id, nav_normalized.lower())
                lookup[key] = definitif

    logger.info(f"Concordance loaded: {loi_count} laws, {len(lookup)} article mappings")
    return lookup


# ---------------------------------------------------------------------------
# Loi resolution helpers
# ---------------------------------------------------------------------------

def _build_loi_lookups(session) -> tuple[
    dict[int, Loi],
    dict[tuple[int, str], Loi],
    dict[int, DossierLegislatif],
    dict[int, DossierLegislatif],
    list[DossierLegislatif],
    dict[int, int],
]:
    """Build various loi/dossier lookup structures.

    Returns:
        - all_lois: id -> Loi
        - year_type_to_loi: (annee, type) -> Loi
        - loi_to_an_dossier: loi_id -> first AN DossierLegislatif
        - loi_to_senat_dossier: loi_id -> first SENAT DossierLegislatif
        - all_dossiers: list of all DossierLegislatif
        - dossier_to_loi: dossier_id -> loi_id
    """
    all_lois = {loi.id: loi for loi in session.query(Loi).all()}
    all_dossiers = session.query(DossierLegislatif).all()

    year_type_to_loi: dict[tuple[int, str], Loi] = {}
    for loi in all_lois.values():
        if loi.type in ("LF", "LFR", "LFSS"):
            key = (loi.annee, loi.type)
            if key not in year_type_to_loi:
                year_type_to_loi[key] = loi

    loi_to_an_dossier: dict[int, DossierLegislatif] = {}
    loi_to_senat_dossier: dict[int, DossierLegislatif] = {}
    dossier_to_loi: dict[int, int] = {}
    for d in all_dossiers:
        dossier_to_loi[d.id] = d.loi_id
        if d.chambre == "AN" and d.loi_id not in loi_to_an_dossier:
            loi_to_an_dossier[d.loi_id] = d
        if d.chambre == "SENAT" and d.loi_id not in loi_to_senat_dossier:
            loi_to_senat_dossier[d.loi_id] = d

    return (
        all_lois, year_type_to_loi, loi_to_an_dossier,
        loi_to_senat_dossier, all_dossiers, dossier_to_loi,
    )


def _resolve_loi_from_titre(titre: str, year_type_to_loi: dict) -> Loi | None:
    """Resolve loi from AMELI txt_titre or txt_doslegsignet.

    Handles patterns like:
        'Projet de loi de finances pour 2018' -> LF 2018
        'PLF pour 2020' -> LF 2020
        'Budget pour 2011' -> LF 2011
        'Finances pour 2010' -> LF 2010
        'loi de finances pour 2022' -> LF 2022
    """
    if not titre:
        return None

    titre_lower = titre.lower().strip()

    # Extract year from title
    year_match = re.search(r'(\d{4})', titre_lower)
    if not year_match:
        return None
    year = int(year_match.group(1))

    # Determine law type
    if "rectificati" in titre_lower or "rectif" in titre_lower:
        law_type = "LFR"
    elif "sécurité sociale" in titre_lower or "plfss" in titre_lower:
        law_type = "LFSS"
    elif any(kw in titre_lower for kw in ("finance", "plf", "budget")):
        law_type = "LF"
    else:
        return None

    # For PLF, the year in the title is the budget year (annee)
    # But the annee in DB is the budget year too, so direct match
    return year_type_to_loi.get((year, law_type))


def _resolve_loi_from_doslegsignet(signet: str, year_type_to_loi: dict) -> Loi | None:
    """Resolve loi from AMELI txt_doslegsignet.

    Handles patterns like:
        'pjlf2018' -> LF 2018
        'pjlf2020' -> LF 2020
    """
    if not signet:
        return None

    match = re.match(r'pjlf(\d{4})', signet.lower())
    if match:
        year = int(match.group(1))
        return year_type_to_loi.get((year, "LF"))

    return None


# ---------------------------------------------------------------------------
# Multi-source index builders
# ---------------------------------------------------------------------------

def _build_amanr_index(raw_dir: Path) -> dict[str, tuple[str, str]]:
    """Build AMANR index: reference_externe -> (article_ref, texteLegislatifRef).

    Scans individual JSON files from data/raw/assemblee/amendements_leg*/json/.
    Uses file-level cache with mtime-based invalidation.
    """
    cache_path = raw_dir.parent / "cache" / "amanr_article_index.json"

    if cache_path.exists():
        cache_mtime = cache_path.stat().st_mtime
        stale = False
        for amend_dir in raw_dir.glob("assemblee/amendements_leg*/json"):
            if amend_dir.stat().st_mtime > cache_mtime:
                stale = True
                break
        if not stale:
            logger.info(f"Loading AMANR cache from {cache_path}")
            with open(cache_path, encoding="utf-8") as f:
                raw = json.load(f)
                return {k: (v[0], v[1]) for k, v in raw.items()}
        logger.info("AMANR cache invalidated, rescanning...")

    logger.info("Scanning AMANR individual JSON files...")
    index: dict[str, tuple[str, str]] = {}

    for amend_dir in raw_dir.glob("assemblee/amendements_leg*/json"):
        for jf in amend_dir.rglob("*.json"):
            if jf.stat().st_size > 50_000_000:
                continue
            try:
                with open(jf, encoding="utf-8") as f:
                    data = json.load(f)

                amend = data.get("amendement", {})
                uid = amend.get("uid", "")
                text_ref = amend.get("texteLegislatifRef", "")

                if not uid:
                    continue

                # Extract article ref from pointeurFragmentTexte
                article_ref = None
                pointeur = amend.get("pointeurFragmentTexte", {})
                if isinstance(pointeur, dict):
                    division = pointeur.get("division", pointeur.get("divisionArticle", {}))
                    if isinstance(division, dict):
                        article_ref = (
                            division.get("articleDesignation")
                            or division.get("articleDesignationCourte")
                            or division.get("titre")
                        )

                index[uid] = (article_ref or "", text_ref)

            except Exception:
                continue

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)
    logger.info(f"AMANR index: {len(index)} entries cached")

    return index


def _build_an_html_index(raw_dir: Path) -> dict[str, tuple[str, int, int]]:
    """Build AN-HTML index: reference_externe -> (article_ref, legislature, text_number).

    Scans aggregate JSON files from data/raw/an_html_amendments/leg*/text_*.json.
    """
    cache_path = raw_dir.parent / "cache" / "an_html_article_index.json"

    an_html_dir = raw_dir / "an_html_amendments"
    if not an_html_dir.exists():
        return {}

    if cache_path.exists():
        cache_mtime = cache_path.stat().st_mtime
        stale = any(
            d.stat().st_mtime > cache_mtime
            for d in an_html_dir.iterdir()
            if d.is_dir()
        )
        if not stale:
            logger.info(f"Loading AN-HTML cache from {cache_path}")
            with open(cache_path, encoding="utf-8") as f:
                # JSON stores tuples as lists, convert back
                raw = json.load(f)
                return {k: (v[0], v[1], v[2]) for k, v in raw.items()}
        logger.info("AN-HTML cache invalidated, rescanning...")

    logger.info("Scanning AN-HTML aggregate JSON files...")
    index: dict[str, tuple[str, int, int]] = {}

    for leg_dir in sorted(an_html_dir.iterdir()):
        if not leg_dir.is_dir() or not leg_dir.name.startswith("leg"):
            continue

        for jf in sorted(leg_dir.glob("text_*.json")):
            try:
                with open(jf, encoding="utf-8") as f:
                    data = json.load(f)

                legislature = data.get("legislature", 0)
                text_number = data.get("text_number", 0)

                for amend in data.get("amendments", []):
                    ref_ext = amend.get("reference_externe", "")
                    article_ref = amend.get("article_ref", "")
                    if ref_ext:
                        index[ref_ext] = (article_ref, legislature, text_number)

            except Exception:
                continue

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)
    logger.info(f"AN-HTML index: {len(index)} entries cached")

    return index


def _build_ameli_index(raw_dir: Path) -> dict[str, tuple[str, str, str]]:
    """Build AMELI index: reference_externe -> (article_ref, txt_titre, txt_doslegsignet).

    Scans aggregate JSON files from data/raw/senat/ameli/amendments/text_*.json.
    """
    cache_path = raw_dir.parent / "cache" / "ameli_article_index.json"

    ameli_dir = raw_dir / "senat" / "ameli" / "amendments"
    if not ameli_dir.exists():
        return {}

    if cache_path.exists():
        cache_mtime = cache_path.stat().st_mtime
        stale = ameli_dir.stat().st_mtime > cache_mtime
        if not stale:
            logger.info(f"Loading AMELI cache from {cache_path}")
            with open(cache_path, encoding="utf-8") as f:
                raw = json.load(f)
                return {k: (v[0], v[1], v[2]) for k, v in raw.items()}
        logger.info("AMELI cache invalidated, rescanning...")

    logger.info("Scanning AMELI aggregate JSON files...")
    index: dict[str, tuple[str, str, str]] = {}

    for jf in sorted(ameli_dir.glob("text_*.json")):
        try:
            with open(jf, encoding="utf-8") as f:
                data = json.load(f)

            txt_titre = data.get("txt_titre", "")
            txt_signet = data.get("txt_doslegsignet", "")

            for amend in data.get("amendments", []):
                ref_ext = amend.get("reference_externe", "")
                article_ref = amend.get("article_ref", "")
                if ref_ext:
                    index[ref_ext] = (article_ref, txt_titre, txt_signet)

        except Exception:
            continue

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)
    logger.info(f"AMELI index: {len(index)} entries cached")

    return index


# ---------------------------------------------------------------------------
# AMANR loi resolution (via texteLegislatifRef)
# ---------------------------------------------------------------------------

LEG_YEARS = {12: 2002, 13: 2007, 14: 2012, 15: 2017, 16: 2022, 17: 2024}


def _resolve_text_ref_to_loi(
    text_ref: str,
    year_type_to_loi: dict[tuple[int, str], Loi],
    all_dossiers: list[DossierLegislatif],
    all_lois: dict[int, Loi] | None = None,
    strict: bool = False,
) -> Loi | None:
    """Resolve a texteLegislatifRef to a Loi.

    Args:
        strict: If True, only return a loi when we have an exact match
                (via dossier reference_externe). This avoids false positives
                when the text_ref could map to multiple lois within a legislature.
                Use strict=True for article matching, strict=False for dossier fixing.

    Tries:
      1. Match dossier reference_externe containing the text_ref (exact)
      2. (non-strict only) Parse legislature + law type from ref pattern (fuzzy)
    """
    # Step 1: Try matching via dossier reference_externe (high confidence)
    for d in all_dossiers:
        if d.reference_externe and text_ref in d.reference_externe:
            if all_lois:
                return all_lois.get(d.loi_id)
            for loi in year_type_to_loi.values():
                if loi.id == d.loi_id:
                    return loi

    # In strict mode, don't use fuzzy matching - risk of false positives
    if strict:
        return None

    # Step 2: Fuzzy matching by legislature + law type (for dossier fixing only)
    leg_match = re.search(r"L(\d{2})", text_ref)
    if not leg_match:
        return None
    leg_num = int(leg_match.group(1))
    start_year = LEG_YEARS.get(leg_num)
    if not start_year:
        return None
    end_year = start_year + 5

    text_ref_upper = text_ref.upper()

    if "PLFSS" in text_ref_upper:
        for year in range(start_year, end_year + 1):
            loi = year_type_to_loi.get((year, "LFSS"))
            if loi:
                return loi
    elif "PLFR" in text_ref_upper:
        for year in range(start_year, end_year + 1):
            loi = year_type_to_loi.get((year, "LFR"))
            if loi:
                return loi
    elif "BTC" in text_ref_upper or "PLF" in text_ref_upper:
        for year in range(start_year, end_year + 1):
            loi = year_type_to_loi.get((year, "LF"))
            if loi:
                return loi

    return None


def _build_an_html_text_to_loi(
    raw_dir: Path,
    year_type_to_loi: dict[tuple[int, str], Loi],
) -> dict[tuple[int, int], tuple[int, bool]]:
    """Build (legislature, text_number) -> (loi_id, is_exact) mapping for AN-HTML.

    Uses the earliest amendment deposit date to identify which budget year.
    ``is_exact`` is True when only one possible loi exists for the resolved
    (year, type) pair, making article matching safe.
    """
    from datetime import datetime

    an_html_dir = raw_dir / "an_html_amendments"
    if not an_html_dir.exists():
        return {}

    mapping: dict[tuple[int, int], tuple[int, bool]] = {}

    for leg_dir in sorted(an_html_dir.iterdir()):
        if not leg_dir.is_dir() or not leg_dir.name.startswith("leg"):
            continue

        for jf in sorted(leg_dir.glob("text_*.json")):
            try:
                with open(jf, encoding="utf-8") as f:
                    data = json.load(f)

                legislature = data.get("legislature", 0)
                text_number = data.get("text_number", 0)

                # Collect all amendment dates
                dates = []
                for amend in data.get("amendments", []):
                    date_str = amend.get("date_depot", "")
                    if date_str:
                        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                            try:
                                dt = datetime.strptime(date_str[:10], fmt)
                                dates.append(dt)
                                break
                            except (ValueError, IndexError):
                                continue

                if not dates:
                    continue

                earliest = min(dates)
                month = earliest.month
                year = earliest.year

                # Oct-Dec → next year's PLF; Jan-Sep → current year LFR/LFSS
                if month >= 10:
                    budget_year = year + 1
                    # PLF filed Oct-Dec: this is almost certainly a LF
                    # Check if there's exactly one LF for this budget year
                    loi = year_type_to_loi.get((budget_year, "LF"))
                    if loi:
                        mapping[(legislature, text_number)] = (loi.id, True)
                        continue
                elif month <= 3:
                    # Jan-Mar: could be LF (late filing) or early LFR
                    budget_year = year
                else:
                    # Apr-Sep: likely LFR for current year
                    budget_year = year

                # Fuzzy: try all types, mark as non-exact
                for law_type in ("LF", "LFR", "LFSS"):
                    loi = year_type_to_loi.get((budget_year, law_type))
                    if loi:
                        mapping[(legislature, text_number)] = (loi.id, False)
                        break

                if (legislature, text_number) not in mapping:
                    for law_type in ("LFR", "LF", "LFSS"):
                        loi = year_type_to_loi.get((year, law_type))
                        if loi:
                            mapping[(legislature, text_number)] = (loi.id, False)
                            break

            except Exception:
                continue

    exact = sum(1 for _, is_exact in mapping.values() if is_exact)
    logger.info(
        f"AN-HTML text→loi mapping: {len(mapping)} texts resolved "
        f"({exact} exact, {len(mapping) - exact} fuzzy)"
    )
    return mapping


# ---------------------------------------------------------------------------
# Main fix function
# ---------------------------------------------------------------------------

def fix_amendement_linkage():
    """Fix amendement-to-article linkage for all 3 sources."""

    session = get_session()
    # Silence SQLAlchemy echo for batch operations
    session.bind.echo = False

    # Stats before
    total = session.query(Amendement).count()
    orphaned_before = session.query(Amendement).filter(
        Amendement.article_id.is_(None)
    ).count()
    logger.info(f"Before: {orphaned_before}/{total} amendements without article_id")

    # Build lookups
    logger.info("Building lookups...")
    (
        all_lois, year_type_to_loi, loi_to_an_dossier,
        loi_to_senat_dossier, all_dossiers, dossier_to_loi,
    ) = _build_loi_lookups(session)
    article_lookup = build_article_lookup(session)

    # Build multi-source indexes
    logger.info("Building multi-source indexes...")
    amanr_index = _build_amanr_index(RAW_DIR)
    an_html_index = _build_an_html_index(RAW_DIR)
    ameli_index = _build_ameli_index(RAW_DIR)

    # Build AN-HTML text→loi mapping
    an_html_text_to_loi = _build_an_html_text_to_loi(RAW_DIR, year_type_to_loi)

    # Load concordance table for PLF→final article translation
    concordance = _load_concordance(all_lois)

    # Cache AMANR texteLegislatifRef → loi resolution (two modes)
    _text_ref_loi_cache: dict[tuple[str, bool], Loi | None] = {}

    def resolve_text_ref_cached(text_ref: str, strict: bool = False) -> Loi | None:
        cache_key = (text_ref, strict)
        if cache_key not in _text_ref_loi_cache:
            _text_ref_loi_cache[cache_key] = _resolve_text_ref_to_loi(
                text_ref, year_type_to_loi, all_dossiers, all_lois,
                strict=strict,
            )
        return _text_ref_loi_cache[cache_key]

    # Cache AMELI titre → loi resolution
    _ameli_loi_cache: dict[tuple[str, str], Loi | None] = {}

    def resolve_ameli_cached(titre: str, signet: str) -> Loi | None:
        cache_key = (titre, signet)
        if cache_key not in _ameli_loi_cache:
            # Try signet first (more precise), then titre
            loi = _resolve_loi_from_doslegsignet(signet, year_type_to_loi)
            if not loi:
                loi = _resolve_loi_from_titre(titre, year_type_to_loi)
            _ameli_loi_cache[cache_key] = loi
        return _ameli_loi_cache[cache_key]

    # Get orphaned amendements
    orphaned_rows = (
        session.query(
            Amendement.id,
            Amendement.reference_externe,
            Amendement.dossier_id,
        )
        .filter(
            Amendement.article_id.is_(None),
            Amendement.reference_externe.isnot(None),
        )
        .all()
    )

    logger.info(f"Processing {len(orphaned_rows)} orphaned amendements...")

    # Counters
    stats = {
        "amanr_resolved": 0,
        "an_html_resolved": 0,
        "ameli_resolved": 0,
        "dossier_fixed": 0,
        "article_linked": 0,
        "no_loi": 0,
        "no_article_ref": 0,
        "no_article_match": 0,
        "concordance_translated": 0,
        "concordance_suppressed": 0,
        "unknown_source": 0,
    }

    # Collect updates: list of (amend_id, new_dossier_id or None, new_article_id or None, new_numero or None)
    updates: list[dict] = []

    for i, (amend_id, ref_externe, dossier_id) in enumerate(orphaned_rows):
        if not ref_externe:
            continue

        article_ref = None
        loi_id = None
        new_dossier_id = None

        # --- AMANR source ---
        if ref_externe in amanr_index:
            art_ref_raw, text_ref = amanr_index[ref_externe]
            article_ref = art_ref_raw

            if text_ref:
                # Non-strict for dossier fixing (better than a default dossier)
                loi_fuzzy = resolve_text_ref_cached(text_ref, strict=False)
                if loi_fuzzy:
                    correct_dossier = loi_to_an_dossier.get(loi_fuzzy.id)
                    if correct_dossier and correct_dossier.id != dossier_id:
                        new_dossier_id = correct_dossier.id

                # Strict for article matching (only when loi is certain)
                loi_strict = resolve_text_ref_cached(text_ref, strict=True)
                if loi_strict:
                    loi_id = loi_strict.id
                    stats["amanr_resolved"] += 1

        # --- AN-HTML source ---
        elif ref_externe in an_html_index:
            art_ref_raw, legislature, text_number = an_html_index[ref_externe]
            article_ref = art_ref_raw

            resolved = an_html_text_to_loi.get((legislature, text_number))
            if resolved:
                resolved_loi_id, is_exact = resolved
                # Fix dossier (even with fuzzy match - better than default)
                correct_dossier = loi_to_an_dossier.get(resolved_loi_id)
                if correct_dossier and correct_dossier.id != dossier_id:
                    new_dossier_id = correct_dossier.id
                # Only set loi_id for article matching when exact
                if is_exact:
                    loi_id = resolved_loi_id
                    stats["an_html_resolved"] += 1

        # --- AMELI source ---
        elif ref_externe in ameli_index:
            art_ref_raw, txt_titre, txt_signet = ameli_index[ref_externe]
            article_ref = art_ref_raw

            loi = resolve_ameli_cached(txt_titre, txt_signet)
            if loi:
                loi_id = loi.id
                # Fix dossier: use the correct SENAT dossier for this loi
                correct_dossier = loi_to_senat_dossier.get(loi.id)
                if correct_dossier and correct_dossier.id != dossier_id:
                    new_dossier_id = correct_dossier.id
                stats["ameli_resolved"] += 1

        else:
            stats["unknown_source"] += 1
            continue

        # If we couldn't resolve the loi, use current dossier's loi
        if not loi_id:
            loi_id = dossier_to_loi.get(dossier_id)
            if not loi_id:
                stats["no_loi"] += 1
                continue

        # Match article
        if not article_ref:
            stats["no_article_ref"] += 1
            # Still apply dossier fix if we have one
            if new_dossier_id:
                updates.append({"id": amend_id, "dossier_id": new_dossier_id})
                stats["dossier_fixed"] += 1
            continue

        # Translate PLF article ref → final law article ref via concordance
        if concordance:
            normalized_ref = normalize_article_ref(article_ref)
            if normalized_ref:
                conc_key = (loi_id, normalized_ref.lower())
                if conc_key in concordance:
                    definitif = concordance[conc_key]
                    if definitif is None:
                        # Article was suppressed - skip linking
                        stats["concordance_suppressed"] += 1
                        if new_dossier_id:
                            updates.append({"id": amend_id, "dossier_id": new_dossier_id})
                            stats["dossier_fixed"] += 1
                        continue
                    # Replace with the definitive article reference
                    article_ref = definitif
                    stats["concordance_translated"] += 1

        article_id = find_matching_article(article_ref, loi_id, article_lookup)

        if article_id:
            upd: dict = {"id": amend_id, "article_id": article_id}
            if new_dossier_id:
                upd["dossier_id"] = new_dossier_id
                stats["dossier_fixed"] += 1
            stats["article_linked"] += 1
            updates.append(upd)
        else:
            stats["no_article_match"] += 1
            # Still apply dossier fix
            if new_dossier_id:
                updates.append({"id": amend_id, "dossier_id": new_dossier_id})
                stats["dossier_fixed"] += 1

        if (i + 1) % 10000 == 0:
            logger.info(
                f"Scanned {i + 1}/{len(orphaned_rows)}, "
                f"articles linked={stats['article_linked']}, "
                f"dossiers fixed={stats['dossier_fixed']}"
            )

    # Group updates by type for efficient batch SQL
    article_only = []  # [{id, article_id}]
    dossier_only = []  # [{id, dossier_id}]
    both = []          # [{id, article_id, dossier_id}]

    for upd in updates:
        has_art = "article_id" in upd
        has_dos = "dossier_id" in upd
        if has_art and has_dos:
            both.append(upd)
        elif has_art:
            article_only.append(upd)
        elif has_dos:
            dossier_only.append(upd)

    logger.info(
        f"Applying {len(updates)} updates: "
        f"{len(article_only)} article-only, {len(dossier_only)} dossier-only, "
        f"{len(both)} both"
    )

    def _batch_update_pg(rows, columns, batch_size=500):
        """Batch update using UPDATE ... FROM (VALUES ...) for PostgreSQL.

        Uses CAST() in the first VALUES row to set column types.
        """
        for batch_start in range(0, len(rows), batch_size):
            batch = rows[batch_start:batch_start + batch_size]

            value_rows = []
            for idx, row in enumerate(batch):
                parts = []
                all_cols = ["id"] + columns
                for col in all_cols:
                    param = f":{col}_{idx}"
                    # Cast first row to establish types
                    if idx == 0:
                        param = f"CAST({param} AS INTEGER)"
                    parts.append(param)
                value_rows.append(f"({', '.join(parts)})")

            col_aliases = ["v_id"] + [f"v_{c}" for c in columns]
            values_sql = ", ".join(value_rows)
            set_clauses = ", ".join(f"{c} = v.v_{c}" for c in columns)
            alias_list = ", ".join(col_aliases)

            sql = (
                f"UPDATE amendements a SET {set_clauses} "
                f"FROM (VALUES {values_sql}) AS v({alias_list}) "
                f"WHERE a.id = v.v_id"
            )

            params = {}
            for idx, row in enumerate(batch):
                params[f"id_{idx}"] = row["id"]
                for col in columns:
                    params[f"{col}_{idx}"] = row[col]

            session.execute(text(sql), params)

        session.commit()

    if article_only:
        _batch_update_pg(article_only, ["article_id"])
        logger.info(f"  article-only: done ({len(article_only)})")

    if dossier_only:
        _batch_update_pg(dossier_only, ["dossier_id"])
        logger.info(f"  dossier-only: done ({len(dossier_only)})")

    if both:
        _batch_update_pg(both, ["article_id", "dossier_id"])
        logger.info(f"  both: done ({len(both)})")

    # Stats after
    orphaned_after = session.query(Amendement).filter(
        Amendement.article_id.is_(None)
    ).count()

    logger.info("\n--- Summary ---")
    logger.info(f"AMANR resolved: {stats['amanr_resolved']}")
    logger.info(f"AN-HTML resolved: {stats['an_html_resolved']}")
    logger.info(f"AMELI resolved: {stats['ameli_resolved']}")
    logger.info(f"Unknown source: {stats['unknown_source']}")
    logger.info(f"No loi found: {stats['no_loi']}")
    logger.info(f"No article_ref: {stats['no_article_ref']}")
    logger.info(f"Concordance translated: {stats['concordance_translated']}")
    logger.info(f"Concordance suppressed: {stats['concordance_suppressed']}")
    logger.info(f"No article match: {stats['no_article_match']}")
    logger.info(f"Dossiers fixed: {stats['dossier_fixed']}")
    logger.info(f"Articles linked: {stats['article_linked']}")
    logger.info(f"Before: {orphaned_before} orphaned")
    logger.info(f"After: {orphaned_after} orphaned")
    logger.info(f"Improvement: {orphaned_before - orphaned_after} amendements linked")

    session.close()


if __name__ == "__main__":
    fix_amendement_linkage()
