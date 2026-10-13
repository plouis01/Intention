#!/usr/bin/env python3
"""Batch parse + import OCR text files into PostgreSQL.

Processes all .txt files from Google Document AI OCR output:
  1. Parse each .txt → ParsedDebat (with speaker splitting, cleaning, etc.)
  2. Validate quality (log issues)
  3. Import into local PostgreSQL as orphan debates (idempotent)

Supports both AN (legislatures 1-11) and Sénat (1958-1996) archives.

Prerequisites:
    - OCR batch completed
    - Local PostgreSQL running: legislateur_local

Usage:
    python scripts/ocr_pipeline/batch_import_ocr.py --all
    python scripts/ocr_pipeline/batch_import_ocr.py --all --dry-run
    python scripts/ocr_pipeline/batch_import_ocr.py --chambre senat --all
    python scripts/ocr_pipeline/batch_import_ocr.py --chambre senat --all --dry-run
    python scripts/ocr_pipeline/batch_import_ocr.py --all --resume
"""

import argparse
import json
import logging
import re
import sys
import time
from collections import Counter
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.db.import_data import import_orphan_debat
from src.parsers.archive_debates import parse_archive_debate_from_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

DB_URL = "postgresql://localhost/legislateur_local"
OCR_DIRS = {
    "an": Path("data/ocr_output/google-docai/an"),
    "senat": Path("data/ocr_output/google-docai/senat"),
}
COMMIT_EVERY = 100

# Quality check patterns
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_GREEK_RE = re.compile(r"[\u0370-\u03FF]")


def discover_txt_files(ocr_dir: Path, legislature: int | None = None) -> list[Path]:
    """Find all OCR .txt files to process."""
    if legislature:
        base = ocr_dir / f"leg{legislature}"
    else:
        base = ocr_dir

    if not base.exists():
        logger.error(f"Directory not found: {base}")
        return []

    # Exclude log/checkpoint files
    exclude = {"batch_processing.jsonl", "batch_checkpoint.json", "quality_report.json"}
    files = sorted(
        f for f in base.rglob("*.txt")
        if f.name not in exclude
    )
    logger.info(f"Found {len(files)} OCR text files in {base}")
    return files


def extract_metadata_an(txt_path: Path, ocr_dir: Path) -> dict:
    """Extract metadata from AN file path.

    Path structure: .../an/leg1/session_name/042.txt
    """
    rel = txt_path.relative_to(ocr_dir)
    parts = rel.parts
    session_name = parts[1] if len(parts) >= 3 else "unknown"
    page = txt_path.stem
    return {
        "session_name": session_name,
        "page": page,
        "chambre": "AN",
        "ref": f"archive_an:{session_name}/{page}",
    }


def extract_metadata_senat(txt_path: Path, ocr_dir: Path) -> dict:
    """Extract metadata from Sénat file path.

    Path structure: .../senat/1970/10/s19701002_1405_1436.txt
    """
    page = txt_path.stem  # e.g. s19701002_1405_1436
    return {
        "session_name": None,
        "page": page,
        "chambre": "SENAT",
        "ref": f"archive_senat:{page}",
    }


def check_quality(debat) -> list[dict]:
    """Run quality checks on a parsed debate. Returns list of issues."""
    issues = []
    for intv in debat.interventions:
        contenu = intv.contenu

        # too_long
        if len(contenu) > 10_000:
            issues.append({
                "type": "too_long",
                "speaker": intv.orateur,
                "chars": len(contenu),
            })

        # too_short
        if len(contenu) < 50:
            issues.append({
                "type": "too_short",
                "speaker": intv.orateur,
                "chars": len(contenu),
            })

        # artifacts
        cyr = len(_CYRILLIC_RE.findall(contenu))
        grk = len(_GREEK_RE.findall(contenu))
        if cyr > 3 or grk > 3:
            issues.append({
                "type": "artifact",
                "speaker": intv.orateur,
                "cyrillic": cyr,
                "greek": grk,
            })

    return issues


def get_session():
    """Create a DB session."""
    engine = create_engine(DB_URL)
    Session = sessionmaker(bind=engine)
    return Session()


def get_existing_references(session, chambre: str = "an") -> set[str]:
    """Get all existing archive references from DB."""
    prefix = f"archive_{chambre}:%"
    rows = session.execute(
        text("SELECT reference_externe FROM debats WHERE reference_externe LIKE :prefix"),
        {"prefix": prefix},
    ).fetchall()
    return {r[0] for r in rows}


