#!/usr/bin/env python3
"""Clean cgi_index quotes and enrich missing quotes for key/high-mod articles.

Step 1:  Clean cgi_index quotes - re-extract from DB when possible, remove noise
Step 1b: Clean topic_search noise - remove off-topic quotes using titre keywords
Step 2:  Fill year gaps on 35 key CGI articles (strategies: ref, keyword, titre-phrase)
Step 2b: Rescue critical no-quote articles (10+ mods, 0 quotes) via phrase search
Step 3:  Fill 63 articles with 6+ mods using titre-based topic search
Step 4:  Fill ~337 articles with 2-5 mods using synthese + titre DB search
Step 5:  Clean off-topic quotes from key articles (all sources, semantic keywords)
Step 6:  Fill from amendment exposé for topic-name articles

Usage:
    python scripts/clean_and_enrich_quotes.py
    python scripts/clean_and_enrich_quotes.py --dry-run
    python scripts/clean_and_enrich_quotes.py --step=1b --verbose
    python scripts/clean_and_enrich_quotes.py --step=5 --dry-run --verbose
    python scripts/clean_and_enrich_quotes.py --step=6 --dry-run --verbose
    python scripts/clean_and_enrich_quotes.py --step=all
"""

import argparse
import html
import json
import logging
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

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
from fill_quotes_and_juris import build_relevance_keywords

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

INDEX_PATH = PROJECT_ROOT / "data" / "article_modifications.json"
BACKUP_PATH = PROJECT_ROOT / "data" / "article_modifications.backup.json"
CGI_INDEX_PATH = PROJECT_ROOT / "data" / "cgi_mention_index.json"
DB_PATH = PROJECT_ROOT / "data" / "legislateur.db"

MAX_QUOTES_PER_YEAR = 3
MAX_TOTAL_PER_ARTICLE = 15

# 35 key CGI articles for French tax law
KEY_ARTICLES = [
    "200 A", "150-0 B ter", "787 B", "199 terdecies-0 A", "150-0 A",
    "885 I bis", "885-0 V bis", "238 bis", "244 quater B", "39", "209",
    "219", "261", "278", "1382", "1407", "1447", "1586 ter", "1691 bis",
    "1729", "80 duodecies", "81", "83", "156", "163 bis G", "726",
    "750 ter", "779", "790 G", "793", "885 A", "4 B", "164 B",
    "182 A", "209 B", "238 A",
]

# ── Additional legislative text patterns (beyond is_quality_quote) ───────────
LEGTEXT_PATTERNS = [
    r"sont\s+insérés\s+les\s+mots",
    r"est\s+inséré\s+un\s+alinéa",
    r"il\s+est\s+inséré",
    r"est\s+abrogé",
    r"alinéa\s+est\s+ainsi\s+rédigé",
    r"les\s+mots\s*:\s*[«\"]",
    r"après\s+les?\s+mots?\s*:",
    r"avant\s+les?\s+mots?\s*:",
    r"remplacer\s+les\s+mots",
    r"sont\s+remplacées?\s+par",
    r"est\s+ainsi\s+modifié",
    r"du\s+même\s+code\s+est",
    r"du\s+code\s+général\s+des\s+impôts\s+est",
    r"^\s*[IVX]+[\.\s]*[-–]",
    r"^\s*\d+°\s",
    r"^«\s",
]


def is_legislative_text(citation: str) -> bool:
    """Check if a quote is legislative amendment text rather than debate commentary."""
    c_lower = citation.lower()
    for pat in LEGTEXT_PATTERNS:
        if re.search(pat, c_lower):
            return True
    # High density of article references suggests amendment text
    art_refs = re.findall(r"article\s+\d+", c_lower)
    if len(art_refs) >= 3 and len(citation) < 400:
        return True
    return False


def is_substantive_quote(citation: str, orateur: str, fonction: str,
                         ref: str, ambiguity: str) -> bool:
    """Strict quality check combining is_quality_quote + legislative text filter."""
    if not is_quality_quote(citation, orateur, fonction, ref, ambiguity,
                            full_contenu=citation):
        return False
    if is_legislative_text(citation):
        return False
    return True


def _extract_best_sentence(contenu: str, ref: str, max_chars: int = 400) -> str | None:
    """Extract the best substantive sentence from an intervention.

    Unlike extract_quote (which looks for 'article {ref}'), this finds the
    longest sentence in the text that looks like substantive debate commentary,
    prioritizing sentences that mention the article ref but accepting others.
    """
    if len(contenu) < 80:
        return None

    # Split into sentences
    sentences = re.split(r'(?<=[.!?])\s+', contenu)

    ref_lower = ref.lower()
    best = None
    best_score = -1

    for sent in sentences:
        sent = sent.strip()
        if len(sent) < 60 or len(sent) > max_chars:
            continue

        s_lower = sent.lower()

        # Skip procedural sentences
        if any(p in s_lower for p in PROCEDURAL_PATTERNS):
            continue

        # Skip legislative text
        if is_legislative_text(sent):
            continue

        # Skip mid-word starts
        stripped = sent.lstrip("\"'« \n\t")
        if stripped and stripped[0].islower():
            continue

        # Score: prefer sentences mentioning the article, longer, by key speakers
        score = len(sent) / 100  # base score from length
        if f"article {ref_lower}" in s_lower or f"l'article {ref_lower}" in s_lower:
            score += 5
        if ref_lower in s_lower:
            score += 2

        if score > best_score:
            best_score = score
            best = re.sub(r"\s+", " ", sent).strip()

    return best


# ── Helpers ──────────────────────────────────────────────────────────────────

def has_quotes(art_data: dict) -> bool:
    cq = art_data.get("curated_quotes")
    if not isinstance(cq, dict):
        return False
    return sum(len(v) for v in cq.values() if isinstance(v, list)) > 0


def count_quotes(art_data: dict) -> int:
    cq = art_data.get("curated_quotes", {})
    if not isinstance(cq, dict):
        return 0
    return sum(len(v) for v in cq.values() if isinstance(v, list))


def get_mod_years(art_data: dict) -> list[int]:
    years = []
    for mod in art_data.get("modifications", []):
        y = mod.get("annee") or mod.get("law_year") or (
            mod.get("date", "")[:4] if mod.get("date") else None)
        if y:
            years.append(int(y))
    return sorted(set(years))


def get_quote_years(art_data: dict) -> set[str]:
    cq = art_data.get("curated_quotes", {})
    if not isinstance(cq, dict):
        return set()
    return {y for y, ql in cq.items() if isinstance(ql, list) and len(ql) > 0}


def build_debate_mappings(articles: dict, conn: sqlite3.Connection):
    """Build article→debate_ids from DILA sources."""
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
                for debate in date_to_debates.get(m.group(1), []):
                    article_debate_ids[art_name].add(debate["id"])

    return article_debate_ids


# Generic keywords too vague for DB search
GENERIC_KEYWORDS = {
    "article", "impôt", "impôts", "impot", "fiscal", "fiscale",
    "régime", "regime", "société", "societe", "cession", "nature",
    "titres", "revenus", "revenu", "montant", "sommes", "droit",
    "droits", "code", "général", "general", "dispositions",
    "conditions", "application", "cadre", "mesure", "mesures",
    "autres", "relative", "relatif", "relatives", "notamment",
    "certains", "certaines", "partir", "depuis", "entre",
    "annexe", "prévues", "prévus", "mentionnés", "mentionnées",
    "premier", "première", "deuxième", "troisième", "projet",
    "présent", "alinéa", "paragraphe", "commission", "rapport",
}


