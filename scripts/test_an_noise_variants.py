#!/usr/bin/env python3
"""Test multiple variants for detecting noise AN debate chunks."""
import sys
sys.stdout.reconfigure(line_buffering=True)

# Force reload of dotenv BEFORE any imports that read settings
import os
from dotenv import load_dotenv
load_dotenv(override=True)

from datetime import timedelta
from src.analysis.article_index import get_article_index
from src.indexing.search import SearchService, _keyword_matches
from src.analysis.article_analyzer import ArticleAnalyzer

index = get_article_index()
search = SearchService.create_configured()
analyzer = ArticleAnalyzer(search_service=search)
generic_kw = analyzer._get_generic_keywords()

print(f"Generic keywords ({len(generic_kw)}): {sorted(generic_kw)[:10]}...")

# Collect all results for final stats
all_chunks = []  # list of dicts with variant results + manual label


def analyze_an_chunks(article_ref, si_list):
    article_info = index.get_article(article_ref)
    skip_types = {'DECRET', 'ORDONNANCE', 'ARRETE', 'LOI_ORGANIQUE', 'INCONNU'}
    skip_natures = {'Codification', 'Transfert'}
    mods = [m for m in article_info.modifications
            if m.law_type not in skip_types and m.nature.value not in skip_natures]
    all_mod_dates = [(m.date, m.law_type) for m in article_info.modifications]

    for si in si_list:
        if si >= len(mods):
            print(f"\n--- {article_ref} S{si} SKIPPED (only {len(mods)} mods) ---")
            continue
        mod = mods[si]
        anchor = mod.date
        law_refs = analyzer._get_debate_refs_for_law(mod.law_numero)
        rapport_kw = analyzer._extract_rapport_keywords(
            mod.law_numero, article_info.reference, article_info.keywords)
        enriched = list(article_info.keywords) + rapport_kw

        results = search.search_article_modification(
            article_ref=article_info.reference, article_keywords=enriched,
            law_type=mod.law_type, law_year=mod.law_year,
            date_start=anchor - timedelta(days=450),
            date_end=anchor + timedelta(days=30),
            top_k=20, min_relevant=5, article_in_law=mod.article_in_law,
            all_modification_dates=all_mod_dates, current_mod_date=mod.date,
            law_debate_refs=law_refs, law_numero=mod.law_numero,
            modification_objet=mod.objet or '',
        )

        debates = results.get('debates', [])
        an_chunks = [(c, s) for c, s in debates
                     if (c.metadata.get('chambre') or '').upper() == 'AN']
        senat_chunks = [(c, s) for c, s in debates
                        if (c.metadata.get('chambre') or '').upper() != 'AN']

        print(f"\n{'='*80}")
        print(f"--- {article_ref} S{si} {mod.law_type} {mod.law_year} (law={mod.law_numero}) ---")
        print(f"Total: {len(debates)} (AN: {len(an_chunks)}, Senat: {len(senat_chunks)})")
        print(f"Enriched keywords ({len(enriched)}): {enriched[:8]}...")

        for c, s in an_chunks:
            content_lower = c.content.lower()
            ref_lower = article_ref.lower()
            has_ref = (ref_lower in content_lower
                       or ref_lower.replace('-', ' ') in content_lower)

            # Also check for article number alone (e.g. "787" for "787 B")
            parts = article_ref.split()
            has_article_num = False
            if len(parts) >= 1:
                art_num = parts[0]
                if f"article {art_num.lower()}" in content_lower:
                    has_article_num = True

            # Variant A: any keyword > 5 chars
            kw_any = [kw for kw in enriched if len(kw) > 5
                      and not kw.lower().startswith('article ')
                      and _keyword_matches(kw, content_lower)]

            # Variant B: any specific keyword (not generic)
            kw_specific = [kw for kw in kw_any if kw not in generic_kw]

            # Variant C: 2+ unique keywords (stem-deduped)
            unique_kw = analyzer._dedup_kw_by_stem(kw_any)

            # Variant D: has explicit ref OR 1+ specific keyword
            pass_d = has_ref or len(kw_specific) >= 1

            # Variant E: has explicit ref OR 2+ unique keywords
            pass_e = has_ref or len(unique_kw) >= 2

            # Variant F: score-based - chunk must have score > 0.3 AND at least 1 keyword
            pass_f = s > 0.3 and len(kw_any) >= 1

            orateur = c.metadata.get('orateur', '?')[:30]
            debat_ref = c.metadata.get('debat_ref', '?')[:40]

            p = lambda v: "PASS" if v else "FAIL"

            print(f"\n  AN [{s:.3f}] {orateur}")
            print(f"    debat_ref: {debat_ref}")
            print(f"    ref={has_ref} art_num={has_article_num} kw_any={len(kw_any)} specific={len(kw_specific)} unique={len(unique_kw)}")
            print(f"    matched_kw: {kw_any[:6]}")
            print(f"    specific_kw: {kw_specific[:6]}")
            print(f"    A(any>0)={p(kw_any)}  B(spec>0)={p(kw_specific)}  C(uniq>=2)={p(len(unique_kw) >= 2)}  D(ref|spec)={p(pass_d)}  E(ref|2kw)={p(pass_e)}  F(score+kw)={p(pass_f)}")
            content_preview = c.content[:250].replace('\n', ' ')
            print(f"    content: {content_preview}")

            # Heuristic labeling: a chunk is "relevant" if it contains the exact article ref
            # OR contains 2+ specific keywords. We'll review these manually.
            # For automated labeling: exact ref = definitely relevant
            # No ref + no specific kw = definitely noise
            # Borderline = 1 specific kw, no ref
            if has_ref:
                label = "RELEVANT"
            elif len(kw_specific) >= 2:
                label = "LIKELY_RELEVANT"
            elif len(kw_specific) == 1 and len(unique_kw) >= 2:
                label = "MAYBE_RELEVANT"
            elif len(kw_any) == 0:
                label = "NOISE"
            else:
                label = "LIKELY_NOISE"

            print(f"    => LABEL: {label}")

            all_chunks.append({
                'article': article_ref,
                'section': si,
                'score': s,
                'has_ref': has_ref,
                'kw_any': len(kw_any),
                'kw_specific': len(kw_specific),
                'unique_kw': len(unique_kw),
                'pass_a': bool(kw_any),
                'pass_b': bool(kw_specific),
                'pass_c': len(unique_kw) >= 2,
                'pass_d': pass_d,
                'pass_e': pass_e,
                'pass_f': pass_f,
                'label': label,
            })


