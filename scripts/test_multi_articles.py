"""Multi-article test harness for chunk selection quality.

Tests the enhanced chunk filter across diverse articles to detect overfitting.
"""

import sys
import re
from datetime import timedelta, date
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analysis.article_index import get_article_index
from src.indexing.search import SearchService
from src.analysis.article_analyzer import ArticleAnalyzer

TEST_ARTICLES = [
    "150-0 B ter",      # Reference article (10 mods, apport-cession)
    "199 terdecies-0 A", # Heavy (59 mods, IR-PME Madelin)
    "39",                # Very heavy (96 mods, BIC, short ref)
    "787 B",             # Moderate (16 mods, Pacte Dutreil)
    "885-0 V bis",       # Abrogé (36 mods, ISF-PME)
    "790 G",             # Simple-moderate (8 mods, donations)
    "200 A",             # Moderate (26 mods, PFU/flat tax)
]


def run_test():
    index = get_article_index()
    search_service = SearchService.create_configured()
    analyzer = ArticleAnalyzer(search_service=search_service)

    skip_types = {"DECRET", "ORDONNANCE", "ARRETE", "LOI_ORGANIQUE", "INCONNU"}
    skip_natures = {"Codification", "Transfert"}

    for article_ref in TEST_ARTICLES:
        article_info = index.get_article(article_ref)
        if not article_info:
            print(f"\n{'='*60}")
            print(f"ARTICLE {article_ref}: NOT FOUND")
            continue

        mods = [m for m in article_info.modifications
                if m.law_type not in skip_types and m.nature.value not in skip_natures]

        # Limit to first 5 mods for speed
        test_mods = mods[:5]
        all_mod_dates = [(m.date, m.law_type) for m in article_info.modifications]

        print(f"\n{'='*70}")
        print(f"ARTICLE {article_ref} - {len(mods)} mods analysables, {len(article_info.keywords)} keywords")
        print(f"État: {article_info.etat_actuel} | Testing first {len(test_mods)} mods")
        print(f"{'='*70}")

        total_raw = 0
        total_filtered = 0
        total_explicit = 0
        sections_with_0_chunks = 0

        for i, mod in enumerate(test_mods):
            anchor = mod.date
            if mod.date_debut_vigueur:
                try:
                    anchor = date.fromisoformat(mod.date_debut_vigueur)
                except ValueError:
                    pass

            law_refs = analyzer._get_debate_refs_for_law(mod.law_numero)

            try:
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
            except Exception as e:
                print(f"  S{i} {mod.law_type} {mod.law_year}: SEARCH ERROR - {e}")
                continue

            raw_debates = results.get("debates", [])
            raw_amends = results.get("amendments", [])

            # Apply enhanced filter
            filtered_debates = analyzer._filter_chunks_by_article_relevance(
                raw_debates, article_info.reference, article_info.keywords
            )
            filtered_amends = analyzer._filter_amendment_chunks(
                raw_amends, article_info.reference, article_info.keywords, mod.date
            )

            n_raw = len(raw_debates) + len(raw_amends)
            n_filtered = len(filtered_debates) + len(filtered_amends)
            n_explicit = sum(1 for c, _ in filtered_debates
                           if article_ref.lower() in c.content.lower()
                           or article_ref.lower().replace("-", " ") in c.content.lower())

            total_raw += n_raw
            total_filtered += n_filtered
            total_explicit += n_explicit
            if n_filtered == 0:
                sections_with_0_chunks += 1

            ratio = n_filtered / n_raw * 100 if n_raw > 0 else 0
            print(f"  S{i} {mod.law_type} {mod.law_year}: {n_raw:2d} raw → {n_filtered:2d} filtered ({ratio:.0f}%) | {n_explicit} explicit_ref")

        # Summary for this article
        overall_ratio = total_filtered / total_raw * 100 if total_raw > 0 else 0
        print(f"\n  SUMMARY: {total_raw} raw → {total_filtered} filtered ({overall_ratio:.0f}%)")
        print(f"  Explicit refs: {total_explicit} | Empty sections: {sections_with_0_chunks}/{len(test_mods)}")

        if sections_with_0_chunks > len(test_mods) / 2:
            print(f"  ⚠️ WARNING: >50% empty sections - filter may be too strict for this article")
        if overall_ratio > 80:
            print(f"  ⚠️ WARNING: >80% pass rate - filter may be too permissive")
        if total_explicit == 0 and total_filtered > 0:
            print(f"  ⚠️ WARNING: 0 explicit refs - all chunks matched by keywords only")


if __name__ == "__main__":
    run_test()
