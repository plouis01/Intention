#!/usr/bin/env python3
"""Fill curated_quotes for all articles missing them.

Runs 4 passes in sequence, targeting articles with no quotes, prioritized by tier:
  - Tier A: 6+ modifications (high-priority)
  - Tier B: 2-5 modifications
  - Tier C: 0-1 modifications

Pass 1: DB-direct search (reuses batch_enrich_quotes.py logic)
Pass 2: CGI mention index search (reuses fill_quotes_and_juris.py logic)
Pass 3: Keyword-based DB search (new logic for articles not in CGI mention index)
Pass 4: Tier C initialization (empty curated_quotes + synthetic objet entries)

Usage:
    python scripts/fill_all_missing_quotes.py
    python scripts/fill_all_missing_quotes.py --dry-run --tier=A
    python scripts/fill_all_missing_quotes.py --pass=3 --verbose
"""

import argparse
import json
import logging
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# ── Ensure project root on sys.path ─────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from batch_enrich_quotes import (
    classify_ref_ambiguity,
    build_article_pattern,
    extract_quote,
    is_quality_quote,
    assign_year,
    deduplicate_quote,
    PROCEDURAL_PATTERNS,
    CGI_RELAXED,
    OTHER_CODES,
)
from fill_quotes_and_juris import (
    build_relevance_keywords,
    score_mention,
    extract_best_quotes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
INDEX_PATH = PROJECT_ROOT / "data" / "article_modifications.json"
BACKUP_PATH = PROJECT_ROOT / "data" / "article_modifications.backup.json"
CGI_INDEX_PATH = PROJECT_ROOT / "data" / "cgi_mention_index.json"
DB_PATH = PROJECT_ROOT / "data" / "legislateur.db"

MAX_NEW_PER_ARTICLE = 10
MAX_TOTAL_PER_ARTICLE = 15


# ── Helpers ──────────────────────────────────────────────────────────────────

def has_quotes(art_data: dict) -> bool:
    """Check if an article already has non-empty curated_quotes."""
    cq = art_data.get("curated_quotes")
    if not isinstance(cq, dict):
        return False
    return sum(len(v) for v in cq.values() if isinstance(v, list)) > 0


def get_tier(art_data: dict) -> str:
    """Classify article into tier A/B/C based on modification count."""
    mods = len(art_data.get("modifications", []))
    if mods >= 6:
        return "A"
    if mods >= 2:
        return "B"
    return "C"


def get_mod_years(art_data: dict) -> list[int]:
    """Extract sorted list of modification years."""
    years = []
    for mod in art_data.get("modifications", []):
        y = mod.get("annee") or (mod.get("date", "")[:4] if mod.get("date") else None)
        if y:
            years.append(int(y))
    return sorted(set(years))


def build_debate_mappings(articles: dict, conn: sqlite3.Connection):
    """Build date→debates and article→debate_ids mappings from DILA sources."""
    date_to_debates = defaultdict(list)
    rows = conn.execute("SELECT id, chambre, date FROM debats").fetchall()
    for r in rows:
        date_str = str(r["date"])[:10]
        date_key = date_str.replace("-", "")[:8]
        date_to_debates[date_key].append({
            "id": r["id"],
            "chambre": r["chambre"],
            "date": date_str,
        })

    article_debate_ids = defaultdict(set)
    for art_name, art_data in articles.items():
        for dila in art_data.get("dila_sources", []):
            m = re.search(r"(\d{8})", dila)
            if m:
                date_key = m.group(1)
                for debate in date_to_debates.get(date_key, []):
                    article_debate_ids[art_name].add(debate["id"])

    return article_debate_ids


def get_no_quote_articles(articles: dict, tier_filter: str | None) -> list[str]:
    """Return sorted list of article refs that have no quotes, optionally filtered by tier."""
    result = []
    for ref, art in articles.items():
        if has_quotes(art):
            continue
        if tier_filter and tier_filter != "all":
            if get_tier(art) != tier_filter:
                continue
        result.append(ref)
    return sorted(result)


# ── Pass 1: DB-direct search ────────────────────────────────────────────────

def pass1_db_direct(articles: dict, conn: sqlite3.Connection, stats: dict,
                    targets: list[str], verbose: bool) -> None:
    """Search DILA-mapped debate interventions for 'article {ref}' mentions."""
    logger.info(f"Pass 1: DB-direct search for {len(targets)} articles")

    article_debate_ids = build_debate_mappings(articles, conn)
    enriched = 0
    added = 0

    for i, art_name in enumerate(targets):
        art_data = articles[art_name]
        if has_quotes(art_data):
            continue  # Already filled by prior processing

        debate_ids = article_debate_ids.get(art_name, set())
        if not debate_ids:
            continue

        ambiguity = classify_ref_ambiguity(art_name)
        pattern = build_article_pattern(art_name)
        mod_years = get_mod_years(art_data)

        debate_id_list = list(debate_ids)
        placeholders = ",".join("?" * len(debate_id_list))

        if ambiguity == "extreme":
            extra_sql = " AND (i.contenu LIKE '%code général des impôts%' OR i.contenu LIKE '% CGI%' OR i.contenu LIKE '%du CGI%')"
        else:
            extra_sql = ""

        like_pattern = f"%article {art_name}%"

        rows = conn.execute(f"""
            SELECT i.orateur, i.fonction, i.contenu, d.chambre, d.date
            FROM interventions i
            JOIN debats d ON i.debat_id = d.id
            WHERE i.debat_id IN ({placeholders})
              AND i.contenu LIKE ?
              AND LENGTH(i.contenu) > 80
              AND i.fonction NOT IN ('président de séance', 'president')
              AND i.orateur NOT LIKE '%président%'
              AND i.orateur NOT LIKE '%présidente%'
              {extra_sql}
            ORDER BY d.date
        """, debate_id_list + [like_pattern]).fetchall()

        # Get existing quotes (may have been set to empty dict)
        curated = art_data.get("curated_quotes", {})
        if not isinstance(curated, dict):
            curated = {}
        existing_flat = []
        for year_quotes in curated.values():
            if isinstance(year_quotes, list):
                existing_flat.extend(year_quotes)

        new_quotes_by_year = defaultdict(list)
        total_new = 0

        for row in rows:
            if total_new >= MAX_NEW_PER_ARTICLE:
                break
            if len(existing_flat) + total_new >= MAX_TOTAL_PER_ARTICLE:
                break

            contenu = row["contenu"]
            orateur = row["orateur"] or ""
            fonction = row["fonction"] or ""
            chambre = row["chambre"] or ""
            date = str(row["date"])[:10]

            if not pattern.search(contenu):
                continue

            quote_text = extract_quote(contenu, art_name)
            if not quote_text:
                continue

            if not is_quality_quote(quote_text, orateur, fonction, art_name,
                                    ambiguity, full_contenu=contenu):
                stats["pass1_rejected_quality"] += 1
                continue

            year = assign_year(date, mod_years)
            if not year:
                continue

            all_existing = existing_flat + [
                q for qs in new_quotes_by_year.values() for q in qs
            ]
            if deduplicate_quote(quote_text, all_existing):
                stats["pass1_rejected_dedup"] += 1
                continue

            clean_orateur = re.sub(r"^(M\.|Mme)\s+le\s+président\.\s*", "", orateur).strip()
            if not clean_orateur:
                clean_orateur = orateur

            new_quotes_by_year[year].append({
                "citation": quote_text,
                "orateur": clean_orateur,
                "fonction": fonction,
                "date": date,
                "chambre": chambre.upper() if chambre else "",
                "source": "db_direct",
            })
            total_new += 1

        if new_quotes_by_year:
            if "curated_quotes" not in art_data:
                art_data["curated_quotes"] = {}
            for year, quotes in new_quotes_by_year.items():
                if year not in art_data["curated_quotes"]:
                    art_data["curated_quotes"][year] = []
                art_data["curated_quotes"][year].extend(quotes)

            enriched += 1
            added += total_new
            if verbose:
                logger.info(f"  Pass1 {art_name}: +{total_new} quotes")

        if (i + 1) % 500 == 0:
            logger.info(f"  Pass 1 progress: {i+1}/{len(targets)}")

    stats["pass1_enriched"] = enriched
    stats["pass1_added"] = added
    logger.info(f"Pass 1 done: {enriched} articles enriched, {added} quotes added")


# ── Pass 2: CGI mention index search ────────────────────────────────────────

def pass2_cgi_index(articles: dict, cgi_index: dict, stats: dict,
                    targets: list[str], verbose: bool) -> None:
    """Search CGI mention index for best quotes."""
    logger.info(f"Pass 2: CGI mention index search for {len(targets)} articles")

    # Filter to articles still without quotes
    remaining = [ref for ref in targets if not has_quotes(articles[ref])]
    logger.info(f"  {len(remaining)} still without quotes after Pass 1")

    enriched = 0
    added = 0

    for ref in remaining:
        art_data = articles[ref]
        mentions = cgi_index.get(ref, [])
        if not mentions:
            continue

        quotes = extract_best_quotes(ref, art_data, mentions)
        if not quotes:
            continue

        # Merge into existing curated_quotes
        if "curated_quotes" not in art_data:
            art_data["curated_quotes"] = {}

        total_new = 0
        for year, year_quotes in quotes.items():
            if year not in art_data["curated_quotes"]:
                art_data["curated_quotes"][year] = []

            existing_flat = []
            for yqs in art_data["curated_quotes"].values():
                if isinstance(yqs, list):
                    existing_flat.extend(yqs)

            for q in year_quotes:
                if not deduplicate_quote(q["citation"], existing_flat):
                    q["source"] = "cgi_index"
                    art_data["curated_quotes"][year].append(q)
                    existing_flat.append(q)
                    total_new += 1

        if total_new > 0:
            enriched += 1
            added += total_new
            if verbose:
                logger.info(f"  Pass2 {ref}: +{total_new} quotes")

    stats["pass2_enriched"] = enriched
    stats["pass2_added"] = added
    logger.info(f"Pass 2 done: {enriched} articles enriched, {added} quotes added")


# ── Pass 3: Keyword-based DB search ─────────────────────────────────────────

# Keywords too generic to be useful for matching debate content
GENERIC_KEYWORDS = {
    "article", "impôt", "impôts", "impot", "fiscal", "fiscale",
    "régime", "regime", "société", "societe", "cession", "nature",
    "titres", "revenus", "revenu", "montant", "sommes", "droit",
    "droits", "code", "général", "general", "dispositions",
    "conditions", "application", "cadre", "mesure", "mesures",
    "autres", "relative", "relatif", "relatives", "notamment",
    "certains", "certaines", "partir", "depuis", "entre",
    "annexe", "prévues", "prévus", "mentionnés", "mentionnées",
    "premier", "première", "deuxième", "troisième",
}


def extract_search_keywords(art_data: dict, art_ref: str) -> list[str]:
    """Extract substantive keywords from an article's keywords and titre fields."""
    raw_keywords = set()

    for kw in art_data.get("keywords", []):
        kw = kw.strip().lower()
        # Skip pure article reference variants
        if re.match(r"^(article\s+)?\d+[\s\-]*(bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies|[a-z])*$", kw, re.IGNORECASE):
            continue
        if re.match(r"^l[''']article\s+", kw, re.IGNORECASE):
            continue
        # Only multi-word keywords or long enough single words
        words = kw.split()
        if len(words) >= 2:
            # Multi-word keyword: check that at least one word is not generic
            non_generic = [w for w in words if w not in GENERIC_KEYWORDS and len(w) > 3]
            if non_generic:
                raw_keywords.add(kw)
        elif len(kw) > 5 and kw not in GENERIC_KEYWORDS:
            raw_keywords.add(kw)

    # Extract from titre
    titre = art_data.get("titre", "").lower()
    for word in re.findall(r"[a-zàâäéèêëïîôùûüÿç-]{6,}", titre):
        if word not in GENERIC_KEYWORDS:
            raw_keywords.add(word)

    return sorted(raw_keywords)


def pass3_keyword_search(articles: dict, conn: sqlite3.Connection, stats: dict,
                         targets: list[str], verbose: bool) -> None:
    """Search DILA-linked debate interventions by article keywords."""
    logger.info(f"Pass 3: Keyword-based DB search for {len(targets)} articles")

    remaining = [ref for ref in targets if not has_quotes(articles[ref])]
    logger.info(f"  {len(remaining)} still without quotes after Pass 1+2")

    article_debate_ids = build_debate_mappings(articles, conn)

    enriched = 0
    added = 0
    skipped_no_kw = 0
    skipped_no_debates = 0

    for i, art_name in enumerate(remaining):
        art_data = articles[art_name]

        debate_ids = article_debate_ids.get(art_name, set())
        if not debate_ids:
            skipped_no_debates += 1
            continue

        search_keywords = extract_search_keywords(art_data, art_name)
        if len(search_keywords) < 2:
            skipped_no_kw += 1
            continue

        ambiguity = classify_ref_ambiguity(art_name)
        mod_years = get_mod_years(art_data)

        debate_id_list = list(debate_ids)
        placeholders = ",".join("?" * len(debate_id_list))

        # Build SQL LIKE conditions: require at least 2 keyword matches
        # We search for interventions containing any of our keywords,
        # then filter in Python for >=2 matches
        # Use the top 6 most specific keywords to keep SQL manageable
        top_keywords = search_keywords[:6]
        like_clauses = " OR ".join(f"i.contenu LIKE ?" for _ in top_keywords)
        like_params = [f"%{kw}%" for kw in top_keywords]

        rows = conn.execute(f"""
            SELECT i.orateur, i.fonction, i.contenu, d.chambre, d.date
            FROM interventions i
            JOIN debats d ON i.debat_id = d.id
            WHERE i.debat_id IN ({placeholders})
              AND ({like_clauses})
              AND LENGTH(i.contenu) > 120
              AND i.fonction NOT IN ('président de séance', 'president')
              AND i.orateur NOT LIKE '%président%'
              AND i.orateur NOT LIKE '%présidente%'
            ORDER BY d.date
            LIMIT 200
        """, debate_id_list + like_params).fetchall()

        curated = art_data.get("curated_quotes", {})
        if not isinstance(curated, dict):
            curated = {}
        existing_flat = []
        for year_quotes in curated.values():
            if isinstance(year_quotes, list):
                existing_flat.extend(year_quotes)

        new_quotes_by_year = defaultdict(list)
        total_new = 0

        for row in rows:
            if total_new >= MAX_NEW_PER_ARTICLE:
                break

            contenu = row["contenu"]
            orateur = row["orateur"] or ""
            fonction = row["fonction"] or ""
            chambre = row["chambre"] or ""
            date = str(row["date"])[:10]

            contenu_lower = contenu.lower()

            # Require >=2 distinct keyword matches
            kw_matches = sum(1 for kw in search_keywords if kw in contenu_lower)
            if kw_matches < 2:
                continue

            # Require fiscal context (not about some other code)
            if not CGI_RELAXED.search(contenu):
                continue
            if OTHER_CODES.search(contenu):
                continue

            # Filter procedural content
            is_procedural = False
            for p in PROCEDURAL_PATTERNS:
                if p in contenu_lower[:200]:
                    is_procedural = True
                    break
            if is_procedural:
                stats["pass3_rejected_procedural"] += 1
                continue

            # Extract best passage: find the densest keyword region
            quote_text = _extract_keyword_passage(contenu, search_keywords, max_chars=400)
            if not quote_text or len(quote_text) < 60:
                continue

            # Filter mid-word starts
            stripped = quote_text.lstrip("\"'« \n\t")
            if stripped and stripped[0].islower():
                continue

            # Assign year
            year = assign_year(date, mod_years)
            if not year:
                continue

            # Dedup
            all_existing = existing_flat + [
                q for qs in new_quotes_by_year.values() for q in qs
            ]
            if deduplicate_quote(quote_text, all_existing):
                stats["pass3_rejected_dedup"] += 1
                continue

            clean_orateur = re.sub(r"^(M\.|Mme)\s+le\s+président\.\s*", "", orateur).strip()
            if not clean_orateur:
                clean_orateur = orateur

            new_quotes_by_year[year].append({
                "citation": quote_text,
                "orateur": clean_orateur,
                "fonction": fonction,
                "date": date,
                "chambre": chambre.upper() if chambre else "",
                "source": "keyword_search",
            })
            total_new += 1

        if new_quotes_by_year:
            if "curated_quotes" not in art_data:
                art_data["curated_quotes"] = {}
            for year, quotes in new_quotes_by_year.items():
                if year not in art_data["curated_quotes"]:
                    art_data["curated_quotes"][year] = []
                art_data["curated_quotes"][year].extend(quotes)

            enriched += 1
            added += total_new
            if verbose:
                logger.info(f"  Pass3 {art_name}: +{total_new} quotes ({kw_matches} kw matches)")

        if (i + 1) % 500 == 0:
            logger.info(f"  Pass 3 progress: {i+1}/{len(remaining)}")

    stats["pass3_enriched"] = enriched
    stats["pass3_added"] = added
    stats["pass3_skipped_no_kw"] = skipped_no_kw
    stats["pass3_skipped_no_debates"] = skipped_no_debates
    logger.info(
        f"Pass 3 done: {enriched} articles enriched, {added} quotes added "
        f"(skipped: {skipped_no_kw} no keywords, {skipped_no_debates} no debates)"
    )


def _extract_keyword_passage(contenu: str, keywords: list[str],
                             max_chars: int = 400) -> str | None:
    """Extract the passage with the highest keyword density."""
    contenu_lower = contenu.lower()

    # Find all keyword positions
    positions = []
    for kw in keywords:
        start = 0
        while True:
            idx = contenu_lower.find(kw, start)
            if idx == -1:
                break
            positions.append(idx)
            start = idx + len(kw)

    if not positions:
        return None

    positions.sort()

    # Find the densest window of max_chars
    best_start = 0
    best_count = 0
    for anchor in positions:
        window_start = max(0, anchor - 100)
        window_end = window_start + max_chars
        count = sum(1 for p in positions if window_start <= p < window_end)
        if count > best_count:
            best_count = count
            best_start = window_start

    # Snap to sentence boundary
    region_start = best_start
    for j in range(best_start, max(0, best_start - 100), -1):
        if contenu[j] in ".!\n" and j < best_start - 5:
            region_start = j + 1
            break

    region_end = min(len(contenu), region_start + max_chars)
    found_end = False
    for j in range(region_start + 100, region_end):
        if contenu[j] in ".!" and j > region_start + 50:
            region_end = j + 1
            found_end = True
            break
        if contenu[j] == "\n" and j > region_start + 30:
            region_end = j
            found_end = True
            break

    quote = contenu[region_start:region_end].strip()
    quote = re.sub(r"\s+", " ", quote)
    if not found_end and len(quote) >= max_chars - 20:
        quote = quote.rstrip() + "..."

    return quote


# ── Pass 4: Tier C initialization ───────────────────────────────────────────

def pass4_tier_c_init(articles: dict, stats: dict, targets: list[str],
                      verbose: bool) -> None:
    """Initialize curated_quotes for remaining articles. For 1-mod articles,
    generate a minimal entry from the modification's objet field."""
    logger.info(f"Pass 4: Tier C initialization for {len(targets)} articles")

    remaining = [ref for ref in targets if not has_quotes(articles[ref])]
    logger.info(f"  {len(remaining)} still without quotes after Pass 1-3")

    initialized = 0
    synthetic = 0

    for ref in remaining:
        art_data = articles[ref]
        mods = art_data.get("modifications", [])

        # Ensure the field exists
        if "curated_quotes" not in art_data:
            art_data["curated_quotes"] = {}
            initialized += 1

        # For articles with exactly 1 modification, create a synthetic entry
        # from the objet field (legislative change description)
        if len(mods) == 1:
            mod = mods[0]
            objet = mod.get("objet", "").strip()
            if objet and len(objet) > 30:
                year = str(mod.get("annee") or mod.get("law_year") or
                          (mod.get("date", "")[:4] if mod.get("date") else ""))
                if year and year.isdigit():
                    # Truncate long objet texts
                    if len(objet) > 500:
                        objet = objet[:497] + "..."

                    if year not in art_data["curated_quotes"]:
                        art_data["curated_quotes"][year] = []

                    art_data["curated_quotes"][year].append({
                        "citation": objet,
                        "orateur": "",
                        "fonction": "",
                        "date": mod.get("date", ""),
                        "chambre": "",
                        "source": "objet_legislatif",
                    })
                    synthetic += 1
                    if verbose:
                        logger.info(f"  Pass4 {ref}: synthetic from objet ({year})")

        elif not art_data["curated_quotes"]:
            # For 0-mod or multi-mod articles still empty, just leave as empty dict
            if verbose:
                logger.info(f"  Pass4 {ref}: initialized empty curated_quotes")

    stats["pass4_initialized"] = initialized
    stats["pass4_synthetic"] = synthetic
    logger.info(f"Pass 4 done: {initialized} fields initialized, {synthetic} synthetic entries")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fill curated_quotes for all articles missing them"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Report stats without saving")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Log per-article details")
    parser.add_argument("--tier", choices=["A", "B", "C", "all"], default="all",
                        help="Run only for a specific tier (default: all)")
    parser.add_argument("--pass", dest="run_pass", choices=["1", "2", "3", "4", "all"],
                        default="all", help="Run only a specific pass (default: all)")
    args = parser.parse_args()

    # Load data
    logger.info(f"Loading index from {INDEX_PATH}")
    with open(INDEX_PATH) as f:
        index_data = json.load(f)
    articles = index_data["articles"]
    logger.info(f"Loaded {len(articles)} articles")

    # Before stats
    before_has = sum(1 for a in articles.values() if has_quotes(a))
    before_no = len(articles) - before_has
    logger.info(f"Before: {before_has} with quotes, {before_no} without")

    # Get target articles
    tier_filter = args.tier if args.tier != "all" else None
    targets = get_no_quote_articles(articles, tier_filter)

    # Tier breakdown
    tier_counts = defaultdict(int)
    for ref in targets:
        tier_counts[get_tier(articles[ref])] += 1
    logger.info(f"Targets: {len(targets)} articles without quotes")
    for t in ("A", "B", "C"):
        logger.info(f"  Tier {t}: {tier_counts[t]}")

    stats = defaultdict(int)
    run_pass = args.run_pass

    # Connect to DB (needed for pass 1 and 3)
    conn = None
    if run_pass in ("1", "3", "all"):
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

    # Load CGI index (needed for pass 2)
    cgi_index = None
    if run_pass in ("2", "all"):
        logger.info(f"Loading CGI mention index from {CGI_INDEX_PATH}")
        with open(CGI_INDEX_PATH) as f:
            cgi_index = json.load(f)
        logger.info(f"CGI mention index: {len(cgi_index)} entries")

    # ── Run passes ───────────────────────────────────────────────────────
    if run_pass in ("1", "all"):
        pass1_db_direct(articles, conn, stats, targets, args.verbose)

        if not args.dry_run and run_pass == "all":
            _save_incremental(index_data, "after pass 1")

    if run_pass in ("2", "all"):
        pass2_cgi_index(articles, cgi_index, stats, targets, args.verbose)

        if not args.dry_run and run_pass == "all":
            _save_incremental(index_data, "after pass 2")

    if run_pass in ("3", "all"):
        pass3_keyword_search(articles, conn, stats, targets, args.verbose)

        if not args.dry_run and run_pass == "all":
            _save_incremental(index_data, "after pass 3")

    if run_pass in ("4", "all"):
        pass4_tier_c_init(articles, stats, targets, args.verbose)

    # ── Save ─────────────────────────────────────────────────────────────
    if not args.dry_run:
        # Create backup
        if INDEX_PATH.exists():
            logger.info(f"Creating backup at {BACKUP_PATH}")
            shutil.copy2(INDEX_PATH, BACKUP_PATH)

        logger.info(f"Saving to {INDEX_PATH}")
        with open(INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump(index_data, f, ensure_ascii=False, indent=2)
        logger.info("Saved.")
    else:
        logger.info("DRY RUN - not saving")

    # ── After stats ──────────────────────────────────────────────────────
    after_has = sum(1 for a in articles.values() if has_quotes(a))
    after_no = len(articles) - after_has

    logger.info("")
    logger.info("=" * 70)
    logger.info("RESULTS SUMMARY")
    logger.info("=" * 70)
    logger.info(f"Before: {before_has} with quotes, {before_no} without")
    logger.info(f"After:  {after_has} with quotes, {after_no} without")
    logger.info(f"Net:    +{after_has - before_has} articles gained quotes")
    logger.info("")

    # Per-tier breakdown after
    tier_after = {"A": 0, "B": 0, "C": 0}
    tier_total = {"A": 0, "B": 0, "C": 0}
    for ref, art in articles.items():
        t = get_tier(art)
        tier_total[t] += 1
        if has_quotes(art):
            tier_after[t] += 1
    for t in ("A", "B", "C"):
        logger.info(f"  Tier {t}: {tier_after[t]}/{tier_total[t]} have quotes")
    logger.info("")

    # Per-pass stats
    for p in ("1", "2", "3", "4"):
        key_enriched = f"pass{p}_enriched"
        key_added = f"pass{p}_added"
        if key_enriched in stats or key_added in stats:
            logger.info(f"Pass {p}: {stats.get(key_enriched, 0)} articles enriched, "
                        f"{stats.get(key_added, 0)} quotes added")
    if stats.get("pass4_initialized"):
        logger.info(f"  Pass 4: {stats['pass4_initialized']} fields initialized, "
                    f"{stats['pass4_synthetic']} synthetic entries")
    logger.info("")

    # Rejection stats
    for key in sorted(stats):
        if "rejected" in key or "skipped" in key:
            logger.info(f"  {key}: {stats[key]}")

    if conn:
        conn.close()


def _save_incremental(index_data: dict, label: str) -> None:
    """Save index data incrementally between passes."""
    logger.info(f"Incremental save ({label})")
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