analyze_an_chunks("150-0 B ter", [0, 3, 5])
analyze_an_chunks("787 B", [0, 1, 2, 3, 4])
analyze_an_chunks("199 terdecies-0 A", [0, 1, 2, 3, 4])

# Summary statistics
print(f"\n{'='*80}")
print("SUMMARY STATISTICS")
print(f"{'='*80}")
print(f"Total AN chunks analyzed: {len(all_chunks)}")

relevant = [c for c in all_chunks if c['label'] in ('RELEVANT', 'LIKELY_RELEVANT', 'MAYBE_RELEVANT')]
noise = [c for c in all_chunks if c['label'] in ('NOISE', 'LIKELY_NOISE')]

print(f"Relevant: {len(relevant)}  Noise: {len(noise)}")

for variant in ['A', 'B', 'C', 'D', 'E', 'F']:
    key = f'pass_{variant.lower()}'
    tp = sum(1 for c in relevant if c[key])
    fn = sum(1 for c in relevant if not c[key])
    fp = sum(1 for c in noise if c[key])
    tn = sum(1 for c in noise if not c[key])

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    # For our use case:
    # "noise_recall" = how often we correctly flag noise as noise (TN / (TN + FP))
    # "false_trigger" = how often we wrongly flag relevant as noise (FN / (FN + TP))
    noise_recall = tn / (tn + fp) if (tn + fp) > 0 else 0
    false_trigger = fn / (fn + tp) if (fn + tp) > 0 else 0

    print(f"\nVariant {variant} (chunk passes = deemed relevant):")
    print(f"  TP={tp} FP={fp} TN={tn} FN={fn}")
    print(f"  Precision={precision:.3f} Recall={recall:.3f} F1={f1:.3f}")
    print(f"  Noise detection rate (want HIGH): {noise_recall:.3f}")
    print(f"  False trigger rate (want LOW): {false_trigger:.3f}")

# Section-level analysis: for expansion trigger decision
print(f"\n{'='*80}")
print("SECTION-LEVEL ANALYSIS (expansion trigger)")
print(f"{'='*80}")
print("Expansion triggers when ALL AN chunks in a section are noise (fail the variant)")

from collections import defaultdict
sections = defaultdict(list)
for c in all_chunks:
    sections[(c['article'], c['section'])].append(c)

for variant in ['A', 'B', 'C', 'D', 'E', 'F']:
    key = f'pass_{variant.lower()}'
    print(f"\nVariant {variant}:")
    correct_decisions = 0
    total_sections = 0
    for (art, si), chunks in sorted(sections.items()):
        any_pass = any(c[key] for c in chunks)
        any_relevant = any(c['label'] in ('RELEVANT', 'LIKELY_RELEVANT', 'MAYBE_RELEVANT') for c in chunks)
        all_noise = all(c['label'] in ('NOISE', 'LIKELY_NOISE') for c in chunks)

        # Expansion triggers when no chunk passes
        expansion_triggers = not any_pass
        # Should trigger when all noise, should NOT trigger when relevant exists
        should_trigger = all_noise
        correct = expansion_triggers == should_trigger

        total_sections += 1
        if correct:
            correct_decisions += 1

        status = "OK" if correct else "WRONG"
        reason = ""
        if not correct:
            if expansion_triggers and not should_trigger:
                reason = " (FALSE TRIGGER - relevant chunks missed!)"
            else:
                reason = " (MISSED - noise not detected, expansion blocked)"

        print(f"  {art} S{si}: AN={len(chunks)} relevant={any_relevant} trigger={expansion_triggers} should={should_trigger} => {status}{reason}")

    print(f"  Accuracy: {correct_decisions}/{total_sections} = {correct_decisions/total_sections:.1%}")
