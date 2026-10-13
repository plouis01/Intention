#!/usr/bin/env python3
"""
Full data pipeline for the legislative intent analysis project.

Orchestrates ALL pipeline steps in the correct dependency order:
  1. COLLECT  - Core collectors + supplemental fetchers (missing laws, DILA archives)
  2. IMPORT   - Core import + DILA + missing laws/articles + Sénat debates + external sources
  3. LINK     - Debate-dossier linking + orphaned debates + amendment-article linking + advanced fixes
  4. FIX      - Encoding fixes
  5. EMBED    - Embeddings generation + term expansions
  6. INDEX    - Generate article modification index for analysis
  7. VALIDATE - Record counts across all tables

Usage:
    python scripts/run_full_pipeline.py --all
    python scripts/run_full_pipeline.py --collect --import --validate
    python scripts/run_full_pipeline.py --link --fix
    python scripts/run_full_pipeline.py --embed
    python scripts/run_full_pipeline.py --reset --import  # Reset DB before import

Each step is idempotent and can be safely re-run without corrupting data.
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# Project root resolution
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Ensure data directory exists for log file
(PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)

# Setup logging - dual handlers (console + file)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(PROJECT_ROOT / "data" / "full_pipeline.log"),
    ],
)
logger = logging.getLogger(__name__)

# Default paths
LAWS_FILE = PROJECT_ROOT / "data" / "lois_finances_2012_2025.json"
CGI_LAWS_FILE = PROJECT_ROOT / "data" / "lois_cgi_1958_2026.json"
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
EMBEDDINGS_DIR = PROJECT_ROOT / "data" / "embeddings"


def load_reference_laws() -> list[dict]:
    """Load LF/LFR/LFSS laws from reference files (finance overlay + CGI).

    Merges lois_finances_2012_2025.json (curated, higher priority) with
    lois_cgi_1958_2026.json (auto-collected), keeping only finance law
    types (LF, LFR, LFSS) from 1959 onward.
    """
    FINANCE_TYPES = {"LF", "LFR", "LFSS"}
    MIN_DATE = "1958-01-01"
    laws_by_numero = {}

    # Load CGI laws first (lower priority), filtered to finance types + date
    if CGI_LAWS_FILE.exists():
        with open(CGI_LAWS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for loi in data.get("lois", []):
            if (loi.get("numero")
                    and loi.get("type") in FINANCE_TYPES
                    and loi.get("date_promulgation", "") >= MIN_DATE):
                laws_by_numero[loi["numero"]] = loi

    # Load finance laws (higher priority, overwrites duplicates)
    with open(LAWS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    for loi in data["lois"]:
        if (loi.get("numero")
                and loi.get("date_promulgation", "") >= MIN_DATE):
            laws_by_numero[loi["numero"]] = loi

    laws = sorted(laws_by_numero.values(),
                  key=lambda x: x.get("date_promulgation", "0000"))
    logger.info(f"Loaded {len(laws)} finance laws (LF/LFR/LFSS, 1958+) "
                f"from CGI + curated finance files")
    return laws


def _run_with_rate_limit_retry(fn, *args, max_retries=3, pause_seconds=60, **kwargs):
    """Run a function, retrying on 429 rate limit errors with a pause."""
    for attempt in range(1, max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if "429" in str(e) and attempt < max_retries:
                logger.warning(
                    f"Rate limited (attempt {attempt}/{max_retries}), "
                    f"pausing {pause_seconds}s before retry..."
                )
                time.sleep(pause_seconds)
            else:
                raise


def _get_missing_jorf_ids(reference_laws: list[dict]) -> list[dict]:
    """Determine which JORF law files are missing from data/raw/legifrance/."""
    legifrance_dir = RAW_DATA_DIR / "legifrance"
    legifrance_dir.mkdir(parents=True, exist_ok=True)
    existing = {p.stem for p in legifrance_dir.glob("*.json")}
    return [law for law in reference_laws if law["jorf"] not in existing]


# ---------------------------------------------------------------------------
# PHASE 1: COLLECT
# ---------------------------------------------------------------------------


def run_collect() -> bool:
    """Execute the collection phase."""
    logger.info("=" * 60)
    logger.info("PHASE: COLLECT")
    logger.info("=" * 60)
    success = True

    # 1. Core collectors
    logger.info("--- Core collectors ---")

    # Légifrance
    try:
        logger.info("Collecting from Légifrance API...")
        from src.collectors.legifrance import collect_law_texts

        _run_with_rate_limit_retry(
            collect_law_texts, LAWS_FILE, RAW_DATA_DIR / "legifrance"
        )
        logger.info("Légifrance collection complete")
    except Exception as e:
        logger.warning(f"Légifrance collection failed: {e}")
        success = False

    # Assemblée Nationale
    try:
        logger.info("Collecting from Assemblée Nationale...")
        from src.collectors.assemblee import collect_an_data

        collect_an_data(LAWS_FILE, RAW_DATA_DIR)
        logger.info("AN collection complete")
    except Exception as e:
        logger.warning(f"AN collection failed: {e}")
        success = False

    # Explode leg14 monolithic Amendements_XIV.json into individual files
    try:
        leg14_big_json = RAW_DATA_DIR / "assemblee" / "amendements_leg14" / "Amendements_XIV.json"
        leg14_json_dir = RAW_DATA_DIR / "assemblee" / "amendements_leg14" / "json"
        if leg14_big_json.exists() and (not leg14_json_dir.exists() or not any(leg14_json_dir.rglob("*.json"))):
            logger.info("--- Exploding leg14 Amendements_XIV.json into individual files ---")
            with open(leg14_big_json, encoding="utf-8") as f:
                leg14_data = json.load(f)

            textes = leg14_data["textesEtAmendements"]["texteleg"]
            leg14_json_dir.mkdir(parents=True, exist_ok=True)
            total_written = 0

            for texte in textes:
                text_ref = texte.get("refTexteLegislatif", "")
                amends_container = texte.get("amendements", {})
                amend_list = amends_container.get("amendement", [])
                if isinstance(amend_list, dict):
                    amend_list = [amend_list]
                if not amend_list:
                    continue

                text_dir = leg14_json_dir / text_ref
                text_dir.mkdir(parents=True, exist_ok=True)

                for amend in amend_list:
                    uid = amend.get("uid")
                    if not uid:
                        continue
                    amend["texteLegislatifRef"] = text_ref
                    output_file = text_dir / f"{uid}.json"
                    with open(output_file, "w", encoding="utf-8") as f:
                        json.dump({"amendement": amend}, f, ensure_ascii=False)
                    total_written += 1

            logger.info(f"Exploded leg14 amendments: {total_written} individual files written")
        elif leg14_json_dir.exists() and any(leg14_json_dir.rglob("*.json")):
            count = sum(1 for _ in leg14_json_dir.rglob("*.json"))
            logger.info(f"Leg14 amendments already exploded ({count} files), skipping")
    except Exception as e:
        logger.warning(f"Leg14 amendments explosion failed: {e}")

    # Sénat
    try:
        logger.info("Collecting from Sénat...")
        from src.collectors.senat import collect_senat_data

        collect_senat_data(LAWS_FILE, RAW_DATA_DIR)
        logger.info("Sénat collection complete")
    except Exception as e:
        logger.warning(f"Sénat collection failed: {e}")
        success = False

    # 2. Dynamic missing law collection
    logger.info("--- Missing law collection (dynamic from reference file) ---")
    reference_laws = load_reference_laws()
    missing_laws = _get_missing_jorf_ids(reference_laws)

    if not missing_laws:
        logger.info("All law files already present in data/raw/legifrance/")
    else:
        logger.info(f"Found {len(missing_laws)} missing law files")
        legifrance_dir = RAW_DATA_DIR / "legifrance"

        # 2a. Try API fetch
        api_failures = []
        try:
            from src.collectors.legifrance import LegifranceClient

            logger.info("Attempting API fetch for missing laws...")
            with LegifranceClient() as client:
                for i, law in enumerate(missing_laws):
                    jorf_id = law["jorf"]
                    output_file = legifrance_dir / f"{jorf_id}.json"
                    if output_file.exists():
                        continue
                    logger.info(
                        f"  [{i+1}/{len(missing_laws)}] Fetching {law['titre']} ({jorf_id})"
                    )
                    fetched = False
                    for attempt in range(1, 4):
                        try:
                            law_data = client.get_law_by_text_id(jorf_id)
                            with open(output_file, "w", encoding="utf-8") as f:
                                json.dump(law_data, f, ensure_ascii=False, indent=2)
                            logger.info(f"  Saved {jorf_id}")
                            fetched = True
                            break
                        except Exception as e:
                            if "429" in str(e) and attempt < 3:
                                pause = 60 * attempt
                                logger.warning(
                                    f"  Rate limited on {jorf_id} (attempt {attempt}/3), "
                                    f"waiting {pause}s..."
                                )
                                time.sleep(pause)
                            else:
                                logger.warning(f"  API failed for {jorf_id}: {e}")
                                break
                    if not fetched:
                        api_failures.append(law)

                    if i < len(missing_laws) - 1:
                        time.sleep(5)
        except Exception as e:
            logger.warning(f"API client unavailable: {e}")
            api_failures = missing_laws

        # 2b. Try scraping for API failures
        scrape_failures = []
        if api_failures:
            logger.info(f"Attempting web scraping for {len(api_failures)} laws...")
            try:
                import httpx
                from bs4 import BeautifulSoup
                import re

                headers = {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml",
                }
                with httpx.Client(
                    timeout=30.0, follow_redirects=True, headers=headers
                ) as http_client:
                    for i, law in enumerate(api_failures):
                        jorf_id = law["jorf"]
                        output_file = legifrance_dir / f"{jorf_id}.json"
                        if output_file.exists():
                            continue
                        try:
                            logger.info(
                                f"  [{i+1}/{len(api_failures)}] Scraping {law['titre']} ({jorf_id})"
                            )
                            url = f"https://www.legifrance.gouv.fr/jorf/id/{jorf_id}"
                            response = http_client.get(url)
                            response.raise_for_status()
                            soup = BeautifulSoup(response.text, "html.parser")

                            title_elem = soup.find("h1")
                            title = (
                                title_elem.get_text(strip=True) if title_elem else None
                            )

                            articles = []
                            article_divs = soup.find_all(
                                "div", class_="article-content"
                            )
                            if not article_divs:
                                article_divs = soup.find_all("article")

                            for idx, div in enumerate(article_divs):
                                num_elem = div.find(class_="article-num") or div.find(
                                    "h2"
                                )
                                num = (
                                    num_elem.get_text(strip=True)
                                    if num_elem
                                    else str(idx + 1)
                                )
                                num = re.sub(
                                    r"^Article\s*", "", num, flags=re.IGNORECASE
                                ).strip()
                                content_elem = div.find(class_="article-body") or div
                                content = content_elem.get_text(
                                    separator="\n", strip=True
                                )
                                if content and len(content) > 10:
                                    articles.append(
                                        {
                                            "num": num,
                                            "content": content,
                                            "id": f"{jorf_id}_ART_{idx+1}",
                                        }
                                    )

                            if articles:
                                law_data = {
                                    "text": {
                                        "id": jorf_id,
                                        "title": title,
                                        "articles": articles,
                                    },
                                    "source": "legifrance_scrape",
                                }
                                with open(output_file, "w", encoding="utf-8") as f:
                                    json.dump(
                                        law_data, f, ensure_ascii=False, indent=2
                                    )
                                logger.info(
                                    f"  Saved {jorf_id} with {len(articles)} articles"
                                )
                            else:
                                logger.warning(
                                    f"  No articles found for {jorf_id}, will create placeholder"
                                )
                                scrape_failures.append(law)
                        except Exception as e:
                            logger.warning(f"  Scraping failed for {jorf_id}: {e}")
                            scrape_failures.append(law)

                        if i < len(api_failures) - 1:
                            time.sleep(2)
            except ImportError as e:
                logger.warning(f"Scraping dependencies unavailable: {e}")
                scrape_failures = api_failures

        # 2c. Create placeholders for remaining failures
        if scrape_failures:
            logger.info(
                f"Creating placeholders for {len(scrape_failures)} remaining laws..."
            )
            for law in scrape_failures:
                jorf_id = law["jorf"]
                output_file = legifrance_dir / f"{jorf_id}.json"
                if output_file.exists():
                    continue
                placeholder = {
                    "text": {
                        "id": jorf_id,
                        "title": law["titre"],
                        "numero": law["numero"],
                        "articles": [],
                        "note": "Full text not available from API. Law metadata only.",
                    },
                    "source": "placeholder",
                }
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(placeholder, f, ensure_ascii=False, indent=2)
                logger.info(f"  Created placeholder for {law['titre']}")

        # Summary
        final_existing = {
            p.stem for p in (RAW_DATA_DIR / "legifrance").glob("*.json")
        }
        still_missing = [
            law for law in reference_laws if law["jorf"] not in final_existing
        ]
        if still_missing:
            logger.warning(
                f"Still missing {len(still_missing)} law files after collection"
            )
            success = False
        else:
            logger.info("All law files now present")

    # 3. DILA archives
    try:
        logger.info("--- DILA archives ---")
        from scripts.download_dila_archives import main as download_dila_main

        download_dila_main()
        logger.info("DILA archive download complete")
    except Exception as e:
        logger.warning(f"DILA archive download failed: {e}")

    # 4. AN HTML debates (legislatures 12-13)
    try:
        logger.info("--- AN HTML debates (legislatures 12-13) ---")
        from src.collectors.an_html_debates import ANHtmlCollector

        an_html_dir = RAW_DATA_DIR / "an_html"
        with ANHtmlCollector(an_html_dir) as collector:
            for leg in [12, 13]:
                collector.download_legislature(leg)
            # Commissions élargies (legislature 13 only)
            for year in [2008, 2009, 2010, 2011]:
                collector.download_commissions_elargies(year)
        logger.info("AN HTML debates download complete")
    except Exception as e:
        logger.warning(f"AN HTML debates download failed: {e}")

    # 5. AN HTML amendments (legislatures 12-13, 2002-2012)
    try:
        logger.info("--- AN HTML amendments (legislatures 12-13) ---")
        from src.collectors.an_html_amendments import collect_an_html_amendments

        an_html_dir = RAW_DATA_DIR / "an_html_amendments"
        collect_an_html_amendments(
            output_dir=an_html_dir,
            legislatures=[12, 13],
        )
        logger.info("AN HTML amendments collection complete")
    except Exception as e:
        logger.warning(f"AN HTML amendments collection failed: {e}")

    # 7. Parse AMELI dump (Sénat amendments)
    try:
        logger.info("--- Parse AMELI dump (Sénat amendments) ---")
        ameli_output = RAW_DATA_DIR / "senat" / "ameli" / "amendments"
        if not ameli_output.exists() or not list(ameli_output.glob("text_*.json")):
            from src.parsers.ameli_parser import parse_ameli_dump

            ameli_sql = RAW_DATA_DIR / "senat" / "ameli" / "var" / "opt" / "opendata" / "ameli.sql"
            if ameli_sql.exists():
                parse_ameli_dump(
                    dump_path=ameli_sql,
                    output_dir=ameli_output,
                    cgi_laws_file=CGI_LAWS_FILE,
                )
                logger.info("AMELI dump parsing complete")
            else:
                logger.warning(f"AMELI SQL dump not found: {ameli_sql}")
        else:
            count = len(list(ameli_output.glob("text_*.json")))
            logger.info(f"AMELI already parsed ({count} text files), skipping")
    except Exception as e:
        logger.warning(f"AMELI dump parsing failed: {e}")

    # Collect concordance tables (PLF article → final law article mapping)
    try:
        logger.info("--- Collect concordance tables ---")
        from scripts.collect_concordance_tables import collect_concordance_tables

        collect_concordance_tables()
        logger.info("Concordance table collection complete")
    except Exception as e:
        logger.warning(f"Concordance table collection failed: {e}")

    return success


# ---------------------------------------------------------------------------
# PHASE 2: IMPORT
# ---------------------------------------------------------------------------


def run_import(reset: bool = False, skip_existing: bool = False) -> bool:
    """Execute the import phase."""
    logger.info("=" * 60)
    logger.info("PHASE: IMPORT")
    logger.info("=" * 60)

    # 1. Core import with merged finance laws (critical - halt on failure)
    try:
        logger.info("--- Core import ---")
        from src.db.import_data import run_import as core_import

        reference_laws = load_reference_laws()
        counts = core_import(
            LAWS_FILE,
            RAW_DATA_DIR,
            reset_db=reset,
            laws_data=reference_laws,
            skip_existing=skip_existing,
        )
        logger.info(f"Core import complete. Counts: {counts}")
    except Exception as e:
        logger.error(f"Core import FAILED (critical): {e}")
        return False

    # 2. Dynamic missing laws DB insertion (catch any stragglers)
    try:
        logger.info("--- Dynamic missing laws insertion ---")
        from datetime import datetime

        from src.db.models import Loi, get_session

        session = get_session()
        try:
            existing_numeros = {
                row[0] for row in session.query(Loi.numero).all()
            }
            added = 0
            for law in reference_laws:
                if law["numero"] not in existing_numeros:
                    try:
                        loi = Loi(
                            numero=law["numero"],
                            type=law["type"],
                            annee=law["annee_budgetaire"],
                            date_promulgation=datetime.strptime(
                                law["date_promulgation"], "%Y-%m-%d"
                            ),
                            titre=law["titre"],
                            url_legifrance=law.get("url_legifrance"),
                        )
                        session.add(loi)
                        session.flush()
                        added += 1
                        logger.info(f"  + {law['numero']} - {law['titre'][:60]}...")
                    except Exception as e:
                        session.rollback()
                        logger.warning(f"  Failed to insert {law.get('numero', '?')}: {e}")
            session.commit()
            logger.info(f"Added {added} missing laws to DB")
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"Dynamic missing laws insertion failed: {e}")

    # 2b. Import Sénat dossiers from DOSLEG dump
    try:
        logger.info("--- Sénat dossiers from DOSLEG ---")
        from src.db.import_data import import_dossiers_from_dosleg
        from src.db.models import get_session as get_db_session

        dosleg_sql = RAW_DATA_DIR / "senat" / "dosleg" / "dosleg.sql"
        if dosleg_sql.exists():
            db_session = get_db_session()
            try:
                count = import_dossiers_from_dosleg(db_session, dosleg_sql)
                logger.info(f"DOSLEG dossiers import: {count} new SENAT dossiers")
            finally:
                db_session.close()
        else:
            logger.warning(f"DOSLEG dump not found: {dosleg_sql}")
    except Exception as e:
        logger.warning(f"DOSLEG dossiers import failed: {e}")

    # 3. Import missing articles
    try:
        logger.info("--- Import missing articles ---")
        from scripts.import_missing_articles import main as import_missing_articles_main

        import_missing_articles_main()
        logger.info("Missing articles import complete")
    except Exception as e:
        logger.warning(f"Missing articles import failed: {e}")

    # 4. DILA debates (with contenu_integral)
    try:
        logger.info("--- DILA debates (with contenu_integral) ---")
        from scripts.fix_dila_parser import reimport_dila_debates

        dila_dir = PROJECT_ROOT / "data" / "raw" / "dila"
        if dila_dir.exists():
            # DILA archives available from 2002 to present
            dila_years = list(range(2002, 2026))  # 2002-2025
            reimport_dila_debates(dila_dir, dila_years)
            logger.info("DILA debates import complete")
        else:
            logger.warning(f"DILA directory not found: {dila_dir}")
    except Exception as e:
        logger.warning(f"DILA debates import failed: {e}")

    # 5. Sénat debates
    try:
        logger.info("--- Sénat debates ---")
        from scripts.import_senat_debats import import_senat_debates

        import_senat_debates()
        logger.info("Sénat debates import complete")
    except Exception as e:
        logger.warning(f"Sénat debates import failed: {e}")

    # 6. Additional Sénat debates (150-0 B ter) - kept as fallback
    try:
        logger.info("--- Additional Sénat debates ---")
        from scripts.add_senat_debates import add_senat_debates

        add_senat_debates()
        logger.info("Additional Sénat debates import complete")
    except Exception as e:
        logger.warning(f"Additional Sénat debates import failed: {e}")

    # 6b. Sénat comptes rendus XML
    try:
        logger.info("--- Sénat comptes rendus XML ---")
        from src.db.import_data import import_debates_from_senat_xml
        from src.db.models import get_session as get_db_session

        senat_cr_dir = RAW_DATA_DIR / "senat" / "comptes_rendus"
        if senat_cr_dir.exists():
            db_session = get_db_session()
            try:
                import_debates_from_senat_xml(db_session, senat_cr_dir)
            finally:
                db_session.close()
            logger.info("Sénat CR XML import complete")
        else:
            logger.warning(f"Sénat CR directory not found: {senat_cr_dir}")
    except Exception as e:
        logger.warning(f"Sénat CR XML import failed: {e}")

    # 7. External sources metadata
    try:
        logger.info("--- External sources ---")
        from scripts.import_sources_externes import import_sources

        import_sources()
        logger.info("External sources import complete")
    except Exception as e:
        logger.warning(f"External sources import failed: {e}")

    # 8. Fetch content for sources with NULL contenu
    try:
        logger.info("--- Fetch sources content ---")
        from scripts.fetch_sources_content import fetch_sources_content

        fetch_sources_content()
        logger.info("Sources content fetch complete")
    except Exception as e:
        logger.warning(f"Sources content fetch failed: {e}")

    # 9. AN HTML debates (legislatures 12-13)
    try:
        logger.info("--- AN HTML debates (legislatures 12-13) ---")
        from src.db.import_data import import_orphan_debat
        from src.db.models import get_session
        from src.parsers.an_html_debates import parse_an_html_debate

        an_html_dir = RAW_DATA_DIR / "an_html"
        if an_html_dir.exists():
            db_session = get_session()
            try:
                count = 0
                failed = 0
                for html_file in sorted(an_html_dir.rglob("*.html")):
                    try:
                        parsed = parse_an_html_debate(html_file)
                        if parsed and parsed.interventions:
                            import_orphan_debat(db_session, parsed)
                            count += 1
                            if count % 50 == 0:
                                db_session.commit()
                                logger.info(f"Imported {count} AN HTML debates...")
                    except Exception as e:
                        db_session.rollback()
                        failed += 1
                        logger.debug(f"Failed to parse/import {html_file.name}: {e}")
                        continue
                db_session.commit()
                logger.info(f"AN HTML debates import complete: {count} imported, {failed} failed")
            finally:
                db_session.close()
        else:
            logger.warning(f"AN HTML directory not found: {an_html_dir}")
    except Exception as e:
        logger.warning(f"AN HTML debates import failed: {e}")

    # 10. Sénat CRI bulk debates (since Jan 2003)
    try:
        logger.info("--- Sénat CRI bulk debates ---")
        from src.db.import_data import import_debates_from_senat_cri
        from src.db.models import get_session as get_db_session

        cri_dir = RAW_DATA_DIR / "senat" / "cri"
        if cri_dir.exists() and (list(cri_dir.glob("d*.xml")) or list(cri_dir.glob("CRI_*.xml"))):
            db_session = get_db_session()
            try:
                debats, intervs = import_debates_from_senat_cri(db_session, cri_dir)
                logger.info(f"Sénat CRI import: {debats} debates, {intervs} interventions")
            finally:
                db_session.close()
        else:
            logger.warning(f"No CRI XML files found in {cri_dir} (run collect phase with Sénat CRI archive first)")
    except Exception as e:
        logger.warning(f"Sénat CRI import failed: {e}")

    # 11. AN HTML amendments (legislatures 12-13, 2002-2012)
    try:
        logger.info("--- AN HTML amendments (legislatures 12-13) ---")
        from src.db.import_data import import_amendments_from_an_html
        from src.db.models import get_session as get_db_session

        an_html_dir = RAW_DATA_DIR / "an_html_amendments"
        if an_html_dir.exists():
            db_session = get_db_session()
            try:
                count = import_amendments_from_an_html(db_session, an_html_dir)
                logger.info(f"AN HTML amendments import: {count} amendments")
            finally:
                db_session.close()
        else:
            logger.warning(f"No AN HTML data found in {an_html_dir} (run collect phase first)")
    except Exception as e:
        logger.warning(f"AN HTML amendments import failed: {e}")

    # 12. AMELI Sénat amendments (parsed from PostgreSQL dump)
    try:
        logger.info("--- AMELI Sénat amendments ---")
        from src.db.import_data import import_amendments_from_ameli
        from src.db.models import get_session as get_db_session

        ameli_dir = RAW_DATA_DIR / "senat" / "ameli" / "amendments"
        if ameli_dir.exists() and list(ameli_dir.glob("text_*.json")):
            db_session = get_db_session()
            try:
                count = import_amendments_from_ameli(db_session, ameli_dir)
                logger.info(f"AMELI import: {count} amendments")
            finally:
                db_session.close()
        else:
            logger.warning(f"No AMELI parsed data found in {ameli_dir} (run collect phase first)")
    except Exception as e:
        logger.warning(f"AMELI amendments import failed: {e}")

    # 13. Extract articles from DILA LEGI dump (pre-2002 laws missing articles)
    try:
        logger.info("--- Extract articles from DILA LEGI dump ---")
        from scripts.extract_legi_articles import main as extract_legi_main

        extract_legi_main()
        logger.info("LEGI article extraction complete")
    except Exception as e:
        logger.warning(f"LEGI article extraction failed: {e}")

    # 14. Import extracted LEGI articles
    try:
        logger.info("--- Import LEGI articles ---")
        from scripts.import_legi_articles import main as import_legi_main

        import_legi_main()
        logger.info("LEGI article import complete")
    except Exception as e:
        logger.warning(f"LEGI article import failed: {e}")

    return True


# ---------------------------------------------------------------------------
# PHASE 3: LINK
# ---------------------------------------------------------------------------


def run_link() -> bool:
    """Execute the linkage phase."""
    logger.info("=" * 60)
    logger.info("PHASE: LINK")
    logger.info("=" * 60)

    # 1. Link debates to dossiers
    try:
        logger.info("--- Link debates to dossiers ---")
        from scripts.link_debates_to_dossiers import link_debates_to_dossiers

        link_debates_to_dossiers()
        logger.info("Debate-dossier linking complete")
    except Exception as e:
        logger.warning(f"Debate-dossier linking failed: {e}")

    # 2. Link orphaned debates
    try:
        logger.info("--- Link orphaned debates ---")
        from scripts.link_orphaned_debates import main as link_orphaned_main

        link_orphaned_main()
        logger.info("Orphaned debate linking complete")
    except Exception as e:
        logger.warning(f"Orphaned debate linking failed: {e}")

    # 3. Fix amendment linkage
    try:
        logger.info("--- Fix amendment linkage ---")
        from scripts.fix_amendement_linkage import fix_amendement_linkage

        fix_amendement_linkage()
        logger.info("Amendment linkage fix complete")
    except Exception as e:
        logger.warning(f"Amendment linkage fix failed: {e}")

    # 4. Advanced data linkage (more sophisticated matching)
    try:
        logger.info("--- Advanced data linkage ---")
        from scripts.fix_data_linkage import (
            fix_debate_dossier_linking,
            fix_amendment_dossier_linking,
            collect_missing_articles,
        )
        from src.db.models import get_session as get_db_session, init_db

        init_db()
        session = get_db_session()
        try:
            fix_debate_dossier_linking(session)
            fix_amendment_dossier_linking(session)
            collect_missing_articles(session)
            # link_amendments_to_articles removed: fix_amendement_linkage.py
            # (step 3 above) handles this faster with concordance support
        finally:
            session.close()
        logger.info("Advanced data linkage complete")
    except Exception as e:
        logger.warning(f"Advanced data linkage failed: {e}")

    # 5. Create missing Sénat dossiers and link orphaned debates
    try:
        logger.info("--- Pre-2002 Sénat dossiers + orphan linking ---")
        from scripts.fix_pre2002_senat_dossiers import main as fix_senat_dossiers_main

        fix_senat_dossiers_main()
        logger.info("Pre-2002 Sénat dossiers fix complete")
    except Exception as e:
        logger.warning(f"Pre-2002 Sénat dossiers fix failed: {e}")

    # 6. Extract amendments from pre-2001 debate text
    try:
        logger.info("--- Extract amendments from pre-2001 debates ---")
        from scripts.extract_amendments_from_debates import main as extract_amend_main

        extract_amend_main()
        logger.info("Pre-2001 amendment extraction complete")
    except Exception as e:
        logger.warning(f"Pre-2001 amendment extraction failed: {e}")

    # 7. Deduplicate extracted amendments (same numero in same dossier)
    try:
        logger.info("--- Dedup extracted amendments ---")
        from scripts.dedup_extracted_amendments import main as dedup_amend_main

        dedup_amend_main()
        logger.info("Amendment deduplication complete")
    except Exception as e:
        logger.warning(f"Amendment deduplication failed: {e}")

    return True


# ---------------------------------------------------------------------------
# PHASE 4: FIX
# ---------------------------------------------------------------------------


def run_fix() -> bool:
    """Execute the fix phase."""
    logger.info("=" * 60)
    logger.info("PHASE: FIX")
    logger.info("=" * 60)

    # 1. Encoding fixes
    try:
        logger.info("--- Encoding fixes ---")
        from scripts.fix_encoding import fix_interventions, fix_sources_externes

        fix_interventions()
        fix_sources_externes()
        logger.info("Encoding fixes complete")
    except Exception as e:
        logger.warning(f"Encoding fixes failed: {e}")

    # 2. Fix misdated AN debates (OCR imports dated 1958-10-04)
    try:
        logger.info("--- Fix misdated AN debates ---")
        from scripts.fix_misdated_an_debates import main as fix_misdated_main

        fix_misdated_main()
        logger.info("Misdated AN debates fix complete")
    except Exception as e:
        logger.warning(f"Misdated AN debates fix failed: {e}")

    # 3. Fix empty debates (re-parse AN, delete empty SENAT)
    try:
        logger.info("--- Fix empty debates ---")
        from scripts.fix_empty_debates import main as fix_empty_main

        fix_empty_main()
        logger.info("Empty debates fix complete")
    except Exception as e:
        logger.warning(f"Empty debates fix failed: {e}")

    # 4. Remove consecutive duplicate interventions (parsing artifacts)
    try:
        logger.info("--- Fix consecutive duplicate interventions ---")
        from scripts.fix_consecutive_duplicates import main as fix_consec_main

        fix_consec_main()
        logger.info("Consecutive duplicates fix complete")
    except Exception as e:
        logger.warning(f"Consecutive duplicates fix failed: {e}")

    # 5. Enrich fonction field on Sénat interventions
    try:
        logger.info("--- Enrich Sénat intervention fonctions ---")
        from scripts.enrich_fonctions import main as enrich_fonctions_main

        enrich_fonctions_main()
        logger.info("Fonction enrichment complete")
    except Exception as e:
        logger.warning(f"Fonction enrichment failed: {e}")

    return True


# ---------------------------------------------------------------------------
# PHASE 5: EMBED
# ---------------------------------------------------------------------------


def run_embed() -> bool:
    """Execute the embeddings phase."""
    logger.info("=" * 60)
    logger.info("PHASE: EMBED")
    logger.info("=" * 60)

    # 1. Embeddings generation
    from src.utils.config import get_settings

    settings = get_settings()
    if settings.embedding_provider == "openai" and not settings.openai_api_key:
        logger.warning(
            "EMBEDDING_PROVIDER=openai but OPENAI_API_KEY not set - "
            "skipping embeddings generation."
        )
    else:
        try:
            logger.info(f"--- Embeddings generation (provider: {settings.embedding_provider}) ---")
            from scripts.run_pipeline import generate_embeddings

            generate_embeddings(EMBEDDINGS_DIR)
            logger.info("Embeddings generation complete")
        except Exception as e:
            logger.error(f"Embeddings generation failed: {e}")
            raise

    # 2. Term expansions
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        logger.warning(
            "ANTHROPIC_API_KEY not set - skipping term expansions. "
            "Set this environment variable to enable term expansions."
        )
    else:
        try:
            logger.info("--- Term expansions ---")
            from scripts.generate_term_expansions import main as term_expansions_main

            term_expansions_main()
            logger.info("Term expansions generation complete")
        except Exception as e:
            logger.warning(f"Term expansions generation failed: {e}")

    return True


# ---------------------------------------------------------------------------
# PHASE 6: INDEX
# ---------------------------------------------------------------------------


def run_index() -> bool:
    """Execute the indexing phase for analysis preparation."""
    logger.info("=" * 60)
    logger.info("PHASE: INDEX")
    logger.info("=" * 60)

    # 1. Generate article modifications index
    try:
        logger.info("--- Generate article modifications index ---")
        from scripts.generate_article_index import (
            build_article_index,
            enrich_from_reference,
            enrich_law_titles,
            generate_article_keywords,
            load_law_titles,
        )
        from src.analysis.cgi_extractor import extract_cgi_modifications_from_db
        from src.db.models import get_session
        import json

        session = get_session()
        try:
            # Load existing index to preserve curated data (jurisprudence, quotes, etc.)
            existing_index = None
            index_path = PROJECT_ROOT / "data" / "article_modifications.json"
            if index_path.exists():
                with open(index_path, encoding="utf-8") as f:
                    existing_index = json.load(f)
                logger.info(f"Loaded existing index with {len(existing_index.get('articles', {}))} articles")

            # Extract CGI modifications from database
            modifications = extract_cgi_modifications_from_db(session, min_confidence=0.5)

            # Build the index, merging with existing to preserve curated data
            index = build_article_index(modifications, existing_index=existing_index)

            # Enrich with reference data
            reference_path = PROJECT_ROOT / "data" / "cgi_articles_reference.json"
            index = enrich_from_reference(index, reference_path)

            # Enrich with law titles
            law_titles = load_law_titles(session)
            index = enrich_law_titles(index, law_titles)

            # Generate keywords
            index = generate_article_keywords(index)

            # Save to file
            output_path = PROJECT_ROOT / "data" / "article_modifications.json"
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)

            article_count = len(index.get("articles", {}))
            logger.info(f"Article index generated: {article_count} articles saved to {output_path}")

        finally:
            session.close()

    except Exception as e:
        logger.warning(f"Article index generation failed: {e}")

    return True


# ---------------------------------------------------------------------------
# PHASE 7: VALIDATE
# ---------------------------------------------------------------------------


def run_validate() -> bool:
    """Execute the validation phase."""
    logger.info("=" * 60)
    logger.info("PHASE: VALIDATE")
    logger.info("=" * 60)

    try:
        from sqlalchemy import text as sa_text

        from src.db.models import (
            Amendement,
            Article,
            Debat,
            DossierLegislatif,
            Intervention,
            Loi,
            SourceExterne,
            get_session,
        )

        session = get_session()
        try:
            results = {
                "lois": session.query(Loi).count(),
                "articles": session.query(Article).count(),
                "dossiers": session.query(DossierLegislatif).count(),
                "debats": session.query(Debat).count(),
                "interventions": session.query(Intervention).count(),
                "amendements": session.query(Amendement).count(),
                "sources_externes": session.query(SourceExterne).count(),
            }

            # Completeness checks
            results["lois_with_articles"] = (
                session.query(Loi).join(Article).distinct().count()
            )
            results["lois_with_dossiers"] = (
                session.query(Loi).join(DossierLegislatif).distinct().count()
            )

            for loi_type in ["LF", "LFR", "LFSS"]:
                results[f"lois_{loi_type}"] = (
                    session.query(Loi).filter(Loi.type == loi_type).count()
                )

            logger.info("Validation Results:")
            for key, value in results.items():
                logger.info(f"  {key}: {value}")

            # --- Data quality checks ---
            warnings = []

            # Check: no misdated archive_an debates
            misdated = session.execute(sa_text(
                "SELECT COUNT(*) FROM debats "
                "WHERE reference_externe LIKE 'archive_an:%' "
                "AND date = '1958-10-04'"
            )).scalar()
            if misdated > 0:
                warnings.append(f"{misdated} AN debates still dated 1958-10-04")

            # Check: no empty debates (0 interventions and no contenu_integral)
            empty_debates = session.execute(sa_text(
                "SELECT COUNT(*) FROM debats d "
                "LEFT JOIN interventions i ON d.id = i.debat_id "
                "GROUP BY d.id HAVING COUNT(i.id) = 0"
            )).fetchall()
            if len(empty_debates) > 5:
                warnings.append(f"{len(empty_debates)} debates with 0 interventions")

            # Check: orphaned SENAT debates
            orphaned_senat = session.execute(sa_text(
                "SELECT COUNT(*) FROM debats "
                "WHERE chambre = 'SENAT' AND dossier_id IS NULL"
            )).scalar()
            if orphaned_senat > 0:
                logger.info(f"  orphaned_senat_debates: {orphaned_senat}")

            # Check: amendments by era
            pre2001_amend = session.execute(sa_text(
                "SELECT COUNT(*) FROM amendements "
                "WHERE reference_externe LIKE 'extracted_from_debate:%'"
            )).scalar()
            logger.info(f"  extracted_pre2001_amendments: {pre2001_amend}")

            # Check: SENAT dossiers
            senat_dossiers = session.execute(sa_text(
                "SELECT COUNT(*) FROM dossiers_legislatifs WHERE chambre = 'SENAT'"
            )).scalar()
            logger.info(f"  senat_dossiers: {senat_dossiers}")

            if warnings:
                logger.warning("Data quality warnings:")
                for w in warnings:
                    logger.warning(f"  ! {w}")

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Validation failed: {e}")
        return False

    return True


# ---------------------------------------------------------------------------
# MAIN ORCHESTRATION
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Run the full legislative data pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument("--collect", action="store_true", help="Run data collection")
    parser.add_argument(
        "--import",
        dest="do_import",
        action="store_true",
        help="Run database import",
    )
    parser.add_argument(
        "--link", action="store_true", help="Run data linkage (debates-dossiers, amendments-articles)"
    )
    parser.add_argument(
        "--fix", action="store_true", help="Run data fixes (encoding)"
    )
    parser.add_argument(
        "--embed",
        action="store_true",
        help="Generate embeddings + term expansions",
    )
    parser.add_argument(
        "--index",
        action="store_true",
        help="Generate article modification index for analysis",
    )
    parser.add_argument(
        "--validate", action="store_true", help="Run data validation"
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset database before import",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help=(
            "Import optimization: skip heavy import steps when data already exists "
            "(applies to --import)"
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run all phases: collect → import → link → fix → embed → index → validate",
    )

    args = parser.parse_args()

    # --all expands to all flags
    if args.all:
        args.collect = True
        args.do_import = True
        args.link = True
        args.fix = True
        args.embed = True
        args.index = True
        args.validate = True

    # No flags → print help and exit
    if not any(
        [args.collect, args.do_import, args.link, args.fix, args.embed, args.index, args.validate]
    ):
        parser.print_help()
        sys.exit(0)

    # Warn if --reset without --import
    if args.reset and not args.do_import:
        logger.warning("--reset has no effect without --import. Add --import to reset the database.")
    if args.skip_existing and not args.do_import:
        logger.warning("--skip-existing has no effect without --import. Add --import to enable it.")

    # Ensure CWD is project root (some scripts use relative DB paths)
    os.chdir(PROJECT_ROOT)

    # Check reference file
    if not LAWS_FILE.exists():
        logger.error(f"Reference file not found: {LAWS_FILE}")
        sys.exit(1)

    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Phase definitions: (name, flag, function, args)
    phases = [
        ("COLLECT", args.collect, run_collect, {}),
        (
            "IMPORT",
            args.do_import,
            run_import,
            {"reset": args.reset, "skip_existing": args.skip_existing},
        ),
        ("LINK", args.link, run_link, {}),
        ("FIX", args.fix, run_fix, {}),
        ("EMBED", args.embed, run_embed, {}),
        ("INDEX", args.index, run_index, {}),
        ("VALIDATE", args.validate, run_validate, {}),
    ]

    # Selected phases
    selected = [(name, fn, kw) for name, flag, fn, kw in phases if flag]
    logger.info(
        f"Starting full pipeline with phases: {', '.join(name for name, _, _ in selected)}"
    )

    pipeline_start = time.time()
    results = []

    try:
        for name, fn, kwargs in selected:
            phase_start = time.time()
            try:
                success = fn(**kwargs)
                duration = time.time() - phase_start
                status = "SUCCESS" if success else "PARTIAL"
                results.append((name, status, duration))
            except Exception as e:
                duration = time.time() - phase_start
                logger.error(f"Phase {name} failed with exception: {e}")
                results.append((name, "FAILED", duration))
                # Only halt on critical phases
                if name in ("IMPORT",):
                    logger.error(f"Critical phase {name} failed - halting pipeline")
                    break

    except KeyboardInterrupt:
        logger.warning("Pipeline interrupted by user")
        results.append(("INTERRUPTED", "ABORTED", 0))

    # Print summary
    total_duration = time.time() - pipeline_start
    logger.info("")
    logger.info("=" * 60)
    logger.info("PIPELINE SUMMARY")
    logger.info("=" * 60)
    logger.info(f"{'Phase':<12} {'Status':<10} {'Duration':<12}")
    logger.info("-" * 34)
    for name, status, duration in results:
        logger.info(f"{name:<12} {status:<10} {duration:>8.1f}s")
    logger.info("-" * 34)
    logger.info(f"{'TOTAL':<12} {'':10} {total_duration:>8.1f}s")
    logger.info("=" * 60)

    # Exit code: 1 for failures, 2 for partial success, 0 for full success
    has_failure = any(status == "FAILED" for _, status, _ in results)
    has_partial = any(status == "PARTIAL" for _, status, _ in results)
    if has_failure:
        sys.exit(1)
    elif has_partial:
        sys.exit(2)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
