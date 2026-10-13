#!/usr/bin/env python3
"""
Fix data quality issues in article_modifications.json.

Corrections applied:
1. Fix aberrant law_year values (EU regulation numbers parsed as years)
2. Fill law_year=0 for all JORF-sourced entries (extract from date field)
3. Standardize field names: loi_numero→law_numero, loi_type→law_type, loi_titre→law_title
4. Enrich missing law_title (hardcoded titles, lois_cgi lookup, constructed fallback)
5. Enrich missing legifrance_url from lois_cgi_1958_2026.json
6. Fix EU regulation law_type for 974-98
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DATA_FILE = PROJECT_ROOT / "data" / "article_modifications.json"
LOIS_FILE = PROJECT_ROOT / "data" / "lois_cgi_1958_2026.json"

# EU regulation numbers incorrectly stored as law_year
ABERRANT_YEARS = {1103, 1260, 1493, 2137, 2866, 3330}

# Mapping for EU regulation law_type correction
EU_REGULATION_PREFIXES = {
    "1103-97": "REGLEMENT_CE",
    "2866-98": "REGLEMENT_CE",
    "1260-2001": "REGLEMENT_CE",
    "1493-99": "REGLEMENT_CE",
    "2137-85": "REGLEMENT_CEE",
    "3330-74": "REGLEMENT_CEE",
    "974-98": "REGLEMENT_CE",
}

# Hardcoded titles for known laws not in reference files
KNOWN_TITLES = {
    "50-478": "Décret n° 50-478 du 6 avril 1950 portant refonte du code général des impôts",
}

# law_type display labels for title construction
TYPE_LABELS = {
    "LOI": "Loi",
    "DECRET": "Décret",
    "LF": "Loi de finances",
    "LFR": "Loi de finances rectificative",
    "LFSS": "Loi de financement de la sécurité sociale",
    "ORDONNANCE": "Ordonnance",
    "REGLEMENT_CE": "Règlement CE",
    "REGLEMENT_CEE": "Règlement CEE",
}


def fix_aberrant_years(mod: dict) -> dict:
    """Fix entries where EU regulation number was stored as law_year."""
    law_year = mod.get("law_year", 0)
    if law_year in ABERRANT_YEARS:
        date = mod.get("date", "")
        if date and len(date) >= 4:
            mod["law_year"] = int(date[:4])

        numero = mod.get("law_numero") or mod.get("loi_numero", "")
        if numero in EU_REGULATION_PREFIXES:
            field = "law_type" if "law_type" in mod else "loi_type"
            mod[field] = EU_REGULATION_PREFIXES[numero]

    return mod


def fix_jorf_law_year(mod: dict) -> dict:
    """Fill law_year from date for JORF entries where law_year is 0."""
    if mod.get("source") == "jorf_legi_articles" and (mod.get("law_year", 0) == 0 or mod.get("law_year") is None):
        date = mod.get("date", "")
        if date and len(date) >= 4:
            mod["law_year"] = int(date[:4])
    return mod


def standardize_field_names(mod: dict) -> dict:
    """Rename JORF-style field names to standard names."""
    renames = {
        "loi_numero": "law_numero",
        "loi_type": "law_type",
        "loi_titre": "law_title",
    }
    for old_name, new_name in renames.items():
        if old_name in mod and new_name not in mod:
            mod[new_name] = mod.pop(old_name)
        elif old_name in mod and new_name in mod:
            # Both exist — keep new_name, drop old
            mod.pop(old_name)
    return mod


def load_lois_reference() -> dict:
    """Load lois_cgi_1958_2026.json and return mappings."""
    with open(LOIS_FILE, "r", encoding="utf-8") as f:
        lois_data = json.load(f)

    titre_map = {}
    url_map = {}
    for loi in lois_data.get("lois", []):
        numero = loi.get("numero", "")
        if not numero:
            continue
        titre = loi.get("titre", "")
        if titre:
            titre_map[numero] = titre
        url = loi.get("url_legifrance", "")
        if url:
            url_map[numero] = url

    return {"titres": titre_map, "urls": url_map}


def fix_missing_titles(articles: dict, lois_ref: dict) -> dict:
    """Enrich missing law_title fields."""
    stats = {"hardcoded": 0, "from_lois_cgi": 0, "constructed": 0, "skipped": 0}

    for art_data in articles.values():
        for mod in art_data.get("modifications", []):
            if mod.get("law_title"):
                continue

            numero = mod.get("law_numero")
            if not numero:
                stats["skipped"] += 1
                continue

            # a) Hardcoded known titles
            if numero in KNOWN_TITLES:
                mod["law_title"] = KNOWN_TITLES[numero]
                stats["hardcoded"] += 1
                continue

            # b) Lookup in lois_cgi reference
            titre = lois_ref["titres"].get(numero)
            if titre:
                mod["law_title"] = titre
                stats["from_lois_cgi"] += 1
                continue

            # c) Construct title from type + numero + date
            law_type = mod.get("law_type", "")
            date = mod.get("date", "")
            type_label = TYPE_LABELS.get(law_type, law_type or "Texte")
            mod["law_title"] = f"{type_label} n° {numero} du {date}"
            stats["constructed"] += 1

    return stats


def fix_missing_urls(articles: dict, lois_ref: dict) -> dict:
    """Enrich missing legifrance_url from lois_cgi reference."""
    stats = {"enriched": 0, "not_found": 0}

    for art_data in articles.values():
        for mod in art_data.get("modifications", []):
            if mod.get("legifrance_url"):
                continue

            numero = mod.get("law_numero")
            if not numero:
                stats["not_found"] += 1
                continue

            url = lois_ref["urls"].get(numero)
            if url:
                mod["legifrance_url"] = url
                stats["enriched"] += 1
            else:
                stats["not_found"] += 1

    return stats


def fix_eu_regulation_type(articles: dict) -> int:
    """Fix law_type for EU regulations (e.g., 974-98 marked as LOI)."""
    fixed = 0
    for art_data in articles.values():
        for mod in art_data.get("modifications", []):
            numero = mod.get("law_numero", "")
            if numero in EU_REGULATION_PREFIXES:
                expected_type = EU_REGULATION_PREFIXES[numero]
                if mod.get("law_type") != expected_type:
                    mod["law_type"] = expected_type
                    fixed += 1
    return fixed


def main():
    print(f"Loading {DATA_FILE}...")
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    articles = data["articles"]
    total_mods_before = sum(len(a.get("modifications", [])) for a in articles.values())

    # Load reference data
    print(f"Loading {LOIS_FILE}...")
    lois_ref = load_lois_reference()
    print(f"  {len(lois_ref['titres'])} titles, {len(lois_ref['urls'])} URLs available")

    # Counters
    stats = {
        "aberrant_years_fixed": 0,
        "jorf_law_year_filled": 0,
        "fields_renamed": 0,
        "eu_law_type_fixed": 0,
    }

    for art_name, art_data in articles.items():
        for mod in art_data.get("modifications", []):
            # 1. Fix aberrant years
            old_year = mod.get("law_year", 0)
            if old_year in ABERRANT_YEARS:
                fix_aberrant_years(mod)
                stats["aberrant_years_fixed"] += 1
                old_type = mod.get("law_type") or mod.get("loi_type", "")
                if old_type.startswith("REGLEMENT"):
                    stats["eu_law_type_fixed"] += 1

            # 2. Fix JORF law_year=0
            if mod.get("source") == "jorf_legi_articles" and (mod.get("law_year", 0) == 0 or mod.get("law_year") is None):
                fix_jorf_law_year(mod)
                stats["jorf_law_year_filled"] += 1

            # 3. Standardize field names
            has_old = any(k in mod for k in ("loi_numero", "loi_type", "loi_titre"))
            if has_old:
                standardize_field_names(mod)
                stats["fields_renamed"] += 1

    # 4. Fix EU regulation types (974-98 etc.)
    eu_type_fixed = fix_eu_regulation_type(articles)

    # 5. Enrich missing titles
    title_stats = fix_missing_titles(articles, lois_ref)

    # 6. Enrich missing URLs
    url_stats = fix_missing_urls(articles, lois_ref)

    # Validation
    total_mods_after = sum(len(a.get("modifications", [])) for a in articles.values())
    assert total_mods_before == total_mods_after, (
        f"Modification count changed! Before: {total_mods_before}, After: {total_mods_after}"
    )

    # Check remaining issues
    remaining_aberrant = 0
    remaining_zero_year = 0
    remaining_old_fields = 0
    remaining_no_title = 0
    remaining_no_url = 0
    for art_name, art_data in articles.items():
        for mod in art_data.get("modifications", []):
            if mod.get("law_year", 0) in ABERRANT_YEARS:
                remaining_aberrant += 1
            if mod.get("law_year", 0) == 0:
                remaining_zero_year += 1
            if any(k in mod for k in ("loi_numero", "loi_type", "loi_titre")):
                remaining_old_fields += 1
            if not mod.get("law_title"):
                remaining_no_title += 1
            if not mod.get("legifrance_url"):
                remaining_no_url += 1

    print("\n=== Phase 1: Field fixes ===")
    print(f"  Aberrant years fixed:    {stats['aberrant_years_fixed']}")
    print(f"  EU law_type corrected:   {stats['eu_law_type_fixed']}")
    print(f"  JORF law_year filled:    {stats['jorf_law_year_filled']}")
    print(f"  Fields renamed:          {stats['fields_renamed']}")

    print(f"\n=== Phase 2: EU regulation type fix ===")
    print(f"  law_type corrected:      {eu_type_fixed}")

    print(f"\n=== Phase 3: Title enrichment ===")
    print(f"  Hardcoded titles:        {title_stats['hardcoded']}")
    print(f"  From lois_cgi reference: {title_stats['from_lois_cgi']}")
    print(f"  Constructed from fields: {title_stats['constructed']}")
    print(f"  Skipped (no numero):     {title_stats['skipped']}")

    print(f"\n=== Phase 4: URL enrichment ===")
    print(f"  URLs enriched:           {url_stats['enriched']}")
    print(f"  Not found in reference:  {url_stats['not_found']}")

    print(f"\n=== Validation ===")
    print(f"  Total modifications:      {total_mods_after} (unchanged: {'OK' if total_mods_before == total_mods_after else 'FAIL'})")
    print(f"  Remaining aberrant years: {remaining_aberrant} {'OK' if remaining_aberrant == 0 else 'FAIL'}")
    print(f"  Remaining law_year=0:     {remaining_zero_year} {'OK' if remaining_zero_year == 0 else 'FAIL'}")
    print(f"  Remaining old fields:     {remaining_old_fields} {'OK' if remaining_old_fields == 0 else 'FAIL'}")
    print(f"  Remaining no title:       {remaining_no_title}")
    print(f"  Remaining no URL:         {remaining_no_url}")

    if remaining_aberrant > 0 or remaining_zero_year > 0 or remaining_old_fields > 0:
        print("\nWARNING: Critical issues remain. Check data manually.")
        sys.exit(1)

    # Save
    print(f"\nSaving to {DATA_FILE}...")
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("Done.")


if __name__ == "__main__":
    main()
