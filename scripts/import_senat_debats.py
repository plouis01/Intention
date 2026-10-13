#!/usr/bin/env python3
"""
Systematic Sénat debate importer.

Generates session URLs from the reference laws file and fetches debates
for each law's expected Sénat debate period, replacing the previous
hardcoded URL approach.
"""

import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.collectors.senat_debats import SenatDebatCollector
from src.db.import_data import FINANCE_LAW_KEYWORDS, find_dossier_for_debate
from src.db.models import Debat, DossierLegislatif, Intervention, Loi, get_session
from src.parsers.debates import extract_article_mentions

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
LAWS_FILE = PROJECT_ROOT / "data" / "lois_finances_2012_2025.json"
CGI_LAWS_FILE = PROJECT_ROOT / "data" / "lois_cgi_1958_2026.json"


def compute_debate_windows(laws: list[dict]) -> list[dict]:
    """
    Compute expected Sénat debate date windows for each law.

    Returns list of dicts with: law_numero, law_type, start_date, end_date, description
    """
    windows = []

    for law in laws:
        law_type = law["type"]
        annee = law["annee_budgetaire"]
        date_promulgation = datetime.strptime(law["date_promulgation"], "%Y-%m-%d")

        if law_type == "LF":
            # LF debates: October to December of year N-1
            start = datetime(annee - 1, 10, 1)
            end = datetime(annee - 1, 12, 31)
        elif law_type == "LFSS":
            # LFSS debates: October to November of year N-1
            start = datetime(annee - 1, 10, 1)
            end = datetime(annee - 1, 12, 15)
        elif law_type == "LFR":
            # LFR debates: variable — 30 to 90 days before promulgation
            end = date_promulgation
            start = date_promulgation - timedelta(days=90)
        else:
            continue

        windows.append({
            "law_numero": law["numero"],
            "law_type": law_type,
            "annee": annee,
            "start_date": start,
            "end_date": end,
            "description": law["titre"],
        })

    return windows


def generate_senat_urls(start_date: datetime, end_date: datetime) -> list[tuple[datetime, str]]:
    """
    Generate Sénat mono session URLs for each date in the window.

    The pattern is: https://www.senat.fr/seances/s{YYYYMM}/s{YYYYMMDD}/s{YYYYMMDD}_mono.html

    Returns list of (date, url) tuples.
    """
    urls = []
    current = start_date

    while current <= end_date:
        date_str = current.strftime("%Y%m%d")
        month_str = current.strftime("%Y%m")
        url = f"https://www.senat.fr/seances/s{month_str}/s{date_str}/s{date_str}_mono.html"
        urls.append((current, url))
        current += timedelta(days=1)

    return urls


