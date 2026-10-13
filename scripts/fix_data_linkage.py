#!/usr/bin/env python3
"""
Fix data linkage issues in the database:
1. Re-link debates to correct dossiers based on content
2. Collect and import missing articles from Légifrance
3. Link amendments to articles based on article references
"""

import json
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from src.db.models import Debat, DossierLegislatif, Loi, Article, Amendement, get_session, init_db
from src.db.import_data import extract_law_reference_from_content, find_dossier_for_debate, build_debate_lookup_cache, import_articles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def fix_debate_dossier_linking(session):
    """Re-link debates to correct dossiers based on content analysis."""
    logger.info("=== Fixing debate-dossier linking ===")

    # Get all data needed for matching
    lois = session.query(Loi).all()
    # Use list instead of dict to avoid key collision (AN and SENAT share reference_externe)
    dossiers = session.query(DossierLegislatif).all()
    debates = session.query(Debat).all()

    logger.info(f"Processing {len(debates)} debates with {len(dossiers)} dossiers...")

    # Build lookup dicts once for all debates
    lookup_cache = build_debate_lookup_cache(dossiers, lois)

    # Track changes
    changes = 0
    dossier_counts = {}
    unmatched = 0

    for debat in debates:
        content = debat.contenu_integral or ""

        # Try to find matching dossier (pass chambre from debate)
        matched_dossier = find_dossier_for_debate(
            dossiers, lois, content, debat.date, debat.chambre,
            _lookup_cache=lookup_cache,
        )

        if matched_dossier and matched_dossier.id != debat.dossier_id:
            old_id = debat.dossier_id
            debat.dossier_id = matched_dossier.id
            changes += 1
            dossier_counts[matched_dossier.id] = dossier_counts.get(matched_dossier.id, 0) + 1
        elif matched_dossier:
            dossier_counts[matched_dossier.id] = dossier_counts.get(matched_dossier.id, 0) + 1
        else:
            unmatched += 1
            dossier_counts[debat.dossier_id] = dossier_counts.get(debat.dossier_id, 0) + 1

    session.commit()

    logger.info(f"Updated {changes} debate-dossier links")
    logger.info(f"Unmatched debates (kept default): {unmatched}")
    logger.info(f"Debate distribution across {len(dossier_counts)} dossiers:")

    # Show distribution with law names
    for dossier_id, count in sorted(dossier_counts.items(), key=lambda x: -x[1])[:15]:
        dossier = session.query(DossierLegislatif).get(dossier_id)
        loi = session.query(Loi).get(dossier.loi_id) if dossier else None
        loi_name = loi.titre[:50] if loi else "Unknown"
        logger.info(f"  Dossier {dossier_id} ({loi_name}): {count} debates")


def _build_ref_to_text_ref(raw_dir: Path) -> dict[str, str]:
    """Build ref_externe → texteLegislatifRef mapping, with file cache.

    Cache is invalidated when the raw amendment directories have been modified
    more recently than the cache file (i.e. after a new COLLECT).
    """
    cache_path = raw_dir.parent / "cache" / "ref_to_text_ref.json"

    if cache_path.exists():
        cache_mtime = cache_path.stat().st_mtime
        # Invalidate if any amendment directory was modified after cache
        stale = False
        for amend_dir in raw_dir.glob("assemblee/amendements_leg*/json"):
            if amend_dir.stat().st_mtime > cache_mtime:
                stale = True
                break
        if stale:
            logger.info("Cache invalidated (raw files newer than cache), rescanning...")
            cache_path.unlink()
        else:
            logger.info(f"Loading amendment ref cache from {cache_path}")
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)

    logger.info("Scanning raw amendment JSON files (first run, will be cached)...")
    ref_to_text_ref: dict[str, str] = {}
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
                if uid and text_ref:
                    ref_to_text_ref[uid] = text_ref
            except Exception:
                continue

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(ref_to_text_ref, f, ensure_ascii=False)
    logger.info(f"Cached {len(ref_to_text_ref)} amendment refs to {cache_path}")

    return ref_to_text_ref


