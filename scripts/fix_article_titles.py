#!/usr/bin/env python3
"""Fix 3 quality issues in article titles in article_modifications.json.

1. 19 articles with empty titre  → fetch sectionParentTitre from API
2. 44 titles in ALL CAPS          → normalize to proper French casing
3. 14 titles with unstripped Roman numeral prefix (VII ter :, etc.)
   → strip prefix in-place

Usage:
    python scripts/fix_article_titles.py --dry-run
    python scripts/fix_article_titles.py
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx

from scripts.collect_cgi_laws import LegifranceCodeClient

INDEX_PATH = PROJECT_ROOT / "data" / "article_modifications.json"
TOC_CACHE_FILE = PROJECT_ROOT / "data" / "cgi_toc_cache.json"

# Mapping for French words that lose accents when uppercased in old CGI sections
_ACCENT_FIX = {
    "D'IMPOTS": "d'impôts",
    "L'IMPOT": "l'impôt",
    "IMPOTS": "impôts",
    "DEGREVEMENTS": "dégrèvements",
    "PENALITES": "pénalités",
    "PARTICULIERES": "particulières",
    "PROCEDURES": "procédures",
}

# Words to keep lowercase in French title case (articles, prepositions)
_LOWERCASE_WORDS = {"de", "du", "d'", "des", "le", "la", "les", "l'", "et", "ou", "en", "au", "aux"}


def normalize_caps_title(title: str) -> str:
    """Convert an ALL-CAPS CGI section title to proper French casing.

    Applies accent corrections for words that lose diacritics in uppercase,
    then converts to sentence case (first word capitalized, rest lowercase
    except proper nouns implied by the structure).
    """
    t = title.rstrip(".")

    # Apply accent fixes before lowercasing (order matters: longer phrases first)
    for caps, fixed in sorted(_ACCENT_FIX.items(), key=lambda x: -len(x[0])):
        t = t.replace(caps, fixed)

    # Split on " - " separators, normalize each segment
    segments = t.split(" - ")
    normalized = []
    for seg in segments:
        seg = seg.strip()
        words = seg.split()
        if not words:
            continue
        # Capitalize first word, lowercase the rest (except after accent fixes)
        result = []
        for i, w in enumerate(words):
            if i == 0:
                # Always capitalize first word, even if it came from accent fix
                result.append(w.capitalize())
            elif w in _ACCENT_FIX.values():
                # Mid-sentence accent-fixed words: keep as-is (already properly cased)
                result.append(w)
            elif w.lower() in _LOWERCASE_WORDS:
                result.append(w.lower())
            else:
                result.append(w.lower())
        normalized.append(" ".join(result))

    return " - ".join(normalized)


def clean_section_parent(sp: str) -> str:
    """Clean sectionParentTitre to use as article title fallback."""
    if not sp:
        return ""
    # Strip numeric/roman prefixes like "1 : ", "III : ", "3° : "
    cleaned = re.sub(
        r"^(Section\s+[IVX\d]+\s*:\s*|\d+[°\w]*\s*:\s*|[IVX]+\s*:\s*|[A-Z]\s*:\s*)",
        "",
        sp.strip(),
        flags=re.IGNORECASE,
    ).strip().rstrip(".")
    if not cleaned:
        return sp.strip().rstrip(".")
    # If still all-caps, normalize
    if cleaned == cleaned.upper() and len(cleaned) > 3:
        cleaned = normalize_caps_title(cleaned)
    return cleaned


def strip_roman_prefix(title: str) -> str:
    """Strip unclean Roman numeral prefixes like 'VII ter : ' from titles."""
    return re.sub(
        r"^[IVX]+(\s+\w+)?\s*:\s*",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()


def main():
    parser = argparse.ArgumentParser(description="Fix article title quality issues")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(INDEX_PATH, encoding="utf-8") as f:
        data = json.load(f)
    arts = data["articles"]

    with open(TOC_CACHE_FILE, encoding="utf-8") as f:
        toc = json.load(f)
    toc_by_num = {a["num"]: a["id"] for a in toc if a.get("num") and a.get("id")}

    # --- Category 1: empty titres ---
    no_titre = [k for k, v in arts.items() if not v.get("titre", "").strip()]
    print(f"Category 1: {len(no_titre)} articles without titre → fetch sectionParentTitre")

    # --- Category 2: ALL CAPS titres ---
    all_caps = [
        k for k, v in arts.items()
        if v.get("titre", "") and v["titre"] == v["titre"].upper() and len(v["titre"]) > 5
    ]
    print(f"Category 2: {len(all_caps)} ALL CAPS titles → normalize casing")

    # --- Category 3: unstripped Roman prefix ---
    roman_prefix = [
        k for k, v in arts.items()
        if v.get("titre", "") and re.match(r"^(VII|VIII|IX|VI|XI)\s", v.get("titre", ""))
    ]
    print(f"Category 3: {len(roman_prefix)} Roman prefix titles → strip prefix")

    fixes = {"cat1": 0, "cat2": 0, "cat3": 0}

    # --- Fix category 2 (no API needed) ---
    for num in all_caps:
        old = arts[num]["titre"]
        new = normalize_caps_title(old)
        if new and new != old:
            if args.dry_run:
                print(f"  [CAT2] {num}: {repr(old[:60])} → {repr(new[:60])}")
            else:
                arts[num]["titre"] = new
            fixes["cat2"] += 1

    # --- Fix category 3 (no API needed) ---
    for num in roman_prefix:
        old = arts[num]["titre"]
        new = strip_roman_prefix(old)
        if new and new != old:
            if args.dry_run:
                print(f"  [CAT3] {num}: {repr(old[:80])} → {repr(new[:80])}")
            else:
                arts[num]["titre"] = new
            fixes["cat3"] += 1

    # --- Fix category 1 (API calls) ---
    print(f"\nFetching sectionParentTitre for {len(no_titre)} articles...")
    with LegifranceCodeClient() as client:
        for num in no_titre:
            if num not in toc_by_num:
                print(f"  {num}: not in TOC, skipping")
                continue
            try:
                resp = client.get_article(toc_by_num[num])
            except httpx.HTTPStatusError:
                continue
            art_data = resp.get("article", {})
            sp = art_data.get("sectionParentTitre") or ""
            titre = clean_section_parent(sp)
            if titre:
                if args.dry_run:
                    print(f"  [CAT1] {num}: {repr(sp[:60])} → {repr(titre[:60])}")
                else:
                    arts[num]["titre"] = titre
                fixes["cat1"] += 1
            time.sleep(0.1)

    print(f"\nFixes applied: cat1={fixes['cat1']}, cat2={fixes['cat2']}, cat3={fixes['cat3']}")
    total = sum(fixes.values())
    print(f"Total: {total} titles fixed")

    if args.dry_run:
        print("Dry run — no files modified.")
        return 0

    # Verify post-2002 count unchanged
    post = sum(
        1 for a in arts.values()
        for m in a.get("modifications", [])
        if m.get("date", "") >= "2002-01-01"
    )
    assert post == 9557, f"Post-2002 count changed: {post}"

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Saved to {INDEX_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