def extract_topic_keywords(art_data: dict) -> list[str]:
    """Extract substantive topic keywords from titre + keywords + synthese."""
    raw = set()

    # From titre
    titre = art_data.get("titre", "").lower()
    for word in re.findall(r"[a-zàâäéèêëïîôùûüÿç-]{5,}", titre):
        if word not in GENERIC_KEYWORDS:
            raw.add(word)

    # Multi-word phrases from titre (more specific)
    # e.g. "taxe sur les véhicules de sociétés" → "véhicules", "sociétés"
    for phrase in re.findall(r"[a-zàâäéèêëïîôùûüÿç]+(?:\s+[a-zàâäéèêëïîôùûüÿç]+){1,3}", titre):
        words = phrase.split()
        non_generic = [w for w in words if w not in GENERIC_KEYWORDS and len(w) > 3]
        if len(non_generic) >= 1:
            raw.add(phrase)

    # From keywords field
    for kw in art_data.get("keywords", []):
        kw = kw.strip().lower()
        if re.match(r"^(article\s+)?\d+[\s\-]*(bis|ter|quater|quinquies|sexies|[a-z])*$", kw, re.IGNORECASE):
            continue
        if re.match(r"^l[''']article\s+", kw, re.IGNORECASE):
            continue
        words = kw.split()
        if len(words) >= 2:
            non_generic = [w for w in words if w not in GENERIC_KEYWORDS and len(w) > 3]
            if non_generic:
                raw.add(kw)
        elif len(kw) > 5 and kw not in GENERIC_KEYWORDS:
            raw.add(kw)

    # From synthese (dict with objectif_constant + preoccupations_recurrentes)
    synthese = art_data.get("synthese", {})
    synthese_text = ""
    if isinstance(synthese, dict):
        obj = synthese.get("objectif_constant", "")
        if isinstance(obj, str):
            synthese_text += obj + " "
        preocc = synthese.get("preoccupations_recurrentes", [])
        if isinstance(preocc, list):
            synthese_text += " ".join(p for p in preocc if isinstance(p, str))
        elif isinstance(preocc, str):
            synthese_text += preocc
    elif isinstance(synthese, str):
        synthese_text = synthese

    if synthese_text:
        for word in re.findall(r"[a-zàâäéèêëïîôùûüÿç-]{6,}", synthese_text.lower()[:500]):
            if word not in GENERIC_KEYWORDS:
                raw.add(word)

    return sorted(raw)


def extract_titre_keywords_and_phrases(art_data: dict) -> tuple[set[str], list[str]]:
    """Extract keywords (6+ chars) and multi-word phrases from titre only.

    Lighter/more precise than extract_topic_keywords - focused on titre
    for relevance scoring of topic_search quotes.
    """
    titre = art_data.get("titre", "").lower()

    # Single keywords: 6+ chars, not generic
    keywords = set()
    for word in re.findall(r"[a-zàâäéèêëïîôùûüÿç-]{6,}", titre):
        if word not in GENERIC_KEYWORDS:
            keywords.add(word)

    # Multi-word phrases: 2+ words with at least 1 substantive word (5+ chars, non-generic)
    phrases = []
    for phrase in re.findall(r"[a-zàâäéèêëïîôùûüÿç]+(?:\s+[a-zàâäéèêëïîôùûüÿç]+){1,3}", titre):
        words = phrase.split()
        if len(words) >= 2:
            substantive = [w for w in words if len(w) >= 5 and w not in GENERIC_KEYWORDS]
            if substantive:
                phrases.append(phrase)

    return keywords, phrases


def extract_distinctive_phrases(art_data: dict) -> list[str]:
    """Extract prioritized multi-word phrases from keywords + titre + objectif_constant.

    Used by step 2b (rescue) and Strategy C (titre-phrase search).
    Returns phrases sorted by specificity (keywords field first, then titre, then objectif).
    """
    phrases = []
    seen = set()

    # Priority 1: Multi-word entries from keywords field (match debate language best)
    for kw in art_data.get("keywords", []):
        kw_clean = kw.strip().lower()
        # Skip article references
        if re.match(r"^(article\s+)?\d+[\s\-]*(bis|ter|quater|quinquies|sexies|[a-z])*$",
                     kw_clean, re.IGNORECASE):
            continue
        if re.match(r"^l[''']article\s+", kw_clean, re.IGNORECASE):
            continue
        words = kw_clean.split()
        if len(words) >= 2:
            non_generic = [w for w in words if w not in GENERIC_KEYWORDS and len(w) > 3]
            if non_generic and kw_clean not in seen:
                phrases.append(kw_clean)
                seen.add(kw_clean)

    # Priority 2: Multi-word phrases from titre
    titre = art_data.get("titre", "").lower()
    for phrase in re.findall(r"[a-zàâäéèêëïîôùûüÿç]+(?:\s+[a-zàâäéèêëïîôùûüÿç]+){1,3}", titre):
        words = phrase.split()
        if len(words) >= 2:
            substantive = [w for w in words if len(w) >= 5 and w not in GENERIC_KEYWORDS]
            if substantive and phrase not in seen:
                phrases.append(phrase)
                seen.add(phrase)

    # Priority 3: Key phrases from objectif_constant (first 300 chars)
    synthese = art_data.get("synthese", {})
    obj_text = ""
    if isinstance(synthese, dict):
        obj = synthese.get("objectif_constant", "")
        if isinstance(obj, str):
            obj_text = obj[:300].lower()
    elif isinstance(synthese, str):
        obj_text = synthese[:300].lower()

    if obj_text:
        for phrase in re.findall(
            r"[a-zàâäéèêëïîôùûüÿç]+(?:\s+[a-zàâäéèêëïîôùûüÿç]+){1,2}", obj_text
        ):
            words = phrase.split()
            if len(words) >= 2:
                substantive = [w for w in words if len(w) >= 5 and w not in GENERIC_KEYWORDS]
                if substantive and phrase not in seen:
                    phrases.append(phrase)
                    seen.add(phrase)

    # Filter out phrases with fragment words from apostrophe-split artifacts
    # (e.g., "intérêts d" from "intérêts d'emprunts" where regex breaks on ')
    phrases = [p for p in phrases if all(len(w) >= 2 for w in p.split())]

    return phrases


