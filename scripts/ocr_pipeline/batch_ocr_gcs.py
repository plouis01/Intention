#!/usr/bin/env python3
"""Batch OCR using Google Document AI Batch API via GCS.

Much faster than the sync API: uploads PDFs to GCS, submits batch operations
(up to 1000 docs each, 500 pages/doc), and downloads results.

Prerequisites:
    pip install -e ".[google-ocr]"
    python scripts/ocr_pipeline/setup_google_docai.py
    # Set GCS_BUCKET in .env (or use --bucket)

Usage:
    python scripts/ocr_pipeline/batch_ocr_gcs.py --all --resume
    python scripts/ocr_pipeline/batch_ocr_gcs.py --legislature 1
    python scripts/ocr_pipeline/batch_ocr_gcs.py --all --resume --bucket my-bucket
    python scripts/ocr_pipeline/batch_ocr_gcs.py --all --resume --batch-size 500
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

RAW_DIRS = {
    "an": Path("data/raw/archives_an"),
    "senat": Path("data/raw/archives_senat/senat"),
}
OUTPUT_DIRS = {
    "an": Path("data/ocr_output/google-docai/an"),
    "senat": Path("data/ocr_output/google-docai/senat"),
}
BATCH_SIZE = 200  # Documents per batch_process_documents call (smaller = more parallelism)
MAX_BATCH_SIZE = 1000  # Document AI hard limit
MAX_CONCURRENT_BATCHES = 5  # Document AI quota: max 5 concurrent batch operations per project
UPLOAD_WORKERS = 16  # Parallel GCS upload threads

# Module-level refs set by run_batch()
RAW_DIR = RAW_DIRS["an"]
OUTPUT_DIR = OUTPUT_DIRS["an"]


def discover_pdfs(legislature: int | None = None) -> list[Path]:
    """Find all PDFs to process."""
    base = RAW_DIR / f"leg{legislature}" if legislature else RAW_DIR
    if not base.exists():
        logger.error(f"Directory not found: {base}")
        return []
    pdfs = sorted(base.rglob("*.pdf"))
    logger.info(f"Found {len(pdfs)} PDFs in {base}")
    return pdfs


def output_path_for_pdf(pdf_path: Path) -> Path:
    """Compute output .txt path mirroring source structure."""
    rel = pdf_path.relative_to(RAW_DIR)
    return OUTPUT_DIR / rel.with_suffix(".txt")


def gcs_key_for_pdf(pdf_path: Path, chambre: str = "an") -> str:
    """GCS object key for a PDF, mirroring local structure."""
    rel = pdf_path.relative_to(RAW_DIR)
    return f"ocr-input/{chambre}/{rel}"


def upload_pdfs_to_gcs(bucket, pdfs: list[Path], chambre: str = "an") -> list[str]:
    """Upload PDFs to GCS in parallel. Returns list of gs:// URIs."""
    uris = []
    uploaded = 0

    def upload_one(pdf_path: Path) -> str:
        key = gcs_key_for_pdf(pdf_path, chambre=chambre)
        blob = bucket.blob(key)
        if not blob.exists():
            blob.upload_from_filename(str(pdf_path), content_type="application/pdf")
        return f"gs://{bucket.name}/{key}"

    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as executor:
        futures = {executor.submit(upload_one, pdf): pdf for pdf in pdfs}
        for future in as_completed(futures):
            uri = future.result()
            uris.append(uri)
            uploaded += 1
            if uploaded % 200 == 0:
                logger.info(f"  Uploaded {uploaded}/{len(pdfs)} PDFs to GCS")

    logger.info(f"Upload complete: {uploaded} PDFs")
    return uris


def submit_batch(
    client,
    processor_name: str,
    gcs_uris: list[str],
    output_gcs_uri: str,
):
    """Submit a batch_process_documents request. Returns the operation."""
    from google.cloud import documentai_v1 as documentai

    gcs_documents = documentai.GcsDocuments(
        documents=[
            documentai.GcsDocument(gcs_uri=uri, mime_type="application/pdf")
            for uri in gcs_uris
        ]
    )

    request = documentai.BatchProcessRequest(
        name=processor_name,
        input_documents=documentai.BatchDocumentsInputConfig(
            gcs_documents=gcs_documents,
        ),
        document_output_config=documentai.DocumentOutputConfig(
            gcs_output_config=documentai.DocumentOutputConfig.GcsOutputConfig(
                gcs_uri=output_gcs_uri,
                field_mask="text",
            ),
        ),
    )

    operation = client.batch_process_documents(request=request)
    return operation


