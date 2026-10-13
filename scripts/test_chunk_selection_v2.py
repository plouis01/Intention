"""Test chunk selection V2 - Focus on amendment contamination and section quality.

Tests different strategies for filtering BOTH debate and amendment chunks,
with detailed quality analysis per section.
"""

import sys
import re
from datetime import timedelta, date
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analysis.article_index import get_article_index
from src.indexing.search import SearchService, _keyword_matches
from src.analysis.article_analyzer import ArticleAnalyzer


def _stem(w):
    return w.lower().rstrip('s').rstrip('e').rstrip('é')

def _dedup_kw(kw_list):
    seen = set()
    out = []
    for kw in kw_list:
        s = _stem(kw.split()[0]) if kw else kw
        if s not in seen:
            seen.add(s)
            out.append(kw)
    return out

GENERIC_KW = {
    "plus-value", "plus-values", "plus-value réalisée", "plus-value en report",
    "impôt sur les sociétés", "impôt sur le revenu",
    "valeurs mobilières, droits sociaux et titres assimilés",
}

PROCEDURAL = [
    "la parole est à", "je mets aux voix", "la séance est levée",
    "la séance est suspendue", "la séance est reprise", "prochaine séance",
    "l'ordre du jour appelle", "nous reprenons la discussion",
    "l'amendement est adopté", "l'amendement est rejeté",
    "l'amendement n'est pas adopté", "en conséquence, un article additionnel",
]

AUTHORITY = {"rapporteur", "rapporteur général", "ministre", "secrétaire d'état",
             "ministre délégué", "président de la commission"}


def is_procedural_chunk(content):
    cl = re.sub(r'\s+', ' ', content.lower())
    sents = [s.strip() for s in content.split('.') if len(s.strip()) > 10]
    if not sents:
        return False
    proc = sum(1 for s in sents if any(p in s.lower() for p in PROCEDURAL))
    return proc / len(sents) > 0.5


def cites_other_article(content, target_ref):
    _re = re.compile(
        r"article\s+(\d+[\w\s\-]*(?:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies)?)",
        re.IGNORECASE,
    )
    found = _re.findall(content)
    if not found:
        return False
    tn = target_ref.lower().replace(" ", "").replace("-", "")
    has_other = has_target = False
    for a in found:
        an = a.strip().lower().replace(" ", "").replace("-", "")
        if len(an) > 2:
            if tn in an or an in tn:
                has_target = True
            else:
                has_other = True
    return has_other and not has_target


def is_explicit_ref(chunk, article_ref):
    cl = chunk.content.lower()
    rl = article_ref.lower()
    if rl in cl or rl.replace("-", " ") in cl:
        return True
    mentions = chunk.metadata.get("articles_mentionnes")
    if mentions:
        for m in mentions:
            mn = m.lower().replace("-", " ").strip()
            if rl.replace("-", " ") in mn or mn in rl.replace("-", " "):
                return True
    return False


def keyword_quality(content, keywords):
    cl = content.lower()
    kw = [k for k in keywords if _keyword_matches(k, cl)]
    unique = _dedup_kw(kw)
    specific = [k for k in unique if k not in GENERIC_KW]
    return len(unique), len(specific)


def filter_enhanced(debate_chunks, amend_chunks, article_ref, keywords, mod_date):
    """Enhanced filter for BOTH debates and amendments."""

    # === DEBATES ===
    explicit_debate_ids = set()
    explicit_debates = []

    for c, s in debate_chunks:
        if is_explicit_ref(c, article_ref):
            explicit_debates.append((c, s, "explicit_ref"))
            explicit_debate_ids.add(c.source_id)

    same_debate = []
    for c, s in debate_chunks:
        if c.source_id not in explicit_debate_ids:
            continue
        if any(c.content[:80] == ec.content[:80] for ec, _, _ in explicit_debates):
            continue
        if is_procedural_chunk(c.content):
            continue
        uk, sk = keyword_quality(c.content, keywords)
        if uk >= 1:
            same_debate.append((c, s, "same_debate"))
            continue
        fn = (c.metadata.get("fonction") or "").lower()
        if any(r in fn for r in AUTHORITY):
            if any(_keyword_matches(k, c.content.lower()) for k in keywords):
                same_debate.append((c, s * 0.95, "authority_adj"))

    other_debate = []
    for c, s in debate_chunks:
        if c.source_id in explicit_debate_ids:
            continue
        if is_procedural_chunk(c.content):
            continue
        if cites_other_article(c.content, article_ref):
            continue
        uk, sk = keyword_quality(c.content, keywords)
        if uk >= 2 and sk >= 1:
            other_debate.append((c, s, "keyword"))
            continue
        fn = (c.metadata.get("fonction") or "").lower()
        if any(r in fn for r in AUTHORITY) and sk >= 1:
            other_debate.append((c, s * 0.9, "authority_other"))

    # Dedup debates
    seen = set()
    debates_out = []
    for c, s, r in explicit_debates + same_debate + other_debate:
        k = c.content[:80]
        if k not in seen:
            seen.add(k)
            debates_out.append((c, s, r))

    # === AMENDMENTS ===
    amends_out = []
    for c, s in amend_chunks:
        # Filter 1: Must mention the article explicitly
        if not is_explicit_ref(c, article_ref):
            # Relaxed: check keywords
            uk, sk = keyword_quality(c.content, keywords)
            if sk < 1:
                continue

        # Filter 2: Reject if cites a different article without citing target
        if cites_other_article(c.content, article_ref):
            continue

        # Filter 3: Temporal - reject amendments > 300 days before modification
        amend_date_str = c.metadata.get("date", "")
        if amend_date_str and mod_date:
            try:
                ad = date.fromisoformat(amend_date_str[:10])
                gap = (mod_date - ad).days
                if gap > 300:
                    continue
            except (ValueError, TypeError):
                pass

        # Filter 4: Procedural
        if is_procedural_chunk(c.content):
            continue

        amends_out.append((c, s, "amendment"))

    return debates_out, amends_out


