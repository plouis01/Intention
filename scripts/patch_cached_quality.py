"""Patch cached analyses in Railway DB to fix known quality issues (no LLM calls).

Fixes applied:
1. N°N° double prefix → N°
2. Off-topic amendments removal (238 bis, 197, 220 Q)
3. Status conflicts: "rejeté" when actually "retiré" (199 terdecies-0 A + all)
4. Cross-modification citation dedup (date proximity)
5. "N citations n'ont pas pu être vérifiées" warning removal
6. Jurisprudence portée additions from article_modifications.json
7. Faible intent suppression (off-topic patterns)
8. n'porte text cleanup

Usage:
    python3 scripts/patch_cached_quality.py --dry-run   # Preview changes
    python3 scripts/patch_cached_quality.py              # Apply changes
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import psycopg2
import psycopg2.extras

DB_URL = os.environ["DATABASE_URL"]

# Target articles (all cached)
TARGET_ARTICLES = [
    "39", "150-0 A", "150-0 B ter", "150 U", "156", "197",
    "199 terdecies-0 A", "199 undecies B", "200", "219", "220 Q", "238 bis",
]


# --- Fix 1: Text cleanup ---

def fix_text_artifacts(text: str) -> str:
    """Fix N°N°, n'porte, and other text artifacts."""
    if not text:
        return text
    text = re.sub(r"N°\s*N°", "N°", text)
    text = re.sub(r"\bn'porte\b", "n'porte", text)
    text = re.sub(r"\b5bisde\b", "5 bis de", text)
    return text


# --- Fix 2: Off-topic amendment removal ---

OFF_TOPIC_RULES = {
    "238 bis": [
        r"sofipeche",
        r"sociétés d.armement à la pêche",
        r"dispositif\s+sofipeche",
        r"audiovisuel(?:les)?(?!\s+(?:de|du|des|pour|dans|sur|en)\s+(?:l['\u2019]article|238))",
        r"sociétés publiques locales.*audiovisuel",
    ],
    "197": [
        r"financer l['\u2019]audiovisuel public",
        r"contribution.*audiovisuel",
        r"audiovisuel public via",
        r"majoration.*barème.*audiovisuel",
        r"financement.*audiovisuel",
    ],
    "220 Q": [
        r"220 quaterdecies",
        r"244 quater [GO]",
        r"crédit d['\u2019]impôt.*(?:apprentissage|métiers d['\u2019]art)",
        r"contribution audiovisuelle.*(?:SMAD|TNT)",
        r"(?:suppression|modification).*article 17 ter",
    ],
}


def is_off_topic_amendment(point: str, article: str) -> bool:
    """Check if a points_vigilance entry is off-topic for the given article."""
    rules = OFF_TOPIC_RULES.get(article, [])
    for pattern in rules:
        if re.search(pattern, point, re.IGNORECASE):
            return True
    return False


# --- Fix 3: Status conflicts ---

def fix_status_conflicts(point: str) -> str:
    """Fix 'rejeté' when text says 'retiré par ses auteurs'."""
    if "retiré par ses auteurs" in point.lower() or "retiré par son auteur" in point.lower():
        point = re.sub(
            r"\)\s*rejeté\s*:",
            ") retiré:",
            point,
            flags=re.IGNORECASE,
        )
    return point


# --- Fix 4: Citation dedup by date proximity ---

def dedup_citations_by_date_proximity(sections: list[dict]) -> tuple[list[dict], int]:
    """Remove duplicate citations, keeping each in the section closest by date."""
    if len(sections) < 2:
        return sections, 0

    citation_locations: dict[tuple, list] = defaultdict(list)

    for sec_idx, section in enumerate(sections):
        mod = section.get("modification", {})
        try:
            law_date = date.fromisoformat(mod.get("date", ""))
        except (ValueError, TypeError):
            continue

        for list_name in ("sources_an", "sources_senat"):
            sources = section.get(list_name, [])
            for src_idx, source in enumerate(sources):
                citation = source.get("citation", "")
                orateur = source.get("orateur", "")
                key = (citation[:80].lower().strip(), orateur.lower().strip())
                try:
                    cite_date = date.fromisoformat(source.get("date", ""))
                    distance = abs((law_date - cite_date).days)
                except (ValueError, TypeError):
                    distance = 9999
                is_curated = source.get("is_curated", False)
                citation_locations[key].append(
                    (sec_idx, list_name, src_idx, distance, is_curated)
                )

    to_remove: set[tuple[int, str, int]] = set()
    for key, locations in citation_locations.items():
        if len(locations) < 2:
            continue
        # Prefer curated, then closest date, then first chronologically
        best = min(locations, key=lambda x: (not x[4], x[3], x[0]))
        for loc in locations:
            if loc is not best:
                to_remove.add((loc[0], loc[1], loc[2]))

    if not to_remove:
        return sections, 0

    for sec_idx, section in enumerate(sections):
        for list_name in ("sources_an", "sources_senat"):
            sources = section.get(list_name, [])
            indices = sorted(
                [i for s, l, i in to_remove if s == sec_idx and l == list_name],
                reverse=True,
            )
            for idx in indices:
                if idx < len(sources):
                    sources.pop(idx)

    return sections, len(to_remove)


