#!/usr/bin/env python3
"""Re-derive `article_in_law` for entries in `data/modifications_cgi_enriched.json`
where the current value is a self-reference (`Art. {CGI_num}`) or a label
spillover (>20 chars starting with `Art. `).

Reads the Légifrance cached responses in `/tmp/legifrance_cache/{LEGIARTI}.json`
and uses the ground-truth `articleNum` field from `lienModifications`. No API
calls. Dry-run by default; pass `--apply` to write back.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.backfill_missing_modifications import (
    LINKTYPE_PRIORITY,
    is_self_reference,
)

INDEX_PATH = PROJECT_ROOT / "data" / "modifications_cgi_enriched.json"
CACHE_DIR = Path("/tmp/legifrance_cache")
DEFAULT_REPORT = Path("/tmp/fix_article_in_law_report.json")

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("fix_ail")


def load_article_liens(article_ids: list[str]) -> list[dict]:
    """Read every {LEGIARTI}.json under CACHE_DIR for the given article_ids
    and return the concatenated lienModifications across versions."""
    all_liens: list[dict] = []
    for aid in article_ids:
        f = CACHE_DIR / f"{aid}.json"
        if not f.exists():
            continue
        try:
            with open(f, encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError):
            continue
        art = data.get("article") or {}
        liens = art.get("lienModifications") or []
        all_liens.extend(liens)
    return all_liens


def build_cid_to_articlenum_map(liens: list[dict]) -> dict[str, str]:
    """Build {textCid -> articleNum}. Skip self-refs and missing articleNum.
    On duplicate textCid, keep the lien with the lowest LINKTYPE_PRIORITY."""
    cid_map: dict[str, str] = {}
    cid_prio: dict[str, int] = {}
    for lien in liens:
        title = (lien.get("textTitle") or "").strip()
        if is_self_reference(title):
            continue
        cid = lien.get("textCid") or ""
        anum = lien.get("articleNum") or ""
        if not cid or not anum:
            continue
        link_type = (lien.get("linkType") or "").upper()
        prio = LINKTYPE_PRIORITY.get(link_type, 99)
        if cid not in cid_map or prio < cid_prio.get(cid, 99):
            cid_map[cid] = anum
            cid_prio[cid] = prio
    return cid_map


def categorize_change(current: str, parent_ref: str) -> str | None:
    """Return one of: 'self_ref_fixed', 'label_spillover_fixed', or None
    if the entry should be left untouched (unknown corruption pattern)."""
    if current == f"Art. {parent_ref}":
        return "self_ref_fixed"
    if len(current) > 20 and current.startswith("Art. "):
        return "label_spillover_fixed"
    return None


def fix_article(
    ref: str, article: dict, cid_map: dict[str, str], stats: dict
) -> bool:
    """Mutate article['modifications'] in place. Returns True if changed."""
    changed = False
    for mod in article.get("modifications", []):
        cid = mod.get("texte_modificateur_cid") or ""
        if not cid or cid not in cid_map:
            stats["cache_miss"] += 1
            continue
        expected = f"Art. {cid_map[cid]}"
        current = mod.get("article_in_law") or ""
        if current == expected:
            stats["unchanged"] += 1
            continue
        if not current:
            stats["skipped_null"] += 1
            continue
        category = categorize_change(current, ref)
        if category is None:
            stats["skipped_other"] += 1
            continue
        stats[category] += 1
        if len(stats["sample_changes"]) < 200:
            stats["sample_changes"].append({
                "ref": ref,
                "cid": cid,
                "before": current,
                "after": expected,
                "category": category,
            })
        mod["article_in_law"] = expected
        mod["article_in_law_fixed"] = True
        changed = True
    return changed


def run(idx: dict, only_refs: set[str] | None) -> dict:
    stats = {
        "articles_processed": 0,
        "articles_modified": 0,
        "articles_skipped_no_ids": 0,
        "self_ref_fixed": 0,
        "label_spillover_fixed": 0,
        "skipped_null": 0,
        "skipped_other": 0,
        "cache_miss": 0,
        "unchanged": 0,
        "sample_changes": [],
    }
    for ref, article in idx["articles"].items():
        if only_refs is not None and ref not in only_refs:
            continue
        stats["articles_processed"] += 1
        article_ids = article.get("article_ids") or []
        if not article_ids:
            stats["articles_skipped_no_ids"] += 1
            continue
        liens = load_article_liens(article_ids)
        cid_map = build_cid_to_articlenum_map(liens)
        if fix_article(ref, article, cid_map, stats):
            stats["articles_modified"] += 1
    return stats


def print_report(stats: dict) -> None:
    logger.info("")
    logger.info("=== fix_article_in_law report ===")
    for k in (
        "articles_processed", "articles_modified", "articles_skipped_no_ids",
        "self_ref_fixed", "label_spillover_fixed",
        "skipped_null", "skipped_other", "cache_miss", "unchanged",
    ):
        logger.info(f"  {k:30} {stats[k]}")
    logger.info(f"  sample_changes captured        {len(stats['sample_changes'])}")
    if stats["sample_changes"]:
        logger.info("")
        logger.info("First 10 sample changes:")
        for s in stats["sample_changes"][:10]:
            logger.info(
                f"  CGI {s['ref']!r:20} cid={s['cid']:24} "
                f"{s['before']!r:30} -> {s['after']!r:15} [{s['category']}]"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write changes back to INDEX_PATH (default: dry-run)")
    parser.add_argument("--refs", type=str, default=None,
                        help="Comma-separated CGI article refs to process (default: all)")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT,
                        help=f"Path to JSON report (default: {DEFAULT_REPORT})")
    args = parser.parse_args()

    only_refs = None
    if args.refs:
        only_refs = {r.strip() for r in args.refs.split(",") if r.strip()}
        logger.info(f"Restricting to {len(only_refs)} refs: {sorted(only_refs)}")

    logger.info(f"Loading index: {INDEX_PATH}")
    with open(INDEX_PATH, encoding="utf-8") as f:
        idx = json.load(f)
    logger.info(f"Articles in index: {len(idx.get('articles', {}))}")

    stats = run(idx, only_refs)
    print_report(stats)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    logger.info(f"\nReport written to: {args.report}")

    if args.apply and stats["articles_modified"] > 0:
        with open(INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=2)
        logger.info(f"\nIndex written to: {INDEX_PATH}")
    elif args.apply:
        logger.info("\nNo articles modified — index left untouched.")
    else:
        logger.info("\nDRY-RUN — index NOT written. Use --apply to persist.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