def run_test(article_ref="150-0 B ter"):
    index = get_article_index()
    article_info = index.get_article(article_ref)
    search_service = SearchService.create_configured()
    analyzer = ArticleAnalyzer(search_service=search_service)
    all_mod_dates = [(m.date, m.law_type) for m in article_info.modifications]

    skip_types = {"DECRET", "ORDONNANCE", "ARRETE", "LOI_ORGANIQUE", "INCONNU"}
    skip_natures = {"Codification", "Transfert"}
    mods = [m for m in article_info.modifications
            if m.law_type not in skip_types and m.nature.value not in skip_natures]

    for i, mod in enumerate(mods):
        anchor = mod.date
        if mod.date_debut_vigueur:
            try:
                anchor = date.fromisoformat(mod.date_debut_vigueur)
            except ValueError:
                pass

        law_refs = analyzer._get_debate_refs_for_law(mod.law_numero)
        results = search_service.search_article_modification(
            article_ref=article_info.reference, article_keywords=article_info.keywords,
            law_type=mod.law_type, law_year=mod.law_year,
            date_start=anchor - timedelta(days=450), date_end=anchor + timedelta(days=30),
            top_k=20, min_relevant=5, article_in_law=mod.article_in_law,
            all_modification_dates=all_mod_dates, current_mod_date=mod.date,
            law_debate_refs=law_refs, law_numero=mod.law_numero,
            modification_objet=mod.objet or "",
        )

        raw_debates = results.get("debates", [])
        raw_amends = results.get("amendments", [])

        debates_out, amends_out = filter_enhanced(
            raw_debates, raw_amends, article_info.reference, article_info.keywords, mod.date
        )

        print(f"\n{'='*80}")
        print(f"S{i}: {mod.law_type} {mod.law_year} (n°{mod.law_numero}) - {mod.nature.value}")
        print(f"Objet: {(mod.objet or '')[:60]}")
        print(f"Raw: {len(raw_debates)} debates, {len(raw_amends)} amends")
        print(f"Enhanced: {len(debates_out)} debates, {len(amends_out)} amends")
        print(f"{'='*80}")

        # Debates detail
        reasons = defaultdict(int)
        for _, _, r in debates_out:
            reasons[r] += 1
        print(f"  Debates: {dict(reasons)}")

        for j, (c, s, r) in enumerate(debates_out[:4]):
            mentions = '150-0' in c.content.lower()
            apport = 'apport' in c.content.lower() and ('cession' in c.content.lower() or 'report' in c.content.lower())
            q = "✅" if mentions else ("🟡" if apport else "⚠️")
            print(f"    {q} [{s:.3f}] {r} | {c.metadata.get('orateur','?')[:25]} ({c.metadata.get('fonction','?')[:15]})")
        if len(debates_out) > 4:
            print(f"    ... +{len(debates_out)-4} more")

        # Amendments detail
        if amends_out:
            print(f"  Amendments ({len(amends_out)}):")
            for j, (c, s, r) in enumerate(amends_out[:3]):
                mentions = '150-0' in c.content.lower()
                amend_date = c.metadata.get('date', '?')
                q = "✅" if mentions else "⚠️"
                print(f"    {q} [{s:.3f}] {c.metadata.get('numero','?')} | {amend_date[:10] if amend_date else '?'}")
                print(f"      \"{c.content[:100]}...\"")
        else:
            print(f"  Amendments: 0")

        # Amendments that were REJECTED
        rejected_amends = len(raw_amends) - len(amends_out)
        if rejected_amends > 0:
            print(f"  Rejected amendments: {rejected_amends}")
            # Show why
            filtered_ids = {c.content[:80] for c, _, _ in amends_out}
            for c, s in raw_amends:
                if c.content[:80] not in filtered_ids:
                    amend_date = c.metadata.get('date', '?')
                    mentions = '150-0' in c.content.lower()
                    other_art = cites_other_article(c.content, article_info.reference)
                    gap = ""
                    if amend_date and amend_date != '?' and mod.date:
                        try:
                            ad = date.fromisoformat(amend_date[:10])
                            gap = f" ({(mod.date - ad).days}d)"
                        except:
                            pass
                    reason = []
                    if not mentions and keyword_quality(c.content, article_info.keywords)[1] < 1:
                        reason.append("no_ref_no_specific_kw")
                    if other_art:
                        reason.append("cites_other_article")
                    if gap and "d)" in gap:
                        days = int(gap.split("(")[1].split("d")[0])
                        if days > 300:
                            reason.append(f"temporal{gap}")
                    if is_procedural_chunk(c.content):
                        reason.append("procedural")
                    print(f"    ❌ [{s:.3f}] {amend_date[:10] if amend_date and amend_date != '?' else '?'}{gap} - {', '.join(reason) if reason else 'unknown'}")
                    if len([c2 for c2, _ in raw_amends if c2.content[:80] not in filtered_ids]) > 3:
                        break


if __name__ == "__main__":
    run_test()
