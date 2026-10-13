#!/usr/bin/env python3
"""
POC: Archive PDF extraction end-to-end validation.

Downloads a sample of AN + Sénat archive PDFs, extracts text, parses debates,
and optionally imports into the database as orphan debates.

Usage:
    python scripts/poc_archive_extraction.py --download-only
    python scripts/poc_archive_extraction.py --parse-only
    python scripts/poc_archive_extraction.py --dry-run
    python scripts/poc_archive_extraction.py --import
"""

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Sample PDFs across different legislatures / decades
AN_SAMPLES = [
    # (legislature, session, pdf_number) — from different eras
    (1, "1958-1959-extraordinaire1", "001"),  # 1958
    (3, "1967-1968-ordinaire1", "001"),       # 1967
    (5, "1973-1974-ordinaire1", "001"),       # 1973
    (7, "1981-1982-ordinaire1", "001"),       # 1981
    (9, "1988-1989-ordinaire1", "001"),       # 1988
    (9, "1988-1989-ordinaire1", "045"),       # 1988 (different session)
    (11, "1997-1998-ordinaire1", "001"),      # 1997
    (11, "1997-1998-ordinaire1", "050"),      # 1997 (different session)
    (11, "2000-2001-ordinaire1", "001"),      # 2000
    (11, "2001-2002-ordinaire1", "001"),      # 2001
]

# Sénat samples: (year, month) — we'll grab the first PDF from each
SENAT_SAMPLE_MONTHS = [
    (1959, 1),   # 1960s
    (1970, 10),  # 1970s
    (1980, 10),  # 1980s
    (1988, 10),  # Late 1980s
    (1995, 10),  # 1990s
]


def download_samples(an_output: Path, senat_output: Path) -> tuple[list[Path], list[Path]]:
    """Download sample PDFs from both chambers."""
    from src.collectors.archive_an import ArchiveANCollector
    from src.collectors.archive_senat import ArchiveSenatCollector

    an_paths = []
    senat_paths = []

    # Download AN samples
    logger.info("=== Downloading AN archive samples ===")
    with ArchiveANCollector(an_output) as collector:
        for leg, session, number in AN_SAMPLES:
            url = f"https://archives.assemblee-nationale.fr/{leg}/cri/{session}/{number}.pdf"
            output_path = an_output / f"leg{leg}" / session / f"{number}.pdf"
            try:
                path = collector.download_pdf(url, output_path)
                an_paths.append(path)
                logger.info(f"  OK: leg {leg}, {session}, {number}")
            except Exception as e:
                logger.error(f"  FAILED: leg {leg}, {session}, {number}: {e}")

    # Download Sénat samples
    logger.info("\n=== Downloading Sénat archive samples ===")
    with ArchiveSenatCollector(senat_output) as collector:
        for year, month in SENAT_SAMPLE_MONTHS:
            try:
                pdfs = collector.list_pdfs(year, month)
                if pdfs:
                    # Grab first PDF of the month
                    info = pdfs[0]
                    filename = info["url"].rsplit("/", 1)[-1]
                    output_path = senat_output / f"senat/{year}/{month:02d}" / filename
                    path = collector.download_pdf(info["url"], output_path)
                    senat_paths.append(path)
                    logger.info(f"  OK: Sénat {year}/{month:02d} ({filename})")
                else:
                    logger.warning(f"  No PDFs found for Sénat {year}/{month:02d}")
            except Exception as e:
                logger.error(f"  FAILED: Sénat {year}/{month:02d}: {e}")

    logger.info(
        f"\nDownloaded: {len(an_paths)} AN PDFs, {len(senat_paths)} Sénat PDFs"
    )
    return an_paths, senat_paths


def find_existing_pdfs(an_output: Path, senat_output: Path) -> tuple[list[Path], list[Path]]:
    """Find already-downloaded PDFs."""
    an_paths = sorted(an_output.rglob("*.pdf"))
    senat_paths = sorted(senat_output.rglob("*.pdf"))
    return an_paths, senat_paths