def fix_amendment_dossier_linking(session):
    """Re-link amendments to correct dossiers by re-reading raw JSON files.

    The initial import assigns all amendments to a single default dossier per
    chamber.  This function scans the raw amendment files to recover the
    texteLegislatifRef, then maps it to the correct loi/dossier.
    """
    logger.info("=== Fixing amendment-dossier linking ===")

    raw_dir = Path(__file__).parent.parent / "data" / "raw"

    ref_to_text_ref = _build_ref_to_text_ref(raw_dir)

    logger.info(f"Built mapping for {len(ref_to_text_ref)} amendment refs")

    # Build texteLegislatifRef → loi mapping
    # Strategy: group all text_refs and for each, find matching loi by content of dossier URL
    # or by building a lookup from the dossier archives
    all_lois = {loi.id: loi for loi in session.query(Loi).all()}
    all_dossiers = session.query(DossierLegislatif).all()

    # Build loi_id → AN dossier
    loi_to_an_dossier: dict[int, DossierLegislatif] = {}
    for d in all_dossiers:
        if d.chambre == "AN" and d.loi_id not in loi_to_an_dossier:
            loi_to_an_dossier[d.loi_id] = d

    # Build year+type → loi mapping for finance laws
    year_type_to_loi: dict[tuple[int, str], Loi] = {}
    for loi in all_lois.values():
        if loi.date_promulgation and loi.type in ("LF", "LFR", "LFSS"):
            # annee_budgetaire is the target year
            year = getattr(loi, "annee", None) or loi.date_promulgation.year
            key = (year, loi.type)
            if key not in year_type_to_loi:
                year_type_to_loi[key] = loi

    # Map texteLegislatifRef → dossier
    text_ref_to_dossier: dict[str, int] = {}

    # Legislature start years
    leg_years = {12: 2002, 13: 2007, 14: 2012, 15: 2017, 16: 2022, 17: 2024}

    unique_text_refs = set(ref_to_text_ref.values())
    logger.info(f"Resolving {len(unique_text_refs)} distinct texteLegislatifRef values")

    for text_ref in unique_text_refs:
        # Try to match via dossier reference_externe
        for d in all_dossiers:
            if d.reference_externe and text_ref in d.reference_externe:
                text_ref_to_dossier[text_ref] = d.id
                break
        if text_ref in text_ref_to_dossier:
            continue

        # Extract legislature from ref: L14, L15, etc.
        leg_match = re.search(r"L(\d{2})", text_ref)
        if not leg_match:
            continue
        leg_num = int(leg_match.group(1))
        start_year = leg_years.get(leg_num)
        if not start_year:
            continue
        end_year = start_year + 5

        # Determine law type from ref
        if "BTC" in text_ref or "PLF" in text_ref.upper():
            # BTC = budget, could be LF or LFR
            # Try each year in the legislature
            for year in range(start_year, end_year + 1):
                for law_type in ("LF", "LFR", "LFSS"):
                    loi = year_type_to_loi.get((year, law_type))
                    if loi and loi.id in loi_to_an_dossier:
                        # Match text number from ref to narrow down
                        # For now, we can't precisely match, so we'll use the
                        # fact that BTC refs often contain the text filing number
                        # Just assign to the year's LF by default for BTC
                        if law_type == "LF":
                            text_ref_to_dossier[text_ref] = loi_to_an_dossier[loi.id].id
                            break
                if text_ref in text_ref_to_dossier:
                    break

        if "PLFSS" in text_ref.upper():
            for year in range(start_year, end_year + 1):
                loi = year_type_to_loi.get((year, "LFSS"))
                if loi and loi.id in loi_to_an_dossier:
                    text_ref_to_dossier[text_ref] = loi_to_an_dossier[loi.id].id
                    break

        if "PLFR" in text_ref.upper():
            for year in range(start_year, end_year + 1):
                loi = year_type_to_loi.get((year, "LFR"))
                if loi and loi.id in loi_to_an_dossier:
                    text_ref_to_dossier[text_ref] = loi_to_an_dossier[loi.id].id
                    break

    resolved = len(text_ref_to_dossier)
    logger.info(f"Resolved {resolved}/{len(unique_text_refs)} text refs to dossiers")

    # Build ref_externe → new_dossier_id mapping (Python-side join)
    ref_to_dossier: dict[str, int] = {}
    for ref, text_ref in ref_to_text_ref.items():
        dossier_id = text_ref_to_dossier.get(text_ref)
        if dossier_id:
            ref_to_dossier[ref] = dossier_id

    # Group refs by target dossier_id for batch SQL updates
    dossier_to_refs: dict[int, list[str]] = defaultdict(list)
    for ref, dossier_id in ref_to_dossier.items():
        dossier_to_refs[dossier_id].append(ref)

    logger.info(f"Will update amendments across {len(dossier_to_refs)} dossiers")

    # Batch SQL updates: one UPDATE per dossier_id, chunked to avoid huge IN clauses
    updated = 0
    chunk_size = 500

    for dossier_id, refs in dossier_to_refs.items():
        for i in range(0, len(refs), chunk_size):
            chunk = refs[i:i + chunk_size]
            placeholders = ", ".join(f":ref_{j}" for j in range(len(chunk)))
            params = {f"ref_{j}": ref for j, ref in enumerate(chunk)}
            params["new_dossier_id"] = dossier_id

            result = session.execute(
                text(f"""
                    UPDATE amendements
                    SET dossier_id = :new_dossier_id
                    WHERE reference_externe IN ({placeholders})
                      AND (dossier_id IS NULL OR dossier_id != :new_dossier_id)
                """),
                params,
            )
            updated += result.rowcount
            session.commit()

    logger.info(f"Updated {updated} amendment dossier links")


