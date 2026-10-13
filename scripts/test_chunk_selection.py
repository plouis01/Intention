"""Test chunk selection with different strategies.

Runs the search pipeline for each modification of 150-0 B ter,
applies custom post-filters, and outputs quality metrics.

Strategies tested:
  1. Baseline (current: min_kw=1, no dedup)
  2. Stem dedup + specific keyword requirement
  3. Context expansion: include chunks adjacent to explicit_ref chunks
  4. Speaker authority boost: prefer rapporteur/ministre over "autre"
  5. Combined best strategy
"""

import sys
import re
from datetime import timedelta, date
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analysis.article_index import get_article_index
from src.indexing.search import SearchService, _keyword_matches
from src.indexing.chunking import Chunk


def _stem(word: str) -> str:
    """Simple French stemming."""
    return word.lower().rstrip('s').rstrip('e').rstrip('é')


def _dedup_keywords_by_stem(kw_matches: list[str]) -> list[str]:
    """Deduplicate keywords by stem."""
    seen = set()
    unique = []
    for kw in kw_matches:
        stem = _stem(kw.split()[0]) if kw else kw
        if stem not in seen:
            seen.add(stem)
            unique.append(kw)
    return unique


GENERIC_KEYWORDS = {
    "plus-value", "plus-values", "plus-value réalisée", "plus-value en report",
    "impôt sur les sociétés", "impôt sur le revenu",
    "valeurs mobilières, droits sociaux et titres assimilés",
}

AUTHORITY_ROLES = {"rapporteur", "rapporteur général", "ministre", "secrétaire d'État",
                   "ministre délégué", "président de la commission des finances"}


def classify_chunk(chunk, score, article_ref, keywords, min_kw=2):
    """Classify a chunk as explicit_ref, keyword_match, adjacent, or noise."""
    content_lower = chunk.content.lower()
    ref_lower = article_ref.lower()
    ref_variants = [ref_lower, ref_lower.replace("-", " ")]

    if any(v in content_lower for v in ref_variants):
        return "explicit_ref"

    kw_matches = [kw for kw in keywords if _keyword_matches(kw, content_lower)]
    unique = _dedup_keywords_by_stem(kw_matches)
    specific = [kw for kw in unique if kw not in GENERIC_KEYWORDS]

    if len(unique) >= min_kw and len(specific) >= 1:
        return "keyword_match"

    return "noise"


def strategy_baseline(debate_chunks, article_ref, keywords):
    """Strategy 1: Current behavior (min_kw=1, no dedup)."""
    kept = []
    for chunk, score in debate_chunks:
        content_lower = chunk.content.lower()
        ref_lower = article_ref.lower()
        if ref_lower in content_lower or ref_lower.replace("-", " ") in content_lower:
            kept.append((chunk, score, "explicit_ref"))
            continue
        kw_matches = [kw for kw in keywords if _keyword_matches(kw, content_lower)]
        if len(kw_matches) >= 1:
            kept.append((chunk, score, f"kw({len(kw_matches)})"))
        # else: dropped
    return kept


def strategy_stem_specific(debate_chunks, article_ref, keywords):
    """Strategy 2: Stem dedup + require 1 specific keyword, min_kw=2."""
    kept = []
    for chunk, score in debate_chunks:
        cat = classify_chunk(chunk, score, article_ref, keywords, min_kw=2)
        if cat != "noise":
            kept.append((chunk, score, cat))
    return kept


