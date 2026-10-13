#!/usr/bin/env python3
"""Batch enrichment of curated_quotes from DB interventions.

For each article in the index, searches parliamentary debate interventions
(restricted to DILA-source debates) for substantive quotes mentioning the article.
Adds verified quotes to curated_quotes in article_modifications.json.
"""

import json
import logging
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Paths
INDEX_PATH = Path("data/article_modifications.json")
DB_PATH = Path("data/legislateur.db")

# ── Article ref classification ──────────────────────────────────────────────

QUALIFIERS = (
    "bis", "ter", "quater", "quinquies", "sexies", "septies",
    "octies", "nonies", "decies", "undecies", "duodecies",
    "terdecies", "quaterdecies", "quindecies", "sexdecies",
)

OTHER_CODES = re.compile(
    r"code\s+(pénal|du\s+travail|de\s+la\s+santé|de\s+commerce|civil|"
    r"de\s+l[''']environnement|de\s+l[''']urbanisme|monétaire|rural|"
    r"de\s+la\s+sécurité\s+sociale|de\s+la\s+défense|de\s+l[''']éducation|"
    r"de\s+la\s+construction|de\s+la\s+route|de\s+procédure|CESEDA|"
    r"de\s+l[''']action\s+sociale|de\s+l[''']entrée|de\s+justice|"
    r"forestier|électoral|des\s+douanes|des\s+transports|de\s+la\s+propriété)",
    re.IGNORECASE,
)

# Strict CGI markers: explicit code reference (for very ambiguous refs)
CGI_STRICT = re.compile(
    r"code\s+général\s+des\s+impôts|du\s+CGI|\bCGI\b|code\s+des\s+impôts",
    re.IGNORECASE,
)

# Relaxed CGI markers: fiscal vocabulary (for moderately ambiguous refs)
CGI_RELAXED = re.compile(
    r"code\s+général\s+des\s+impôts|du\s+CGI|\bCGI\b|"
    r"code\s+des\s+impôts|fiscal|impôt|taxe|droit\s+de\s+mutation|"
    r"plus-value|exonération|réduction\s+d[''']impôt|crédit\s+d[''']impôt|"
    r"défiscalisation|abattement|barème|imposition|contribuable",
    re.IGNORECASE,
)

# Patterns that indicate "article N" refers to the law article, not CGI
LAW_ARTICLE_CONTEXT = re.compile(
    r"article\s+(premier|1er)\s+du\s+(projet|texte|présent)|"
    r"(projet|texte)\s+de\s+loi.*article\s+\d|"
    r"l[''']article\s+\d+\s+(du\s+projet|du\s+présent|du\s+texte)",
    re.IGNORECASE,
)


def classify_ref_ambiguity(ref: str) -> str:
    """Classify article ref ambiguity level.

    Returns:
        "unambiguous" - unique CGI ref (e.g., "787 B", "150-0 B ter")
        "moderate" - could match other codes but distinct enough (e.g., "1649 quater B bis")
        "high" - short pure numbers (e.g., "200", "1411")
        "extreme" - very short (1-2 digits: "1", "39") that match law articles too
    """
    ref_lower = ref.lower()
    words = ref_lower.split()

    # Contains qualifiers like bis/ter → at least moderate
    has_qualifier = any(q in words for q in QUALIFIERS)
    # Contains letter suffix like "B", "A" after a space
    has_letter_suffix = bool(re.search(r"\d+\s+[A-Z]", ref))
    # Complex numbering
    has_complex = "-" in ref and len(ref) > 5

    if has_qualifier and has_letter_suffix:
        return "unambiguous"  # "150-0 B ter", "199 terdecies-0 A"
    if has_qualifier or has_letter_suffix or has_complex:
        return "moderate"  # "39 bis", "787 B", "150-0"

    # Pure number
    digits_only = ref.replace("-", "").replace(" ", "")
    if len(digits_only) <= 2:
        return "extreme"  # "1", "39", "44"
    if len(digits_only) <= 4:
        return "high"  # "200", "1411", "1729"
    return "moderate"  # "16586" — long enough to be fairly unique