def link_amendments_to_articles(session):
    """Link amendments to articles based on article number references."""
    logger.info("=== Linking amendments to articles ===")

    # Get all articles grouped by loi
    articles_by_loi = {}
    for article in session.query(Article).all():
        if article.loi_id not in articles_by_loi:
            articles_by_loi[article.loi_id] = {}
        # Index by article number
        articles_by_loi[article.loi_id][article.numero_article] = article

    # Get dossier -> loi mapping
    dossier_to_loi = {}
    for dossier in session.query(DossierLegislatif).all():
        dossier_to_loi[dossier.id] = dossier.loi_id

    # Patterns to extract article numbers from amendment content
    article_patterns = [
        r"article\s+(\d+(?:\s*[a-zA-Z])?(?:\s*bis|\s*ter|\s*quater)?)",
        r"art\.\s*(\d+(?:\s*[a-zA-Z])?)",
        r"l['']\s*article\s+(\d+)",
    ]

    amendments = session.query(Amendement).filter(Amendement.article_id.is_(None)).all()
    logger.info(f"Processing {len(amendments)} amendments without article links...")

    linked = 0
    batch_size = 1000

    for i, amendment in enumerate(amendments):
        if not amendment.dossier_id:
            continue

        loi_id = dossier_to_loi.get(amendment.dossier_id)
        if not loi_id or loi_id not in articles_by_loi:
            continue

        # Try to extract article number from content or objet
        content = (amendment.contenu or "") + " " + (amendment.objet or "")
        content_lower = content.lower()

        for pattern in article_patterns:
            match = re.search(pattern, content_lower)
            if match:
                article_num = match.group(1).strip()
                # Normalize: "1 bis" -> "1 bis", "1bis" -> "1 bis"
                article_num = re.sub(r"(\d+)\s*(bis|ter|quater)", r"\1 \2", article_num)

                # Try to find matching article
                articles = articles_by_loi[loi_id]
                if article_num in articles:
                    amendment.article_id = articles[article_num].id
                    linked += 1
                    break
                # Try numeric only
                num_only = re.match(r"(\d+)", article_num)
                if num_only and num_only.group(1) in articles:
                    amendment.article_id = articles[num_only.group(1)].id
                    linked += 1
                    break

        # Commit in batches
        if (i + 1) % batch_size == 0:
            session.commit()
            logger.info(f"Processed {i + 1} amendments, linked {linked}...")

    session.commit()
    logger.info(f"Linked {linked} amendments to articles")