def wait_for_operation(operation, batch_idx: int, total_batches: int):
    """Poll operation until complete, logging progress."""
    op_name = operation.operation.name
    logger.info(f"  Batch {batch_idx}/{total_batches}: operation {op_name}")

    start = time.time()
    while not operation.done():
        elapsed = time.time() - start
        logger.info(
            f"  Batch {batch_idx}/{total_batches}: "
            f"waiting... ({elapsed/60:.1f}min elapsed)"
        )
        time.sleep(30)

    elapsed = time.time() - start
    logger.info(
        f"  Batch {batch_idx}/{total_batches}: "
        f"completed in {elapsed/60:.1f}min"
    )
    return operation


def download_results(
    storage_client,
    bucket_name: str,
    output_prefix: str,
    gcs_uris: list[str],
) -> dict:
    """Download batch results from GCS and save as local .txt files.

    Returns stats dict with counts.
    """
    from google.cloud import documentai_v1 as documentai

    # Build a map from GCS input URI -> local output path
    uri_to_local = {}
    for uri in gcs_uris:
        # uri = gs://bucket/ocr-input/{chambre}/leg1/session/001.pdf
        match = re.match(r"gs://[^/]+/ocr-input/[^/]+/(.*)", uri)
        if match:
            rel = match.group(1)
            local_out = OUTPUT_DIR / Path(rel).with_suffix(".txt")
            uri_to_local[uri] = local_out

    bucket = storage_client.bucket(bucket_name)
    blobs = list(bucket.list_blobs(prefix=output_prefix))

    ok = 0
    errors = 0

    # Group blobs by their output subdirectory (one per input document)
    json_blobs = [b for b in blobs if b.name.endswith(".json")]

    for blob in json_blobs:
        try:
            content = blob.download_as_bytes()
            document = documentai.Document.from_json(content, ignore_unknown_fields=True)
            text = document.text or ""

            # Find which input this corresponds to by checking the source in metadata
            # The output structure is: output_prefix/operation_id/input_idx/output.json
            # We need to match back to input URIs
            # Use the input_gcs_source from the operation metadata instead
            # For now, we extract from the blob path structure
            parts = blob.name.replace(output_prefix, "").strip("/").split("/")
            if len(parts) >= 2:
                # parts[0] = operation_number, parts[1] = input_file_index
                input_idx = int(parts[1])
                if input_idx < len(gcs_uris):
                    local_path = uri_to_local.get(gcs_uris[input_idx])
                    if local_path and text:
                        local_path.parent.mkdir(parents=True, exist_ok=True)
                        tmp = local_path.with_suffix(".txt.tmp")
                        tmp.write_text(text, encoding="utf-8")
                        tmp.rename(local_path)
                        ok += 1
                        continue

            # Fallback: still save if we got text
            errors += 1
            logger.warning(f"Could not map result blob {blob.name} to input PDF")

        except Exception as e:
            errors += 1
            logger.error(f"Failed to process result {blob.name}: {e}")

    return {"ok": ok, "errors": errors}


DOWNLOAD_WORKERS = 32  # Parallel GCS download threads