# --- Fix 5: Unverified citation warning ---

def remove_unverified_warning(text: str) -> str:
    """Remove 'N citations n'ont pas pu être vérifiées' from justification."""
    if not text:
        return text
    return re.sub(
        r"\s*\(Avertissement:\s*\d+\s+citations?\s+n['\u2019]ont pas pu être vérifiées dans les sources\.\)",
        "",
        text,
    )


# --- Fix 6: Jurisprudence portée ---

def load_jurisprudence_portee() -> dict[str, dict[tuple, str]]:
    """Load portée values from article_modifications.json."""
    path = Path(__file__).parent.parent / "data" / "article_modifications.json"
    if not path.exists():
        return {}

    data = json.loads(path.read_text())
    articles = data.get("articles", {})
    result = {}

    for ref, art_data in articles.items():
        for j in art_data.get("jurisprudence", []):
            portee = j.get("portee", "")
            if not portee:
                continue
            key = (j.get("decision", ""), j.get("date", ""))
            if ref not in result:
                result[ref] = {}
            result[ref][key] = portee

    return result


def patch_jurisprudence(analysis: dict, article: str, portee_map: dict) -> int:
    """Add missing portée to jurisprudence entries. Returns count of fixes."""
    article_portees = portee_map.get(article, {})
    if not article_portees:
        return 0

    juris = analysis.get("jurisprudence", [])
    fixed = 0
    for j in juris:
        if j.get("portee"):
            continue
        key = (j.get("decision", ""), j.get("date", ""))
        if key in article_portees:
            j["portee"] = article_portees[key]
            fixed += 1
    return fixed


# --- Fix 7: Faible intent suppression ---

FAIBLE_OFF_TOPIC_PATTERNS = [
    r"aucun débat pertinent identifié",
    r"aucun extrait pertinent",
    r"aucun débat substantiel",
    r"ne (porte|concerne|traite) pas",
    r"ne concerne pas l['\u2019]article",
    r"confusion.{0,30}(cgi|code|loi)",
    r"exclusivement d['\u2019]autres sujets",
    r"ne documente pas l['\u2019]article",
    r"sans rapport.{0,20}avec",
    r"traitent exclusivement d['\u2019]autres",
    r"sans lien direct avec",
    r"sans éclairer l['\u2019]intention",
]


def maybe_suppress_faible_intent(section: dict) -> bool:
    """Suppress intent on faible sections that self-admit off-topic."""
    if section.get("niveau_certitude") != "faible":
        return False
    if not section.get("intention"):
        return False

    justification = (section.get("certitude_justification") or "").lower()
    is_off_topic = any(re.search(pat, justification) for pat in FAIBLE_OFF_TOPIC_PATTERNS)
    if not is_off_topic:
        return False

    has_sources = bool(section.get("sources_an") or section.get("sources_senat"))
    if has_sources:
        return False

    section["intention"] = ""
    section["evolution_debats"] = ""
    section["points_vigilance"] = []
    return True


# --- Main patch logic ---