def collect_missing_articles(session):
    """Collect and import articles for laws that have none."""
    logger.info("=== Collecting missing articles from Légifrance ===")

    # Find laws without articles
    result = session.execute(text("""
        SELECT l.id, l.titre, l.url_legifrance
        FROM lois l
        LEFT JOIN articles a ON a.loi_id = l.id
        GROUP BY l.id
        HAVING COUNT(a.id) = 0
    """))

    missing_lois = list(result)
    if not missing_lois:
        logger.info("All laws have articles, nothing to collect")
        return

    logger.info(f"Found {len(missing_lois)} laws without articles")

    # Try to import from Légifrance
    try:
        from src.collectors.legifrance import LegifranceClient
        client = LegifranceClient()
    except Exception as e:
        logger.warning(f"Cannot initialize Légifrance client: {e}")
        logger.warning("Skipping article collection - configure PISTE credentials")
        return

    raw_dir = Path(__file__).parent.parent / "data" / "raw" / "legifrance"
    raw_dir.mkdir(parents=True, exist_ok=True)

    for row in missing_lois:
        loi_id, titre, url = row
        if not url:
            logger.warning(f"  Loi {loi_id} has no URL, skipping")
            continue

        # Extract JORFTEXT from URL
        jorftext = url.split("/")[-1]
        json_path = raw_dir / f"{jorftext}.json"

        logger.info(f"  Collecting {jorftext} for: {titre[:50]}...")

        try:
            # Check if already downloaded
            if json_path.exists():
                with open(json_path) as f:
                    data = json.load(f)
            else:
                # Fetch from Légifrance
                data = client.get_law_by_text_id(jorftext)
                # Save to file
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                logger.info(f"    Downloaded to {json_path}")

            # Extract and import articles
            loi = session.query(Loi).get(loi_id)
            articles_data = extract_articles_from_legifrance_data(data)

            if articles_data:
                articles = import_articles(session, loi, articles_data)
                session.commit()
                logger.info(f"    Imported {len(articles)} articles")
            else:
                logger.warning(f"    No articles found in data")

        except Exception as e:
            logger.error(f"    Error collecting {jorftext}: {e}")
            continue


def extract_articles_from_legifrance_data(data: dict) -> list[dict]:
    """Extract article data from Légifrance JSON response."""
    articles = []

    def extract_from_structure(structure, path=""):
        """Recursively extract articles from nested structure."""
        if not structure:
            return

        if isinstance(structure, dict):
            # Check if this is an article
            if structure.get("nature") == "ARTICLE" or "articleLegi" in structure:
                article_data = structure.get("articleLegi", structure)
                numero = article_data.get("num", "")
                contenu = article_data.get("texte", "")
                if numero or contenu:
                    articles.append({
                        "numero": numero,
                        "contenu": contenu,
                        "titre": article_data.get("intOrdre", ""),
                    })

            # Recurse into children
            for key in ["articles", "sections", "children", "structure"]:
                if key in structure:
                    extract_from_structure(structure[key], path)

        elif isinstance(structure, list):
            for item in structure:
                extract_from_structure(item, path)

    # Try different data structures
    if "articles" in data:
        extract_from_structure(data["articles"])
    if "structure" in data:
        extract_from_structure(data["structure"])
    if "texte" in data:
        extract_from_structure(data["texte"])

    return articles


def report_missing_data(session):
    """Report on missing data that needs collection."""
    logger.info("=== Final Status Report ===")

    # Laws without articles
    result = session.execute(text("""
        SELECT l.id, l.titre, l.url_legifrance,
               (SELECT COUNT(*) FROM articles a WHERE a.loi_id = l.id) as nb_articles
        FROM lois l
        ORDER BY l.date_promulgation
    """))

    logger.info("Articles per law:")
    for row in result:
        status = "✓" if row[3] > 0 else "✗"
        logger.info(f"  {status} Loi {row[0]}: {row[3]:4d} articles - {row[1][:50]}")

    # Dossiers debate distribution
    result = session.execute(text("""
        SELECT d.id, d.chambre, l.titre,
               (SELECT COUNT(*) FROM debats db WHERE db.dossier_id = d.id) as nb_debats
        FROM dossiers_legislatifs d
        JOIN lois l ON d.loi_id = l.id
        WHERE d.chambre = 'AN'
        ORDER BY l.date_promulgation
    """))

    logger.info("\nDebates per AN dossier:")
    for row in result:
        status = "✓" if row[3] > 0 else "✗"
        logger.info(f"  {status} Dossier {row[0]:2d}: {row[3]:4d} debates - {row[2][:45]}")


def main():
    """Run all fixes."""
    logger.info("Starting data linkage fixes...")

    init_db()
    session = get_session()

    try:
        # Step 1: Fix debate linking
        fix_debate_dossier_linking(session)

        # Step 2: Fix amendment dossier linking
        fix_amendment_dossier_linking(session)

        # Step 3: Collect and import missing articles
        collect_missing_articles(session)

        # Step 4: Link amendments to articles
        link_amendments_to_articles(session)

        # Step 4: Report final status
        report_missing_data(session)

        logger.info("Data linkage fixes complete!")

    except Exception as e:
        logger.error(f"Error: {e}")
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
