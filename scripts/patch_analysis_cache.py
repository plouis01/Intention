#!/usr/bin/env python3
"""
Patch an existing analysis_cache entry by re-running search + LLM for empty-intention
sections, and computing cross_references / plf_lf_deltas / operational_params.

Only re-runs the LLM for sections that have empty intention — does NOT re-run the full analysis.

Usage:
    python scripts/patch_analysis_cache.py "39"
    python scripts/patch_analysis_cache.py "39" --dry-run
"""

import argparse
import json
import logging
import sys
from datetime import datetime, UTC
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.analysis.article_analyzer import ArticleAnalyzer
from src.analysis.article_index import get_article_index
from src.analysis.cross_law_analyzer import CrossLawAnalyzer
from src.api.cgi_routes import (
    _serialize_section,
    _serialize_quoted_source,
    get_article_index as get_api_article_index,
)
from src.db.models import AnalysisCache, get_session
from src.indexing.search import SearchService
from src.utils.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def patch_analysis(article_ref: str, dry_run: bool = False) -> bool:
    """Patch empty sections and compute missing fields for an analysis_cache entry."""

    # Load current cache entry
    with get_session() as session:
        cache = (
            session.query(AnalysisCache)
            .filter(AnalysisCache.cgi_article == article_ref)
            .first()
        )
        if not cache:
            print(f"ERROR: No cache entry for article '{article_ref}'")
            return False
        analysis = cache.analysis_json

    sections = analysis.get("sections", [])
    empty_indices = [i for i, s in enumerate(sections) if not s.get("intention", "").strip()]
    print(f"Found {len(empty_indices)}/{len(sections)} sections with empty intention")

    if not empty_indices and analysis.get("cross_references"):
        print("Nothing to patch.")
        return True

    # Initialize analyzer
    print("Loading search service...")
    search_service = SearchService.create_configured()
    analyzer = ArticleAnalyzer(search_service=search_service)

    index = get_article_index()
    article_info = index.get_article(article_ref)
    if not article_info:
        print(f"ERROR: Article '{article_ref}' not found in index")
        return False

    modifications = article_info.modifications
    # Build a lookup from (law_numero, nature) -> modification
    from itertools import groupby
    sorted_mods = sorted(modifications, key=lambda m: m.law_numero)
    grouped_mods = []
    for _key, group in groupby(sorted_mods, key=lambda m: m.law_numero):
        grouped_mods.append(list(group))

    total_interventions = 0
    used_curated_citations: set[str] = set()
    all_sections_obj = []  # SectionAnalysis objects for cross-ref / plf-lf delta

    print(f"\n--- Re-analyzing {len(empty_indices)} empty sections ---")
    for section_idx in range(len(sections)):
        s = sections[section_idx]
        mod_data = s["modification"]

        # Find matching modification from index
        matching_mod = None
        for mod in modifications:
            if (mod.law_numero == mod_data.get("law_numero") and
                    mod.nature.value == mod_data.get("nature")):
                matching_mod = mod
                break

        if not matching_mod:
            # Try by law_ref
            for mod in modifications:
                if mod.law_ref == mod_data.get("law_ref"):
                    matching_mod = mod
                    break

        if section_idx in empty_indices and matching_mod:
            print(f"  Re-analyzing section {section_idx}: {mod_data.get('law_ref')}...")
            try:
                section_obj, debates_count, interventions_count = analyzer._analyze_modification(
                    article_info, matching_mod
                )
                section_obj = analyzer._augment_with_curated_quotes(
                    article_info.reference, matching_mod, section_obj,
                    article_keywords=article_info.keywords,
                    used_curated_citations=used_curated_citations,
                )
                total_interventions += interventions_count

                # Serialize back to dict
                new_section = _serialize_section(section_obj, section_idx)
                # Preserve original fields that might be richer
                new_section["modification"] = s["modification"]  # keep original mod metadata
                sections[section_idx] = new_section
                all_sections_obj.append(section_obj)

                if new_section.get("intention", "").strip():
                    print(f"    -> Filled intention ({len(new_section['intention'])} chars)")
                else:
                    print(f"    -> Still empty (no relevant debates found)")
            except Exception as e:
                logger.error(f"    -> Error: {e}")
                all_sections_obj.append(None)
        else:
            # Keep existing section, but try to reconstruct SectionAnalysis for cross-refs
            all_sections_obj.append(None)

    # Compute cross_references (no LLM needed)
    print("\nComputing cross-references...")
    cross_references = []
    try:
        cross_analyzer = CrossLawAnalyzer()
        sections_by_law = {}
        debate_chunks_by_law = {}

        for section_obj in all_sections_obj:
            if section_obj is None:
                continue
            mod = section_obj.modification
            law_key = f"{mod.law_type}_{mod.law_year}"
            sections_by_law[law_key] = {
                "law_type": mod.law_type,
                "law_year": mod.law_year,
                "law_numero": mod.law_numero,
            }
            # Re-search for chunks
            from datetime import timedelta
            if mod.law_type in ("LF", "LFSS"):
                ds = mod.date - timedelta(days=450)
            else:
                ds = mod.date - timedelta(days=180)
            de = mod.date + timedelta(days=30)
            try:
                sr = search_service.search_article_modification(
                    article_ref=article_info.reference,
                    article_keywords=article_info.keywords,
                    law_type=mod.law_type,
                    law_year=mod.law_year,
                    date_start=ds, date_end=de,
                    top_k=10, min_relevant=1,
                    article_in_law=mod.article_in_law,
                )
                debate_chunks_by_law[law_key] = sr.get("debates", [])
            except Exception:
                pass

        if sections_by_law and debate_chunks_by_law:
            raw_refs = cross_analyzer.detect_cross_references(sections_by_law, debate_chunks_by_law)
            cross_references = [
                {
                    "source_law": f"{cr.source_law_type} {cr.source_law_year}",
                    "target_law": cr.target_law_ref,
                    "citation": cr.citation,
                }
                for cr in raw_refs
            ]
            print(f"  Found {len(cross_references)} cross-references")
    except Exception as e:
        logger.warning(f"  Cross-reference detection failed: {e}")

    # Compute plf_lf_deltas (no LLM needed)
    print("Computing PLF-LF deltas...")
    # Need SectionAnalysis objects for all sections — use existing ones where available
    # For sections we didn't re-analyze, we can't compute deltas (need SectionAnalysis objects)
    # But the method only needs section.intention and section.modification.law_year
    plf_lf_deltas = []
    try:
        # Build minimal SectionAnalysis-like objects from the JSON sections
        from dataclasses import dataclass, field
        from src.analysis.models import SectionAnalysis, LawModification, ModificationNature, NiveauCertitude

        proxy_sections = []
        for s_data in sections:
            mod_d = s_data["modification"]
            try:
                mod = LawModification(
                    date=datetime.fromisoformat(mod_d["date"]).date() if mod_d.get("date") else None,
                    law_numero=mod_d.get("law_numero", ""),
                    law_type=mod_d.get("law_type", ""),
                    law_year=mod_d.get("law_year", 0),
                    article_in_law=mod_d.get("article_in_law", ""),
                    nature=ModificationNature(mod_d.get("nature", "Modification")),
                )
                proxy_section = SectionAnalysis(
                    modification=mod,
                    contexte=s_data.get("contexte", ""),
                    intention=s_data.get("intention", ""),
                    sources_an=[],
                    sources_senat=[],
                    evolution_debats="",
                    points_vigilance=[],
                    niveau_certitude=NiveauCertitude(s_data.get("niveau_certitude", "moyen")),
                )
                proxy_sections.append(proxy_section)
            except Exception:
                pass

        if proxy_sections:
            plf_lf_deltas = analyzer._compute_plf_lf_delta(article_info, proxy_sections)
            print(f"  Found {len(plf_lf_deltas)} PLF-LF deltas")
    except Exception as e:
        logger.warning(f"  PLF-LF delta computation failed: {e}")

    # Update the analysis dict
    analysis["sections"] = sections
    analysis["cross_references"] = cross_references
    analysis["plf_lf_deltas"] = plf_lf_deltas

    # Update metadata
    total_debates = sum(s.get("debates_analyzed", 0) for s in sections)
    analysis["metadata"]["total_debates_searched"] = total_debates
    analysis["metadata"]["total_interventions_found"] = total_interventions
    analysis["metadata"]["generated_date"] = datetime.now(UTC).strftime("%Y-%m-%d")

    # Count improvements
    still_empty = sum(1 for s in sections if not s.get("intention", "").strip())
    filled = len(empty_indices) - still_empty
    print(f"\n--- Results ---")
    print(f"  Intentions filled: {filled}/{len(empty_indices)}")
    print(f"  Still empty: {still_empty}/{len(sections)}")
    print(f"  Cross-references: {len(cross_references)}")
    print(f"  PLF-LF deltas: {len(plf_lf_deltas)}")

    if dry_run:
        print("\n[DRY RUN] Not saving to DB.")
        return True

    # Save to DB
    print("\nSaving to DB...")
    with get_session() as session:
        cache = (
            session.query(AnalysisCache)
            .filter(AnalysisCache.cgi_article == article_ref)
            .first()
        )
        if cache:
            cache.analysis_json = analysis
            cache.created_at = datetime.now(UTC).replace(tzinfo=None)

            # Recompute quality score
            certitude_scores = {"élevé": 1.0, "moyen": 0.5, "faible": 0.0}
            scores = [certitude_scores.get(s.get("niveau_certitude", "moyen"), 0.5) for s in sections]
            total_sources = sum(len(s.get("sources_an", [])) + len(s.get("sources_senat", [])) for s in sections)
            cache.quality_score = int((sum(scores) / len(scores)) * 100) if scores else 0
            cache.sources_count = total_sources
            min_cert = min(scores) if scores else 0.5
            cache.confidence_level = {1.0: "élevé", 0.5: "moyen", 0.0: "faible"}.get(min_cert, "moyen")

            session.commit()
            print("Done!")
        else:
            print("ERROR: Cache entry disappeared")
            return False

    return True


def main():
    parser = argparse.ArgumentParser(description="Patch analysis cache with missing data")
    parser.add_argument("article", help="CGI article reference")
    parser.add_argument("--dry-run", action="store_true", help="Don't save to DB")
    args = parser.parse_args()

    success = patch_analysis(args.article, dry_run=args.dry_run)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
