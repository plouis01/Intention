#!/usr/bin/env python3
"""Enrich article_modifications.json with curated data.

Usage:
    # DB-only enrichment (fast, no API calls)
    python scripts/enrich_article_index.py

    # Full enrichment (DB + LLM)
    python scripts/enrich_article_index.py --with-llm

    # Synthese only (skip jurisprudence, 1 LLM call per article)
    python scripts/enrich_article_index.py --with-llm --skip-jurisprudence --delay 1

    # Jurisprudence only (skip synthese + forward-looking)
    python scripts/enrich_article_index.py --with-llm --only-jurisprudence --delay 1

    # With Anthropic fallback when Groq keys are exhausted
    python scripts/enrich_article_index.py --with-llm --anthropic-fallback --delay 2

    # Generate Légifrance URLs for all articles (no API calls)
    python scripts/enrich_article_index.py --legifrance-urls

    # Resume after interruption
    python scripts/enrich_article_index.py --resume

    # Enrich specific articles only
    python scripts/enrich_article_index.py --articles "150-0 B ter,199 terdecies-0 A"

    # Dry run (don't save)
    python scripts/enrich_article_index.py --dry-run
"""

import argparse
import copy
import html as html_mod
import json
import logging
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from src.analysis.cgi_extractor import CGIExtractor
from src.db.models import (
    Debat,
    DossierLegislatif,
    Intervention,
    Loi,
    SourceExterne,
    get_session,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Silence SQLAlchemy SQL logging (engine uses echo=True in dev mode)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy").setLevel(logging.WARNING)

INDEX_PATH = Path(__file__).parent.parent / "data" / "article_modifications.json"
CHECKPOINT_PATH = Path(__file__).parent.parent / "data" / "enrichment_checkpoint.json"

# Fields that are manually curated and should never be overwritten
CURATED_FIELDS = [
    "jurisprudence",
    "curated_quotes",
    "synthese",
    "synthese_evolution",
    "forward_looking",
    "rapports_parlementaires",
    "dila_sources",
]

# Procedural phrases to reject in quotes (from article_analyzer.py)
PROCEDURAL_PATTERNS = [
    "l'article est ainsi modifié",
    "l'article est ainsi rédigé",
    "la parole est à",
    "je mets aux voix",
    "l'amendement est adopté",
    "l'amendement est rejeté",
    "il en est ainsi décidé",
    "la séance est levée",
    "la séance est suspendue",
    "je vous remercie",
    "rappel au règlement",
    "avis du gouvernement",
    "avis de la commission",
]


@dataclass
class MentionRecord:
    """A mention of a CGI article in an intervention."""

    intervention_id: int
    debat_date: str
    orateur: str
    fonction: str
    chambre: str
    snippet: str
    law_numero: str | None = None
    law_year: int | None = None
    law_type: str | None = None


def speaker_weight(fonction: str) -> float:
    """Weight a speaker by importance (reuses logic from models.py).

    Handles HTML entities in Sénat data (e.g. 'pr&#233;sident' for 'président').
    """
    import html as html_mod

    f = html_mod.unescape((fonction or "")).lower()
    if "rapporteur" in f and ("général" in f or "genera" in f):
        return 2.0
    if "rapporteur" in f:
        return 1.5
    if "ministre" in f or "secrétaire d" in f:
        return 1.5
    if "président" in f and "commission" in f:
        return 1.0
    return 0.5


def is_procedural(text: str) -> bool:
    """Check if text is primarily procedural content."""
    text_lower = text.lower()
    for pattern in PROCEDURAL_PATTERNS:
        if pattern in text_lower:
            return True
    return len(text.strip()) < 50


def is_president_de_seance(fonction: str) -> bool:
    """Check if the speaker is a président de séance (low-value quotes)."""
    f = html_mod.unescape((fonction or "")).lower()
    if "président" not in f:
        return False
    # président de commission/rapporteur are valuable - only filter session chairs
    if "commission" in f or "rapporteur" in f:
        return False
    return True


def clean_name(name: str) -> str:
    """Fix doubled names and HTML entities in orateur/fonction strings.

    'M. le président. M. le président' → 'M. le président'
    'M. Pierre Lellouche, M. Pierre Lellouche,' → 'M. Pierre Lellouche'
    'pr&#233;sident de s&#233;ance' → 'président de séance'
    """
    if not name:
        return name
    # Unescape HTML entities
    name = html_mod.unescape(name)
    # Strip trailing punctuation artifacts (trailing commas, dots)
    name = name.rstrip(",. ")
    # Fix doubled names with ". " separator
    # e.g. "M. le président. M. le président"
    parts = name.split(". ")
    if len(parts) >= 2:
        mid = len(parts) // 2
        first_half = ". ".join(parts[:mid])
        second_half = ". ".join(parts[mid:])
        if first_half == second_half:
            return first_half
        # "M. Name. M. Name" where first part is "M"
        if len(parts) == 4 and parts[0] == parts[2] and parts[1] == parts[3]:
            return f"{parts[0]}. {parts[1]}"
    # Fix doubled names with ", " separator
    # e.g. "M. Pierre Lellouche, M. Pierre Lellouche"
    comma_parts = name.split(", ")
    if len(comma_parts) == 2 and comma_parts[0] == comma_parts[1]:
        return comma_parts[0]
    # Fix doubled names where the name is simply repeated with space
    # e.g. "rapporteur général rapporteur général"
    words = name.split()
    if len(words) >= 2 and len(words) % 2 == 0:
        mid = len(words) // 2
        if words[:mid] == words[mid:]:
            return " ".join(words[:mid])
    return name


def clean_quote(quote: dict) -> dict:
    """Apply all mechanical fixes to a single quote dict."""
    return {
        **quote,
        "orateur": clean_name(quote.get("orateur", "")),
        "fonction": clean_name(quote.get("fonction", "")),
    }


_ARTICLE_NUM_RE = re.compile(r"(?:l')?articles?\s+\d", re.IGNORECASE)

# Common words in French parliamentary debates about finance laws.
# These are too generic for relevance filtering.
_GENERIC_WORDS = {
    # French stopwords
    "dans", "avec", "pour", "plus", "cette", "entre", "leurs", "après",
    "avoir", "faire", "être", "notre", "autres", "comme", "aussi",
    "toutes", "encore", "avant", "depuis", "toujours",
    # Parliamentary/legislative generic
    "article", "articles", "mesure", "mesures", "disposition", "dispositions",
    "amendement", "amendements", "commission", "rapport", "rapporteur",
    "conditions", "modalités", "application", "contribuables",
    "gouvernement", "projet",
    # Tax/finance generic
    "impôt", "impôts", "fiscal", "fiscale", "fiscaux", "fiscales",
    "revenu", "revenus", "société", "sociétés", "droit", "droits",
    "régime", "régimes", "montant", "taux", "budget",
    "crédit", "réduction", "exonération", "taxe", "taxes",
    "recettes", "dépenses", "plafond", "barème",
    # Very common French words that happen to be ≥6 chars
    "nature", "nombre", "partie", "période", "système",
    "lors", "titres", "titre", "report", "values",
    "global", "globale", "cession", "charges",
}


def build_relevance_keywords(article_ref: str, article_data: dict) -> set[str]:
    """Build a set of substantive keywords for relevance filtering.

    Returns lowercase keywords derived from the article's `keywords` field
    and `titre`. Prioritizes multi-word phrases over generic single words.

    Strategy:
    - Multi-word phrases (containing spaces): always included (very discriminating)
    - Single words ≥ 6 chars: only if not in the parliamentary debate stoplist
    """
    kws = set()
    ref_lower = article_ref.lower()
    for kw in article_data.get("keywords", []):
        kw_lower = kw.lower().strip()
        if len(kw_lower) <= 3:
            continue
        if kw_lower.isdigit():
            continue
        if kw_lower == ref_lower:
            continue
        if kw_lower in (f"article {ref_lower}", f"l'article {ref_lower}"):
            continue
        if _ARTICLE_NUM_RE.match(kw_lower):
            continue

        # Multi-word phrases are always included (more specific)
        if " " in kw_lower:
            kws.add(kw_lower)
        elif len(kw_lower) >= 6 and kw_lower not in _GENERIC_WORDS:
            kws.add(kw_lower)
    # Add titre words - only specific ones
    titre = article_data.get("titre", "")
    for word in titre.lower().split():
        if len(word) >= 6 and word not in _GENERIC_WORDS and not word.isdigit():
            kws.add(word)
    return kws


def is_bare_numeric_ref(ref: str) -> bool:
    """Check if a CGI article ref is a bare number (e.g. '1', '39', '150').

    Bare numeric refs are ambiguous in debate text because 'article 1' usually
    refers to article 1 of the bill being discussed, not CGI article 1.
    """
    return ref.strip().isdigit()


# Patterns confirming that a nearby "article X" really refers to the CGI
_CGI_CONTEXT_RE = re.compile(
    r"(?:du\s+)?(?:code\s+g[eé]n[eé]ral\s+des\s+imp[oô]ts|CGI|m[eê]me\s+code)",
    re.IGNORECASE,
)


def has_cgi_context(text: str, match_start: int, match_end: int, window: int = 300) -> bool:
    """Check if CGI-confirming text exists near the match position.

    Looks for 'CGI', 'code général des impôts', or 'même code' within
    ±window characters of the match.
    """
    start = max(0, match_start - window)
    end = min(len(text), match_end + window)
    return bool(_CGI_CONTEXT_RE.search(text[start:end]))


def extract_snippet(text: str, match_start: int, context_chars: int = 500) -> str:
    """Extract a snippet around a regex match position."""
    # Find paragraph boundaries
    para_start = text.rfind("\n\n", 0, match_start)
    para_start = para_start + 2 if para_start >= 0 else max(0, match_start - context_chars // 2)

    para_end = text.find("\n\n", match_start)
    para_end = para_end if para_end >= 0 else min(len(text), match_start + context_chars // 2)

    # Limit to context_chars
    snippet = text[para_start:para_end].strip()
    if len(snippet) > context_chars:
        snippet = snippet[:context_chars] + "..."
    return snippet


# ============================================================
# Phase 1: Build CGI Mention Index
# ============================================================


def build_cgi_mention_index(session) -> dict[str, list[MentionRecord]]:
    """Scan all interventions and build an inverted index of CGI article mentions.

    Uses a two-phase approach for performance on remote databases:
    1. Pre-load debate metadata (debat_id → chambre/date/law info) into a dict
    2. Scan interventions with minimal columns, looking up metadata from the dict

    Returns:
        Dict mapping normalized CGI article ref -> list of MentionRecord
    """
    logger.info("Phase 1: Building CGI mention index from interventions...")
    extractor = CGIExtractor()

    # Step 1: Pre-load debate metadata into a lookup dict
    # This avoids expensive joinedload chains on each batch
    logger.info("  Step 1: Pre-loading debate metadata...")
    debat_meta: dict[int, tuple[str, str, str | None, int | None, str | None]] = {}
    # Query: debat_id -> (chambre, date, law_numero, law_year, law_type)
    debat_rows = (
        session.query(
            Debat.id,
            Debat.chambre,
            Debat.date,
            Loi.numero,
            Loi.annee,
            Loi.type,
        )
        .outerjoin(DossierLegislatif, Debat.dossier_id == DossierLegislatif.id)
        .outerjoin(Loi, DossierLegislatif.loi_id == Loi.id)
        .all()
    )
    for row in debat_rows:
        debat_id, chambre, date, law_num, law_year, law_type = row
        date_str = date.strftime("%Y-%m-%d") if date else ""
        debat_meta[debat_id] = (chambre or "?", date_str, law_num, law_year, law_type)
    logger.info(f"  Loaded metadata for {len(debat_meta):,} debates")
    session.expire_all()

    # Step 2: Scan interventions with minimal columns
    total = session.query(func.count(Intervention.id)).scalar()
    logger.info(f"  Step 2: Scanning {total:,} interventions...")

    mention_index: dict[str, list[MentionRecord]] = defaultdict(list)
    batch_size = 5000
    processed = 0
    mentions_found = 0
    bare_numeric_filtered = 0

    for offset in range(0, total, batch_size):
        # Query only the columns we need (no joinedload)
        interventions = (
            session.query(
                Intervention.id,
                Intervention.debat_id,
                Intervention.orateur,
                Intervention.fonction,
                Intervention.contenu,
            )
            .order_by(Intervention.id)
            .offset(offset)
            .limit(batch_size)
            .all()
        )

        for interv in interventions:
            interv_id, debat_id, orateur, fonction, contenu = interv
            contenu = contenu or ""
            if len(contenu) < 20:
                continue

            # Use CGI extractor to find references
            refs = extractor.extract_cgi_references(contenu, context_chars=500)
            if not refs:
                continue

            # Look up debate metadata from pre-loaded dict
            meta = debat_meta.get(debat_id)
            if meta:
                chambre, debat_date, law_numero, law_year, law_type = meta
            else:
                chambre, debat_date = "?", ""
                law_numero, law_year, law_type = None, None, None

            for ref in refs:
                # Filter bare numeric refs: require CGI context confirmation
                # to avoid false positives (e.g. "article 1" of the bill)
                if is_bare_numeric_ref(ref.article_ref) and not has_cgi_context(
                    contenu, ref.match_start, ref.match_end
                ):
                    bare_numeric_filtered += 1
                    continue

                # Extract snippet around the match
                snippet = extract_snippet(contenu, ref.match_start)

                record = MentionRecord(
                    intervention_id=interv_id,
                    debat_date=debat_date,
                    orateur=orateur or "",
                    fonction=fonction or "",
                    chambre=chambre,
                    snippet=snippet,
                    law_numero=law_numero,
                    law_year=law_year,
                    law_type=law_type,
                )
                mention_index[ref.article_ref].append(record)
                mentions_found += 1

        processed += len(interventions)
        if processed % 50000 == 0 or processed >= total:
            logger.info(
                f"  Processed {processed:,}/{total:,} interventions, "
                f"found {mentions_found:,} mentions across {len(mention_index)} articles"
            )

        # Expire objects to free memory
        session.expire_all()

    logger.info(
        f"Phase 1 complete: {mentions_found:,} mentions for {len(mention_index)} CGI articles"
        f" (filtered {bare_numeric_filtered:,} bare numeric false positives)"
    )
    return dict(mention_index)


# ============================================================
# Phase 2: DB-sourced enrichment
# ============================================================


def enrich_curated_quotes(
    article_ref: str,
    article_data: dict,
    mention_index: dict[str, list[MentionRecord]],
    relevance_keywords: set[str] | None = None,
) -> dict:
    """Generate curated_quotes from CGI mention index.

    Groups quotes by law year, selects top quotes by speaker weight,
    filters out procedural content and irrelevant snippets.

    Args:
        relevance_keywords: If provided, snippets must contain at least one
            keyword to be included. This filters out false positives where
            the CGI article number matches but the content is about a
            different topic.
    """
    mentions = mention_index.get(article_ref, [])
    if not mentions:
        return {}

    # Group by year
    by_year: dict[str, list[MentionRecord]] = defaultdict(list)
    for m in mentions:
        if m.law_year:
            by_year[str(m.law_year)].append(m)
        elif m.debat_date:
            year = m.debat_date[:4]
            by_year[year].append(m)

    curated_quotes = {}
    for year, year_mentions in sorted(by_year.items()):
        # Filter out procedural, short snippets, and président de séance
        substantive = [
            m for m in year_mentions
            if not is_procedural(m.snippet) and not is_president_de_seance(m.fonction)
        ]

        # Keyword relevance filter: drop snippets that don't mention
        # any substantive keyword related to the article's subject
        if relevance_keywords:
            substantive = [
                m for m in substantive
                if any(kw in m.snippet.lower() for kw in relevance_keywords)
            ]

        if not substantive:
            continue

        # Sort by speaker weight (descending), then by snippet length
        substantive.sort(key=lambda m: (-speaker_weight(m.fonction), -len(m.snippet)))

        # Group by chamber and pick top quotes
        by_chamber: dict[str, list[MentionRecord]] = defaultdict(list)
        for m in substantive:
            if m.chambre in ("AN", "an"):
                ch = "AN"
            elif m.chambre in ("SENAT", "senat", "Sénat"):
                ch = "Sénat"
            else:
                ch = m.chambre
            by_chamber[ch].append(m)

        year_quotes = []
        for chamber, chamber_mentions in by_chamber.items():
            # Deduplicate by orateur+date
            seen = set()
            for m in chamber_mentions[:3]:  # Cap: 3 per chamber per year
                dedup_key = (m.orateur, m.debat_date)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                # Truncate snippet to reasonable length for a quote
                citation = m.snippet
                if len(citation) > 400:
                    citation = citation[:397] + "..."

                year_quotes.append({
                    "citation": citation,
                    "orateur": clean_name(m.orateur),
                    "fonction": clean_name(m.fonction),
                    "date": m.debat_date,
                    "chambre": chamber,
                })

        if year_quotes:
            curated_quotes[year] = year_quotes

    return curated_quotes


def enrich_rapports_parlementaires(
    article_ref: str,
    article_data: dict,
    mention_index: dict[str, list[MentionRecord]],
    session,
) -> list[dict]:
    """Generate rapports_parlementaires from debate metadata and SourceExterne."""
    rapports = []
    seen = set()

    # From mention index: find rapporteur interventions
    mentions = mention_index.get(article_ref, [])
    fonction_lower = lambda f: html_mod.unescape((f or "")).lower()
    rapporteur_mentions = [
        m for m in mentions
        if "rapporteur" in fonction_lower(m.fonction)
        and not is_president_de_seance(m.fonction)
    ]

    for m in rapporteur_mentions:
        if not m.law_year or not m.law_type:
            continue

        # Build a rapport reference
        chambre_label = "AN" if m.chambre in ("AN", "an") else "Sénat"
        key = (chambre_label, m.law_year)
        if key in seen:
            continue
        seen.add(key)

        # Find the article_in_law from the modification history
        article_in_law = ""
        for mod in article_data.get("modifications", []):
            if mod.get("law_year") == m.law_year and mod.get("law_type") == m.law_type:
                article_in_law = mod.get("article_in_law", "")
                break

        title = f"{m.law_type} {m.law_year}"
        if article_in_law:
            title += f" ({article_in_law})"

        rapports.append({
            "type": chambre_label,
            "year": m.law_year,
            "title": title,
            "rapporteur": clean_name(m.orateur),
        })

    # From SourceExterne: check for rapports linked to this article
    try:
        sources = (
            session.query(SourceExterne)
            .filter(SourceExterne.type == "rapport")
            .all()
        )
        for src in sources:
            articles_lies = src.articles_cgi_lies or []
            if article_ref not in articles_lies:
                continue
            chambre = src.chambre or "AN"
            year = src.date.year if src.date else 0
            key = (chambre, year)
            if key in seen:
                continue
            seen.add(key)
            rapports.append({
                "type": chambre,
                "year": year,
                "title": src.titre or f"Rapport {chambre} {year}",
                "rapporteur": src.auteur or "",
                "url": src.url or "",
            })
    except Exception as e:
        logger.debug(f"Could not query SourceExterne: {e}")

    # Sort by year
    rapports.sort(key=lambda r: r.get("year", 0))
    return rapports


def enrich_dila_sources(
    article_ref: str,
    article_data: dict,
    mention_index: dict[str, list[MentionRecord]],
    session,
) -> list[str]:
    """Generate dila_sources from mention_index.

    Only includes debate sessions where the CGI article was actually
    mentioned (via mention_index), not every debate for the law.
    Groups by law to build readable references.
    """
    mentions = mention_index.get(article_ref, [])
    if not mentions:
        return []

    # Build a map: law_numero -> article_in_law from modifications
    mod_map = {}
    for mod in article_data.get("modifications", []):
        num = mod.get("law_numero")
        if num:
            mod_map[num] = {
                "article_in_law": mod.get("article_in_law", ""),
                "law_type": mod.get("law_type", ""),
                "law_year": mod.get("law_year", ""),
            }

    # Collect unique (debat_date, chambre, law) tuples from mentions
    seen = set()
    dila_sources = []

    for m in mentions:
        if not m.law_numero or not m.debat_date:
            continue

        # Filter out future dates (data errors)
        if m.debat_date > "2025-12-31":
            continue

        # Deduplicate by date + chamber (one entry per session)
        dedup_key = (m.debat_date, m.chambre)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        # Build label
        chambre_short = "AN" if m.chambre in ("AN", "an") else "Sénat"
        date_compact = m.debat_date.replace("-", "")
        ref_label = f"{chambre_short}_{date_compact}"

        info = mod_map.get(m.law_numero)
        if info:
            law_ref = f"{info['law_type']} {info['law_year']}"
            if info["article_in_law"]:
                law_ref += f" - {info['article_in_law']}"
            ref_label += f" ({law_ref})"

        dila_sources.append(ref_label)

    return sorted(dila_sources)


def enrich_synthese_evolution(article_data: dict) -> list[dict]:
    """Generate synthese_evolution from modification history."""
    modifications = article_data.get("modifications", [])
    if not modifications:
        return []

    evolution = []
    for mod in modifications:
        law_type = mod.get("law_type", "")
        law_year = mod.get("law_year", "")
        nature = mod.get("nature", "Modification")
        article_in_law = mod.get("article_in_law", "")

        row = {
            "periode": str(law_year),
            "nature": nature,
            "loi": f"{law_type} {law_year}",
        }
        if article_in_law:
            row["article"] = article_in_law

        evolution.append(row)

    return evolution


def run_db_enrichment(
    articles: dict,
    mention_index: dict[str, list[MentionRecord]],
    session,
    article_filter: set[str] | None = None,
) -> dict:
    """Phase 2: Enrich all articles with DB-sourced data.

    Returns:
        Updated articles dict (deep-copied, originals preserved)
    """
    logger.info("Phase 2: DB-sourced enrichment...")
    enriched = copy.deepcopy(articles)
    stats = defaultdict(int)

    article_refs = list(enriched.keys())
    if article_filter:
        article_refs = [r for r in article_refs if r in article_filter]

    total = len(article_refs)
    for i, ref in enumerate(article_refs):
        art = enriched[ref]

        # curated_quotes: generate or clean up existing
        kws = build_relevance_keywords(ref, art)
        existing_quotes = art.get("curated_quotes")
        if not existing_quotes:
            quotes = enrich_curated_quotes(ref, art, mention_index, relevance_keywords=kws)
            if quotes:
                art["curated_quotes"] = quotes
                stats["curated_quotes"] += 1
        elif kws:
            # Clean up existing quotes: remove those that don't match any keyword
            cleaned = {}
            for year, year_quotes in existing_quotes.items():
                filtered = [
                    q for q in year_quotes
                    if any(kw in q.get("citation", "").lower() for kw in kws)
                ]
                if filtered:
                    cleaned[year] = filtered
            removed = sum(len(v) for v in existing_quotes.values()) - sum(len(v) for v in cleaned.values())
            if removed > 0:
                art["curated_quotes"] = cleaned
                stats["quotes_cleaned"] += removed

        # rapports_parlementaires: only generate if article has none
        # (preserves hand-curated rapports with URLs/numéros)
        if not art.get("rapports_parlementaires"):
            rapports = enrich_rapports_parlementaires(ref, art, mention_index, session)
            if rapports:
                art["rapports_parlementaires"] = rapports
                stats["rapports_parlementaires"] += 1

        # dila_sources: always regenerate (derived from mention_index)
        # except for manually curated articles (e.g. 150-0 B ter)
        # Old auto-generated entries start with CRI_/CRSANR/senat_
        # New auto-generated entries start with AN_/Sénat_
        # Hand-curated entries use descriptive labels (Législature...)
        existing_dila = art.get("dila_sources", [])
        auto_prefixes = ("CRI_", "CRSANR", "senat_", "AN_", "Sénat_")
        is_hand_curated = existing_dila and any(
            not s.startswith(auto_prefixes) for s in existing_dila
        )
        if not is_hand_curated:
            dila = enrich_dila_sources(ref, art, mention_index, session)
            if dila:
                art["dila_sources"] = dila
                stats["dila_sources"] += 1

        # synthese_evolution: only add if empty
        if not art.get("synthese_evolution"):
            evolution = enrich_synthese_evolution(art)
            if evolution:
                art["synthese_evolution"] = evolution
                stats["synthese_evolution"] += 1

        if (i + 1) % 500 == 0 or (i + 1) == total:
            logger.info(f"  DB enrichment: {i + 1}/{total} articles processed")

    logger.info(f"Phase 2 complete: {dict(stats)}")
    return enriched


# ============================================================
# Phase 2b: Law-debate enrichment (article-in-law search)
# ============================================================

# Regex to extract article number from "Art. 24", "Art. 5 bis", etc.
_ART_NUM_RE = re.compile(r"^Art\.?\s*", re.IGNORECASE)


def _normalize_article_num(article_in_law: str) -> str:
    """Extract article number from 'Art. 24' -> '24', 'Art. 5 bis' -> '5 bis'."""
    return _ART_NUM_RE.sub("", article_in_law).strip()


def _build_article_pattern(art_num: str) -> re.Pattern | None:
    """Build regex to find article-in-law mentions in debate text.

    Matches patterns like:
    - "article 24"
    - "l'article 24"
    - "articles 24 et 25" (captures 24)
    - "article 5 bis"

    Avoids false positives for very short numbers (1-digit) by requiring
    word boundary or specific context.
    """
    if not art_num:
        return None

    escaped = re.escape(art_num)

    # For bare single-digit numbers, require more specific context
    # to avoid matching "article 1 de la loi..." which refers to
    # article 1 of the bill, not CGI.
    if art_num.isdigit() and len(art_num) == 1:
        # Require the number to be followed by a word boundary
        # and NOT by common continuation patterns
        pattern = rf"articles?\s+{escaped}(?:\s|,|\.|\)|$)"
    else:
        pattern = rf"articles?\s+{escaped}(?:\b|[\s,.\)\-]|$)"

    return re.compile(pattern, re.IGNORECASE)


def enrich_from_law_debates(
    articles: dict,
    session,
    article_filter: set[str] | None = None,
) -> dict:
    """Phase 2b: Enrich using targeted article-in-law search in law debates.

    For each modification, searches debates of the specific law for interventions
    that mention the article-in-law number (e.g., "article 24"). This catches
    many more relevant quotes than the CGI mention index which only finds
    explicit CGI article references.

    Returns:
        Stats dict with counts of enriched fields.
    """
    logger.info("Phase 2b: Law-debate enrichment (article-in-law search)...")

    # Step 1: Build reverse index: law_numero -> [(article_ref, art_num, mod)]
    law_to_articles: dict[str, list[tuple[str, str, dict]]] = defaultdict(list)
    for ref, art in articles.items():
        if article_filter and ref not in article_filter:
            continue
        for mod in art.get("modifications", []):
            law_num = mod.get("law_numero")
            art_in_law = mod.get("article_in_law", "")
            if law_num and art_in_law:
                art_num = _normalize_article_num(art_in_law)
                if art_num:
                    law_to_articles[law_num].append((ref, art_num, mod))

    logger.info(
        f"  {len(law_to_articles)} laws to scan, "
        f"{sum(len(v) for v in law_to_articles.values())} article-law pairs"
    )

    # Build keyword sets per article for relevance validation
    article_keywords: dict[str, set[str]] = {}
    for ref, art in articles.items():
        article_keywords[ref] = build_relevance_keywords(ref, art)

    # Step 2: For each law, query debates and search interventions
    stats = defaultdict(int)
    # Collect new quotes/dila per article: ref -> {year -> [quotes]}, ref -> [dila]
    new_quotes: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    new_dila: dict[str, list[str]] = defaultdict(list)
    new_rapports: dict[str, list[dict]] = defaultdict(list)

    for law_idx, (law_numero, article_mods) in enumerate(
        sorted(law_to_articles.items())
    ):
        # Get all debate IDs for this law
        debate_rows = (
            session.query(Debat.id, Debat.date, Debat.chambre)
            .join(DossierLegislatif, Debat.dossier_id == DossierLegislatif.id)
            .join(Loi, DossierLegislatif.loi_id == Loi.id)
            .filter(Loi.numero == law_numero)
            .all()
        )

        if not debate_rows:
            continue

        debate_ids = [r.id for r in debate_rows]
        debate_meta = {r.id: (r.date, r.chambre) for r in debate_rows}

        # Build lookup: article number -> list of (ref, mod)
        # and a single combined regex for all article numbers in this law
        art_num_to_entries: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        unique_nums = set()
        for ref, art_num, mod in article_mods:
            if art_num and len(art_num) > 1:  # Skip single-char nums (too noisy)
                art_num_to_entries[art_num].append((ref, mod))
                unique_nums.add(art_num)

        if not unique_nums:
            continue

        # Build a single combined regex: article (24|5 bis|12 A|...)
        # Sort by length descending to match longer patterns first
        sorted_nums = sorted(unique_nums, key=len, reverse=True)
        combined = "|".join(re.escape(n) for n in sorted_nums)
        combined_re = re.compile(
            rf"articles?\s+({combined})(?:\b|[\s,.\)\-]|$)",
            re.IGNORECASE,
        )

        # Query interventions for this law's debates in batches
        batch_size = 500
        for batch_start in range(0, len(debate_ids), batch_size):
            batch_ids = debate_ids[batch_start:batch_start + batch_size]
            interventions = (
                session.query(
                    Intervention.id,
                    Intervention.debat_id,
                    Intervention.orateur,
                    Intervention.fonction,
                    Intervention.contenu,
                )
                .filter(Intervention.debat_id.in_(batch_ids))
                .all()
            )

            for interv in interventions:
                contenu = interv.contenu or ""
                if len(contenu) < 50:
                    continue
                if is_president_de_seance(interv.fonction or ""):
                    continue

                # Single regex scan finds all article mentions
                for match in combined_re.finditer(contenu):
                    art_num_found = match.group(1)
                    entries = art_num_to_entries.get(art_num_found, [])
                    if not entries:
                        continue

                    snippet = extract_snippet(contenu, match.start())
                    if is_procedural(snippet):
                        continue

                    debat_date_dt, chambre = debate_meta.get(
                        interv.debat_id, (None, "?")
                    )
                    debat_date = (
                        debat_date_dt.strftime("%Y-%m-%d")
                        if debat_date_dt
                        else ""
                    )
                    chambre_label = (
                        "AN" if chambre in ("AN", "an") else "Sénat"
                    )

                    citation = snippet
                    if len(citation) > 400:
                        citation = citation[:397] + "..."

                    quote = {
                        "citation": citation,
                        "orateur": clean_name(interv.orateur or ""),
                        "fonction": clean_name(interv.fonction or ""),
                        "date": debat_date,
                        "chambre": chambre_label,
                    }

                    for ref, mod in entries:
                        # Keyword relevance filter for bare numeric article numbers
                        # (e.g., "article 10", "article 17") which produce many false positives
                        if art_num_found.isdigit():
                            kws = article_keywords.get(ref, set())
                            if kws:
                                snippet_lower = citation.lower()
                                if not any(kw in snippet_lower for kw in kws):
                                    continue  # No keyword match → likely noise

                        law_year = mod.get("law_year", "")
                        year_key = str(law_year) if law_year else debat_date[:4]
                        new_quotes[ref][year_key].append(quote)

                        date_compact = debat_date.replace("-", "")
                        law_type = mod.get("law_type", "")
                        dila_label = f"{chambre_label}_{date_compact}"
                        art_in_law = mod.get("article_in_law", "")
                        if law_type and law_year:
                            dila_label += f" ({law_type} {law_year}"
                            if art_in_law:
                                dila_label += f" - {art_in_law}"
                            dila_label += ")"
                        new_dila[ref].append(dila_label)

            session.expire_all()

        # Build rapports from law metadata for each article
        loi = session.query(Loi).filter(Loi.numero == law_numero).first()
        if loi:
            for ref, art_num, mod in article_mods:
                # Check if we found any debates mentioning this article
                if ref in new_quotes and str(mod.get("law_year", "")) in new_quotes[ref]:
                    law_type = mod.get("law_type", "")
                    law_year = mod.get("law_year", 0)
                    art_in_law = mod.get("article_in_law", "")
                    title = f"{law_type} {law_year}"
                    if art_in_law:
                        title += f" ({art_in_law})"
                    new_rapports[ref].append({
                        "type": "AN/Sénat",
                        "year": law_year,
                        "title": title,
                    })

        if (law_idx + 1) % 5 == 0 or (law_idx + 1) == len(law_to_articles):
            logger.info(
                f"  Scanned {law_idx + 1}/{len(law_to_articles)} laws"
            )

    # Step 3: Merge results into articles (only fill gaps, never overwrite)
    for ref, year_quotes in new_quotes.items():
        art = articles[ref]
        existing = art.get("curated_quotes", {})
        if existing:
            # Add quotes for years not yet covered
            for year, quotes in year_quotes.items():
                if year not in existing:
                    # Dedupe by (orateur, date), pick top by speaker weight
                    deduped = _dedupe_quotes(quotes)
                    if deduped:
                        existing[year] = deduped[:6]  # Max 6 quotes per year
                        stats["quotes_years_added"] += 1
            art["curated_quotes"] = existing
        else:
            # Article has no quotes at all - add all
            curated = {}
            for year, quotes in year_quotes.items():
                deduped = _dedupe_quotes(quotes)
                if deduped:
                    curated[year] = deduped[:6]
            if curated:
                art["curated_quotes"] = curated
                stats["curated_quotes_new"] += 1

    for ref, dila_entries in new_dila.items():
        art = articles[ref]
        existing = set(art.get("dila_sources", []))
        auto_prefixes = ("CRI_", "CRSANR", "senat_", "AN_", "Sénat_")
        is_hand = existing and any(
            not s.startswith(auto_prefixes) for s in existing
        )
        if not is_hand:
            # Merge new dila entries with existing
            merged = existing | set(dila_entries)
            art["dila_sources"] = sorted(merged)
            if len(merged) > len(existing):
                stats["dila_sources_added"] += 1

    for ref, rapports in new_rapports.items():
        art = articles[ref]
        if not art.get("rapports_parlementaires"):
            # Dedupe by (type, year)
            seen = set()
            deduped = []
            for r in rapports:
                key = (r.get("type"), r.get("year"))
                if key not in seen:
                    seen.add(key)
                    deduped.append(r)
            if deduped:
                art["rapports_parlementaires"] = sorted(
                    deduped, key=lambda r: r.get("year", 0)
                )
                stats["rapports_new"] += 1

    logger.info(f"Phase 2b complete: {dict(stats)}")
    return dict(stats)


def _dedupe_quotes(quotes: list[dict]) -> list[dict]:
    """Deduplicate quotes by (orateur, date), pick best by speaker weight."""
    # Sort by speaker weight descending
    quotes.sort(
        key=lambda q: (-speaker_weight(q.get("fonction", "")), -len(q.get("citation", "")))
    )
    seen = set()
    result = []
    for q in quotes:
        key = (q.get("orateur", ""), q.get("date", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(q)
    return result


# ============================================================
# Phase 3: LLM enrichment (optional)
# ============================================================


def run_llm_enrichment(
    articles: dict,
    mention_index: dict[str, list[MentionRecord]],
    article_filter: set[str] | None = None,
    checkpoint_path: Path = CHECKPOINT_PATH,
    call_delay: float = 0.0,
    anthropic_fallback: bool = False,
    skip_jurisprudence: bool = False,
    only_jurisprudence: bool = False,
) -> dict:
    """Phase 3: Enrich with LLM (synthese + jurisprudence + forward_looking).

    Uses GroqClientPool for rate limit management with exponential backoff.

    Args:
        articles: Article data dict (already deep-copied from phase 2).
        mention_index: CGI mention index.
        article_filter: Only enrich these articles (None = all).
        checkpoint_path: Path for checkpoint persistence.
        call_delay: Seconds to wait between LLM calls.
        anthropic_fallback: Fall back to Anthropic when all Groq keys exhausted.
        skip_jurisprudence: Skip jurisprudence generation (synthese + forward-looking only).
        only_jurisprudence: Only generate jurisprudence (skip synthese + forward-looking).
    """
    from src.utils.config import get_settings
    from src.utils.groq_pool import get_groq_pool, reset_default_pool

    settings = get_settings()
    # Re-create pool with enrichment-specific settings
    reset_default_pool()
    pool = get_groq_pool(call_delay=call_delay, anthropic_fallback=anthropic_fallback)
    logger.info(
        f"Phase 3: LLM enrichment with {pool.total_keys_count} Groq API keys"
        f" (delay={call_delay}s, anthropic_fallback={anthropic_fallback},"
        f" skip_juris={skip_jurisprudence}, only_juris={only_jurisprudence})"
    )

    enriched = articles  # Already deep-copied in phase 2

    # Load checkpoint if resuming
    completed_articles: set[str] = set()
    llm_results: dict = {}  # {ref: {synthese, jurisprudence, forward_looking}}
    if checkpoint_path.exists():
        try:
            with open(checkpoint_path) as f:
                checkpoint = json.load(f)
            completed_articles = set(checkpoint.get("completed", []))
            llm_results = checkpoint.get("results", {})
            logger.info(
                f"  Resuming: {len(completed_articles)} articles done, "
                f"{len(llm_results)} results cached"
            )
            # Restore cached results into enriched articles
            for ref, fields in llm_results.items():
                if ref in enriched:
                    for field, value in fields.items():
                        if value and not enriched[ref].get(field):
                            enriched[ref][field] = value
        except Exception:
            pass

    article_refs = list(enriched.keys())
    if article_filter:
        article_refs = [r for r in article_refs if r in article_filter]

    # Skip already completed
    article_refs = [r for r in article_refs if r not in completed_articles]

    total = len(article_refs)
    stats = defaultdict(int)
    errors = 0
    checkpoint_interval = 25  # Save every 25 articles for more granular resume

    for i, ref in enumerate(article_refs):
        art = enriched[ref]
        titre = art.get("titre", "")

        # 3a: Synthese (only if empty, skip in only_jurisprudence mode)
        if not only_jurisprudence and not art.get("synthese"):
            try:
                synthese = _generate_synthese(
                    ref, titre, art, mention_index, pool, settings
                )
                if synthese:
                    art["synthese"] = synthese
                    stats["synthese"] += 1
            except Exception as e:
                logger.warning(f"  Synthese failed for {ref}: {e}")
                errors += 1

        # 3b: Jurisprudence (only if empty, skip in skip_jurisprudence mode)
        if not skip_jurisprudence and not art.get("jurisprudence"):
            try:
                juris = _generate_jurisprudence(ref, titre, pool, settings)
                if juris:
                    art["jurisprudence"] = juris
                    stats["jurisprudence"] += 1
            except Exception as e:
                logger.warning(f"  Jurisprudence failed for {ref}: {e}")
                errors += 1

        # 3c: Forward-looking (skip in only_jurisprudence mode)
        if not only_jurisprudence and not art.get("forward_looking"):
            recent_mentions = [
                m for m in mention_index.get(ref, [])
                if m.debat_date and m.debat_date >= "2025"
            ]
            if recent_mentions:
                try:
                    fl = _generate_forward_looking(
                        ref, titre, recent_mentions, pool, settings
                    )
                    if fl:
                        art["forward_looking"] = fl
                        stats["forward_looking"] += 1
                except Exception as e:
                    logger.warning(f"  Forward-looking failed for {ref}: {e}")
                    errors += 1

        # Cache LLM results for checkpoint persistence
        ref_results = {}
        if art.get("synthese"):
            ref_results["synthese"] = art["synthese"]
        if art.get("jurisprudence"):
            ref_results["jurisprudence"] = art["jurisprudence"]
        if art.get("forward_looking"):
            ref_results["forward_looking"] = art["forward_looking"]
        if ref_results:
            llm_results[ref] = ref_results

        completed_articles.add(ref)

        # Checkpoint every N articles
        if (i + 1) % checkpoint_interval == 0:
            _save_checkpoint(checkpoint_path, completed_articles, llm_results)
            _flush_results_to_index(llm_results)
            logger.info(
                f"  LLM enrichment: {i + 1}/{total} articles, "
                f"stats={dict(stats)}, errors={errors}"
            )

    # Final checkpoint
    _save_checkpoint(checkpoint_path, completed_articles, llm_results)
    _flush_results_to_index(llm_results)
    logger.info(f"Phase 3 complete: {dict(stats)}, errors={errors}")

    return enriched


def _generate_synthese(
    ref: str,
    titre: str,
    article_data: dict,
    mention_index: dict[str, list[MentionRecord]],
    pool,
    settings,
) -> dict:
    """Generate synthese using LLM."""
    from src.analysis.prompts import SYNTHESE_ENRICHMENT_PROMPT

    # Build modification history text
    mods = article_data.get("modifications", [])
    mod_lines = []
    for mod in mods:
        line = (
            f"- {mod.get('law_type', '')} {mod.get('law_year', '')} "
            f"(loi n° {mod.get('law_numero', '')}, {mod.get('article_in_law', '')}): "
            f"{mod.get('nature', 'Modification')}"
        )
        mod_lines.append(line)
    modification_history = "\n".join(mod_lines) if mod_lines else "Aucune modification répertoriée"

    # Build best quotes text
    mentions = mention_index.get(ref, [])
    substantive = [m for m in mentions if not is_procedural(m.snippet)]
    substantive.sort(key=lambda m: -speaker_weight(m.fonction))
    best = substantive[:10]

    if best:
        quote_lines = []
        for m in best:
            snippet = m.snippet[:300]
            quote_lines.append(
                f"[{m.orateur} ({m.fonction}), {m.chambre}, {m.debat_date}]\n{snippet}"
            )
        best_quotes = "\n\n---\n\n".join(quote_lines)
    else:
        best_quotes = "[Aucune citation parlementaire trouvée]"

    prompt = SYNTHESE_ENRICHMENT_PROMPT.format(
        ref=ref,
        titre=titre,
        modification_history=modification_history,
        best_quotes=best_quotes,
    )

    response = pool.chat_completion(
        model=settings.groq_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Tu es un assistant juridique expert en droit fiscal français. "
                    "Tu réponds uniquement en JSON valide."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        max_tokens=1500,
        temperature=0.2,
    )

    result = _parse_json_response(response)
    if result and result.get("objectif_constant"):
        return result
    return {}


def _generate_jurisprudence(ref: str, titre: str, pool, settings) -> list[dict]:
    """Generate jurisprudence using LLM."""
    from src.analysis.prompts import JURISPRUDENCE_ENRICHMENT_PROMPT

    prompt = JURISPRUDENCE_ENRICHMENT_PROMPT.format(ref=ref, titre=titre)

    response = pool.chat_completion(
        model=settings.groq_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Tu es un assistant juridique expert en droit fiscal français. "
                    "Tu réponds uniquement en JSON valide."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        max_tokens=2000,
        temperature=0.1,
    )

    result = _parse_json_response(response)
    if isinstance(result, list):
        # Filter out entries without required fields
        valid = [
            j for j in result
            if j.get("juridiction") and j.get("reference") and j.get("titre")
        ]
        return valid
    return []


def _generate_forward_looking(
    ref: str,
    titre: str,
    recent_mentions: list[MentionRecord],
    pool,
    settings,
) -> list[dict]:
    """Generate forward_looking using LLM for articles with recent debate mentions."""
    from src.analysis.prompts import FORWARD_LOOKING_PROMPT

    # Build debate chunks from recent mentions
    chunks_text = []
    for m in recent_mentions[:10]:
        snippet = m.snippet[:400]
        chunks_text.append(
            f"[{m.orateur} ({m.fonction}), {m.chambre}, {m.debat_date}]\n{snippet}"
        )

    debate_text = "\n\n---\n\n".join(chunks_text)

    # Determine PLF year from mentions
    years = {int(m.debat_date[:4]) for m in recent_mentions if m.debat_date}
    plf_year = max(years) + 1 if years else 2026

    prompt = FORWARD_LOOKING_PROMPT.format(
        article_ref=ref,
        article_code="CGI",
        plf_year=plf_year,
        debate_chunks=debate_text,
    )

    response = pool.chat_completion(
        model=settings.groq_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Tu es un assistant juridique expert en droit fiscal français. "
                    "Tu réponds uniquement en JSON valide."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        max_tokens=1500,
        temperature=0.2,
    )

    result = _parse_json_response(response)
    if result and result.get("propositions"):
        return [result]
    return []


def _parse_json_response(response_text: str):
    """Parse JSON from LLM response (handles markdown fences)."""
    text = response_text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON in text
        # Try array first
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        # Try object
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        logger.debug(f"Failed to parse JSON: {text[:200]}...")
        return None


def _save_checkpoint(path: Path, completed: set[str], results: dict | None = None):
    """Save enrichment checkpoint with LLM results."""
    data = {"completed": sorted(completed), "timestamp": datetime.now().isoformat()}
    if results:
        data["results"] = results
    with open(path, "w") as f:
        json.dump(data, f)


def _flush_results_to_index(results: dict, index_path: Path = INDEX_PATH):
    """Flush LLM results to article_modifications.json (non-destructive merge)."""
    if not results:
        return
    with open(index_path, encoding="utf-8") as f:
        data = json.load(f)
    updated = 0
    for ref, fields in results.items():
        if ref not in data["articles"]:
            continue
        art = data["articles"][ref]
        for field, value in fields.items():
            if value and not art.get(field):
                art[field] = value
                updated += 1
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"  Flushed {updated} fields to {index_path}")


# ============================================================
# Phase 4: Merge and save
# ============================================================


def merge_and_save(
    original_data: dict,
    enriched_articles: dict,
    output_path: Path = INDEX_PATH,
    dry_run: bool = False,
):
    """Merge enriched data into original, preserving manually curated fields.

    Rules:
    - Never overwrite non-empty curated fields
    - Only add/update fields that are currently empty
    """
    logger.info("Phase 4: Merging enriched data...")

    merged = copy.deepcopy(original_data)
    stats = defaultdict(int)

    # Fields that are always overwritten (auto-generated from mention index).
    # curated_quotes is included because Phase 2 already handles the logic:
    # - Preserves hand-curated quotes (doesn't regenerate if quotes exist)
    # - Cleans noisy auto-generated quotes via keyword filtering
    # - Only generates fresh quotes for articles that have none
    always_overwrite = {"dila_sources", "curated_quotes"}

    for ref, enriched_art in enriched_articles.items():
        original_art = merged["articles"].get(ref, {})

        for field_name in CURATED_FIELDS:
            if field_name in always_overwrite:
                # Always take the enriched version (which may have been cleaned).
                # Use `in` check (not truthiness) to handle empty dicts/lists
                # that Phase 2 explicitly set after cleaning noisy data.
                if field_name in enriched_art:
                    original_art[field_name] = enriched_art[field_name]
                    if enriched_art[field_name]:
                        stats[field_name] += 1
            else:
                # Only set if the original is empty/missing
                if not original_art.get(field_name) and enriched_art.get(field_name):
                    original_art[field_name] = enriched_art[field_name]
                    stats[field_name] += 1

        merged["articles"][ref] = original_art

    logger.info(f"Merge stats: {dict(stats)}")

    if dry_run:
        logger.info("DRY RUN: Not saving to disk")
        # Print summary
        total_with_quotes = sum(
            1 for a in merged["articles"].values() if a.get("curated_quotes")
        )
        total_with_rapports = sum(
            1 for a in merged["articles"].values() if a.get("rapports_parlementaires")
        )
        total_with_dila = sum(
            1 for a in merged["articles"].values() if a.get("dila_sources")
        )
        total_with_synthese = sum(
            1 for a in merged["articles"].values() if a.get("synthese")
        )
        total_with_juris = sum(
            1 for a in merged["articles"].values() if a.get("jurisprudence")
        )
        total_with_evolution = sum(
            1 for a in merged["articles"].values() if a.get("synthese_evolution")
        )

        total = len(merged["articles"])
        logger.info(f"\n{'='*60}")
        logger.info(f"ENRICHMENT SUMMARY ({total} articles)")
        logger.info(f"{'='*60}")
        def _pct(n):
            return f"{n:>5} ({n * 100 // total}%)"

        logger.info(f"  curated_quotes:         {_pct(total_with_quotes)}")
        logger.info(f"  rapports_parlementaires:{_pct(total_with_rapports)}")
        logger.info(f"  dila_sources:           {_pct(total_with_dila)}")
        logger.info(f"  synthese:               {_pct(total_with_synthese)}")
        logger.info(f"  jurisprudence:          {_pct(total_with_juris)}")
        logger.info(f"  synthese_evolution:     {_pct(total_with_evolution)}")
        logger.info(f"{'='*60}")
        return merged

    # Save with backup
    backup_path = output_path.with_suffix(".json.bak")
    if output_path.exists():
        import shutil
        shutil.copy2(output_path, backup_path)
        logger.info(f"  Backup saved to {backup_path}")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    logger.info(f"  Saved enriched index to {output_path}")

    # Clean up checkpoint
    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        logger.info("  Removed checkpoint file")

    return merged


# ============================================================
# Cleanup: fix mechanical issues in existing data
# ============================================================


def cleanup_article_data(data: dict, dry_run: bool = False) -> dict:
    """Apply mechanical fixes to existing article_modifications.json.

    Fixes:
    - HTML entities in orateur/fonction (e.g. pr&#233;sident → président)
    - Doubled names (e.g. "M. le président. M. le président")
    - Procedural quotes removal
    - Président de séance quotes removal (low-value)
    - Duplicate rapports_parlementaires removal
    - Président falsely listed as rapporteur
    - Future dates in dila_sources
    """
    articles = data["articles"]
    stats = {
        "quotes_html_fixed": 0,
        "quotes_name_fixed": 0,
        "quotes_procedural_removed": 0,
        "quotes_president_removed": 0,
        "quotes_years_emptied": 0,
        "rapports_name_fixed": 0,
        "rapports_president_removed": 0,
        "rapports_deduped": 0,
        "dila_future_removed": 0,
    }

    for ref, art in articles.items():
        # --- Fix curated_quotes ---
        curated_quotes = art.get("curated_quotes", {})
        if curated_quotes:
            cleaned_quotes = {}
            for year, quotes in curated_quotes.items():
                fixed_quotes = []
                for q in quotes:
                    orateur_orig = q.get("orateur", "")
                    fonction_orig = q.get("fonction", "")

                    # Clean names
                    orateur_clean = clean_name(orateur_orig)
                    fonction_clean = clean_name(fonction_orig)

                    if orateur_clean != orateur_orig:
                        stats["quotes_name_fixed"] += 1
                    if fonction_clean != fonction_orig:
                        stats["quotes_html_fixed"] += 1

                    # Filter président de séance
                    if is_president_de_seance(fonction_clean):
                        stats["quotes_president_removed"] += 1
                        continue

                    # Filter procedural
                    citation = q.get("citation", "")
                    if is_procedural(citation):
                        stats["quotes_procedural_removed"] += 1
                        continue

                    fixed_quotes.append({
                        **q,
                        "orateur": orateur_clean,
                        "fonction": fonction_clean,
                    })

                if fixed_quotes:
                    cleaned_quotes[year] = fixed_quotes
                else:
                    stats["quotes_years_emptied"] += 1

            art["curated_quotes"] = cleaned_quotes

        # --- Fix rapports_parlementaires ---
        rapports = art.get("rapports_parlementaires", [])
        if rapports:
            fixed_rapports = []
            seen_keys = set()
            for r in rapports:
                rap_name = clean_name(r.get("rapporteur", ""))
                if rap_name != r.get("rapporteur", ""):
                    stats["rapports_name_fixed"] += 1

                # Filter président listed as rapporteur
                if is_president_de_seance(rap_name):
                    stats["rapports_president_removed"] += 1
                    continue

                # Deduplicate by (type, year)
                dedup_key = (r.get("type"), r.get("year"))
                if dedup_key in seen_keys:
                    stats["rapports_deduped"] += 1
                    continue
                seen_keys.add(dedup_key)

                fixed_rapports.append({**r, "rapporteur": rap_name})

            art["rapports_parlementaires"] = fixed_rapports

        # --- Fix dila_sources ---
        dila = art.get("dila_sources", [])
        if dila:
            fixed_dila = []
            for entry in dila:
                # Filter future dates (extract YYYYMMDD from entries like "AN_20260115")
                date_match = re.search(r"_(\d{8})", entry)
                if date_match:
                    date_str = date_match.group(1)
                    if date_str > "20251231":
                        stats["dila_future_removed"] += 1
                        continue
                fixed_dila.append(entry)
            art["dila_sources"] = fixed_dila

    return stats


# ============================================================
# Phase 5: Légifrance URL enrichment
# ============================================================


def enrich_legifrance_urls(data: dict, dry_run: bool = False) -> int:
    """Generate Légifrance search URLs for all articles missing legifrance_url.

    Uses the Légifrance code search URL pattern which reliably links to
    the CGI article page. This doesn't require API calls.

    Args:
        data: The full article_modifications.json data dict (mutated in-place).
        dry_run: If True, don't save changes.

    Returns:
        Number of URLs added.
    """
    import urllib.parse

    CGI_CODE_ID = "LEGITEXT000006069577"  # Code général des impôts
    BASE_URL = "https://www.legifrance.gouv.fr/codes/id"

    articles = data["articles"]
    added = 0

    for ref, art in articles.items():
        if art.get("legifrance_url"):
            continue

        # Build search URL: encodes the article reference into a Légifrance search
        encoded_ref = urllib.parse.quote(f"article {ref}")
        url = f"{BASE_URL}/{CGI_CODE_ID}/?query={encoded_ref}"
        art["legifrance_url"] = url
        added += 1

    logger.info(f"Légifrance URLs: added {added} URLs ({len(articles) - added} already had one)")
    return added


# ============================================================
# Phase 5b: Modification-level Légifrance URL enrichment
# ============================================================


def enrich_mod_legifrance_urls(data: dict, session) -> int:
    """Populate legifrance_url on individual modifications from DB Loi records.

    For each modification missing a legifrance_url, looks up the Loi by
    law_numero and uses its url_legifrance (JORF text URL).

    Args:
        data: The full article_modifications.json data dict (mutated in-place).
        session: SQLAlchemy session.

    Returns:
        Number of modification URLs added.
    """
    # Build lookup: law_numero -> url_legifrance
    lois = session.query(Loi.numero, Loi.url_legifrance).all()
    law_url_map = {
        numero: url for numero, url in lois if url
    }
    logger.info(f"  Loaded {len(law_url_map)} law URLs from DB")

    articles = data["articles"]
    added = 0
    total_mods = 0

    for ref, art in articles.items():
        for mod in art.get("modifications", []):
            total_mods += 1
            if mod.get("legifrance_url"):
                continue  # Already has a URL (e.g. article-specific JORF URL)

            law_numero = mod.get("law_numero")
            if not law_numero:
                continue

            url = law_url_map.get(law_numero)
            if url:
                mod["legifrance_url"] = url
                added += 1

    logger.info(
        f"Modification URLs: added {added} URLs "
        f"({total_mods - added} already had one or no matching law)"
    )
    return added


# ============================================================
# Main
# ============================================================


def main():
    parser = argparse.ArgumentParser(
        description="Enrich article_modifications.json with DB and LLM data"
    )
    parser.add_argument(
        "--with-llm",
        action="store_true",
        help="Include LLM enrichment (synthese, jurisprudence, forward_looking)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume LLM enrichment from checkpoint",
    )
    parser.add_argument(
        "--articles",
        type=str,
        help="Comma-separated list of articles to enrich (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't save results, just print stats",
    )
    parser.add_argument(
        "--skip-mention-index",
        action="store_true",
        help="Skip Phase 1 (use cached mention index if available)",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Only run cleanup fixes on existing data (no DB/mention index needed)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds to wait between LLM calls (default: 0)",
    )
    parser.add_argument(
        "--anthropic-fallback",
        action="store_true",
        help="Fall back to Anthropic API when all Groq keys are exhausted",
    )
    parser.add_argument(
        "--skip-jurisprudence",
        action="store_true",
        help="Skip jurisprudence generation (synthese + forward-looking only)",
    )
    parser.add_argument(
        "--only-jurisprudence",
        action="store_true",
        help="Only generate jurisprudence (skip synthese + forward-looking)",
    )
    parser.add_argument(
        "--legifrance-urls",
        action="store_true",
        help="Generate Légifrance URLs for all articles (no DB/LLM needed)",
    )
    parser.add_argument(
        "--mod-urls",
        action="store_true",
        help="Generate Légifrance URLs on modifications from DB law URLs",
    )
    args = parser.parse_args()

    # Parse article filter
    article_filter = None
    if args.articles:
        article_filter = {a.strip() for a in args.articles.split(",")}
        logger.info(f"Filtering to {len(article_filter)} articles: {article_filter}")

    # Load current index
    logger.info(f"Loading article index from {INDEX_PATH}...")
    with open(INDEX_PATH, encoding="utf-8") as f:
        original_data = json.load(f)
    articles = original_data["articles"]
    logger.info(f"  Loaded {len(articles)} articles")

    # Cleanup-only mode: fix mechanical issues without DB/mention index
    if args.cleanup:
        logger.info("Running cleanup fixes on existing data...")
        stats = cleanup_article_data(original_data, dry_run=args.dry_run)
        logger.info(f"\n{'='*60}")
        logger.info("CLEANUP RESULTS")
        logger.info(f"{'='*60}")
        for key, value in stats.items():
            logger.info(f"  {key}: {value}")
        logger.info(f"{'='*60}")

        if not args.dry_run:
            import shutil
            backup_path = INDEX_PATH.with_suffix(".json.bak")
            shutil.copy2(INDEX_PATH, backup_path)
            logger.info(f"  Backup saved to {backup_path}")
            with open(INDEX_PATH, "w", encoding="utf-8") as f:
                json.dump(original_data, f, ensure_ascii=False, indent=2)
            logger.info(f"  Saved cleaned index to {INDEX_PATH}")
        else:
            logger.info("DRY RUN: Not saving to disk")
        logger.info("Done!")
        return

    # Légifrance URL-only mode: no DB/LLM needed
    if args.legifrance_urls:
        added = enrich_legifrance_urls(original_data, dry_run=args.dry_run)
        if not args.dry_run and added > 0:
            import shutil
            backup_path = INDEX_PATH.with_suffix(".json.bak")
            shutil.copy2(INDEX_PATH, backup_path)
            logger.info(f"  Backup saved to {backup_path}")
            with open(INDEX_PATH, "w", encoding="utf-8") as f:
                json.dump(original_data, f, ensure_ascii=False, indent=2)
            logger.info(f"  Saved index with {added} new Légifrance URLs to {INDEX_PATH}")
        elif args.dry_run:
            logger.info(f"DRY RUN: Would add {added} Légifrance URLs")
        logger.info("Done!")
        return

    # Modification-level Légifrance URL mode: needs DB
    if args.mod_urls:
        session = get_session()
        session.get_bind().echo = False
        added = enrich_mod_legifrance_urls(original_data, session)
        session.close()
        if not args.dry_run and added > 0:
            import shutil
            backup_path = INDEX_PATH.with_suffix(".json.bak")
            shutil.copy2(INDEX_PATH, backup_path)
            logger.info(f"  Backup saved to {backup_path}")
            with open(INDEX_PATH, "w", encoding="utf-8") as f:
                json.dump(original_data, f, ensure_ascii=False, indent=2)
            logger.info(f"  Saved index with {added} modification URLs to {INDEX_PATH}")
        elif args.dry_run:
            logger.info(f"DRY RUN: Would add {added} modification URLs")
        logger.info("Done!")
        return

    # Open DB session (suppress SQLAlchemy echo logging)
    session = get_session()
    session.get_bind().echo = False

    # Phase 1: Build CGI mention index
    mention_index_path = Path(__file__).parent.parent / "data" / "cgi_mention_index.json"
    if args.skip_mention_index and mention_index_path.exists():
        logger.info("Loading cached mention index...")
        with open(mention_index_path) as f:
            raw = json.load(f)
        # Reconstruct MentionRecord objects
        mention_index = {}
        for ref, records in raw.items():
            mention_index[ref] = [
                MentionRecord(**r) for r in records
            ]
        logger.info(f"  Loaded mention index for {len(mention_index)} articles")
    else:
        start = time.time()
        mention_index = build_cgi_mention_index(session)
        elapsed = time.time() - start
        logger.info(f"  Phase 1 took {elapsed:.1f}s")

        # Cache mention index for reuse
        logger.info(f"  Caching mention index to {mention_index_path}...")
        serializable = {}
        for ref, records in mention_index.items():
            serializable[ref] = [
                {
                    "intervention_id": r.intervention_id,
                    "debat_date": r.debat_date,
                    "orateur": r.orateur,
                    "fonction": r.fonction,
                    "chambre": r.chambre,
                    "snippet": r.snippet,
                    "law_numero": r.law_numero,
                    "law_year": r.law_year,
                    "law_type": r.law_type,
                }
                for r in records
            ]
        with open(mention_index_path, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False)
        size_mb = mention_index_path.stat().st_size / 1024 / 1024
        logger.info(f"  Cached mention index ({size_mb:.1f} MB)")

    # Phase 2: DB enrichment (mention-index based)
    start = time.time()
    enriched_articles = run_db_enrichment(articles, mention_index, session, article_filter)
    elapsed = time.time() - start
    logger.info(f"  Phase 2 took {elapsed:.1f}s")

    # Phase 2b: Law-debate enrichment (article-in-law search)
    start = time.time()
    enrich_from_law_debates(enriched_articles, session, article_filter)
    elapsed = time.time() - start
    logger.info(f"  Phase 2b took {elapsed:.1f}s")

    # Phase 3: LLM enrichment (optional)
    if args.with_llm:
        start = time.time()
        enriched_articles = run_llm_enrichment(
            enriched_articles,
            mention_index,
            article_filter,
            call_delay=args.delay,
            anthropic_fallback=args.anthropic_fallback,
            skip_jurisprudence=args.skip_jurisprudence,
            only_jurisprudence=args.only_jurisprudence,
        )
        elapsed = time.time() - start
        logger.info(f"  Phase 3 took {elapsed:.1f}s")

    # Phase 4: Merge and save
    merge_and_save(original_data, enriched_articles, INDEX_PATH, dry_run=args.dry_run)

    session.close()
    logger.info("Done!")


if __name__ == "__main__":
    main()
