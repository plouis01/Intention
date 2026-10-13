#!/usr/bin/env python3
"""Audit missing modifications in modifications_cgi_enriched.json by comparing
with Légifrance lienModifications for every article_id of every article.

Caches each getArticle response on disk so reruns are incremental. Outputs:
- /tmp/legifrance_cache/{LEGIARTI_id}.json   (per-id cache)
- /tmp/missing_modifications_report.json     (final report)
"""

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.fetch_article_history import LegifranceCGIClient

CACHE_DIR = Path("/tmp/legifrance_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
INDEX_PATH = PROJECT_ROOT / "data" / "modifications_cgi_enriched.json"
REPORT_PATH = Path("/tmp/missing_modifications_report.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("scripts.fetch_article_history").setLevel(logging.WARNING)
logger = logging.getLogger("audit")


LINK_TO_NATURE = {
    "MODIFIE": "Modification",
    "MODIFICATION": "Modification",
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


def load_or_fetch(client: LegifranceCGIClient, aid: str) -> dict | None:
    """Load article data from cache, or fetch from Légifrance and cache it."""
    cache_path = CACHE_DIR / f"{aid}.json"
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                return json.load(f)
        except Exception:
            cache_path.unlink(missing_ok=True)
    try:
        data = client.get_article_by_id(aid)
        if data:
            with open(cache_path, "w") as f:
                json.dump(data, f, ensure_ascii=False)
        return data
    except Exception as e:
        logger.debug(f"fetch failed for {aid}: {e}")
        return None


def fetch_all_for_article(client: LegifranceCGIClient, article_ids: list[str]) -> list[dict]:
    """Fetch all version data for an article."""
    versions = []
    for aid in article_ids:
        data = load_or_fetch(client, aid)
        if data:
            art = data.get("article", {})
            versions.append({
                "id": aid,
                "dateDebut": art.get("dateDebut"),
                "dateFin": art.get("dateFin"),
                "etat": art.get("etat"),
                "liens": art.get("lienModifications", []),
            })
    return versions


def fetch_with_retry(client: LegifranceCGIClient, aid: str, max_retries: int = 3) -> bool:
    """Fetch one article with retry on rate-limit. Returns True on success."""
    cache_path = CACHE_DIR / f"{aid}.json"
    if cache_path.exists():
        return True
    for attempt in range(max_retries):
        try:
            data = client.get_article_by_id(aid)
            if data:
                with open(cache_path, "w") as f:
                    json.dump(data, f, ensure_ascii=False)
                return True
            return False
        except Exception as e:
            msg = str(e)
            if "401" in msg or "429" in msg or "403" in msg:
                time.sleep(2 ** attempt)
                continue
            logger.debug(f"err {aid}: {e}")
            return False
    return False


def build_expected(versions: list[dict]) -> dict[tuple, dict]:
    """Build the set of expected modifications keyed by (textCid, linkType).

    A single modification often appears on multiple versions (the start of v2
    is the end of v1), so we dedupe.
    """
    expected: dict[tuple, dict] = {}
    for v in versions:
        for lien in v.get("liens", []):
            cid = lien.get("textCid", "")
            ltype = (lien.get("linkType") or "").upper()
            if not cid or not ltype:
                continue
            key = (cid, ltype)
            if key not in expected:
                expected[key] = {
                    "textCid": cid,
                    "linkType": ltype,
                    "textTitle": (lien.get("textTitle") or "").replace("\n", " ").strip(),
                    "natureText": lien.get("natureText") or "",
                    "dateDebutCible": lien.get("dateDebutCible") or "",
                    "datePubliTexte": lien.get("datePubliTexte") or "",
                    "dateSignaTexte": lien.get("dateSignaTexte") or "",
                    "articleNum": lien.get("articleNum") or "",
                }
    return expected


def build_actual(mods: list[dict]) -> set[str]:
    """Build the set of cids present in the article's modifications array."""
    cids = set()
    for m in mods:
        cid = m.get("texte_modificateur_cid")
        if cid:
            cids.add(cid)
    return cids


def audit_articles(refs: list[str], idx: dict, workers: int = 4) -> dict:
    """Run audit on the given list of article refs."""
    articles = idx["articles"]

    # Collect all unique IDs to fetch first
    needed_ids = set()
    for ref in refs:
        for aid in articles[ref].get("article_ids", []):
            needed_ids.add(aid)
    needed_ids -= {p.stem for p in CACHE_DIR.glob("*.json")}
    logger.info(f"IDs to fetch: {len(needed_ids)} (cache hits = {len(refs) and 'on'})")

    # Parallel fetch with shared client
    if needed_ids:
        client = LegifranceCGIClient()
        try:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(fetch_with_retry, client, aid): aid
                          for aid in needed_ids}
                done = 0
                for fut in as_completed(futures):
                    done += 1
                    if done % 200 == 0:
                        logger.info(f"  fetched {done}/{len(needed_ids)}")
        finally:
            client.close()
        logger.info(f"Fetch complete: {done if needed_ids else 0} new")

    # Now compare locally for each article
    report = {"audited_at": datetime.now().isoformat(),
              "total_audited": 0, "articles_with_missing": 0,
              "missing_by_link_type": {}, "missing_by_nature_text": {},
              "details": []}

    for ref in refs:
        data = articles[ref]
        # Build expected from cache
        versions = []
        for aid in data.get("article_ids", []):
            cache_path = CACHE_DIR / f"{aid}.json"
            if not cache_path.exists():
                continue
            try:
                with open(cache_path) as f:
                    d = json.load(f)
                a = d.get("article", {})
                versions.append({
                    "id": aid,
                    "dateDebut": a.get("dateDebut"),
                    "dateFin": a.get("dateFin"),
                    "etat": a.get("etat"),
                    "liens": a.get("lienModifications", []),
                })
            except Exception:
                continue
        expected = build_expected(versions)
        actual = build_actual(data.get("modifications", []))

        # Missing = expected cids not in actual
        missing = [v for (cid, lt), v in expected.items() if cid not in actual]
        report["total_audited"] += 1
        if missing:
            report["articles_with_missing"] += 1
            for m in missing:
                lt = m["linkType"]
                nt = m["natureText"] or "?"
                report["missing_by_link_type"][lt] = report["missing_by_link_type"].get(lt, 0) + 1
                report["missing_by_nature_text"][nt] = report["missing_by_nature_text"].get(nt, 0) + 1
            report["details"].append({
                "ref": ref,
                "etat_actuel": data.get("etat_actuel"),
                "n_versions": len(versions),
                "n_modifications_index": len(data.get("modifications", [])),
                "n_missing": len(missing),
                "missing": missing,
            })

    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=0,
                        help="Audit only N first articles (0 = all)")
    parser.add_argument("--refs", type=str, default="",
                        help="Comma-separated list of article refs to audit")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, default=REPORT_PATH)
    args = parser.parse_args()

    with open(INDEX_PATH) as f:
        idx = json.load(f)

    if args.refs:
        refs = [r.strip() for r in args.refs.split(",") if r.strip()]
    elif args.sample:
        refs = list(idx["articles"].keys())[:args.sample]
    else:
        refs = list(idx["articles"].keys())

    logger.info(f"Auditing {len(refs)} articles")
    report = audit_articles(refs, idx, workers=args.workers)

    with open(args.output, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print()
    print("=" * 70)
    print("AUDIT REPORT")
    print("=" * 70)
    print(f"  Audited: {report['total_audited']}")
    print(f"  With missing modifications: {report['articles_with_missing']} "
          f"({100*report['articles_with_missing']/max(1,report['total_audited']):.1f}%)")
    print("\n  Missing by linkType:")
    for k, v in sorted(report["missing_by_link_type"].items(), key=lambda kv: -kv[1]):
        print(f"    {k:15s}: {v}")
    print("\n  Missing by natureText:")
    for k, v in sorted(report["missing_by_nature_text"].items(), key=lambda kv: -kv[1]):
        print(f"    {k:15s}: {v}")
    print(f"\nFull report saved to {args.output}")


if __name__ == "__main__":
    main()