def build_article_pattern(ref: str) -> re.Pattern:
    """Build regex to match 'article {ref}' precisely (not partial)."""
    escaped = re.escape(ref)
    # After the ref, must NOT be followed by more alphanumeric or qualifier
    if ref[-1].isalpha():
        # Ref ends with letter (e.g., "787 B", "150-0 B ter")
        suffix = r"(?!\w)"
    else:
        # Ref ends with digit (e.g., "39", "1411")
        # Must not be followed by: more digits, space+digits (spaced numbers like "1 499"),
        # qualifier words, or ordinal suffixes (1er, 2e)
        qualifiers_alt = "|".join(QUALIFIERS + tuple("ABCDEFGHIJKLMNO"))
        suffix = (
            rf"(?!\d)"                           # not "390" when searching "39"
            rf"(?!\s+\d)"                        # not "1 499" when searching "1"
            rf"(?!\s*(?:er|ère|e|ème)\b)"        # not "1er", "2e"
            rf"(?!\s+(?:{qualifiers_alt})\b)"    # not "39 bis", "200 A"
        )
    return re.compile(rf"article\s+{escaped}{suffix}", re.IGNORECASE)


# ── Quote extraction ────────────────────────────────────────────────────────

PROCEDURAL_PATTERNS = [
    "est ainsi libellé",
    "sont ainsi libellé",   # plural
    "est ainsi modifié",
    "est ainsi rédigé",
    "est ainsi conçu",
    "amendement est adopt",
    "amendement est retir",
    "amendement n'est pas adopt",
    "scrutin public",
    "mise aux voix",
    "quel est l'avis du gouvernement",
    "quel est l'avis de la commission",
    "je mets aux voix",
    "la parole est à",
    "en conséquence,",
    "je suis saisi",
    "il n'y a pas d'opposition",
    "insérer un article additionnel",
    "est adopté",
    "est supprimé",
    "n'est pas adopté",
    "amendement n°",
    "amendement no",
    "amendements nos",
    "amendements n°",
]


def extract_quote(contenu: str, ref: str, max_chars: int = 400) -> str | None:
    """Extract a relevant passage from the intervention around the article mention."""
    # Find the mention
    match = re.search(rf"article\s+{re.escape(ref)}", contenu, re.IGNORECASE)
    if not match:
        return None

    mention_pos = match.start()

    # Find sentence start: go back to previous sentence end
    start = max(0, mention_pos - 200)
    for i in range(mention_pos - 1, start, -1):
        if contenu[i] in ".!\n" and i < mention_pos - 10:
            start = i + 1
            break

    # Find sentence end: go forward
    end = min(len(contenu), mention_pos + max_chars)
    found_end = False
    for i in range(mention_pos + len(ref) + 8, end):
        if contenu[i] in ".!" and i > mention_pos + 50:
            end = i + 1
            found_end = True
            break
        if contenu[i] == "\n" and i > mention_pos + 30:
            end = i
            found_end = True
            break

    # If we didn't find a natural end, add ellipsis
    quote = contenu[start:end].strip()
    quote = re.sub(r"\s+", " ", quote)
    if not found_end and len(quote) >= max_chars - 20:
        quote = quote.rstrip() + "..."

    return quote