def parse_samples(
    an_paths: list[Path], senat_paths: list[Path]
) -> list[tuple[Path, "ParsedDebat | None"]]:
    """Parse all sample PDFs and return results."""
    from src.parsers.archive_debates import parse_archive_debate

    results = []

    logger.info("\n=== Parsing AN PDFs ===")
    for pdf_path in an_paths:
        # Extract session name from path: leg{N}/{session}/{number}.pdf
        # session_name format: "{legislature}/{session}" e.g. "9/1988-1989-ordinaire1"
        parts = pdf_path.relative_to(pdf_path.parent.parent.parent).parts
        if len(parts) >= 2:
            leg_str = parts[0].replace("leg", "")  # "leg9" → "9"
            session_name = f"{leg_str}/{parts[1]}"
        else:
            session_name = None

        debat = parse_archive_debate(
            pdf_path,
            chambre="AN",
            url_source=None,
            session_name=session_name,
        )
        results.append((pdf_path, debat))
        _print_parse_result(pdf_path, debat)

    logger.info("\n=== Parsing Sénat PDFs ===")
    for pdf_path in senat_paths:
        debat = parse_archive_debate(
            pdf_path,
            chambre="SENAT",
            url_source=None,
            session_name=pdf_path.stem,
        )
        results.append((pdf_path, debat))
        _print_parse_result(pdf_path, debat)

    return results


def _print_parse_result(pdf_path: Path, debat) -> None:
    """Print a summary of a parse result."""
    if debat is None:
        logger.warning(f"  SKIP: {pdf_path.name} (no date or empty)")
        return

    total_articles = sum(len(i.articles_mentionnes) for i in debat.interventions)
    speakers = set(i.orateur for i in debat.interventions)
    functions = {}
    for i in debat.interventions:
        functions[i.fonction] = functions.get(i.fonction, 0) + 1

    logger.info(
        f"  {pdf_path.name}: date={debat.date.strftime('%Y-%m-%d')}, "
        f"interventions={len(debat.interventions)}, "
        f"speakers={len(speakers)}, "
        f"articles_mentioned={total_articles}, "
        f"functions={dict(functions)}"
    )

    # Print first speaker as sample
    if debat.interventions:
        first = debat.interventions[0]
        preview = first.contenu[:120].replace("\n", " ")
        logger.info(f"    First: {first.orateur} ({first.fonction}): {preview}...")


def write_parse_report_md(
    results: list[tuple[Path, "ParsedDebat | None"]], output_path: Path
) -> None:
    """Write detailed parse results to a Markdown file."""
    successful = [(p, d) for p, d in results if d is not None]
    failed = [(p, d) for p, d in results if d is None]

    total_interventions = sum(len(d.interventions) for _, d in successful)
    all_speakers = set()
    for _, d in successful:
        for i in d.interventions:
            all_speakers.add(i.orateur)
    avg_interventions = total_interventions / len(successful) if successful else 0

    lines = []
    lines.append("# POC Archive Extraction — Parse Results\n")

    # Summary table
    lines.append("## Summary\n")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total PDFs | {len(results)} |")
    lines.append(f"| Parsed OK | {len(successful)} |")
    lines.append(f"| Failed/skipped | {len(failed)} |")
    lines.append(f"| Total interventions | {total_interventions} |")
    lines.append(f"| Avg interventions/debate | {avg_interventions:.1f} |")
    lines.append(f"| Unique speakers | {len(all_speakers)} |")
    lines.append("")

    # Per-debate detail
    lines.append("## Debates\n")
    for pdf_path, debat in results:
        if debat is None:
            lines.append(f"### {pdf_path.name} — SKIPPED\n")
            lines.append("No date found or insufficient text.\n")
            continue

        speakers = set(i.orateur for i in debat.interventions)
        total_articles = sum(len(i.articles_mentionnes) for i in debat.interventions)
        functions = {}
        for i in debat.interventions:
            functions[i.fonction] = functions.get(i.fonction, 0) + 1

        lines.append(f"### {pdf_path.name}\n")
        lines.append(f"- **Chambre**: {debat.chambre}")
        lines.append(f"- **Date**: {debat.date.strftime('%Y-%m-%d')}")
        lines.append(f"- **Reference**: `{debat.reference}`")
        lines.append(f"- **Interventions**: {len(debat.interventions)}")
        lines.append(f"- **Speakers**: {len(speakers)}")
        lines.append(f"- **Articles mentioned**: {total_articles}")
        lines.append(f"- **Functions**: {functions}")
        lines.append("")

        # Interventions detail
        lines.append("| # | Speaker | Function | Content (preview) | Articles |")
        lines.append("|---|---------|----------|-------------------|----------|")
        for i in debat.interventions:
            preview = i.contenu[:150].replace("\n", " ").replace("|", "\\|")
            articles = ", ".join(i.articles_mentionnes) if i.articles_mentionnes else "-"
            lines.append(
                f"| {i.ordre} | {i.orateur} | {i.fonction} | {preview}... | {articles} |"
            )
        lines.append("")

    # Failed list
    if failed:
        lines.append("## Failed PDFs\n")
        for p, _ in failed:
            lines.append(f"- `{p.name}`")
        lines.append("")

    # Speaker list
    if all_speakers:
        lines.append("## All Speakers\n")
        for s in sorted(all_speakers):
            lines.append(f"- {s}")
        lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"Parse report written to {output_path}")