def run_batch(
    legislature: int | None = None,
    all_legs: bool = False,
    dry_run: bool = False,
    resume: bool = False,
    chambre: str = "an",
):
    """Main batch processing."""
    ocr_dir = OCR_DIRS[chambre]

    # Discover files
    if chambre == "senat" and all_legs:
        files = discover_txt_files(ocr_dir)
    elif all_legs:
        files = []
        for leg in range(1, 12):
            files.extend(discover_txt_files(ocr_dir, legislature=leg))
    elif legislature:
        files = discover_txt_files(ocr_dir, legislature=legislature)
    else:
        logger.error("Specify --legislature N or --all")
        sys.exit(1)

    if not files:
        logger.error("No OCR text files found")
        sys.exit(1)

    # DB setup
    session = None
    existing_refs = set()
    if not dry_run:
        session = get_session()
        if resume:
            existing_refs = get_existing_references(session, chambre)
            logger.info(f"Resume: {len(existing_refs)} debates already in DB")

    total = len(files)
    logger.info(f"Processing {total} files (chambre={chambre}, dry_run={dry_run})")

    # Metadata extractor
    extract_meta = extract_metadata_senat if chambre == "senat" else extract_metadata_an

    # Stats
    parsed_ok = 0
    parsed_none = 0
    imported = 0
    skipped = 0
    issue_counts = Counter()
    all_issues = []
    total_interventions = 0
    failed_files = []
    start_time = time.time()

    for i, txt_path in enumerate(files):
        meta = extract_meta(txt_path, ocr_dir)

        # Resume check
        if resume and meta["ref"] in existing_refs:
            skipped += 1
            continue

        # Read and parse
        try:
            raw_text = txt_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to read {txt_path}: {e}")
            parsed_none += 1
            continue

        debat = parse_archive_debate_from_text(
            raw_text=raw_text,
            chambre=meta["chambre"],
            session_name=meta["session_name"],
            filename=meta["page"],
        )

        if debat is None:
            parsed_none += 1
            failed_files.append(str(txt_path.relative_to(ocr_dir)))
            logger.warning(f"Parse failed: {txt_path.relative_to(ocr_dir)}")
            continue

        parsed_ok += 1
        total_interventions += len(debat.interventions)

        # Quality checks
        issues = check_quality(debat)
        for issue in issues:
            issue_counts[issue["type"]] += 1
        if issues:
            all_issues.append({"file": str(txt_path.relative_to(ocr_dir)), "issues": issues})

        # Import to DB
        if not dry_run and session:
            try:
                import_orphan_debat(session, debat)
                imported += 1

                # Commit in batches
                if imported % COMMIT_EVERY == 0:
                    session.commit()
                    logger.info(f"  Committed batch ({imported} imported)")
            except Exception as e:
                logger.error(f"DB import failed for {txt_path.name}: {e}")
                session.rollback()

        # Progress
        if (i + 1) % 500 == 0 or (i + 1) == total:
            elapsed = time.time() - start_time
            processed = i + 1
            rate = processed / elapsed if elapsed > 0 else 0
            eta = (total - processed) / rate if rate > 0 else 0
            logger.info(
                f"[{processed}/{total}] "
                f"parsed={parsed_ok}, failed={parsed_none}, "
                f"imported={imported}, skipped={skipped} "
                f"[ETA: {eta/60:.0f}min]"
            )

    # Final commit
    if not dry_run and session:
        session.commit()
        session.close()

    # Save quality report
    report_path = ocr_dir / "quality_report.json"
    elapsed = time.time() - start_time
    report = {
        "total_files": total,
        "parsed_ok": parsed_ok,
        "parsed_none": parsed_none,
        "skipped_resume": skipped,
        "imported": imported,
        "total_interventions": total_interventions,
        "avg_interventions": round(total_interventions / max(parsed_ok, 1), 1),
        "issues": dict(issue_counts),
        "issue_rate": {
            k: f"{v / max(total_interventions, 1) * 100:.2f}%"
            for k, v in issue_counts.items()
        },
        "failed_files": failed_files,
        "elapsed_seconds": round(elapsed, 1),
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    # Summary
    print(f"\n{'=' * 60}")
    print(f"BATCH IMPORT {'(DRY RUN) ' if dry_run else ''}COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Files:              {total}")
    print(f"  Parsed OK:          {parsed_ok}")
    print(f"  Parse failed/None:  {parsed_none}")
    print(f"  Skipped (resume):   {skipped}")
    print(f"  Imported to DB:     {imported}")
    print(f"  Interventions:      {total_interventions:,}")
    print(f"  Avg intv/debate:    {total_interventions / max(parsed_ok, 1):.0f}")
    print(f"  Issues:")
    for issue_type, count in sorted(issue_counts.items()):
        pct = count / max(total_interventions, 1) * 100
        print(f"    {issue_type}: {count} ({pct:.1f}%)")
    print(f"  Time:               {elapsed / 60:.1f} min")
    print(f"  Report:             {report_path}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Batch parse + import OCR text files into PostgreSQL"
    )
    parser.add_argument("--legislature", type=int, help="Process one legislature (1-11, AN only)")
    parser.add_argument("--all", action="store_true", help="Process all")
    parser.add_argument("--chambre", type=str, default="an", choices=["an", "senat"],
                        help="Chamber (default: an)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse + validate only, no DB import")
    parser.add_argument("--resume", action="store_true",
                        help="Skip debates already in DB")
    args = parser.parse_args()

    run_batch(
        legislature=args.legislature,
        all_legs=args.all,
        dry_run=args.dry_run,
        resume=args.resume,
        chambre=args.chambre,
    )