def is_quality_quote(
    quote: str, orateur: str, fonction: str, ref: str, ambiguity: str,
    full_contenu: str = "",
) -> bool:
    """Check if a quote passes quality filters."""
    if not quote or len(quote) < 60:
        return False

    # Filter president/procedural speakers
    if fonction:
        f_lower = fonction.lower()
        if "president" in f_lower or "président" in f_lower:
            return False
    # Also check orateur field for president (DB sometimes has it there)
    if orateur:
        orateur_lower = orateur.lower()
        if "président" in orateur_lower or "présidente" in orateur_lower:
            return False

    # Filter mid-word starts
    stripped = quote.lstrip("\"'« \n\t")
    if stripped and stripped[0].islower():
        return False

    # Filter procedural content
    quote_lower = quote.lower()
    for p in PROCEDURAL_PATTERNS:
        if p in quote_lower:
            return False

    # Filter amendment readings (lists of senator names)
    if re.match(r"^(MM?\.|Mmes?)\s+[\w\s,]+est ainsi", quote):
        return False

    # Filter raw legislative text (article content, not commentary)
    if re.search(r"^[«»\"]?\s*[IVX]+[\.\s]*[-–]", stripped):
        return False
    if re.search(r"est\s+complété\s+par|est\s+remplacé\s+par|sont\s+supprimés", quote_lower):
        return False

    # Filter article/amendment procedural sequences
    # e.g., "(L'article 39 ter est adopté.)" or "Article 39 ter (p."
    if re.search(r"\(l[''']article\s+\S+.*?est\s+adopt", quote_lower):
        return False
    # Amendment listing: "Amendement no 190Articles 39 et 39 bis..."
    if re.match(r"^(Amendement|Article)\s+(n[o°]|nos|\d)", stripped):
        return False

    # ── CGI context verification based on ambiguity level ──
    # Use full_contenu for broader context when available
    context = full_contenu if full_contenu else quote

    if ambiguity == "extreme":
        # Very short refs (1-2 digits): require STRICT CGI reference NEAR the mention
        if not CGI_STRICT.search(context):
            return False
        if OTHER_CODES.search(context):
            return False
        # The CGI reference must be within 200 chars of the article mention
        mention = re.search(rf"article\s+{re.escape(ref)}\b", context, re.IGNORECASE)
        if mention:
            window_start = max(0, mention.start() - 200)
            window_end = min(len(context), mention.end() + 200)
            window = context[window_start:window_end]
            if not CGI_STRICT.search(window):
                return False
            # Check it's not "article N of the law being discussed"
            after = context[mention.end():mention.end() + 100]
            if re.match(r"\s+(du\s+projet|du\s+présent|du\s+texte|de\s+la\s+proposition)", after, re.IGNORECASE):
                return False
            # Check it's not "article N modifié/adopté" (law article)
            if re.match(r"[,\s]+(modifié|est\s+adopt|est\s+supprim|est\s+rétab)", after, re.IGNORECASE):
                return False

    elif ambiguity == "high":
        # Medium numeric refs (3-4 digits): require relaxed CGI context
        if not CGI_RELAXED.search(context):
            return False
        if OTHER_CODES.search(context):
            return False

    elif ambiguity == "moderate":
        # Has some specificity but check for other codes
        if OTHER_CODES.search(quote):
            return False

    # "unambiguous" refs don't need CGI context checks

    return True


def assign_year(debate_date: str, modification_years: list[int]) -> str | None:
    """Assign a quote to the closest modification year based on debate date."""
    if not modification_years:
        return None
    debate_year = int(str(debate_date)[:4])
    debate_month = int(str(debate_date)[5:7]) if len(str(debate_date)) >= 7 else 6

    # Finance law debates happen in Oct-Dec for the following year's law
    # So a debate in Oct 2018 → LF 2019 (year 2019 in modifications)
    candidates = []
    for y in modification_years:
        # Debate year matches mod year (Jan-Sep) or mod year - 1 (Oct-Dec)
        if y == debate_year:
            candidates.append((abs(y - debate_year), y))
        elif y == debate_year + 1 and debate_month >= 9:
            candidates.append((0, y))  # Best match: fall debate for next year's law
        else:
            candidates.append((abs(y - debate_year), y))

    if candidates:
        candidates.sort()
        return str(candidates[0][1])
    return str(debate_year)


def deduplicate_quote(new_quote: str, existing_quotes: list[dict], threshold: int = 60) -> bool:
    """Returns True if the quote is too similar to an existing one."""
    new_norm = new_quote[:threshold].lower().strip()
    for eq in existing_quotes:
        existing_norm = eq.get("citation", "")[:threshold].lower().strip()
        if new_norm == existing_norm:
            return True
    return False