def strategy_context_expansion(debate_chunks, article_ref, keywords, db_session):
    """Strategy 3: Start from explicit_ref chunks, expand to adjacent interventions."""
    from src.db.models import Intervention, Debat
    from sqlalchemy import func

    # Phase 1: Find explicit_ref chunks
    explicit_chunks = []
    explicit_debate_positions = []  # (debat_id, ordre)

    for chunk, score in debate_chunks:
        content_lower = chunk.content.lower()
        ref_lower = article_ref.lower()
        if ref_lower in content_lower or ref_lower.replace("-", " ") in content_lower:
            explicit_chunks.append((chunk, score, "explicit_ref"))
            # Extract debate_id and approximate position
            debat_id = chunk.source_id
            # Try to find the intervention ordre from content match
            explicit_debate_positions.append(debat_id)

    # Phase 2: For each debate that has an explicit_ref, find ALL interventions
    # in that debate that mention article keywords (relaxed: 1 specific keyword)
    debate_ids_with_ref = set(explicit_debate_positions)

    keyword_chunks = []
    for chunk, score in debate_chunks:
        if chunk.source_id in debate_ids_with_ref:
            cat = classify_chunk(chunk, score, article_ref, keywords, min_kw=1)
            if cat == "keyword_match":
                keyword_chunks.append((chunk, score, "adjacent_keyword"))
            elif cat == "noise":
                # Even noise chunks in a debate with explicit_ref get a chance
                # if they have at least 1 keyword (any, even generic)
                content_lower = chunk.content.lower()
                kw_matches = [kw for kw in keywords if _keyword_matches(kw, content_lower)]
                if len(kw_matches) >= 1:
                    keyword_chunks.append((chunk, score * 0.9, "adjacent_generic"))

    # Phase 3: Also include keyword matches from other debates (strict: 2 specific)
    for chunk, score in debate_chunks:
        if chunk.source_id not in debate_ids_with_ref:
            cat = classify_chunk(chunk, score, article_ref, keywords, min_kw=2)
            if cat != "noise":
                keyword_chunks.append((chunk, score, cat))

    # Dedup by content prefix
    seen = set()
    result = []
    for chunk, score, reason in explicit_chunks + keyword_chunks:
        key = chunk.content[:80]
        if key not in seen:
            seen.add(key)
            result.append((chunk, score, reason))

    return result


def strategy_authority_boost(debate_chunks, article_ref, keywords):
    """Strategy 4: Boost authoritative speakers (rapporteur, ministre)."""
    kept = []
    for chunk, score in debate_chunks:
        cat = classify_chunk(chunk, score, article_ref, keywords, min_kw=2)
        if cat != "noise":
            kept.append((chunk, score, cat))
            continue

        # For noise chunks: rescue if speaker is authoritative AND has 1+ keyword
        fonction = (chunk.metadata.get("fonction") or "").lower()
        is_authority = any(role in fonction for role in AUTHORITY_ROLES)
        if is_authority:
            content_lower = chunk.content.lower()
            kw_matches = [kw for kw in keywords if _keyword_matches(kw, content_lower)]
            specific = [kw for kw in _dedup_keywords_by_stem(kw_matches) if kw not in GENERIC_KEYWORDS]
            if len(specific) >= 1:
                kept.append((chunk, score, f"authority({fonction[:20]})"))

    return kept