def search_interventions_by_keywords(
    conn: sqlite3.Connection,
    debate_ids: set[int],
    keywords: list[str],
    art_name: str,
    ambiguity: str,
    mod_years: list[int],
    existing_flat: list[dict],
    max_new: int = 5,
    min_kw_matches: int = 2,
    target_year: int | None = None,
) -> list[tuple[str, dict]]:
    """Search interventions by keyword matches. Returns list of (year, quote_dict)."""
    if not debate_ids or len(keywords) < 2:
        return []

    debate_id_list = list(debate_ids)
    placeholders = ",".join("?" * len(debate_id_list))

    # Use top 8 keywords for SQL
    top_kw = keywords[:8]
    like_clauses = " OR ".join("i.contenu LIKE ?" for _ in top_kw)
    like_params = [f"%{kw}%" for kw in top_kw]

    # Optional year filter for targeted searches
    year_filter = ""
    year_params = []
    if target_year:
        # Finance law debates happen Oct-Dec for next year
        year_filter = " AND (CAST(strftime('%Y', d.date) AS INTEGER) = ? OR CAST(strftime('%Y', d.date) AS INTEGER) = ?)"
        year_params = [target_year, target_year - 1]

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
          {year_filter}
        ORDER BY d.date
        LIMIT 300
    """, debate_id_list + like_params + year_params).fetchall()

    results = []
    for row in rows:
        if len(results) >= max_new:
            break

        contenu = row["contenu"]
        orateur = row["orateur"] or ""
        fonction = row["fonction"] or ""
        chambre = row["chambre"] or ""
        date = str(row["date"])[:10]

        contenu_lower = contenu.lower()

        # Require keyword matches with multi-word phrase bonus
        # Multi-word keywords (phrases) count as 2 matches since they're more specific
        kw_score = 0
        for kw in keywords:
            if kw in contenu_lower:
                kw_score += 2 if " " in kw else 1
        if kw_score < max(min_kw_matches + 1, 3):
            continue

        # Require fiscal context
        if not CGI_RELAXED.search(contenu):
            continue
        if OTHER_CODES.search(contenu):
            continue

        # Filter procedural content in first 200 chars
        is_proc = any(p in contenu_lower[:200] for p in PROCEDURAL_PATTERNS)
        if is_proc:
            continue

        # Extract best passage - require keywords IN the extracted passage too
        quote_text = _extract_keyword_passage(contenu, keywords)
        if not quote_text or len(quote_text) < 60:
            continue

        # Verify the passage itself has keyword density (not just the full intervention)
        quote_lower = quote_text.lower()
        passage_kw_score = 0
        for kw in keywords:
            if kw in quote_lower:
                passage_kw_score += 2 if " " in kw else 1
        if passage_kw_score < 2:
            continue

        # Filter mid-word starts
        stripped = quote_text.lstrip("\"'« \n\t")
        if stripped and stripped[0].islower():
            continue

        # Filter legislative text
        if is_legislative_text(quote_text):
            continue

        year = assign_year(date, mod_years)
        if not year:
            continue

        # Dedup
        all_existing = existing_flat + [q for _, q in results]
        if deduplicate_quote(quote_text, all_existing):
            continue

        clean_orateur = re.sub(r"^(M\.|Mme)\s+le\s+président\.\s*", "", orateur).strip() or orateur

        results.append((year, {
            "citation": quote_text,
            "orateur": clean_orateur,
            "fonction": fonction,
            "date": date,
            "chambre": chambre.upper() if chambre else "",
            "source": "topic_search",
        }))

    return results


def _extract_keyword_passage(contenu: str, keywords: list[str],
                             max_chars: int = 400) -> str | None:
    """Extract the passage with the highest keyword density."""
    contenu_lower = contenu.lower()
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

    best_start = 0
    best_count = 0
    for anchor in positions:
        window_start = max(0, anchor - 100)
        window_end = window_start + max_chars
        count = sum(1 for p in positions if window_start <= p < window_end)
        if count > best_count:
            best_count = count
            best_start = window_start

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


# ═══════════════════════════════════════════════════════════════════════════
# STEP 1: Clean cgi_index quotes
# ═══════════════════════════════════════════════════════════════════════════

def step1_clean_cgi_quotes(articles: dict, conn: sqlite3.Connection,
                           cgi_index: dict, stats: dict, verbose: bool) -> None:
    """Clean cgi_index quotes: re-extract salvageable ones from DB, remove noise."""
    logger.info("Step 1: Cleaning cgi_index quotes")

    # Build snippet → intervention_id lookup
    snippet_to_mention = {}
    for ref, mentions in cgi_index.items():
        for m in mentions:
            snippet_start = m.get("snippet", "")[:60]
            if snippet_start:
                snippet_to_mention[(ref, snippet_start)] = m

    total_cgi = 0
    kept = 0
    re_extracted = 0
    removed = 0
    removed_articles = set()

    for ref, art in articles.items():
        cq = art.get("curated_quotes", {})
        if not isinstance(cq, dict):
            continue

        ambiguity = classify_ref_ambiguity(ref)
        mod_years = get_mod_years(art)
        modified = False

        for year in list(cq.keys()):
            ql = cq[year]
            if not isinstance(ql, list):
                continue

            new_list = []
            for q in ql:
                if q.get("source") != "cgi_index":
                    new_list.append(q)
                    continue

                total_cgi += 1
                citation = q.get("citation", "")
                orateur = q.get("orateur", "")
                fonction = q.get("fonction", "")

                # Check if it passes strict quality
                if is_substantive_quote(citation, orateur, fonction, ref, ambiguity):
                    kept += 1
                    new_list.append(q)
                    continue

                # Try re-extraction from DB
                snippet_key = (ref, citation[:60])
                mention = snippet_to_mention.get(snippet_key)

                if mention:
                    intervention_id = mention["intervention_id"]
                    row = conn.execute(
                        "SELECT i.contenu, i.orateur, i.fonction, d.chambre, d.date "
                        "FROM interventions i JOIN debats d ON i.debat_id = d.id "
                        "WHERE i.id = ?", (intervention_id,)
                    ).fetchone()

                    if row:
                        full_contenu = row["contenu"]
                        db_orateur = row["orateur"] or ""
                        db_fonction = row["fonction"] or ""
                        db_chambre = row["chambre"] or ""
                        db_date = str(row["date"])[:10]

                        # Skip president/chair speakers early
                        if db_fonction:
                            fl = db_fonction.lower()
                            if "president" in fl or "président" in fl:
                                removed += 1
                                modified = True
                                removed_articles.add(ref)
                                continue
                        if db_orateur:
                            ol = db_orateur.lower()
                            if "président" in ol or "présidente" in ol:
                                removed += 1
                                modified = True
                                removed_articles.add(ref)
                                continue

                        # Strategy A: extract_quote around article {ref}
                        new_quote = extract_quote(full_contenu, ref)

                        # Strategy B: find best sentence in the full text
                        if not new_quote or not is_substantive_quote(
                            new_quote, db_orateur, db_fonction, ref, ambiguity
                        ):
                            new_quote = _extract_best_sentence(full_contenu, ref)

                        if new_quote and is_substantive_quote(
                            new_quote, db_orateur, db_fonction, ref, ambiguity
                        ):
                            # Check for dedup against other quotes
                            all_existing = new_list + [
                                qq for yy, qql in cq.items()
                                if yy != year and isinstance(qql, list)
                                for qq in qql
                            ]
                            if not deduplicate_quote(new_quote, all_existing):
                                clean_orateur = re.sub(
                                    r"^(M\.|Mme)\s+le\s+président\.\s*", "",
                                    db_orateur
                                ).strip() or db_orateur

                                new_year = assign_year(db_date, mod_years) or year
                                new_list.append({
                                    "citation": new_quote,
                                    "orateur": clean_orateur,
                                    "fonction": db_fonction,
                                    "date": db_date,
                                    "chambre": db_chambre.upper() if db_chambre else "",
                                    "source": "cgi_index_reextracted",
                                })
                                re_extracted += 1
                                modified = True

                                if verbose:
                                    logger.info(f"  Re-extracted [{ref}] {year}: "
                                                f"\"{new_quote[:80]}...\"")
                                continue

                # Not salvageable → remove
                removed += 1
                modified = True
                removed_articles.add(ref)

                if verbose and removed <= 20:
                    logger.info(f"  Removed [{ref}] {year}: \"{citation[:80]}...\"")

            if modified:
                cq[year] = new_list

        # Clean up empty year keys
        if modified:
            empty_years = [y for y, ql in cq.items()
                           if isinstance(ql, list) and len(ql) == 0]
            for y in empty_years:
                del cq[y]

    stats["step1_total_cgi"] = total_cgi
    stats["step1_kept"] = kept
    stats["step1_re_extracted"] = re_extracted
    stats["step1_removed"] = removed
    stats["step1_affected_articles"] = len(removed_articles)

    logger.info(f"Step 1 done: {total_cgi} cgi_index quotes reviewed")
    logger.info(f"  Kept (passed quality): {kept}")
    logger.info(f"  Re-extracted from DB:  {re_extracted}")
    logger.info(f"  Removed (noise):       {removed}")
    logger.info(f"  Articles affected:     {len(removed_articles)}")


# ═══════════════════════════════════════════════════════════════════════════
# STEP 1b: Clean topic_search noise
# ═══════════════════════════════════════════════════════════════════════════

def step1b_clean_topic_search_quotes(articles: dict, stats: dict, verbose: bool) -> None:
    """Clean topic_search quotes - remove off-topic noise using titre keywords."""
    logger.info("Step 1b: Cleaning topic_search noise")

    total_topic = 0
    kept = 0
    removed = 0
    removed_articles = set()

    for ref, art in articles.items():
        cq = art.get("curated_quotes", {})
        if not isinstance(cq, dict):
            continue

        # Get titre-specific keywords and phrases for this article
        titre_kw, titre_phrases = extract_titre_keywords_and_phrases(art)
        if not titre_kw and not titre_phrases:
            # No titre keywords - keep all quotes (can't evaluate relevance)
            continue

        ref_lower = ref.lower()
        modified = False

        for year in list(cq.keys()):
            ql = cq[year]
            if not isinstance(ql, list):
                continue

            new_list = []
            for q in ql:
                if q.get("source") != "topic_search":
                    new_list.append(q)
                    continue

                total_topic += 1
                citation = q.get("citation", "").lower()

                # Keep if citation mentions the article reference
                if f"article {ref_lower}" in citation or f"l'article {ref_lower}" in citation:
                    kept += 1
                    new_list.append(q)
                    continue

                # Compute relevance score
                relevance_score = 0
                for phrase in titre_phrases:
                    if phrase in citation:
                        relevance_score += 2
                for kw in titre_kw:
                    if kw in citation:
                        relevance_score += 1

                if relevance_score >= 2:
                    kept += 1
                    new_list.append(q)
                else:
                    removed += 1
                    removed_articles.add(ref)
                    modified = True
                    if verbose and removed <= 20:
                        logger.info(f"  Removed [{ref}] {year}: score={relevance_score} "
                                    f"\"{q.get('citation', '')[:80]}...\"")

            if modified:
                cq[year] = new_list

        # Clean up empty year keys
        if modified:
            empty_years = [y for y, ql in cq.items()
                           if isinstance(ql, list) and len(ql) == 0]
            for y in empty_years:
                del cq[y]

    stats["step1b_total_topic_search"] = total_topic
    stats["step1b_kept"] = kept
    stats["step1b_removed"] = removed
    stats["step1b_affected_articles"] = len(removed_articles)

    logger.info(f"Step 1b done: {total_topic} topic_search quotes reviewed")
    logger.info(f"  Kept (relevant):       {kept}")
    logger.info(f"  Removed (off-topic):   {removed}")
    logger.info(f"  Articles affected:     {len(removed_articles)}")


# ═══════════════════════════════════════════════════════════════════════════
# STEP 2: Fill year gaps on 35 key articles
# ═══════════════════════════════════════════════════════════════════════════

def step2_fill_key_articles(articles: dict, conn: sqlite3.Connection,
                            stats: dict, verbose: bool) -> None:
    """Fill year gaps for key CGI articles using titre + law year DB search."""
    logger.info("Step 2: Filling year gaps on key articles")

    article_debate_ids = build_debate_mappings(articles, conn)

    enriched = 0
    quotes_added = 0

    for ref in KEY_ARTICLES:
        art = articles.get(ref)
        if not art:
            if verbose:
                logger.info(f"  Key article {ref} not found in index")
            continue

        mod_years = get_mod_years(art)
        quote_years = get_quote_years(art)
        missing_years = [y for y in mod_years if str(y) not in quote_years]

        if not missing_years:
            continue

        debate_ids = article_debate_ids.get(ref, set())
        if not debate_ids:
            if verbose:
                logger.info(f"  {ref}: no DILA debates")
            continue

        ambiguity = classify_ref_ambiguity(ref)
        pattern = build_article_pattern(ref)
        keywords = extract_topic_keywords(art)

        existing_flat = []
        cq = art.get("curated_quotes", {})
        if isinstance(cq, dict):
            for ql in cq.values():
                if isinstance(ql, list):
                    existing_flat.extend(ql)

        art_added = 0

        for target_year in missing_years:
            # Strategy A: exact article ref match for this year
            found = _search_article_ref_for_year(
                conn, debate_ids, ref, pattern, ambiguity,
                mod_years, existing_flat, target_year
            )

            # Strategy B: keyword search for this year
            if not found:
                found = search_interventions_by_keywords(
                    conn, debate_ids, keywords, ref, ambiguity,
                    mod_years, existing_flat, max_new=2,
                    min_kw_matches=2, target_year=target_year,
                )

            # Strategy C: titre-phrase search for this year
            if not found:
                found = _search_titre_phrases_for_year(
                    conn, debate_ids, ref, art, mod_years, existing_flat, target_year
                )

            if found:
                if "curated_quotes" not in art:
                    art["curated_quotes"] = {}
                for year, q in found:
                    if year not in art["curated_quotes"]:
                        art["curated_quotes"][year] = []
                    art["curated_quotes"][year].append(q)
                    existing_flat.append(q)
                    art_added += 1

        if art_added > 0:
            enriched += 1
            quotes_added += art_added
            if verbose:
                logger.info(f"  {ref}: +{art_added} quotes for missing years")

    stats["step2_enriched"] = enriched
    stats["step2_added"] = quotes_added
    logger.info(f"Step 2 done: {enriched} key articles enriched, {quotes_added} quotes added")


def _search_article_ref_for_year(
    conn: sqlite3.Connection,
    debate_ids: set[int],
    ref: str,
    pattern: re.Pattern,
    ambiguity: str,
    mod_years: list[int],
    existing_flat: list[dict],
    target_year: int,
) -> list[tuple[str, dict]]:
    """Search for 'article {ref}' in debates for a specific year."""
    debate_id_list = list(debate_ids)
    placeholders = ",".join("?" * len(debate_id_list))

    like_pattern = f"%article {ref}%"

    if ambiguity == "extreme":
        extra = " AND (i.contenu LIKE '%code général des impôts%' OR i.contenu LIKE '% CGI%')"
    else:
        extra = ""

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
          AND (CAST(strftime('%Y', d.date) AS INTEGER) = ?
               OR CAST(strftime('%Y', d.date) AS INTEGER) = ?)
          {extra}
        ORDER BY d.date
        LIMIT 50
    """, debate_id_list + [like_pattern, target_year, target_year - 1]).fetchall()

    results = []
    for row in rows:
        if len(results) >= 2:
            break

        contenu = row["contenu"]
        orateur = row["orateur"] or ""
        fonction = row["fonction"] or ""
        chambre = row["chambre"] or ""
        date = str(row["date"])[:10]

        if not pattern.search(contenu):
            continue

        quote_text = extract_quote(contenu, ref)
        if not quote_text:
            continue

        if not is_substantive_quote(quote_text, orateur, fonction, ref, ambiguity):
            continue

        year = assign_year(date, mod_years)
        if not year:
            continue

        all_existing = existing_flat + [q for _, q in results]
        if deduplicate_quote(quote_text, all_existing):
            continue

        clean_orateur = re.sub(r"^(M\.|Mme)\s+le\s+président\.\s*", "", orateur).strip() or orateur

        results.append((year, {
            "citation": quote_text,
            "orateur": clean_orateur,
            "fonction": fonction,
            "date": date,
            "chambre": chambre.upper() if chambre else "",
            "source": "key_article_fill",
        }))

    return results