def patch_analysis(analysis: dict, article: str, portee_map: dict) -> dict[str, int]:
    """Apply all patches to an analysis. Returns stats dict."""
    stats = defaultdict(int)

    sections = analysis.get("sections", [])

    for section in sections:
        # Fix 1: Text artifacts in all text fields
        for field in ("contexte", "intention", "evolution_debats", "certitude_justification"):
            original = section.get(field, "")
            fixed = fix_text_artifacts(original)
            if fixed != original:
                section[field] = fixed
                stats["text_artifacts"] += 1

        # Fix 1b: Text artifacts in points_vigilance
        pv = section.get("points_vigilance", [])
        new_pv = []
        for p in pv:
            fixed = fix_text_artifacts(p)
            if fixed != p:
                stats["text_artifacts"] += 1
            new_pv.append(fixed)
        section["points_vigilance"] = new_pv

        # Fix 1c: Text artifacts in sources
        for list_name in ("sources_an", "sources_senat"):
            for source in section.get(list_name, []):
                for field in ("citation", "orateur", "fonction"):
                    original = source.get(field, "")
                    fixed = fix_text_artifacts(original)
                    if fixed != original:
                        source[field] = fixed
                        stats["text_artifacts"] += 1

        # Fix 2: Off-topic amendments
        if article in OFF_TOPIC_RULES:
            pv = section.get("points_vigilance", [])
            filtered = [p for p in pv if not is_off_topic_amendment(p, article)]
            removed = len(pv) - len(filtered)
            if removed:
                section["points_vigilance"] = filtered
                stats["off_topic_removed"] += removed

        # Fix 3: Status conflicts
        pv = section.get("points_vigilance", [])
        for i, p in enumerate(pv):
            fixed = fix_status_conflicts(p)
            if fixed != p:
                pv[i] = fixed
                stats["status_fixed"] += 1

        # Fix 5: Unverified citation warning
        justification = section.get("certitude_justification", "")
        fixed_just = remove_unverified_warning(justification)
        if fixed_just != justification:
            section["certitude_justification"] = fixed_just
            stats["unverified_warnings_removed"] += 1

        # Fix 7: Faible intent suppression
        if maybe_suppress_faible_intent(section):
            stats["faible_suppressed"] += 1

    # Fix 4: Citation dedup
    sections, dedup_count = dedup_citations_by_date_proximity(sections)
    analysis["sections"] = sections
    stats["citations_deduped"] = dedup_count

    # Fix 6: Jurisprudence portée
    juris_fixed = patch_jurisprudence(analysis, article, portee_map)
    stats["portee_added"] = juris_fixed

    # Fix 1d: Text artifacts in synthesis
    synthesis = analysis.get("synthesis", {})
    if synthesis:
        for field in ("objectif",):
            original = synthesis.get(field, "")
            fixed = fix_text_artifacts(original)
            if fixed != original:
                synthesis[field] = fixed
                stats["text_artifacts"] += 1
        for preoc in synthesis.get("preoccupations", []):
            if isinstance(preoc, dict):
                for field in ("theme", "detail"):
                    original = preoc.get(field, "")
                    fixed = fix_text_artifacts(original)
                    if fixed != original:
                        preoc[field] = fixed
                        stats["text_artifacts"] += 1

    return dict(stats)


def main():
    parser = argparse.ArgumentParser(description="Patch cached analyses quality (no LLM)")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    parser.add_argument("--articles", nargs="*", help="Specific articles (default: all)")
    args = parser.parse_args()

    articles = args.articles or TARGET_ARTICLES
    portee_map = load_jurisprudence_portee()

    import time
    for attempt in range(3):
        try:
            conn = psycopg2.connect(DB_URL, connect_timeout=15)
            break
        except psycopg2.OperationalError:
            if attempt < 2:
                print(f"  Connection failed, retrying ({attempt + 1}/3)...")
                time.sleep(2)
            else:
                raise
    cur = conn.cursor()

    total_stats = defaultdict(int)

    for article in articles:
        cur.execute("SELECT id, analysis_json FROM analysis_cache WHERE cgi_article = %s", (article,))
        row = cur.fetchone()
        if not row:
            print(f"  {article}: not found in cache, skipping")
            continue

        cache_id, analysis = row
        stats = patch_analysis(analysis, article, portee_map)

        changes = sum(stats.values())
        if changes == 0:
            print(f"  {article}: no changes needed")
            continue

        print(f"  {article}: {changes} fixes — {dict(stats)}")
        for k, v in stats.items():
            total_stats[k] += v

        if not args.dry_run:
            cur.execute(
                "UPDATE analysis_cache SET analysis_json = %s WHERE id = %s",
                (psycopg2.extras.Json(analysis), cache_id),
            )

    print(f"\n  Total: {dict(total_stats)}")

    if args.dry_run:
        print("\n  DRY RUN — no changes written. Run without --dry-run to apply.")
        conn.rollback()
    else:
        conn.commit()
        print("\n  Changes committed to DB.")

    conn.close()


if __name__ == "__main__":
    main()
