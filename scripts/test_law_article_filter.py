#!/usr/bin/env python3
"""Test false positive mitigation for law article number search broadening.

Compares search results WITH vs WITHOUT article_in_law to measure:
1. How many NEW chunks are gained (only match law article, not CGI ref)
2. Whether new chunks are relevant or false positives
3. Whether reranker + tier filtering adequately demote irrelevant chunks
"""

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analysis.article_index import get_article_index
from src.indexing.search import SearchService, _generate_article_variants, _generate_law_article_variants
from src.utils.config import get_settings


def check_chunk_mentions(chunk_content: str, cgi_ref: str, law_article_variants: list[str]) -> dict:
    """Check what a chunk mentions: CGI ref, law article, both, or neither."""
    content_lower = chunk_content.lower()
    cgi_variants = [cgi_ref.lower()] + [v.lower() for v in _generate_article_variants(cgi_ref)]

    has_cgi = any(v in content_lower for v in cgi_variants)
    has_law_art = any(v.lower() in content_lower for v in law_article_variants)

    return {"has_cgi": has_cgi, "has_law_art": has_law_art}


def run_test_case(search_service, article_ref, law_type, law_year, article_in_law):
    """Run search WITH and WITHOUT article_in_law, compare results."""
    # Date range
    if law_type in ("LF", "LFSS"):
        date_start = date(law_year - 1, 3, 1)
        date_end = date(law_year, 2, 28)
    else:
        date_start = date(law_year - 1, 6, 1)
        date_end = date(law_year, 3, 31)

    index = get_article_index()
    article_info = index.get_article(article_ref)
    if not article_info:
        print(f"  SKIP: Article {article_ref} not in index")
        return None
    keywords = article_info.keywords

    law_variants = _generate_law_article_variants(article_in_law)
    print(f"\n{'='*70}")
    print(f"  {article_ref} / {law_type} {law_year}")
    print(f"  article_in_law: {article_in_law!r}")
    print(f"  law article variants: {law_variants}")
    print(f"  keywords: {keywords[:5]}...")
    print(f"{'='*70}")

    # Search WITHOUT article_in_law (baseline)
    results_without = search_service.search_article_modification(
        article_ref=article_ref,
        article_keywords=keywords,
        law_type=law_type,
        law_year=law_year,
        date_start=date_start,
        date_end=date_end,
        top_k=15,
        min_relevant=3,
        article_in_law="",  # No law article
    )

    # Search WITH article_in_law
    results_with = search_service.search_article_modification(
        article_ref=article_ref,
        article_keywords=keywords,
        law_type=law_type,
        law_year=law_year,
        date_start=date_start,
        date_end=date_end,
        top_k=15,
        min_relevant=3,
        article_in_law=article_in_law,
    )

    # Compare debate results
    debates_without = results_without.get("debates", [])
    debates_with = results_with.get("debates", [])

    ids_without = {id(c) for c, _ in debates_without}
    # Can't compare by object identity across searches, use content hash
    content_without = {c.content[:200] for c, _ in debates_without}
    content_with = {c.content[:200] for c, _ in debates_with}

    new_chunks = content_with - content_without
    lost_chunks = content_without - content_with

    print(f"\n  Results WITHOUT article_in_law: {len(debates_without)} debate chunks")
    print(f"  Results WITH    article_in_law: {len(debates_with)} debate chunks")
    print(f"  New chunks gained: {len(new_chunks)}")
    print(f"  Chunks lost:       {len(lost_chunks)}")

    # Analyze ALL chunks in the WITH results
    print(f"\n  --- Chunk analysis (WITH article_in_law) ---")
    cgi_only = 0
    law_only = 0
    both = 0
    neither = 0

    for i, (chunk, score) in enumerate(debates_with):
        mentions = check_chunk_mentions(chunk.content, article_ref, law_variants)
        is_new = chunk.content[:200] in new_chunks

        if mentions["has_cgi"] and mentions["has_law_art"]:
            both += 1
            label = "CGI+LAW"
        elif mentions["has_cgi"]:
            cgi_only += 1
            label = "CGI-only"
        elif mentions["has_law_art"]:
            law_only += 1
            label = "LAW-only"
        else:
            neither += 1
            label = "NEITHER"

        marker = " [NEW]" if is_new else ""
        # Show first 120 chars of content for manual inspection
        preview = chunk.content[:120].replace("\n", " ")
        print(f"    #{i+1} ({score:.3f}) [{label}]{marker}: {preview}...")

    print(f"\n  Summary:")
    print(f"    CGI+LAW: {both}  |  CGI-only: {cgi_only}  |  LAW-only: {law_only}  |  NEITHER: {neither}")

    if law_only > 0:
        print(f"    >>> {law_only} chunks match ONLY the law article number (potential false positives)")
        print(f"    >>> Inspect these manually to assess relevance")

    return {
        "case": f"{article_ref}/{law_type}/{law_year}",
        "without": len(debates_without),
        "with": len(debates_with),
        "new": len(new_chunks),
        "lost": len(lost_chunks),
        "cgi_only": cgi_only,
        "law_only": law_only,
        "both": both,
        "neither": neither,
    }


def main():
    # Test cases with varying article_in_law values
    test_cases = [
        # Multi-digit law articles (should be added to filters)
        ("200 A", "LF", 2013, "Art. 10"),       # 2-digit
        ("150-0 B ter", "LFR", 2012, "Art. 18"), # 2-digit
        ("150-0 B ter", "LFR", 2015, "Art. 26"), # 2-digit
        ("787 B", "LF", 2019, "Art. 40"),         # 2-digit
        ("199 terdecies-0 A", "LF", 2018, "Art. 74"),  # 2-digit
        ("150-0 D", "LF", 2014, "Art. 17"),       # 2-digit
        ("209", "LF", 2019, "Art. 34"),            # 2-digit, SHORT CGI ref
        # High article number (very specific)
        ("39", "LF", 2019, "Art. 277"),            # 3-digit (very specific)
    ]

    settings = get_settings()
    print(f"Vector store: {settings.vector_store_provider}")

    search_service = SearchService()
    if settings.vector_store_provider == "pgvector":
        from src.indexing.vector_stores import create_vector_store
        pgvector_store = create_vector_store("pgvector")
        search_service.debate_store = pgvector_store
        search_service.amendment_store = pgvector_store
        print(f"pgvector connected ({pgvector_store.count()} chunks)")

    results = []
    for article_ref, law_type, law_year, article_in_law in test_cases:
        try:
            r = run_test_case(search_service, article_ref, law_type, law_year, article_in_law)
            if r:
                results.append(r)
        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()

    # Summary table
    print(f"\n\n{'='*80}")
    print(f"SUMMARY")
    print(f"{'='*80}")
    print(f"{'Case':<35} {'w/o':>4} {'with':>4} {'new':>4} {'lost':>4} {'CGI':>4} {'LAW':>4} {'both':>4} {'?':>4}")
    print("-" * 80)
    for r in results:
        print(
            f"{r['case']:<35} {r['without']:>4} {r['with']:>4} "
            f"{r['new']:>4} {r['lost']:>4} {r['cgi_only']:>4} "
            f"{r['law_only']:>4} {r['both']:>4} {r['neither']:>4}"
        )

    total_law_only = sum(r["law_only"] for r in results)
    total_with = sum(r["with"] for r in results)
    if total_with > 0:
        print(f"\nTotal LAW-only chunks: {total_law_only}/{total_with} ({100*total_law_only/total_with:.1f}%)")
        print("If LAW-only % is high, false positive mitigation may be insufficient.")


if __name__ == "__main__":
    main()