def _search_titre_phrases_for_year(
    conn: sqlite3.Connection,
    debate_ids: set[int],
    ref: str,
    art_data: dict,
    mod_years: list[int],
    existing_flat: list[dict],
    target_year: int,
) -> list[tuple[str, dict]]:
    """Search debate interventions using titre/keyword phrases for a specific year.

    Strategy C for step 2: finds interventions matching distinctive phrases
    from the article's keywords, titre, and objectif_constant.
    """
    phrases = extract_distinctive_phrases(art_data)
    if not phrases:
        return []

    debate_id_list = list(debate_ids)
    if not debate_id_list:
        return []
    placeholders = ",".join("?" * len(debate_id_list))

    # Use top 6 phrases for SQL LIKE search
    top_phrases = phrases[:6]
    like_clauses = " OR ".join("i.contenu LIKE ?" for _ in top_phrases)
    like_params = [f"%{p}%" for p in top_phrases]

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
          AND (CAST(strftime('%Y', d.date) AS INTEGER) = ?
               OR CAST(strftime('%Y', d.date) AS INTEGER) = ?)
        ORDER BY d.date
        LIMIT 200
    """, debate_id_list + like_params + [target_year, target_year - 1]).fetchall()

    ref_lower = ref.lower()
    results = []

    for row in rows:
        if len(results) >= 2:
            break

        contenu = row["contenu"]
        orateur = row["orateur"] or ""
        fonction = row["fonction"] or ""
        chambre = row["chambre"] or ""
        date = str(row["date"])[:10]
        contenu_lower = contenu.lower()

        # Reject other legal codes (penal, labor, etc.) but don't require CGI mention
        # since these articles are often discussed by topic name, not "article N du CGI"
        if OTHER_CODES.search(contenu):
            continue

        # Filter procedural content
        if any(p in contenu_lower[:200] for p in PROCEDURAL_PATTERNS):
            continue

        # Normalize line wraps for better sentence extraction
        contenu_norm = re.sub(r"(?<!\n)\n(?!\n)", " ", contenu)

        # Extract best sentence (prefers ref-mentioning)
        quote_text = _extract_best_sentence(contenu_norm, ref)
        if not quote_text or len(quote_text) < 60:
            continue

        quote_lower = quote_text.lower()

        # If ref NOT in extracted quote, require 1+ search phrase in passage
        has_ref = (f"article {ref_lower}" in quote_lower or
                   f"l'article {ref_lower}" in quote_lower or
                   ref_lower in quote_lower)
        if not has_ref:
            phrase_in_passage = sum(1 for p in phrases if p in quote_lower)
            if phrase_in_passage < 1:
                continue

        # Filter mid-word starts
        stripped = quote_text.lstrip("\"'« \n\t")
        if stripped and stripped[0].islower():
            continue

        # Filter legislative text
        if is_legislative_text(quote_text):
            continue

        year = assign_year(date, mod_years)
        if not year:
            continue

        # Dedup
        all_existing = existing_flat + [q for _, q in results]
        if deduplicate_quote(quote_text, all_existing):
            continue

        clean_orateur = re.sub(r"^(M\.|Mme)\s+le\s+président\.\s*", "", orateur).strip() or orateur

        results.append((year, {
            "citation": quote_text,
            "orateur": clean_orateur,
            "fonction": fonction,
            "date": date,
            "chambre": chambre.upper() if chambre else "",
            "source": "titre_phrase_fill",
        }))

    return results


# ═══════════════════════════════════════════════════════════════════════════
# STEP 2b: Rescue critical no-quote articles (10+ mods, 0 quotes)
# ═══════════════════════════════════════════════════════════════════════════

def step2b_rescue_critical_no_quote_articles(articles: dict, conn: sqlite3.Connection,
                                              stats: dict, verbose: bool) -> None:
    """Rescue articles with 10+ mods but 0 quotes using titre-phrase and objectif search."""
    logger.info("Step 2b: Rescuing critical no-quote articles (10+ mods, 0 quotes)")

    article_debate_ids = build_debate_mappings(articles, conn)

    # Find targets: 10+ mods, 0 quotes
    targets = []
    for ref, art in articles.items():
        if has_quotes(art):
            continue
        if len(art.get("modifications", [])) >= 10:
            targets.append(ref)
    targets.sort()

    logger.info(f"  {len(targets)} articles with 10+ mods and 0 quotes")

    enriched = 0
    quotes_added = 0

    for ref in targets:
        art = articles[ref]
        debate_ids = article_debate_ids.get(ref, set())
        if not debate_ids:
            if verbose:
                logger.info(f"  {ref}: no DILA debates - skipped")
            continue

        mod_years = get_mod_years(art)
        existing_flat = []

        # Strategy 1: Titre-phrase search (broad, no year filter)
        phrases = extract_distinctive_phrases(art)
        if not phrases:
            if verbose:
                logger.info(f"  {ref}: no distinctive phrases - skipped")
            continue

        found = _rescue_by_phrases(conn, debate_ids, ref, phrases, mod_years, existing_flat,
                                   max_new=5)

        # Strategy 2: Objectif-constant phrase search (if strategy 1 found nothing)
        if not found:
            synthese = art.get("synthese", {})
            obj_text = ""
            if isinstance(synthese, dict):
                obj = synthese.get("objectif_constant", "")
                if isinstance(obj, str):
                    obj_text = obj[:300].lower()
            elif isinstance(synthese, str):
                obj_text = synthese[:300].lower()

            if obj_text:
                obj_phrases = []
                for phrase in re.findall(
                    r"[a-zàâäéèêëïîôùûüÿç]+(?:\s+[a-zàâäéèêëïîôùûüÿç]+){1,2}", obj_text
                ):
                    words = phrase.split()
                    substantive = [w for w in words if len(w) >= 5 and w not in GENERIC_KEYWORDS]
                    if len(words) >= 2 and substantive:
                        obj_phrases.append(phrase)

                if obj_phrases:
                    found = _rescue_by_phrases(conn, debate_ids, ref, obj_phrases,
                                              mod_years, existing_flat, max_new=5)

        if found:
            if "curated_quotes" not in art:
                art["curated_quotes"] = {}
            for year, q in found:
                if year not in art["curated_quotes"]:
                    art["curated_quotes"][year] = []
                if len(art["curated_quotes"][year]) < MAX_QUOTES_PER_YEAR:
                    art["curated_quotes"][year].append(q)

            enriched += 1
            added_count = len(found)
            quotes_added += added_count
            if verbose:
                logger.info(f"  {ref} ({art.get('titre', '')[:50]}): +{added_count} quotes")

    stats["step2b_targets"] = len(targets)
    stats["step2b_enriched"] = enriched
    stats["step2b_added"] = quotes_added
    logger.info(f"Step 2b done: {enriched}/{len(targets)} articles rescued, "
                f"{quotes_added} quotes added")


def _rescue_by_phrases(
    conn: sqlite3.Connection,
    debate_ids: set[int],
    ref: str,
    phrases: list[str],
    mod_years: list[int],
    existing_flat: list[dict],
    max_new: int = 5,
) -> list[tuple[str, dict]]:
    """Search debate interventions by phrases for rescue (no year filter, broad)."""
    debate_id_list = list(debate_ids)
    if not debate_id_list:
        return []
    placeholders = ",".join("?" * len(debate_id_list))

    # Use top 6 phrases for SQL
    top_phrases = phrases[:6]
    like_clauses = " OR ".join("i.contenu LIKE ?" for _ in top_phrases)
    like_params = [f"%{p}%" for p in top_phrases]

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
        LIMIT 500
    """, debate_id_list + like_params).fetchall()

    results = []
    for row in rows:
        if len(results) >= max_new:
            break

        contenu = row["contenu"]
        orateur = row["orateur"] or ""
        fonction = row["fonction"] or ""
        chambre = row["chambre"] or ""
        date = str(row["date"])[:10]
        contenu_lower = contenu.lower()

        # Reject other legal codes but don't require CGI mention - these articles
        # are specifically ones missed by ref-based search, discussed by topic name
        if OTHER_CODES.search(contenu):
            continue

        # Filter procedural content
        if any(p in contenu_lower[:200] for p in PROCEDURAL_PATTERNS):
            continue

        # Normalize line wraps for better passage extraction - parliamentary text
        # has hard line breaks within sentences that truncate keyword passages
        contenu_norm = re.sub(r"(?<!\n)\n(?!\n)", " ", contenu)

        # Extract using keyword passage (require 2+ phrases in passage)
        quote_text = _extract_keyword_passage(contenu_norm, phrases)
        if not quote_text or len(quote_text) < 60:
            # Fallback: extract best sentence
            quote_text = _extract_best_sentence(contenu_norm, ref)
            if not quote_text or len(quote_text) < 60:
                continue

        # Verify passage has phrase/keyword relevance - for rescue, a single
        # distinctive multi-word phrase match is sufficient since we have multiple
        # other quality filters and these articles have 0 quotes
        quote_lower = quote_text.lower()
        phrase_score = sum(1 for p in phrases if p in quote_lower)
        if phrase_score < 1:
            continue

        # Filter mid-word starts
        stripped = quote_text.lstrip("\"'« \n\t")
        if stripped and stripped[0].islower():
            continue

        # Filter legislative text
        if is_legislative_text(quote_text):
            continue

        year = assign_year(date, mod_years)
        if not year:
            continue

        # Dedup
        all_existing = existing_flat + [q for _, q in results]
        if deduplicate_quote(quote_text, all_existing):
            continue

        clean_orateur = re.sub(r"^(M\.|Mme)\s+le\s+président\.\s*", "", orateur).strip() or orateur

        results.append((year, {
            "citation": quote_text,
            "orateur": clean_orateur,
            "fonction": fonction,
            "date": date,
            "chambre": chambre.upper() if chambre else "",
            "source": "rescue_critical",
        }))

    return results