def import_to_db(results: list[tuple[Path, "ParsedDebat | None"]]) -> None:
    """Import parsed debates into the database as orphans."""
    from src.db.import_data import import_orphan_debat
    from src.db.models import Debat, get_session, init_db

    init_db()
    session = get_session()

    imported = 0
    skipped = 0

    try:
        for pdf_path, debat in results:
            if debat is None:
                skipped += 1
                continue

            db_debat = import_orphan_debat(session, debat)
            imported += 1

            if imported % 10 == 0:
                session.commit()

        session.commit()

        # Validation query
        orphan_count = session.query(Debat).filter(
            Debat.dossier_id.is_(None)
        ).count()

        logger.info(f"\n=== Import Results ===")
        logger.info(f"  Imported: {imported}")
        logger.info(f"  Skipped: {skipped}")
        logger.info(f"  Total orphan debates in DB: {orphan_count}")

    except Exception as e:
        session.rollback()
        logger.error(f"Import failed: {e}")
        raise
    finally:
        session.close()


def print_report(results: list[tuple[Path, "ParsedDebat | None"]]) -> None:
    """Print final validation report."""
    successful = [(p, d) for p, d in results if d is not None]
    failed = [(p, d) for p, d in results if d is None]

    total_interventions = sum(len(d.interventions) for _, d in successful)
    all_speakers = set()
    for _, d in successful:
        for i in d.interventions:
            all_speakers.add(i.orateur)

    avg_interventions = total_interventions / len(successful) if successful else 0

    logger.info("\n" + "=" * 70)
    logger.info("VALIDATION REPORT")
    logger.info("=" * 70)
    logger.info(f"  Total PDFs processed: {len(results)}")
    logger.info(f"  Successfully parsed:  {len(successful)}")
    logger.info(f"  Failed/skipped:       {len(failed)}")
    logger.info(f"  Total interventions:  {total_interventions}")
    logger.info(f"  Avg interventions/debate: {avg_interventions:.1f}")
    logger.info(f"  Unique speakers:      {len(all_speakers)}")

    if all_speakers:
        logger.info(f"  Sample speakers: {', '.join(sorted(all_speakers)[:10])}")

    if failed:
        logger.info(f"\n  Failed PDFs:")
        for p, _ in failed:
            logger.info(f"    - {p.name}")

    logger.info("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="POC Archive PDF Extraction")
    parser.add_argument("--download-only", action="store_true", help="Only download PDFs")
    parser.add_argument("--parse-only", action="store_true", help="Parse already-downloaded PDFs")
    parser.add_argument("--dry-run", action="store_true", help="Download + parse, no import")
    parser.add_argument("--import", dest="do_import", action="store_true", help="Full pipeline: download + parse + import")
    parser.add_argument("--an-dir", type=Path, default=Path("data/raw/archives_an"), help="AN output dir")
    parser.add_argument("--senat-dir", type=Path, default=Path("data/raw/archives_senat"), help="Sénat output dir")
    parser.add_argument("--output-md", type=Path, default=Path("data/archive_parse_report.md"), help="Markdown report output path")

    args = parser.parse_args()

    # Default to --dry-run if no flag given
    if not any([args.download_only, args.parse_only, args.dry_run, args.do_import]):
        args.dry_run = True

    # Step 1: Download
    if args.download_only or args.dry_run or args.do_import:
        an_paths, senat_paths = download_samples(args.an_dir, args.senat_dir)
        if args.download_only:
            logger.info("Download complete. Use --parse-only or --dry-run to continue.")
            return

    # Step 1b: Find existing PDFs if parse-only
    if args.parse_only:
        an_paths, senat_paths = find_existing_pdfs(args.an_dir, args.senat_dir)
        logger.info(f"Found {len(an_paths)} AN PDFs, {len(senat_paths)} Sénat PDFs")

    if not an_paths and not senat_paths:
        logger.error("No PDFs found. Run with --download-only first.")
        sys.exit(1)

    # Step 2: Parse
    results = parse_samples(an_paths, senat_paths)

    # Step 3: Report
    print_report(results)
    write_parse_report_md(results, args.output_md)

    # Step 4: Import (if requested)
    if args.do_import:
        import_to_db(results)


if __name__ == "__main__":
    main()
