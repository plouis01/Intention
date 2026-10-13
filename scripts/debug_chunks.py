"""Debug chunks sent to LLM for specific sections."""
import sys
sys.stdout.reconfigure(line_buffering=True)

from datetime import date, timedelta
from src.analysis.article_index import get_article_index
from src.indexing.search import SearchService
from src.analysis.article_analyzer import ArticleAnalyzer

index = get_article_index()
search = SearchService.create_configured()
analyzer = ArticleAnalyzer(search_service=search)

article_info = index.get_article("150-0 B ter")
skip_types = {"DECRET", "ORDONNANCE", "ARRETE", "LOI_ORGANIQUE", "INCONNU"}
skip_natures = {"Codification", "Transfert"}
mods = [m for m in article_info.modifications
        if m.law_type not in skip_types and m.nature.value not in skip_natures]
all_mod_dates = [(m.date, m.law_type) for m in article_info.modifications]

for si in [0, 1, 3, 4, 7]:
    mod = mods[si]
    anchor = mod.date
    if mod.date_debut_vigueur:
        try:
            anchor = date.fromisoformat(mod.date_debut_vigueur)
        except ValueError:
            pass

    law_refs = analyzer._get_debate_refs_for_law(mod.law_numero)
    rapport_kw = analyzer._extract_rapport_keywords(
        mod.law_numero, article_info.reference, article_info.keywords
    )
    enriched = list(article_info.keywords) + rapport_kw

    results = search.search_article_modification(
        article_ref=article_info.reference,
        article_keywords=enriched,
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

    debates = results.get("debates", [])
    amends = results.get("amendments", [])

    filtered_debates = analyzer._filter_chunks_by_article_relevance(
        debates, article_info.reference, enriched
    )
    filtered_amends = analyzer._filter_amendment_chunks(
        amends, article_info.reference, article_info.keywords, mod.date
    )

    an = [(c, s) for c, s in filtered_debates if c.metadata.get("chambre") == "AN"]
    senat = [(c, s) for c, s in filtered_debates if c.metadata.get("chambre") == "Sénat"]

    print(f"\n{'='*80}")
    print(f"S{si}: {mod.law_type} {mod.law_year} ({mod.law_numero}) - {mod.nature.value}")
    print(f"Objet: {(mod.objet or '(vide)')[:150]}")
    print(f"Rapport keywords: {rapport_kw}")
    print(f"Raw: {len(debates)} debates, {len(amends)} amendments")
    print(f"Filtered: {len(filtered_debates)} debates ({len(an)} AN, {len(senat)} Sénat), {len(filtered_amends)} amendments")

    print(f"\n--- AN CHUNKS ({len(an)}) ---")
    for c, s in an:
        orateur = c.metadata.get("orateur", "?")
        fonction = c.metadata.get("fonction", "")
        has_ref = "150-0 b ter" in c.content.lower()
        print(f"  [{s:.3f}] {orateur} ({fonction}) explicit_ref={has_ref}")
        print(f"    {c.content[:250].replace(chr(10), ' ')}")

    print(f"\n--- SÉNAT CHUNKS ({len(senat)}) ---")
    for c, s in senat:
        orateur = c.metadata.get("orateur", "?")
        fonction = c.metadata.get("fonction", "")
        has_ref = "150-0 b ter" in c.content.lower()
        print(f"  [{s:.3f}] {orateur} ({fonction}) explicit_ref={has_ref}")
        print(f"    {c.content[:250].replace(chr(10), ' ')}")

    print(f"\n--- AMENDMENTS ({len(filtered_amends)}) ---")
    for c, s in filtered_amends:
        chambre = c.metadata.get("chambre", "?")
        has_ref = "150-0 b ter" in c.content.lower()
        print(f"  [{s:.3f}] {chambre} explicit_ref={has_ref}: {c.content[:200].replace(chr(10), ' ')}")