# ═══════════════════════════════════════════════════════════════════════════
# STEP 3: Fill 63 articles with 6+ mods and no quotes
# ═══════════════════════════════════════════════════════════════════════════

def step3_fill_high_mod_articles(articles: dict, conn: sqlite3.Connection,
                                 stats: dict, verbose: bool) -> None:
    """Fill quotes for articles with 6+ modifications using topic-based search."""
    logger.info("Step 3: Filling high-mod articles (6+ mods, no quotes)")

    article_debate_ids = build_debate_mappings(articles, conn)

    # Find target articles: 6+ mods, no quotes
    targets = []
    for ref, art in articles.items():
        if has_quotes(art):
            continue
        if len(art.get("modifications", [])) >= 6:
            targets.append(ref)
    targets.sort()

    logger.info(f"  {len(targets)} articles with 6+ mods and no quotes")

    enriched = 0
    quotes_added = 0

    for ref in targets:
        art = articles[ref]
        debate_ids = article_debate_ids.get(ref, set())
        if not debate_ids:
            if verbose:
                logger.info(f"  {ref}: no DILA debates - skipped")
            continue

        ambiguity = classify_ref_ambiguity(ref)
        mod_years = get_mod_years(art)
        keywords = extract_topic_keywords(art)

        if len(keywords) < 2:
            if verbose:
                logger.info(f"  {ref}: insufficient keywords - skipped")
            continue

        existing_flat = []
        cq = art.get("curated_quotes", {})
        if isinstance(cq, dict):
            for ql in cq.values():
                if isinstance(ql, list):
                    existing_flat.extend(ql)

        # Also try exact article ref match first
        pattern = build_article_pattern(ref)
        found_ref = _search_article_ref_all_years(
            conn, debate_ids, ref, pattern, ambiguity, mod_years, existing_flat
        )

        # Then keyword search
        found_kw = search_interventions_by_keywords(
            conn, debate_ids, keywords, ref, ambiguity,
            mod_years, existing_flat + [q for _, q in found_ref],
            max_new=8, min_kw_matches=2,
        )

        all_found = found_ref + found_kw
        if all_found:
            if "curated_quotes" not in art:
                art["curated_quotes"] = {}
            for year, q in all_found:
                if year not in art["curated_quotes"]:
                    art["curated_quotes"][year] = []
                if len(art["curated_quotes"][year]) < MAX_QUOTES_PER_YEAR:
                    art["curated_quotes"][year].append(q)

            enriched += 1
            added_count = len(all_found)
            quotes_added += added_count
            if verbose:
                logger.info(f"  {ref} ({art.get('titre', '')[:50]}): "
                            f"+{added_count} quotes")

    stats["step3_targets"] = len(targets)
    stats["step3_enriched"] = enriched
    stats["step3_added"] = quotes_added
    logger.info(f"Step 3 done: {enriched}/{len(targets)} articles enriched, "
                f"{quotes_added} quotes added")