def _download_one(bucket, status, uri_to_local):
    """Download a single OCR result from GCS. Returns (ok: bool, msg: str)."""
    from google.cloud import documentai_v1 as documentai

    input_uri = status.input_gcs_source
    output_dest = status.output_gcs_destination

    local_path = uri_to_local.get(input_uri)
    if not local_path:
        return False, f"No local mapping for {input_uri}"

    if local_path.exists() and local_path.stat().st_size > 0:
        return True, None  # already done

    if status.status.code != 0:
        return False, f"Processing failed for {input_uri}: {status.status.message}"

    try:
        match = re.match(r"gs://[^/]+/(.*)", output_dest)
        if not match:
            return False, f"Cannot parse output_dest: {output_dest}"
        prefix = match.group(1)

        text_parts = []
        for blob in bucket.list_blobs(prefix=prefix):
            if not blob.name.endswith(".json"):
                continue
            content = blob.download_as_bytes()
            document = documentai.Document.from_json(content, ignore_unknown_fields=True)
            if document.text:
                text_parts.append(document.text)

        if text_parts:
            text = "\n".join(text_parts)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = local_path.with_suffix(".txt.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.rename(local_path)
            return True, None
        else:
            return False, f"No text extracted for {input_uri}"

    except Exception as e:
        return False, f"Failed to download result for {input_uri}: {e}"


def download_results_from_metadata(
    storage_client,
    operation,
    gcs_uris: list[str],
    bucket_name: str,
) -> dict:
    """Download batch results using operation metadata for accurate mapping (parallel).

    `operation` can be a long-running Operation wrapper (from batch_process_documents)
    or an already-unpacked BatchProcessMetadata object passed via a simple wrapper with
    a `.metadata` attribute.
    """
    from google.cloud import documentai_v1 as documentai

    raw_meta = operation.metadata
    if isinstance(raw_meta, documentai.BatchProcessMetadata):
        metadata = raw_meta
    else:
        metadata = documentai.BatchProcessMetadata(raw_meta)

    if metadata.state != documentai.BatchProcessMetadata.State.SUCCEEDED:
        logger.error(f"Batch failed with state: {metadata.state_message}")
        return {"ok": 0, "errors": len(gcs_uris)}

    # Build URI -> local path map
    uri_to_local = {}
    for uri in gcs_uris:
        # uri = gs://bucket/ocr-input/{chambre}/...
        match = re.match(r"gs://[^/]+/ocr-input/[^/]+/(.*)", uri)
        if match:
            rel = match.group(1)
            local_out = OUTPUT_DIR / Path(rel).with_suffix(".txt")
            uri_to_local[uri] = local_out

    bucket = storage_client.bucket(bucket_name)
    statuses = list(metadata.individual_process_statuses)
    total = len(statuses)
    ok = 0
    errors = 0
    done = 0

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
        futures = {
            executor.submit(_download_one, bucket, s, uri_to_local): s
            for s in statuses
        }
        for future in as_completed(futures):
            success, msg = future.result()
            done += 1
            if success:
                ok += 1
            else:
                errors += 1
                if msg:
                    logger.warning(msg)
            if done % 100 == 0:
                logger.info(f"  Downloaded {done}/{total} ({ok} OK, {errors} errors)")

    return {"ok": ok, "errors": errors}


def cleanup_gcs(bucket, prefix: str):
    """Delete all blobs under a prefix."""
    blobs = list(bucket.list_blobs(prefix=prefix))
    if blobs:
        bucket.delete_blobs(blobs)
        logger.info(f"Cleaned up {len(blobs)} GCS objects under {prefix}")


def run_batch(
    legislature: int | None = None,
    all_legs: bool = False,
    resume: bool = True,
    bucket_name: str | None = None,
    batch_size: int = BATCH_SIZE,
    cleanup: bool = True,
    chambre: str = "an",
):
    """Main batch processing with GCS Batch API."""
    global RAW_DIR, OUTPUT_DIR

    from google.api_core.client_options import ClientOptions
    from google.cloud import documentai_v1 as documentai
    from google.cloud import storage

    RAW_DIR = RAW_DIRS[chambre]
    OUTPUT_DIR = OUTPUT_DIRS[chambre]

    # Config
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("DOCAI_LOCATION", "eu")
    processor_id = os.environ.get("DOCAI_PROCESSOR_ID")
    bucket_name = bucket_name or os.environ.get("GCS_BUCKET")

    if not all([project_id, processor_id, bucket_name]):
        logger.error(
            "Required env vars: GOOGLE_CLOUD_PROJECT, DOCAI_PROCESSOR_ID, GCS_BUCKET. "
            "Run: python scripts/ocr_pipeline/setup_google_docai.py"
        )
        sys.exit(1)

    # Clients
    opts = ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")
    docai_client = documentai.DocumentProcessorServiceClient(client_options=opts)
    storage_client = storage.Client(project=project_id)
    bucket = storage_client.bucket(bucket_name)

    processor_name = docai_client.processor_path(project_id, location, processor_id)

    # Discover PDFs
    if chambre == "senat" and all_legs:
        pdfs = discover_pdfs(legislature=None)
    elif all_legs:
        pdfs = []
        for leg in range(1, 12):
            pdfs.extend(discover_pdfs(legislature=leg))
    elif legislature:
        pdfs = discover_pdfs(legislature=legislature)
    else:
        logger.error("Specify --legislature N or --all")
        sys.exit(1)

    if not pdfs:
        logger.error("No PDFs found")
        sys.exit(1)

    # Resume: skip already processed
    if resume:
        pending = []
        for p in pdfs:
            out = output_path_for_pdf(p)
            if not out.exists() or out.stat().st_size == 0:
                pending.append(p)
        skipped = len(pdfs) - len(pending)
        if skipped > 0:
            logger.info(f"Resume: skipping {skipped} already processed PDFs")
        pdfs = pending

    if not pdfs:
        logger.info("All PDFs already processed!")
        return

    total = len(pdfs)
    num_batches = (total + batch_size - 1) // batch_size
    logger.info(
        f"Processing {total} PDFs in {num_batches} PARALLEL batch(es) "
        f"of up to {batch_size}"
    )

    start_time = time.time()
    total_ok = 0
    total_errors = 0

    # Split into batch chunks
    batches = []
    for batch_idx, batch_start in enumerate(range(0, total, batch_size), 1):
        batch_pdfs = pdfs[batch_start : batch_start + batch_size]
        batches.append((batch_idx, batch_pdfs))

    # Step 1: Upload ALL PDFs to GCS at once
    logger.info(f"Step 1/3: Uploading all {total} PDFs to GCS...")
    t0 = time.time()
    all_gcs_uris = upload_pdfs_to_gcs(bucket, pdfs, chambre=chambre)
    logger.info(f"  Upload done in {time.time() - t0:.0f}s")

    # Build URI list per batch (same order as pdfs)
    batch_uris = []
    for batch_idx, batch_pdfs in batches:
        start = (batch_idx - 1) * batch_size
        end = start + len(batch_pdfs)
        batch_uris.append(all_gcs_uris[start:end])

    # Step 2: Submit batches with concurrency limit, download as they complete
    logger.info(
        f"Step 2/3: Submitting {num_batches} batch operations "
        f"(max {MAX_CONCURRENT_BATCHES} concurrent)..."
    )
    # Queue of batches to submit: (batch_idx, gcs_uris)
    to_submit = list(zip(batches, batch_uris))
    # Running operations: {batch_idx: (operation, gcs_uris)}
    running = {}
    # Completed operations for download
    completed = []
    submitted_count = 0

    while to_submit or running:
        # Submit up to MAX_CONCURRENT_BATCHES
        while to_submit and len(running) < MAX_CONCURRENT_BATCHES:
            (batch_idx, batch_pdfs), gcs_uris = to_submit.pop(0)
            output_prefix = f"ocr-output/batch-{batch_idx}/"
            output_gcs_uri = f"gs://{bucket_name}/{output_prefix}"
            operation = submit_batch(
                docai_client, processor_name, gcs_uris, output_gcs_uri
            )
            op_name = operation.operation.name
            submitted_count += 1
            logger.info(
                f"  Batch {batch_idx}/{num_batches}: submitted "
                f"({len(batch_pdfs)} PDFs) — {op_name}"
            )
            running[batch_idx] = (operation, gcs_uris)

        if not running:
            break

        # Poll running operations
        time.sleep(15)
        done_ids = []
        for batch_idx, (op, gcs_uris) in running.items():
            if op.done():
                elapsed = time.time() - start_time
                logger.info(
                    f"  Batch {batch_idx}/{num_batches}: DONE "
                    f"({elapsed/60:.1f}min total)"
                )
                completed.append((batch_idx, op, gcs_uris))
                done_ids.append(batch_idx)
        for bid in done_ids:
            del running[bid]

        if running:
            elapsed = time.time() - start_time
            remaining = len(running) + len(to_submit)
            logger.info(
                f"  {len(running)} running, {len(to_submit)} queued, "
                f"{len(completed)} done ({elapsed/60:.1f}min elapsed)"
            )

    total_processing = time.time() - start_time
    logger.info(f"All {num_batches} batches completed in {total_processing/60:.1f}min")

    # Step 3: Download ALL results
    logger.info(f"Step 3/3: Downloading results from all {num_batches} batches...")
    for batch_idx, operation, gcs_uris in completed:
        output_prefix = f"ocr-output/batch-{batch_idx}/"
        t0 = time.time()
        stats = download_results_from_metadata(
            storage_client, operation, gcs_uris, bucket_name
        )
        logger.info(
            f"  Batch {batch_idx}: downloaded in {time.time() - t0:.0f}s — "
            f"{stats['ok']} OK, {stats['errors']} errors"
        )
        total_ok += stats["ok"]
        total_errors += stats["errors"]

    # Cleanup GCS
    if cleanup:
        logger.info("Cleaning up GCS objects...")
        cleanup_gcs(bucket, "ocr-input/")
        cleanup_gcs(bucket, "ocr-output/")

    # Summary
    total_time = time.time() - start_time
    print(f"\n{'='*60}")
    print("BATCH OCR COMPLETE (GCS Batch API)")
    print(f"{'='*60}")
    print(f"  PDFs processed: {total_ok + total_errors}")
    print(f"  Successful:     {total_ok}")
    print(f"  Errors:         {total_errors}")
    print(f"  Wall time:      {total_time/60:.1f} min")
    print(f"  Avg per PDF:    {total_time/max(total_ok + total_errors, 1):.1f}s")
    print(f"  Output:         {OUTPUT_DIR}")
    print(f"{'='*60}")


def resume_operation(operation_name: str, bucket_name: str | None = None):
    """Resume a running or completed batch operation and download its results."""
    from google.api_core.client_options import ClientOptions
    from google.cloud import documentai_v1 as documentai
    from google.cloud import storage

    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("DOCAI_LOCATION", "eu")
    bucket_name = bucket_name or os.environ.get("GCS_BUCKET")

    opts = ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")
    docai_client = documentai.DocumentProcessorServiceClient(client_options=opts)
    storage_client = storage.Client(project=project_id)
    bucket = storage_client.bucket(bucket_name)

    logger.info(f"Resuming operation: {operation_name}")

    # Poll until done
    while True:
        op = docai_client.get_operation({"name": operation_name})
        if op.done:
            break
        logger.info("  Still processing... waiting 30s")
        time.sleep(30)

    logger.info("Operation complete. Downloading results...")

    # Unpack the Any field — op.metadata is a google.protobuf.Any
    from google.cloud.documentai_v1.types import BatchProcessMetadata as _BPMeta
    metadata_pb = _BPMeta.pb()()  # raw protobuf message
    op.metadata.Unpack(metadata_pb)
    metadata = _BPMeta(metadata_pb)

    if metadata.state != documentai.BatchProcessMetadata.State.SUCCEEDED:
        logger.error(f"Operation failed: {metadata.state_message}")
        return

    class _OpWrapper:
        def __init__(self, meta):
            self.metadata = meta

    gcs_uris = [s.input_gcs_source for s in metadata.individual_process_statuses]
    stats = download_results_from_metadata(storage_client, _OpWrapper(metadata), gcs_uris, bucket_name)
    logger.info(f"Done: {stats['ok']} OK, {stats['errors']} errors")

    logger.info("Cleaning up GCS...")
    cleanup_gcs(bucket, "ocr-input/")
    cleanup_gcs(bucket, "ocr-output/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Batch OCR via Google Document AI Batch API (GCS)"
    )
    parser.add_argument(
        "--legislature", type=int, help="Process one legislature (1-11, AN only)"
    )
    parser.add_argument(
        "--all", action="store_true", help="Process all"
    )
    parser.add_argument(
        "--chambre", type=str, default="an", choices=["an", "senat"],
        help="Chamber (default: an)"
    )
    parser.add_argument(
        "--resume", action="store_true", default=True,
        help="Skip already processed PDFs (default: true)"
    )
    parser.add_argument(
        "--bucket", type=str, help="GCS bucket name (overrides GCS_BUCKET env var)"
    )
    parser.add_argument(
        "--batch-size", type=int, default=BATCH_SIZE,
        help=f"Documents per batch request (default: {BATCH_SIZE}, max: 1000)"
    )
    parser.add_argument(
        "--no-cleanup", action="store_true",
        help="Keep GCS objects after processing (default: cleanup)"
    )
    parser.add_argument(
        "--resume-operation", type=str, metavar="OPERATION_NAME",
        help="Resume a running/completed batch operation and download its results"
    )
    args = parser.parse_args()

    if args.resume_operation:
        resume_operation(args.resume_operation, bucket_name=args.bucket)
    else:
        run_batch(
            legislature=args.legislature,
            all_legs=args.all,
            resume=args.resume,
            bucket_name=args.bucket,
            batch_size=min(args.batch_size, MAX_BATCH_SIZE),
            cleanup=not args.no_cleanup,
            chambre=args.chambre,
        )