def strategy_combined(debate_chunks, article_ref, keywords):
    """Strategy 5: Context expansion + authority boost + stem dedup."""
    # Phase 1: Find explicit_ref chunks and their debates
    explicit_debate_ids = set()
    explicit_chunks = []

    for chunk, score in debate_chunks:
        content_lower = chunk.content.lower()
        ref_lower = article_ref.lower()
        if ref_lower in content_lower or ref_lower.replace("-", " ") in content_lower:
            explicit_chunks.append((chunk, score, "explicit_ref"))
            explicit_debate_ids.add(chunk.source_id)

    # Phase 2: From debates with explicit_ref, include chunks with relaxed criteria
    adjacent_chunks = []
    for chunk, score in debate_chunks:
        if chunk.source_id in explicit_debate_ids:
            if classify_chunk(chunk, score, article_ref, keywords, min_kw=1) != "noise":
                adjacent_chunks.append((chunk, score, "same_debate"))
                continue
            # Authority rescue in same debate
            fonction = (chunk.metadata.get("fonction") or "").lower()
            if any(r in fonction for r in AUTHORITY_ROLES):
                content_lower = chunk.content.lower()
                kw = [k for k in keywords if _keyword_matches(k, content_lower)]
                if len(kw) >= 1:
                    adjacent_chunks.append((chunk, score * 0.95, "authority_adjacent"))

    # Phase 3: From OTHER debates, strict criteria (2 specific keywords)
    other_chunks = []
    for chunk, score in debate_chunks:
        if chunk.source_id not in explicit_debate_ids:
            cat = classify_chunk(chunk, score, article_ref, keywords, min_kw=2)
            if cat != "noise":
                other_chunks.append((chunk, score, cat))
            else:
                # Authority rescue with 1 specific keyword
                fonction = (chunk.metadata.get("fonction") or "").lower()
                if any(r in fonction for r in AUTHORITY_ROLES):
                    content_lower = chunk.content.lower()
                    kw = [k for k in keywords if _keyword_matches(k, content_lower)]
                    specific = [k for k in _dedup_keywords_by_stem(kw) if k not in GENERIC_KEYWORDS]
                    if len(specific) >= 1:
                        other_chunks.append((chunk, score * 0.9, f"authority_other"))

    # Dedup
    seen = set()
    result = []
    for chunk, score, reason in explicit_chunks + adjacent_chunks + other_chunks:
        key = chunk.content[:80]
        if key not in seen:
            seen.add(key)
            result.append((chunk, score, reason))

    return result


PROCEDURAL_PATTERNS = [
    "la parole est à", "je mets aux voix", "il en est ainsi décidé",
    "la séance est levée", "la séance est suspendue", "la séance est reprise",
    "rappel au règlement", "prochaine séance", "l'ordre du jour appelle",
    "nous reprenons la discussion", "nous abordons l'examen", "nous passons à l'examen",
    "l'amendement est adopté", "l'amendement est rejeté", "l'amendement n'est pas adopté",
    "en conséquence, un article additionnel", "je mets aux voix l'article",
]


def _is_procedural_chunk(content: str) -> bool:
    """Check if a chunk is primarily procedural content."""
    content_lower = re.sub(r'\s+', ' ', content.lower())
    # Count procedural sentences
    sentences = [s.strip() for s in content.split('.') if len(s.strip()) > 10]
    if not sentences:
        return False
    proc_count = sum(1 for s in sentences if any(p in s.lower() for p in PROCEDURAL_PATTERNS))
    return proc_count / len(sentences) > 0.5


def _has_article_in_mentions(chunk, article_ref: str) -> bool | None:
    """Check articles_mentionnes metadata. Returns True/False/None (no data)."""
    mentions = chunk.metadata.get("articles_mentionnes")
    if not mentions:
        return None  # No data - can't filter
    ref_lower = article_ref.lower().replace("-", " ").replace("  ", " ").strip()
    ref_variants = {ref_lower, ref_lower.replace(" ", ""), article_ref.lower()}
    for m in mentions:
        m_norm = m.lower().replace("-", " ").replace("  ", " ").strip()
        if m_norm in ref_variants or any(v in m_norm for v in ref_variants):
            return True
    return False


def _cites_different_article(content: str, target_ref: str) -> bool:
    """Check if content cites a DIFFERENT CGI article (not the target)."""
    _article_re = re.compile(
        r"article\s+(\d+[\w\s\-]*(?:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies)?)",
        re.IGNORECASE,
    )
    found = _article_re.findall(content)
    if not found:
        return False
    target_norm = target_ref.lower().replace(" ", "").replace("-", "")
    has_other = False
    has_target = False
    for a in found:
        a_norm = a.strip().lower().replace(" ", "").replace("-", "")
        if len(a_norm) > 2:
            if target_norm in a_norm or a_norm in target_norm:
                has_target = True
            else:
                has_other = True
    # Only reject if it cites another article WITHOUT citing the target
    return has_other and not has_target