# ── Main enrichment logic ───────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    limit = None
    for arg in sys.argv:
        if arg.startswith("--limit="):
            limit = int(arg.split("=")[1])

    logger.info(f"Loading index from {INDEX_PATH}")
    with open(INDEX_PATH) as f:
        index_data = json.load(f)
    articles = index_data["articles"]
    logger.info(f"Loaded {len(articles)} articles")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Step 1: Build date → debate_id mapping
    logger.info("Building debate date mapping...")
    date_to_debates = defaultdict(list)
    rows = conn.execute("SELECT id, chambre, date FROM debats").fetchall()
    for r in rows:
        date_str = str(r["date"])[:10]  # YYYY-MM-DD
        date_key = date_str.replace("-", "")[:8]  # YYYYMMDD
        date_to_debates[date_key].append({
            "id": r["id"],
            "chambre": r["chambre"],
            "date": date_str,
        })
    logger.info(f"Mapped {len(date_to_debates)} debate dates, {sum(len(v) for v in date_to_debates.values())} debates")

    # Step 2: Build debate_id → article_refs mapping (from DILA sources)
    logger.info("Building debate→article mapping from DILA sources...")
    debate_to_articles = defaultdict(set)  # debate_id → set of article refs
    article_debate_ids = defaultdict(set)  # article_ref → set of debate_ids

    for art_name, art_data in articles.items():
        dila_sources = art_data.get("dila_sources", [])
        for dila in dila_sources:
            # Extract YYYYMMDD from dila source string
            m = re.search(r"(\d{8})", dila)
            if m:
                date_key = m.group(1)
                for debate in date_to_debates.get(date_key, []):
                    debate_to_articles[debate["id"]].add(art_name)
                    article_debate_ids[art_name].add(debate["id"])

    logger.info(f"Mapped {len(debate_to_articles)} debates to articles")
    articles_with_debates = sum(1 for v in article_debate_ids.values() if v)
    logger.info(f"Articles with mapped debates: {articles_with_debates}")

    # Step 3: Process each article
    stats = {
        "articles_processed": 0,
        "articles_enriched": 0,
        "quotes_added": 0,
        "quotes_rejected_quality": 0,
        "quotes_rejected_dedup": 0,
        "articles_skipped_no_debates": 0,
        "by_ambiguity": defaultdict(lambda: {"processed": 0, "enriched": 0, "added": 0}),
    }

    article_names = sorted(articles.keys())
    if limit:
        article_names = article_names[:limit]

    for i, art_name in enumerate(article_names):
        art_data = articles[art_name]
        debate_ids = article_debate_ids.get(art_name, set())

        if not debate_ids:
            stats["articles_skipped_no_debates"] += 1
            continue

        stats["articles_processed"] += 1

        # Get existing quotes
        curated = art_data.get("curated_quotes", {})
        existing_flat = []
        for year_quotes in curated.values():
            existing_flat.extend(year_quotes)
        existing_count = len(existing_flat)

        # Build modification years
        mod_years = []
        for mod in art_data.get("modifications", []):
            y = mod.get("annee") or (mod.get("date", "")[:4] if mod.get("date") else None)
            if y:
                mod_years.append(int(y))
        mod_years = sorted(set(mod_years))

        # Classify ref ambiguity
        ambiguity = classify_ref_ambiguity(art_name)
        pattern = build_article_pattern(art_name)
        stats["by_ambiguity"][ambiguity]["processed"] += 1

        # Query interventions in relevant debates
        debate_id_list = list(debate_ids)
        placeholders = ",".join("?" * len(debate_id_list))

        # For extreme/high ambiguity, also require CGI context in SQL
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

        # Cap: max quotes to add per article (avoid flooding)
        MAX_NEW_PER_ARTICLE = 10
        MAX_TOTAL_PER_ARTICLE = 15

        # Filter and extract quotes
        new_quotes_by_year = defaultdict(list)
        total_new = 0
        for row in rows:
            contenu = row["contenu"]
            orateur = row["orateur"] or ""
            fonction = row["fonction"] or ""
            chambre = row["chambre"] or ""
            date = str(row["date"])[:10]

            # Check caps
            if total_new >= MAX_NEW_PER_ARTICLE:
                break
            if existing_count + total_new >= MAX_TOTAL_PER_ARTICLE:
                break

            # Verify exact article ref match (not partial)
            if not pattern.search(contenu):
                continue

            # Extract quote
            quote_text = extract_quote(contenu, art_name)
            if not quote_text:
                continue

            # Quality check
            if not is_quality_quote(
                quote_text, orateur, fonction, art_name, ambiguity,
                full_contenu=contenu,
            ):
                stats["quotes_rejected_quality"] += 1
                continue

            # Assign year
            year = assign_year(date, mod_years)
            if not year:
                continue

            # Dedup against existing + newly added
            all_existing = existing_flat + [
                q for qs in new_quotes_by_year.values() for q in qs
            ]
            if deduplicate_quote(quote_text, all_existing):
                stats["quotes_rejected_dedup"] += 1
                continue

            # Clean speaker name
            clean_orateur = re.sub(r"^(M\.|Mme)\s+le\s+président\.\s*", "", orateur).strip()
            if not clean_orateur:
                clean_orateur = orateur

            new_quotes_by_year[year].append({
                "citation": quote_text,
                "orateur": clean_orateur,
                "fonction": fonction,
                "date": date,
                "chambre": chambre.upper() if chambre else "",
                "source_verified": True,
            })
            total_new += 1

        # Add new quotes
        if new_quotes_by_year:
            if "curated_quotes" not in art_data:
                art_data["curated_quotes"] = {}

            added_count = 0
            for year, quotes in new_quotes_by_year.items():
                if year not in art_data["curated_quotes"]:
                    art_data["curated_quotes"][year] = []
                art_data["curated_quotes"][year].extend(quotes)
                added_count += len(quotes)

            stats["articles_enriched"] += 1
            stats["quotes_added"] += added_count
            stats["by_ambiguity"][ambiguity]["enriched"] += 1
            stats["by_ambiguity"][ambiguity]["added"] += added_count

            if verbose:
                logger.info(f"  {art_name} [{ambiguity}]: +{added_count} quotes (had {existing_count})")

        if (i + 1) % 500 == 0:
            logger.info(f"Progress: {i+1}/{len(article_names)} articles processed")

    # Step 4: Save
    if not dry_run:
        logger.info(f"Saving to {INDEX_PATH}")
        with open(INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump(index_data, f, ensure_ascii=False, indent=2)
        logger.info("Saved.")
    else:
        logger.info("DRY RUN — not saving")

    # Report
    logger.info("=" * 60)
    logger.info("ENRICHMENT RESULTS")
    logger.info("=" * 60)
    logger.info(f"Articles processed:       {stats['articles_processed']}")
    logger.info(f"Articles skipped (no debates): {stats['articles_skipped_no_debates']}")
    logger.info(f"Articles enriched:        {stats['articles_enriched']}")
    logger.info(f"Quotes added:             {stats['quotes_added']}")
    logger.info(f"Quotes rejected (quality): {stats['quotes_rejected_quality']}")
    logger.info(f"Quotes rejected (dedup):   {stats['quotes_rejected_dedup']}")
    logger.info("")
    logger.info("By ambiguity level:")
    for level in ("unambiguous", "moderate", "high", "extreme"):
        s = stats["by_ambiguity"].get(level, {"processed": 0, "enriched": 0, "added": 0})
        logger.info(f"  {level:12s}: {s['processed']:4d} processed, {s['enriched']:4d} enriched, {s['added']:4d} quotes added")

    conn.close()


if __name__ == "__main__":
    main()