def import_senat_debates():
    """Import Sénat debates systematically from reference laws file."""

    # Load reference laws (CGI + finance overlay, LF/LFR/LFSS only, 2002+)
    finance_types = {"LF", "LFR", "LFSS"}
    min_date = "2002-01-01"
    laws_by_numero = {}

    if CGI_LAWS_FILE.exists():
        with open(CGI_LAWS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for loi in data.get("lois", []):
            if (loi.get("numero")
                    and loi.get("type") in finance_types
                    and loi.get("date_promulgation", "") >= min_date):
                laws_by_numero[loi["numero"]] = loi

    if LAWS_FILE.exists():
        with open(LAWS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for loi in data["lois"]:
            if (loi.get("numero")
                    and loi.get("date_promulgation", "") >= min_date):
                laws_by_numero[loi["numero"]] = loi

    laws = sorted(laws_by_numero.values(),
                  key=lambda x: x.get("date_promulgation", "0000"))
    windows = compute_debate_windows(laws)

    print(f"Computed {len(windows)} debate windows from {len(laws)} laws\n")

    session = get_session()
    collector = SenatDebatCollector()

    # Load lois and dossiers for content-based matching
    lois = session.query(Loi).all()
    dossiers_list = session.query(DossierLegislatif).all()

    # Collect all unique URLs to avoid duplicates across overlapping windows
    all_urls: dict[str, dict] = {}  # url -> {date, law_info}
    for window in windows:
        urls = generate_senat_urls(window["start_date"], window["end_date"])
        for date, url in urls:
            if url not in all_urls:
                all_urls[url] = {
                    "date": date,
                    "description": window["description"],
                    "law_numero": window["law_numero"],
                }

    print(f"Generated {len(all_urls)} unique URLs to check\n")

    # Filter out already imported URLs and references
    existing_urls = {
        row[0] for row in session.query(Debat.url_source).filter(
            Debat.chambre == "SENAT"
        ).all() if row[0]
    }
    existing_refs = {
        row[0] for row in session.query(Debat.reference_externe).filter(
            Debat.chambre == "SENAT"
        ).all() if row[0]
    }

    urls_to_fetch = {
        url: info for url, info in all_urls.items()
        if url not in existing_urls
    }

    print(f"Skipping {len(all_urls) - len(urls_to_fetch)} already imported, "
          f"{len(urls_to_fetch)} to fetch\n")

    imported = 0
    skipped_404 = 0
    failed = 0

    # Sort by date for consistent ordering
    sorted_urls = sorted(urls_to_fetch.items(), key=lambda x: x[1]["date"])

    for i, (url, info) in enumerate(sorted_urls):
        if (i + 1) % 50 == 0:
            print(f"Progress: {i + 1}/{len(sorted_urls)} "
                  f"(imported: {imported}, 404: {skipped_404}, failed: {failed})")

        try:
            # Fetch from senat.fr
            result = collector.fetch_seance(url)

            if result is None:
                skipped_404 += 1
                time.sleep(0.5)
                continue

            interventions = result.get("interventions", [])
            if not interventions:
                skipped_404 += 1
                time.sleep(0.5)
                continue

            # Build full text for content matching
            full_text = " ".join(
                interv.get("contenu", "") for interv in interventions
            )

            # Check if this contains finance law content
            content_lower = full_text.lower()
            is_finance = any(kw in content_lower for kw in FINANCE_LAW_KEYWORDS)

            if not is_finance:
                time.sleep(0.5)
                continue

            # Create debate record
            date = info["date"]
            ref = f"senat_{date.strftime('%Y%m%d')}_mono"

            # Check reference_externe too (in case URL format varies)
            if ref in existing_refs:
                time.sleep(0.5)
                continue

            # Match to a dossier based on content
            matched_dossier = find_dossier_for_debate(
                dossiers_list, lois, full_text[:50000], date, chambre="SENAT"
            )

            debat = Debat(
                dossier_id=matched_dossier.id if matched_dossier else None,
                chambre="SENAT",
                date=date,
                etape="seance_publique",
                url_source=url,
                reference_externe=ref,
                contenu_integral=full_text[:50000] if full_text else None,
            )
            session.add(debat)
            session.flush()

            # Add interventions
            for j, interv in enumerate(interventions):
                intervention = Intervention(
                    debat_id=debat.id,
                    orateur=interv["orateur"],
                    fonction=interv.get("fonction"),
                    contenu=interv["contenu"],
                    articles_mentionnes=extract_article_mentions(interv["contenu"]),
                    ordre=j + 1,
                )
                session.add(intervention)

            imported += 1
            print(f"  + {date.strftime('%Y-%m-%d')}: {len(interventions)} interventions "
                  f"({info['description'][:50]})")

            # Commit every 5 debates
            if imported % 5 == 0:
                session.commit()

        except Exception as e:
            session.rollback()
            failed += 1
            logger.warning(f"Error processing {url}: {e}")

        # Rate limit between requests
        time.sleep(1)

    session.commit()
    collector.close()

    # Summary
    print(f"\n--- Summary ---")
    print(f"Imported: {imported} new Sénat debates")
    print(f"Skipped (404/empty): {skipped_404}")
    print(f"Failed: {failed}")

    total_senat = session.query(Debat).filter_by(chambre="SENAT").count()
    total_an = session.query(Debat).filter_by(chambre="AN").count()
    print(f"Total debates: {total_senat} Sénat + {total_an} AN = {total_senat + total_an}")

    session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Systematic Sénat debate import...\n")
    import_senat_debates()