def _search_article_ref_all_years(
    conn: sqlite3.Connection,
    debate_ids: set[int],
    ref: str,
    pattern: re.Pattern,
    ambiguity: str,
    mod_years: list[int],
    existing_flat: list[dict],
) -> list[tuple[str, dict]]:
    """Search for 'article {ref}' across all years."""
    debate_id_list = list(debate_ids)
    placeholders = ",".join("?" * len(debate_id_list))

    like_pattern = f"%article {ref}%"

    if ambiguity == "extreme":
        extra = " AND (i.contenu LIKE '%code général des impôts%' OR i.contenu LIKE '% CGI%')"
    else:
        extra = ""

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
          {extra}
        ORDER BY d.date
        LIMIT 100
    """, debate_id_list + [like_pattern]).fetchall()

    results = []
    for row in rows:
        if len(results) >= 5:
            break

        contenu = row["contenu"]
        orateur = row["orateur"] or ""
        fonction = row["fonction"] or ""
        chambre = row["chambre"] or ""
        date = str(row["date"])[:10]

        if not pattern.search(contenu):
            continue

        quote_text = extract_quote(contenu, ref)
        if not quote_text:
            continue

        if not is_substantive_quote(quote_text, orateur, fonction, ref, ambiguity):
            continue

        year = assign_year(date, mod_years)
        if not year:
            continue

        all_existing = existing_flat + [q for _, q in results]
        if deduplicate_quote(quote_text, all_existing):
            continue

        clean_orateur = re.sub(r"^(M\.|Mme)\s+le\s+président\.\s*", "", orateur).strip() or orateur

        results.append((year, {
            "citation": quote_text,
            "orateur": clean_orateur,
            "fonction": fonction,
            "date": date,
            "chambre": chambre.upper() if chambre else "",
            "source": "ref_search",
        }))

    return results


# ═══════════════════════════════════════════════════════════════════════════
# STEP 4: Fill ~337 articles with 2-5 mods
# ═══════════════════════════════════════════════════════════════════════════

def step4_fill_mid_mod_articles(articles: dict, conn: sqlite3.Connection,
                                stats: dict, verbose: bool) -> None:
    """Fill quotes for articles with 2-5 modifications using synthese + titre."""
    logger.info("Step 4: Filling mid-mod articles (2-5 mods, no quotes)")

    article_debate_ids = build_debate_mappings(articles, conn)

    targets = []
    for ref, art in articles.items():
        if has_quotes(art):
            continue
        mods = len(art.get("modifications", []))
        if 2 <= mods <= 5:
            targets.append(ref)
    targets.sort()

    logger.info(f"  {len(targets)} articles with 2-5 mods and no quotes")

    enriched = 0
    quotes_added = 0

    for i, ref in enumerate(targets):
        art = articles[ref]
        debate_ids = article_debate_ids.get(ref, set())
        if not debate_ids:
            continue

        ambiguity = classify_ref_ambiguity(ref)
        mod_years = get_mod_years(art)
        keywords = extract_topic_keywords(art)

        if len(keywords) < 2:
            continue

        existing_flat = []

        # Try exact article ref first
        pattern = build_article_pattern(ref)
        found_ref = _search_article_ref_all_years(
            conn, debate_ids, ref, pattern, ambiguity, mod_years, existing_flat
        )

        # Then keyword search
        found_kw = search_interventions_by_keywords(
            conn, debate_ids, keywords, ref, ambiguity,
            mod_years, existing_flat + [q for _, q in found_ref],
            max_new=4, min_kw_matches=2,
        )

        all_found = found_ref + found_kw
        if all_found:
            if "curated_quotes" not in art:
                art["curated_quotes"] = {}
            for year, q in all_found:
                if year not in art["curated_quotes"]:
                    art["curated_quotes"][year] = []
                if len(art["curated_quotes"][year]) < MAX_QUOTES_PER_YEAR:
                    art["curated_quotes"][year].append(q)

            enriched += 1
            quotes_added += len(all_found)
            if verbose:
                logger.info(f"  {ref}: +{len(all_found)} quotes")

        if (i + 1) % 100 == 0:
            logger.info(f"  Step 4 progress: {i+1}/{len(targets)}")

    stats["step4_targets"] = len(targets)
    stats["step4_enriched"] = enriched
    stats["step4_added"] = quotes_added
    logger.info(f"Step 4 done: {enriched}/{len(targets)} articles enriched, "
                f"{quotes_added} quotes added")


# ═══════════════════════════════════════════════════════════════════════════
# STEP 5: Clean off-topic quotes from key articles (all sources)
# ═══════════════════════════════════════════════════════════════════════════

def build_semantic_keywords(art_data: dict) -> set[str]:
    """Build a set of distinctive semantic keywords for an article.

    Extracts from keywords (multi-word → individual substantive words),
    objectif_constant, preoccupations_recurrentes, and titre.
    Excludes GENERIC_KEYWORDS and words < 6 chars (except acronyms 3+ chars).

    Returns words like: {"recherche", "innovation", "développement"} for CIR.
    """
    words = set()

    # From keywords field: split multi-word entries into individual words
    for kw in art_data.get("keywords", []):
        kw_clean = kw.strip().lower()
        # Skip article references
        if re.match(r"^(article\s+)?\d+[\s\-]*(bis|ter|quater|quinquies|sexies|[a-z])*$",
                     kw_clean, re.IGNORECASE):
            continue
        if re.match(r"^l[''']article\s+", kw_clean, re.IGNORECASE):
            continue
        for w in re.findall(r"[a-zàâäéèêëïîôùûüÿç-]{6,}", kw_clean):
            if w not in GENERIC_KEYWORDS:
                words.add(w)
        # Also extract short standalone words (3-5 chars) from keywords field
        # e.g. "isf", "pme", "cir", "tgap" - distinctive fiscal abbreviations
        # too short for the 6-char threshold but highly specific as keywords.
        # Only whole words (bounded by spaces/punctuation/start/end), not fragments.
        for w in re.findall(r"(?:^|[\s'''\-/])([a-zàâäéèêëïîôùûüÿç]{3,5})(?:[\s'''\-/]|$)", kw_clean):
            if w not in GENERIC_KEYWORDS and w not in {
                "dans", "pour", "avec", "plus", "sans", "sous", "tout",
                "tous", "très", "bien", "fait", "même", "être", "avoir",
                "cette", "leur", "elle", "nous", "vous", "mais",
                "donc", "quel", "dont", "sont", "sera", "ceux",
                "aussi", "comme", "après", "avant", "alors",
                "bis", "ter", "les", "des", "une", "aux", "sur",
                "par", "qui", "que", "pas", "son", "ses", "taux",
            }:
                words.add(w)

    # From synthese.objectif_constant (first 500 chars)
    synthese = art_data.get("synthese", {})
    if isinstance(synthese, dict):
        obj = synthese.get("objectif_constant", "")
        if isinstance(obj, str) and obj:
            for w in re.findall(r"[a-zàâäéèêëïîôùûüÿç-]{6,}", obj[:500].lower()):
                if w not in GENERIC_KEYWORDS:
                    words.add(w)

        # From synthese.preoccupations_recurrentes
        preocc = synthese.get("preoccupations_recurrentes", [])
        if isinstance(preocc, str):
            preocc = [preocc]
        if isinstance(preocc, list):
            preocc_text = " ".join(p for p in preocc if isinstance(p, str))
            for w in re.findall(r"[a-zàâäéèêëïîôùûüÿç-]{6,}", preocc_text.lower()):
                if w not in GENERIC_KEYWORDS:
                    words.add(w)

    # From titre
    titre = art_data.get("titre", "").lower()
    for w in re.findall(r"[a-zàâäéèêëïîôùûüÿç-]{6,}", titre):
        if w not in GENERIC_KEYWORDS:
            words.add(w)

    return words


def _build_titre_phrases_for_autokeep(art_data: dict) -> list[str]:
    """Build multi-word phrases from titre for auto-keep matching.

    Returns phrases like "flat tax", "impôt sur les sociétés", "taxe foncière",
    "taxe d'habitation", "frais professionnels" that are strong topic indicators.
    """
    phrases = []
    titre = art_data.get("titre", "").lower()

    # Extract 2-3 word phrases with at least one substantive word (5+ chars)
    for phrase in re.findall(
        r"[a-zàâäéèêëïîôùûüÿç]+(?:[\s'''-]+[a-zàâäéèêëïîôùûüÿç]+){1,3}", titre
    ):
        words = re.findall(r"[a-zàâäéèêëïîôùûüÿç]+", phrase)
        if len(words) >= 2:
            substantive = [w for w in words if len(w) >= 5 and w not in GENERIC_KEYWORDS]
            if substantive:
                # Normalize to space-separated for matching
                phrases.append(" ".join(words))

    # Also include multi-word keywords (very specific)
    for kw in art_data.get("keywords", []):
        kw_clean = kw.strip().lower()
        if re.match(r"^(article\s+)?\d+[\s\-]*(bis|ter|quater|quinquies|sexies|[a-z])*$",
                     kw_clean, re.IGNORECASE):
            continue
        if re.match(r"^l[''']article\s+", kw_clean, re.IGNORECASE):
            continue
        words = re.findall(r"[a-zàâäéèêëïîôùûüÿç]+", kw_clean)
        if len(words) >= 2:
            non_generic = [w for w in words if w not in GENERIC_KEYWORDS and len(w) > 3]
            if non_generic:
                phrases.append(" ".join(words))

    return phrases


def _normalize_for_ref_match(text: str) -> str:
    """Normalize encoding artifacts for article reference matching.

    Replaces broken bytes (\\x80, \\xa0 etc.) and curly quotes with spaces/straight quotes
    so that 'l'article\\x80238 bis' matches 'article 238 bis'.
    """
    # Replace non-ASCII control bytes and non-breaking spaces with regular space
    result = re.sub(r'[\x80-\x9f\xa0]', ' ', text)
    # Normalize curly apostrophes
    result = result.replace('\u2019', "'").replace('\u2018', "'")
    # Collapse multiple spaces
    result = re.sub(r' +', ' ', result)
    return result


def step5_clean_offtopic_key_articles(articles: dict, stats: dict, verbose: bool) -> None:
    """Clean off-topic quotes from key articles using semantic keyword matching.

    Checks ALL quotes regardless of source. Removes quotes with < 2 relevance
    score (unless the quote mentions the article reference or titre phrases).
    """
    logger.info("Step 5: Cleaning off-topic quotes from key articles (all sources)")

    total_checked = 0
    kept = 0
    removed = 0
    removed_articles = set()

    for ref in KEY_ARTICLES:
        art = articles.get(ref)
        if not art:
            continue

        cq = art.get("curated_quotes", {})
        if not isinstance(cq, dict):
            continue

        sem_kw = build_semantic_keywords(art)
        if not sem_kw:
            # No semantic keywords - can't evaluate relevance, keep all
            continue

        titre_phrases = _build_titre_phrases_for_autokeep(art)
        ref_lower = ref.lower()
        modified = False

        for year in list(cq.keys()):
            ql = cq[year]
            if not isinstance(ql, list):
                continue

            new_list = []
            for q in ql:
                total_checked += 1
                citation = q.get("citation", "")
                citation_lower = citation.lower()
                # Normalized version for ref matching (handles encoding artifacts)
                citation_norm = _normalize_for_ref_match(citation_lower)

                # Auto-keep if citation mentions article reference
                if (f"article {ref_lower}" in citation_norm or
                        f"l'article {ref_lower}" in citation_norm):
                    kept += 1
                    new_list.append(q)
                    continue

                # Compute relevance score:
                # - Each single-word keyword match = 1 point
                # - Each multi-word titre/keyword phrase match = 2 points (highly specific)
                score = sum(1 for kw in sem_kw if kw in citation_lower)

                for phrase in titre_phrases:
                    if phrase in citation_norm:
                        score += 2

                if score >= 2:
                    kept += 1
                    new_list.append(q)
                else:
                    removed += 1
                    removed_articles.add(ref)
                    modified = True
                    if verbose:
                        logger.info(
                            f"  Removed [{ref}] {year} (score={score}, "
                            f"src={q.get('source', 'unknown')}): "
                            f"\"{citation[:80]}...\""
                        )

            if modified:
                cq[year] = new_list

        # Clean up empty year keys
        if modified:
            empty_years = [y for y, ql in cq.items()
                           if isinstance(ql, list) and len(ql) == 0]
            for y in empty_years:
                del cq[y]

    stats["step5_total_checked"] = total_checked
    stats["step5_kept"] = kept
    stats["step5_removed"] = removed
    stats["step5_affected_articles"] = len(removed_articles)

    logger.info(f"Step 5 done: {total_checked} quotes checked on {len(KEY_ARTICLES)} key articles")
    logger.info(f"  Kept (relevant):       {kept}")
    logger.info(f"  Removed (off-topic):   {removed}")
    logger.info(f"  Articles affected:     {len(removed_articles)}")


# ═══════════════════════════════════════════════════════════════════════════
# STEP 6: Fill from amendment exposé for topic-name articles
# ═══════════════════════════════════════════════════════════════════════════

# Legislature number → conservative midpoint year
_LEGISLATURE_YEAR_MAP = {
    "14": "2014",
    "15": "2019",
    "16": "2023",
    "17": "2025",
}


def _extract_legislature_year(reference_externe: str | None) -> str | None:
    """Extract year from amendment reference_externe via legislature number.

    Maps AMANR5L16... → "2023", AMANR5L17... → "2025", etc.
    """
    if not reference_externe:
        return None
    m = re.search(r"L(\d+)", reference_externe)
    if m:
        return _LEGISLATURE_YEAR_MAP.get(m.group(1))
    return None


def step6_fill_from_amendments(articles: dict, conn: sqlite3.Connection,
                               stats: dict, verbose: bool) -> None:
    """Fill quotes from amendment exposé motifs for topic-name articles.

    Targets KEY_ARTICLES with < 50% year coverage AND < 10 quotes.
    Extracts from expose_motifs of amendments matching topic phrases.
    """
    logger.info("Step 6: Filling from amendment exposé for topic-name articles")

    # Find targets: key articles with poor coverage
    targets = []
    for ref in KEY_ARTICLES:
        art = articles.get(ref)
        if not art:
            continue

        mod_years = get_mod_years(art)
        if not mod_years:
            continue

        quote_years = get_quote_years(art)
        coverage = len(quote_years & {str(y) for y in mod_years}) / len(mod_years)
        n_quotes = count_quotes(art)

        if coverage < 0.5 and n_quotes < 10:
            targets.append(ref)
            if verbose:
                logger.info(f"  Target: {ref} (coverage={coverage:.0%}, quotes={n_quotes})")

    logger.info(f"  {len(targets)} key articles with <50% coverage and <10 quotes")

    enriched = 0
    quotes_added = 0

    for ref in targets:
        art = articles[ref]
        phrases = extract_distinctive_phrases(art)
        if not phrases:
            if verbose:
                logger.info(f"  {ref}: no distinctive phrases - skipped")
            continue

        # Build SQL query for topic phrases in expose_motifs
        top_phrases = phrases[:6]
        like_clauses = " OR ".join("a.expose_motifs LIKE ?" for _ in top_phrases)
        like_params = [f"%{p}%" for p in top_phrases]

        rows = conn.execute(f"""
            SELECT a.auteur, a.expose_motifs, a.sort, a.objet, a.reference_externe
            FROM amendements a
            WHERE ({like_clauses})
              AND LENGTH(a.expose_motifs) > 100
            ORDER BY CASE WHEN a.sort = 'Adopté' THEN 0
                          WHEN a.sort = 'Retiré' THEN 1
                          ELSE 2 END
            LIMIT 200
        """, like_params).fetchall()

        if not rows:
            if verbose:
                logger.info(f"  {ref}: no matching amendments")
            continue

        # Existing quotes for dedup
        existing_flat = []
        cq = art.get("curated_quotes", {})
        if isinstance(cq, dict):
            for ql in cq.values():
                if isinstance(ql, list):
                    existing_flat.extend(ql)

        mod_years = get_mod_years(art)
        ref_lower = ref.lower()
        art_added = 0

        for row in rows:
            if art_added >= 3:
                break

            raw_expose = row["expose_motifs"] or ""
            auteur = row["auteur"] or ""
            sort_val = row["sort"] or ""
            reference_externe = row["reference_externe"] or ""

            # Clean HTML from expose_motifs
            cleaned = re.sub(r'<[^>]+>', '', html.unescape(raw_expose)).strip()
            cleaned = re.sub(r'\s+', ' ', cleaned)

            if len(cleaned) < 100:
                continue

            # Extract quote via best sentence or keyword passage
            quote_text = _extract_best_sentence(cleaned, ref)
            if not quote_text or len(quote_text) < 60:
                quote_text = _extract_keyword_passage(cleaned, phrases)
            if not quote_text or len(quote_text) < 60:
                continue

            # Filter: reject other legal codes
            if OTHER_CODES.search(quote_text):
                continue

            # Filter: reject legislative text
            if is_legislative_text(quote_text):
                continue

            # Require at least 1 topic phrase in the passage
            quote_lower = quote_text.lower()
            phrase_matches = sum(1 for p in phrases if p in quote_lower)
            if phrase_matches < 1:
                continue

            # Filter mid-word starts
            stripped = quote_text.lstrip("\"'« \n\t")
            if stripped and stripped[0].islower():
                continue

            # Assign year from legislature
            year = _extract_legislature_year(reference_externe)
            if not year:
                # Fallback: use assign_year with a fake recent date
                year = None

            if not year:
                continue

            # Dedup
            all_existing = existing_flat
            if deduplicate_quote(quote_text, all_existing):
                continue

            quote_dict = {
                "citation": quote_text,
                "orateur": auteur,
                "fonction": "auteur d'amendement",
                "source": "amendment_expose",
            }
            if sort_val:
                quote_dict["sort"] = sort_val

            if "curated_quotes" not in art:
                art["curated_quotes"] = {}
            if year not in art["curated_quotes"]:
                art["curated_quotes"][year] = []
            if len(art["curated_quotes"][year]) < MAX_QUOTES_PER_YEAR:
                art["curated_quotes"][year].append(quote_dict)
                existing_flat.append(quote_dict)
                art_added += 1

                if verbose:
                    logger.info(
                        f"  Added [{ref}] {year} (sort={sort_val}): "
                        f"\"{quote_text[:80]}...\""
                    )

        if art_added > 0:
            enriched += 1
            quotes_added += art_added

    stats["step6_targets"] = len(targets)
    stats["step6_enriched"] = enriched
    stats["step6_added"] = quotes_added
    logger.info(f"Step 6 done: {enriched}/{len(targets)} articles enriched, "
                f"{quotes_added} quotes added")


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Clean and enrich article quotes")
    parser.add_argument("--dry-run", action="store_true", help="Don't save changes")
    parser.add_argument("--verbose", "-v", action="store_true", help="Per-article logging")
    parser.add_argument("--step", choices=["1", "1b", "2", "2b", "3", "4", "5", "6", "all"],
                        default="all", help="Run only a specific step")
    args = parser.parse_args()

    logger.info(f"Loading index from {INDEX_PATH}")
    with open(INDEX_PATH) as f:
        index_data = json.load(f)
    articles = index_data["articles"]
    logger.info(f"Loaded {len(articles)} articles")

    # Before stats
    before_with = sum(1 for a in articles.values() if has_quotes(a))
    before_total_q = sum(count_quotes(a) for a in articles.values())
    logger.info(f"Before: {before_with} articles with quotes, {before_total_q} total quotes")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    cgi_index = None
    if args.step in ("1", "all"):
        with open(CGI_INDEX_PATH) as f:
            cgi_index = json.load(f)

    stats = defaultdict(int)

    # ── Create backup before any modifications ──────────────────────────
    if not args.dry_run and INDEX_PATH.exists():
        logger.info(f"Creating backup at {BACKUP_PATH}")
        shutil.copy2(INDEX_PATH, BACKUP_PATH)

    # ── Run steps ────────────────────────────────────────────────────────
    if args.step in ("1", "all"):
        step1_clean_cgi_quotes(articles, conn, cgi_index, stats, args.verbose)
        if not args.dry_run and args.step == "all":
            _save(index_data, "after step 1")

    if args.step in ("1b", "all"):
        step1b_clean_topic_search_quotes(articles, stats, args.verbose)
        if not args.dry_run and args.step == "all":
            _save(index_data, "after step 1b")

    if args.step in ("2", "all"):
        step2_fill_key_articles(articles, conn, stats, args.verbose)
        if not args.dry_run and args.step == "all":
            _save(index_data, "after step 2")

    if args.step in ("2b", "all"):
        step2b_rescue_critical_no_quote_articles(articles, conn, stats, args.verbose)
        if not args.dry_run and args.step == "all":
            _save(index_data, "after step 2b")

    if args.step in ("3", "all"):
        step3_fill_high_mod_articles(articles, conn, stats, args.verbose)
        if not args.dry_run and args.step == "all":
            _save(index_data, "after step 3")

    if args.step in ("4", "all"):
        step4_fill_mid_mod_articles(articles, conn, stats, args.verbose)
        if not args.dry_run and args.step == "all":
            _save(index_data, "after step 4")

    if args.step in ("5", "all"):
        step5_clean_offtopic_key_articles(articles, stats, args.verbose)
        if not args.dry_run and args.step == "all":
            _save(index_data, "after step 5")

    if args.step in ("6", "all"):
        step6_fill_from_amendments(articles, conn, stats, args.verbose)

    # ── Save ─────────────────────────────────────────────────────────────
    if not args.dry_run:
        _save(index_data, "final")
    else:
        logger.info("DRY RUN - not saving")

    # ── After stats ──────────────────────────────────────────────────────
    after_with = sum(1 for a in articles.values() if has_quotes(a))
    after_total_q = sum(count_quotes(a) for a in articles.values())

    logger.info("")
    logger.info("=" * 70)
    logger.info("RESULTS SUMMARY")
    logger.info("=" * 70)
    logger.info(f"Articles with quotes: {before_with} → {after_with} ({after_with - before_with:+d})")
    logger.info(f"Total quotes:         {before_total_q} → {after_total_q} ({after_total_q - before_total_q:+d})")
    logger.info("")

    for step_n in ("1", "1b", "2", "2b", "3", "4", "5", "6"):
        keys = [k for k in sorted(stats) if k.startswith(f"step{step_n}_")]
        if keys:
            logger.info(f"Step {step_n}:")
            for k in keys:
                logger.info(f"  {k}: {stats[k]}")

    # Key article coverage
    logger.info("")
    logger.info("Key articles coverage after cleanup + enrichment:")
    for ref in KEY_ARTICLES[:15]:
        art = articles.get(ref)
        if not art:
            continue
        n_quotes = count_quotes(art)
        n_mods = len(art.get("modifications", []))
        q_years = get_quote_years(art)
        m_years = set(str(y) for y in get_mod_years(art))
        covered = len(q_years & m_years)
        logger.info(f"  {ref:25s}: {n_quotes:3d} quotes, {covered}/{len(m_years)} years covered, "
                    f"{n_mods} mods")

    conn.close()


def _save(index_data: dict, label: str) -> None:
    logger.info(f"Saving ({label})")
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