def strategy_enhanced_combined(debate_chunks, article_ref, keywords):
    """Strategy 6: Combined + articles_mentionnes + exclude other CGI + procedural filter."""

    # === PHASE 0: Pre-filter - remove procedural chunks and chunks citing other articles ===
    filtered_chunks = []
    for chunk, score in debate_chunks:
        # Skip procedural chunks
        if _is_procedural_chunk(chunk.content):
            continue
        # Skip chunks that cite a DIFFERENT CGI article without citing the target
        if _cites_different_article(chunk.content, article_ref):
            continue
        filtered_chunks.append((chunk, score))

    # === PHASE 1: Find explicit_ref chunks (text match + articles_mentionnes) ===
    explicit_debate_ids = set()
    explicit_chunks = []

    for chunk, score in filtered_chunks:
        content_lower = chunk.content.lower()
        ref_lower = article_ref.lower()

        # Text-based explicit ref
        is_explicit = ref_lower in content_lower or ref_lower.replace("-", " ") in content_lower

        # Metadata-based explicit ref (articles_mentionnes)
        mentions_check = _has_article_in_mentions(chunk, article_ref)
        if mentions_check is True:
            is_explicit = True

        if is_explicit:
            explicit_chunks.append((chunk, score, "explicit_ref"))
            explicit_debate_ids.add(chunk.source_id)

    # === PHASE 2: Same-debate chunks (relaxed criteria for debates with explicit_ref) ===
    # NOTE: No _cites_different_article filter here - context from same debate is trusted
    adjacent_chunks = []
    for chunk, score in debate_chunks:  # Use original chunks, not filtered
        if chunk.source_id in explicit_debate_ids:
            # Skip procedural, but don't filter by other-article
            if _is_procedural_chunk(chunk.content):
                continue
            # Already explicit? Skip (handled in phase 1)
            if any(chunk.content[:80] == ec[0].content[:80] for ec in explicit_chunks):
                continue
            # Relaxed: 1 keyword match (any, including generic)
            cat = classify_chunk(chunk, score, article_ref, keywords, min_kw=1)
            if cat != "noise":
                adjacent_chunks.append((chunk, score, "same_debate"))
                continue
            # Authority rescue in same debate: any keyword
            fonction = (chunk.metadata.get("fonction") or "").lower()
            if any(r in fonction for r in AUTHORITY_ROLES):
                content_lower = chunk.content.lower()
                kw = [k for k in keywords if _keyword_matches(k, content_lower)]
                if len(kw) >= 1:
                    adjacent_chunks.append((chunk, score * 0.95, "authority_adjacent"))

    # === PHASE 3: Other debates (strict: 2 unique keywords, 1 specific) ===
    other_chunks = []
    for chunk, score in filtered_chunks:
        if chunk.source_id in explicit_debate_ids:
            continue
        # Strict filter
        cat = classify_chunk(chunk, score, article_ref, keywords, min_kw=2)
        if cat != "noise":
            other_chunks.append((chunk, score, cat))
        else:
            # Authority rescue with 1 specific keyword
            fonction = (chunk.metadata.get("fonction") or "").lower()
            if any(r in fonction for r in AUTHORITY_ROLES):
                content_lower = chunk.content.lower()
                kw = [k for k in keywords if _keyword_matches(k, content_lower)]
                specific = [k for k in _dedup_keywords_by_stem(kw) if k not in GENERIC_KEYWORDS]
                if len(specific) >= 1:
                    other_chunks.append((chunk, score * 0.9, "authority_other"))

    # === Dedup and return ===
    seen = set()
    result = []
    for chunk, score, reason in explicit_chunks + adjacent_chunks + other_chunks:
        key = chunk.content[:80]
        if key not in seen:
            seen.add(key)
            result.append((chunk, score, reason))

    return result


