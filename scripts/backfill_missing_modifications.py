#!/usr/bin/env python3
"""Back-fill missing modifications in modifications_cgi_enriched.json from
audit report. Builds each modification entry from the Légifrance lien data.

Quality rules applied:
- Exclude CGI self-references (textTitle starts with "Code général des impôts")
- Dedupe same (textCid) across linkType variants (PERIME/PEREMPTION etc.)
- Resolve dateDebutCible=2999-01-01 sentinel via dateSignaTexte or title
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime, date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

INDEX_PATH = PROJECT_ROOT / "data" / "modifications_cgi_enriched.json"
REPORT_PATH = Path("/tmp/missing_modifications_full.json")

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill")

LINK_TO_NATURE = {
    "MODIFIE": "Modification",
    "MODIFICATION": "Modification",
    "RECTIFICATION": "Modification",
    "CREATION": "Création",
    "CREE": "Création",
    "CODIFICATION": "Codification",
    "PERIME": "Abrogation",
    "PEREMPTION": "Abrogation",
    "ABROGE": "Abrogation",
    "ABROGATION": "Abrogation",
    "TRANSFERT": "Transfert",
    "TRANSFERE": "Transfert",
    "DISJOINT": "Disjoint",
    "DEPLACEMENT": "Déplacement",
    "DEPLACE": "Déplacement",
}

# When same textCid appears with multiple linkTypes, keep the most specific
LINKTYPE_PRIORITY = {
    "ABROGATION": 0, "ABROGE": 1, "PEREMPTION": 2, "PERIME": 3,
    "TRANSFERT": 4, "TRANSFERE": 5, "DISJOINT": 6,
    "MODIFICATION": 10, "MODIFIE": 11, "RECTIFICATION": 12,
    "CREATION": 20, "CREE": 21, "CODIFICATION": 22,
    "DEPLACEMENT": 30, "DEPLACE": 31,
}

MONTHS = {
    "janvier": "01", "février": "02", "mars": "03", "avril": "04",
    "mai": "05", "juin": "06", "juillet": "07", "août": "08",
    "septembre": "09", "octobre": "10", "novembre": "11", "décembre": "12",
}


def is_self_reference(textTitle: str) -> bool:
    """Detect CGI self-references which are not real modifications."""
    if not textTitle:
        return True
    t = textTitle.strip().lower()
    return t.startswith("code général des impôts")


def parse_date_safe(s: str) -> str:
    """Return ISO date if parseable and not the 2999 sentinel, else empty."""
    if not s or s.startswith("2999"):
        return ""
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except Exception:
        return ""


def extract_date_from_title(title: str) -> str:
    """Extract a date from a Légifrance lien textTitle, e.g. 'Loi 89-935 1989-12-29 ...'."""
    if not title:
        return ""
    # ISO date pattern
    m = re.search(r"(\d{4}-\d{2}-\d{2})", title)
    if m:
        try:
            return date.fromisoformat(m.group(1)).isoformat()
        except Exception:
            pass
    # French date pattern (1er janvier 1980)
    m = re.search(
        r"(\d{1,2})(?:er)?\s+(janvier|février|mars|avril|mai|juin|"
        r"juillet|août|septembre|octobre|novembre|décembre)\s+(\d{4})",
        title, re.IGNORECASE,
    )
    if m:
        day = m.group(1).zfill(2)
        month = MONTHS.get(m.group(2).lower(), "")
        year = m.group(3)
        if month:
            try:
                return date.fromisoformat(f"{year}-{month}-{day}").isoformat()
            except Exception:
                pass
    return ""


def extract_numero(title: str) -> str:
    """Extract law number from title, e.g. 'Loi 89-935 ...' -> '89-935'."""
    if not title:
        return ""
    # 'n°' or 'n ° ' followed by YYYY-NNN
    m = re.search(r"n[°o]?\s*(\d{2,4}-\d{1,5})", title)
    if m: return m.group(1)
    # Bare YYYY-NNN after Loi/Décret/Ordonnance
    m = re.search(r"(?:Loi|D[ée]cret|Ordonnance|Arr[êe]t[ée])\s+(?:n[°o]?\s*)?(\d{2,4}-\d{1,5})",
                  title, re.IGNORECASE)
    if m: return m.group(1)
    return ""


def classify_law_type(title: str, natureText: str) -> str:
    """Classify law type — title takes precedence (Légifrance's natureText is
    sometimes 'RAPPORT' for ordonnances ratifiées, etc.)."""
    t = (title or "").lower()
    # Most specific first
    if "finances rectificative" in t or "finances rect" in t or "fin de gestion" in t:
        return "LFR"
    if "financement de la sécurité sociale" in t:
        return "LFSS"
    if "finances pour" in t or "budget" in t:
        return "LF"
    # Title-based detection of nature
    if re.match(r"^\s*ordonnance", t):
        return "ORDONNANCE"
    if re.match(r"^\s*d[ée]cret", t):
        return "DECRET"
    if re.match(r"^\s*arr[êe]t[ée]", t):
        return "ARRETE"
    if re.match(r"^\s*loi\b", t):
        return "LOI"
    # Fallback: natureText
    nt = (natureText or "").upper()
    if nt == "ORDONNANCE": return "ORDONNANCE"
    if nt == "DECRET": return "DECRET"
    if nt == "LOI": return "LOI"
    if nt == "ARRETE": return "ARRETE"
    if nt and nt != "RAPPORT": return nt
    return "LOI"


def build_modification_entry(lien: dict, last_known_version_id: str = "") -> dict | None:
    """Build a modification entry from a Légifrance lien dict.
    Returns None if the lien should be excluded as noise."""
    title = (lien.get("textTitle") or "").replace("\n", " ").strip()
    if is_self_reference(title):
        return None
    cid = lien.get("textCid") or ""
    if not cid:
        return None

    # Effective date resolution priority:
    # 1. dateDebutCible (if not 2999)
    # 2. dateSignaTexte (if not 2999)
    # 3. datePubliTexte (if not 2999)
    # 4. extracted from textTitle
    effective_date = (
        parse_date_safe(lien.get("dateDebutCible", ""))
        or parse_date_safe(lien.get("dateSignaTexte", ""))
        or parse_date_safe(lien.get("datePubliTexte", ""))
        or extract_date_from_title(title)
    )
    publi_date = (
        parse_date_safe(lien.get("datePubliTexte", ""))
        or parse_date_safe(lien.get("dateSignaTexte", ""))
        or extract_date_from_title(title)
    )
    debut_vigueur = parse_date_safe(lien.get("dateDebutCible", "")) or effective_date
    if not effective_date:
        # Cannot establish a date — skip
        return None

    numero = extract_numero(title)
    annee = int(effective_date[:4]) if effective_date else 0
    law_type = classify_law_type(title, lien.get("natureText", ""))
    article_num = lien.get("articleNum") or ""
    article_in_law = f"Art. {article_num}" if article_num else ""
    nature = LINK_TO_NATURE.get((lien.get("linkType") or "").upper(), "Modification")
    legifrance_url = f"https://www.legifrance.gouv.fr/jorf/id/{cid}" if cid else ""

    entry = {
        "date": publi_date or effective_date,
        "law_numero": numero,
        "law_type": law_type,
        "law_year": annee,
        "law_title": title,
        "article_in_law": article_in_law,
        "nature": nature,
        "legifrance_url": legifrance_url,
        "objet": f"{nature} par {title}",
        "texte_modificateur_cid": cid,
        "date_debut_vigueur": debut_vigueur,
        "back_filled": True,
    }
    if last_known_version_id:
        entry["article_version_id"] = last_known_version_id
    return entry


def dedupe_liens_for_article(liens: list[dict]) -> list[dict]:
    """When multiple liens share the same textCid, keep the one with the most specific linkType."""
    best: dict[str, dict] = {}
    for l in liens:
        cid = l.get("textCid") or ""
        if not cid:
            continue
        prio = LINKTYPE_PRIORITY.get((l.get("linkType") or "").upper(), 99)
        if cid not in best or prio < LINKTYPE_PRIORITY.get(
            (best[cid].get("linkType") or "").upper(), 99
        ):
            best[cid] = l
    return list(best.values())


def backfill(idx: dict, report: dict, only_refs: list[str] | None = None,
             dry_run: bool = False) -> dict:
    """Apply back-fill to the index. Returns stats."""
    stats = {
        "articles_processed": 0,
        "articles_modified": 0,
        "entries_added": 0,
        "entries_excluded_self_ref": 0,
        "entries_excluded_no_date": 0,
        "entries_deduped": 0,
        "details": [],
    }
    target_set = set(only_refs) if only_refs else None
    for d in report["details"]:
        ref = d["ref"]
        if target_set and ref not in target_set:
            continue
        stats["articles_processed"] += 1
        article = idx["articles"].get(ref)
        if not article:
            continue

        # Dedupe missing entries by (textCid)
        liens_in = d["missing"]
        deduped = dedupe_liens_for_article(liens_in)
        stats["entries_deduped"] += len(liens_in) - len(deduped)

        # Existing CIDs in our index
        existing_cids = {m.get("texte_modificateur_cid")
                        for m in article.get("modifications", [])}

        new_entries = []
        for lien in deduped:
            if is_self_reference(lien.get("textTitle", "")):
                stats["entries_excluded_self_ref"] += 1
                continue
            if lien.get("textCid") in existing_cids:
                continue
            entry = build_modification_entry(lien)
            if entry is None:
                stats["entries_excluded_no_date"] += 1
                continue
            new_entries.append(entry)

        if not new_entries:
            continue

        stats["details"].append({
            "ref": ref,
            "etat": d["etat_actuel"],
            "added": new_entries,
        })

        if not dry_run:
            article["modifications"].extend(new_entries)
            # Sort by date
            article["modifications"].sort(key=lambda m: m.get("date", ""))
            article["nombre_modifications"] = len(article["modifications"])

        stats["articles_modified"] += 1
        stats["entries_added"] += len(new_entries)

    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refs", type=str, default="",
                        help="Comma-separated refs to back-fill (default: all)")
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't write back to the index file")
    parser.add_argument("--output-preview", type=Path,
                        default=Path("/tmp/backfill_preview.json"))
    args = parser.parse_args()

    with open(args.report) as f:
        report = json.load(f)
    with open(INDEX_PATH) as f:
        idx = json.load(f)

    only_refs = None
    if args.refs:
        only_refs = [r.strip() for r in args.refs.split(",") if r.strip()]
        logger.info(f"Limited to refs: {only_refs}")

    stats = backfill(idx, report, only_refs=only_refs, dry_run=args.dry_run)

    print()
    print("=" * 70)
    print("BACK-FILL " + ("(DRY RUN)" if args.dry_run else "(APPLIED)"))
    print("=" * 70)
    print(f"  Articles processed: {stats['articles_processed']}")
    print(f"  Articles modified:  {stats['articles_modified']}")
    print(f"  Entries added:      {stats['entries_added']}")
    print(f"  Entries deduped:    {stats['entries_deduped']}")
    print(f"  Excluded (self-ref): {stats['entries_excluded_self_ref']}")
    print(f"  Excluded (no date):  {stats['entries_excluded_no_date']}")

    # Save preview
    with open(args.output_preview, "w") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f"\nDetailed preview saved to {args.output_preview}")

    # Write back to index if not dry run
    if not args.dry_run and stats["articles_modified"] > 0:
        with open(INDEX_PATH, "w") as f:
            json.dump(idx, f, ensure_ascii=False, indent=2)
        print(f"Index updated: {INDEX_PATH}")


if __name__ == "__main__":
    main()
