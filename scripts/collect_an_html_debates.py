#!/usr/bin/env python3
"""Collect, parse and import AN HTML debates (legislatures 12-13, 2002-2012).

This script handles the complete pipeline for HTML debate data:
  --download : Download raw HTML from assemblee-nationale.fr
  --parse    : Parse downloaded HTML files (display stats)
  --import-db: Parse + import into database via import_orphan_debat()

Filter by legislature and session:
  --leg 13                   : Only legislature 13
  --session 2011-2012        : Only one session
  --leg 13 --session 2011-2012 : Specific legislature + session

No flags = print help and exit.
"""

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "raw" / "an_html"
COMMISSION_ELARGIE_YEARS = [2008, 2009, 2010, 2011]


def run_download(args):
    """Download phase: fetch raw HTML from assemblee-nationale.fr."""
    from src.collectors.an_html_debates import SESSIONS, ANHtmlCollector

    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_DIR
    legislatures = [args.leg] if args.leg else [12, 13]

    with ANHtmlCollector(output_dir) as collector:
        for leg in legislatures:
            if args.session:
                sessions = [args.session]
            else:
                sessions = SESSIONS.get(leg, [])

            for session in sessions:
                logger.info(f"Downloading leg {leg}, session {session}...")
                collector.download_session(leg, session, force=args.force)

            # Commissions élargies for leg 13
            if leg == 13 and not args.session:
                for year in COMMISSION_ELARGIE_YEARS:
                    logger.info(f"Downloading commissions élargies PLF {year}...")
                    collector.download_commissions_elargies(year)


def run_parse(args):
    """Parse phase: parse downloaded HTML and display statistics."""
    from src.parsers.an_html_debates import parse_an_html_debate

    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_DIR
    html_files = _glob_html_files(output_dir, args.leg, args.session)

    total = 0
    parsed = 0
    failed = 0

    for html_file in html_files:
        total += 1
        try:
            result = parse_an_html_debate(html_file)
            if result:
                parsed += 1
                # Count interventions per debate for stats
                n = len(result.interventions)
                logger.debug(f"{html_file.name}: {n} interventions")
            else:
                failed += 1
        except Exception as e:
            failed += 1
            logger.debug(f"Failed to parse {html_file.name}: {e}")

    print("\nParse results:")
    print(f"  Total files:  {total}")
    print(f"  Parsed:       {parsed}")
    print(f"  Skipped/fail: {failed}")


def run_import(args):
    """Import phase: parse HTML files and import into database."""
    from src.db.import_data import import_orphan_debat
    from src.db.models import get_session
    from src.parsers.an_html_debates import parse_an_html_debate

    output_dir = Path(args.output_dir) if args.output_dir else DEFAULT_OUTPUT_DIR
    html_files = _glob_html_files(output_dir, args.leg, args.session)

    db_session = get_session()
    total = 0
    imported = 0
    skipped = 0
    failed = 0

    try:
        for html_file in html_files:
            total += 1
            try:
                parsed = parse_an_html_debate(html_file)
                if parsed and parsed.interventions:
                    import_orphan_debat(db_session, parsed)
                    imported += 1
                    if imported % 50 == 0:
                        db_session.commit()
                        logger.info(f"Imported {imported} debates...")
                else:
                    skipped += 1
            except Exception as e:
                db_session.rollback()
                failed += 1
                logger.debug(f"Failed to import {html_file.name}: {e}")
                continue

        db_session.commit()
    finally:
        db_session.close()

    print("\nImport results:")
    print(f"  Total files: {total}")
    print(f"  Imported:    {imported}")
    print(f"  Skipped:     {skipped}")
    print(f"  Failed:      {failed}")


def _glob_html_files(output_dir: Path, leg: int | None, session: str | None) -> list[Path]:
    """Glob HTML files based on filters."""
    if leg and session:
        pattern_dir = output_dir / f"leg{leg}" / session
        files = sorted(pattern_dir.glob("*.html")) if pattern_dir.exists() else []
    elif leg:
        pattern_dir = output_dir / f"leg{leg}"
        files = sorted(pattern_dir.rglob("*.html")) if pattern_dir.exists() else []
    else:
        files = sorted(output_dir.rglob("*.html")) if output_dir.exists() else []
    return files


def main():
    parser = argparse.ArgumentParser(
        description="Collect, parse and import AN HTML debates (legislatures 12-13)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--download", action="store_true", help="Download raw HTML files")
    parser.add_argument("--parse", action="store_true", help="Parse downloaded HTML (stats only)")
    parser.add_argument("--import-db", action="store_true", help="Parse + import into database")
    parser.add_argument("--leg", type=int, choices=[12, 13], help="Filter by legislature")
    parser.add_argument("--session", type=str, help="Filter by session name")
    parser.add_argument(
        "--output-dir", type=str, help="Output directory (default: data/raw/an_html)"
    )
    parser.add_argument("--force", action="store_true", help="Re-download existing files")

    args = parser.parse_args()

    if not any([args.download, args.parse, args.import_db]):
        parser.print_help()
        sys.exit(0)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.download:
        print("=== DOWNLOAD PHASE ===")
        run_download(args)

    if args.parse:
        print("\n=== PARSE PHASE ===")
        run_parse(args)

    if args.import_db:
        print("\n=== IMPORT PHASE ===")
        run_import(args)


if __name__ == "__main__":
    main()