def run_all_strategies(article_ref="150-0 B ter"):
    index = get_article_index()
    article_info = index.get_article(article_ref)
    search_service = SearchService.create_configured()

    from src.analysis.article_analyzer import ArticleAnalyzer
    analyzer = ArticleAnalyzer(search_service=search_service)
    all_mod_dates = [(m.date, m.law_type) for m in article_info.modifications]

    skip_types = {"DECRET", "ORDONNANCE", "ARRETE", "LOI_ORGANIQUE", "INCONNU"}
    skip_natures = {"Codification", "Transfert"}
    mods = [m for m in article_info.modifications
            if m.law_type not in skip_types and m.nature.value not in skip_natures]

    strategies = {
        "1_baseline": strategy_baseline,
        "5_combined": strategy_combined,
        "6_enhanced": strategy_enhanced_combined,
    }

    # Collect raw chunks for each section
    all_results = {}
    for i, mod in enumerate(mods):
        anchor = mod.date
        if mod.date_debut_vigueur:
            try:
                anchor = date.fromisoformat(mod.date_debut_vigueur)
            except ValueError:
                pass

        law_refs = analyzer._get_debate_refs_for_law(mod.law_numero)
        results = search_service.search_article_modification(
            article_ref=article_info.reference,
            article_keywords=article_info.keywords,
            law_type=mod.law_type, law_year=mod.law_year,
            date_start=anchor - timedelta(days=450),
            date_end=anchor + timedelta(days=30),
            top_k=20, min_relevant=5,
            article_in_law=mod.article_in_law,
            all_modification_dates=all_mod_dates,
            current_mod_date=mod.date,
            law_debate_refs=law_refs,
            law_numero=mod.law_numero,
            modification_objet=mod.objet or "",
        )
        all_results[i] = (mod, results.get("debates", []))

    # Run each strategy on each section
    print(f"\n{'='*90}")
    print(f"STRATEGY COMPARISON - {article_ref}")
    print(f"{'='*90}")

    summary = {name: {"kept": 0, "explicit": 0, "sections": {}} for name in strategies}

    for i, (mod, debate_chunks) in all_results.items():
        label = f"S{i} {mod.law_type} {mod.law_year}"
        print(f"\n--- {label} ({len(debate_chunks)} raw chunks) ---")

        for name, fn in strategies.items():
            kept = fn(debate_chunks, article_info.reference, article_info.keywords)
            n_explicit = sum(1 for _, _, r in kept if r == "explicit_ref")
            n_kept = len(kept)

            reasons = defaultdict(int)
            for _, _, r in kept:
                reasons[r] += 1
            reason_str = ", ".join(f"{r}={c}" for r, c in sorted(reasons.items()))

            summary[name]["kept"] += n_kept
            summary[name]["explicit"] += n_explicit
            summary[name]["sections"][i] = (n_kept, n_explicit, reason_str)

            print(f"  {name:20s}: {n_kept:2d} kept ({reason_str})")

    # Final summary
    print(f"\n{'='*90}")
    print(f"FINAL SUMMARY")
    print(f"{'='*90}")
    print(f"{'Strategy':<22s} {'Total kept':>12s} {'Explicit ref':>14s} {'Avg/section':>13s}")
    print("-" * 65)
    for name in strategies:
        s = summary[name]
        n_sections = len(s["sections"])
        avg = s["kept"] / n_sections if n_sections else 0
        print(f"{name:<22s} {s['kept']:>12d} {s['explicit']:>14d} {avg:>13.1f}")

    # Per-section detail
    print(f"\n{'Section':<25s}", end="")
    for name in strategies:
        print(f" {name[:12]:>12s}", end="")
    print()
    print("-" * (25 + 13 * len(strategies)))
    for i, (mod, _) in all_results.items():
        label = f"S{i} {mod.law_type} {mod.law_year}"
        print(f"{label:<25s}", end="")
        for name in strategies:
            kept, explicit, _ = summary[name]["sections"].get(i, (0, 0, ""))
            print(f" {kept:>5d}({explicit:>2d}e)", end="")
        print()


if __name__ == "__main__":
    run_all_strategies()
