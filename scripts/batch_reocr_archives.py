#!/usr/bin/env python3
"""Batch re-OCR for Assemblée Nationale historical archive PDFs.

Downloads and re-OCRs parliamentary debate PDFs from legislatures 1-11 (1958-2002)
using Tesseract 5 with column pre-splitting and hOCR footnote detection.

Uses tesserocr (C API binding) when available for faster processing by keeping
the Tesseract model loaded in memory across pages. Falls back to pytesseract
(subprocess per OCR call) if tesserocr is not installed.

Usage:
    python scripts/batch_reocr_archives.py --legislature 1          # Single legislature
    python scripts/batch_reocr_archives.py --all                    # All legislatures
    python scripts/batch_reocr_archives.py --legislature 1 --dpi 300  # Faster (lower DPI)
    python scripts/batch_reocr_archives.py --legislature 1 --download-only
    python scripts/batch_reocr_archives.py --legislature 1 --skip-download
    python scripts/batch_reocr_archives.py --legislature 1 --workers 4  # Parallel OCR
    python scripts/batch_reocr_archives.py --legislature 1 --force-pytesseract  # Benchmark
"""

import argparse
import logging
import os
import re
import statistics
import time
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

import cv2
import fitz
import numpy as np
import pytesseract
from bs4 import BeautifulSoup
from PIL import Image

from src.collectors.archive_an import ArchiveANCollector

try:
    from tesserocr import OEM, PSM, PyTessBaseAPI

    _USE_TESSEROCR = True
except ImportError:
    _USE_TESSEROCR = False

# Module-level globals for tesserocr persistent API
_tesserocr_api = None
_tessdata_dir = None  # Discovered by main process, passed to workers via initargs

# --- Constants ---

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PDF_DIR = PROJECT_ROOT / "data" / "raw" / "archives_an"
REOCR_DIR = PROJECT_ROOT / "data" / "reocr"
LEGISLATURES = list(range(1, 12))  # 1 through 11
TESSERACT_LANG = "fra"
TESSERACT_CONFIG = "--oem 1 --psm 4 -c preserve_interword_spaces=1"
DEFAULT_DPI = 400
SMALL_PDF_THRESHOLD = 15  # PDFs with <= this many pages use adaptive workers
LARGE_PDF_WORKERS = 8     # Fixed worker count for PDFs above the threshold

# Expected tessdata_best fra.traineddata size (± 10%) for validation
_TESSDATA_BEST_FRA_MIN_SIZE = 3_500_000  # ~3.5 MB (tessdata_fast is ~1.3 MB)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OCR functions — copied from scripts/test_reocr_quality.py (stable, tested)
# Adapted: process_page() uses logger.debug() instead of print()
# ---------------------------------------------------------------------------


def check_tesseract_environment():
    """Verify Tesseract version and tessdata_best model at startup.

    Raises RuntimeError if Tesseract is not found.
    """
    try:
        version = pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError:
        raise RuntimeError(
            "Tesseract not found. Install with: brew install tesseract"
        )

    if version.major < 5:
        logger.warning(f"Tesseract {version} detected. Version 5+ recommended.")

    # Check tessdata_best fra model
    tessdata_dir = os.environ.get("TESSDATA_PREFIX", "")
    if not tessdata_dir:
        for candidate in [
            "/opt/homebrew/share/tessdata",
            "/usr/share/tesseract-ocr/5/tessdata",
            "/usr/share/tessdata",
        ]:
            if Path(candidate).exists():
                tessdata_dir = candidate
                break

    if tessdata_dir:
        fra_path = Path(tessdata_dir) / "fra.traineddata"
        if fra_path.exists():
            size = fra_path.stat().st_size
            if size < _TESSDATA_BEST_FRA_MIN_SIZE:
                logger.warning(
                    f"fra.traineddata is {size / 1e6:.1f} MB — "
                    f"likely tessdata_fast, not tessdata_best. "
                    f"For best quality, install tessdata_best model."
                )
        else:
            raise RuntimeError(
                f"fra.traineddata not found in {tessdata_dir}. "
                f"Install with: curl -L -o {tessdata_dir}/fra.traineddata "
                f"https://github.com/tesseract-ocr/tessdata_best/raw/main/fra.traineddata"
            )
    else:
        raise RuntimeError(
            "Cannot locate tessdata directory. Set TESSDATA_PREFIX environment variable."
        )

    logger.info(f"Tesseract {version}, lang={TESSERACT_LANG}")


def preprocess_image(gray: np.ndarray) -> np.ndarray:
    """Preprocess a grayscale image for OCR.

    Steps: deskew -> denoise -> Otsu binarization.
    Input must be a single-channel (grayscale) numpy array.
    """

    # Deskew — uses aspect-ratio method for angle, compatible with all OpenCV 4.x+
    coords = np.column_stack(np.where(gray < 200))
    if len(coords) >= 100:
        rect = cv2.minAreaRect(coords)
        (_, (w_rect, h_rect), angle) = rect
        if w_rect < h_rect:
            angle = angle - 90
        if 0.5 < abs(angle) < 15:
            (h, w) = gray.shape[:2]
            center = (w // 2, h // 2)
            M = cv2.getRotationMatrix2D(center, angle, 1.0)
            gray = cv2.warpAffine(
                gray, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
            )

    # Denoise — medianBlur is ~10-20x faster than fastNlMeansDenoising
    # and sufficient for offset-printed parliamentary scans (low noise).
    denoised = cv2.medianBlur(gray, 3)

    # Binarize (Otsu)
    _, binary = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    return binary


def detect_and_split_columns(
    binary_image: np.ndarray, dpi: int = DEFAULT_DPI
) -> tuple[list[np.ndarray], bool]:
    """Detect gutter via vertical projection profile and split into columns.

    Analysis zone is restricted to central 10-90% of page height to exclude
    headers/footers that span full width and fill the gutter.

    Returns:
        (column_images, is_two_column)
    """
    h, w = binary_image.shape

    # Crop to central 10-90% height for analysis only
    crop_top = int(h * 0.10)
    crop_bottom = int(h * 0.90)
    analysis_zone = binary_image[crop_top:crop_bottom, :]

    # Vertical projection profile
    inverted = cv2.bitwise_not(analysis_zone)
    v_profile = np.sum(inverted, axis=0) / 255

    # Smooth
    kernel_size = max(1, w // 50)
    smoothed = np.convolve(v_profile, np.ones(kernel_size) / kernel_size, mode="same")

    # Search for gutter in central 30-70% of width
    search_start = int(w * 0.30)
    search_end = int(w * 0.70)
    center_region = smoothed[search_start:search_end]
    gutter_pos = search_start + int(np.argmin(center_region))

    # Verify it's a real gutter
    gutter_value = smoothed[gutter_pos]
    mean_density = np.mean(smoothed)

    if gutter_value < mean_density * 0.5:
        # Two-column detected — split full image
        # Scale margin with DPI (15px at 400 DPI ≈ 1mm)
        margin = max(1, int(15 * dpi / 400))
        left = binary_image[:, : gutter_pos - margin]
        right = binary_image[:, gutter_pos + margin :]
        return ([left, right], True)

    # Single column
    return ([binary_image], False)


def _extract_bbox(title_str: str) -> tuple[int, int, int, int] | None:
    """Parse hOCR title attribute to extract bbox coordinates."""
    match = re.search(r"bbox (\d+) (\d+) (\d+) (\d+)", title_str or "")
    if match:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4)))
    return None


def _extract_x_size(title_str: str) -> float | None:
    """Parse hOCR title attribute to extract x_size."""
    match = re.search(r"x_size ([\d.]+)", title_str or "")
    if match:
        return float(match.group(1))
    return None


def _clean_hocr_text(raw_text: str) -> str:
    """Clean text extracted from hOCR — rejoin chars split by newlines."""
    text = raw_text.replace("\n", " ")
    text = re.sub(r"  +", " ", text)
    return text.strip()


def ocr_column(column_image: np.ndarray) -> str:
    """Run Tesseract once per column, return hOCR HTML string."""
    pil_img = Image.fromarray(column_image)
    hocr_bytes = pytesseract.image_to_pdf_or_hocr(
        pil_img,
        lang=TESSERACT_LANG,
        config=TESSERACT_CONFIG,
        extension="hocr",
    )
    return hocr_bytes.decode("utf-8")


def _discover_tessdata_dir() -> str | None:
    """Discover tessdata directory path for tesserocr initialization."""
    tessdata_dir = os.environ.get("TESSDATA_PREFIX", "")
    if tessdata_dir:
        return tessdata_dir
    for candidate in [
        "/opt/homebrew/share/tessdata",
        "/usr/share/tesseract-ocr/5/tessdata",
        "/usr/share/tessdata",
    ]:
        if Path(candidate).exists():
            return candidate
    return None


def _init_tesserocr_worker(tessdata_dir: str | None = None, limit_omp: bool = True):
    """Initialize a persistent PyTessBaseAPI in each worker process.

    Args:
        tessdata_dir: Path to tessdata directory (discovered by main process).
        limit_omp: If True, set OMP_THREAD_LIMIT=1 to prevent OpenMP thread
            thrashing with multiple workers. Set to False for single-worker mode.

    Registers atexit cleanup for proper C resource release.
    """
    import atexit

    global _tesserocr_api
    if limit_omp:
        os.environ["OMP_THREAD_LIMIT"] = "1"
    api_kwargs = dict(
        lang=TESSERACT_LANG,
        psm=PSM.SINGLE_COLUMN,
        oem=OEM.LSTM_ONLY,
    )
    if tessdata_dir:
        api_kwargs["path"] = tessdata_dir
    _tesserocr_api = PyTessBaseAPI(**api_kwargs)
    _tesserocr_api.SetVariable("preserve_interword_spaces", "1")

    # Smoke test: verify hOCR output is parseable by lxml-xml
    _smoke_img = Image.new("L", (10, 10), 255)
    _tesserocr_api.SetImage(_smoke_img)
    _tesserocr_api.Recognize()
    _smoke_hocr = _tesserocr_api.GetHOCRText(0)
    _tesserocr_api.Clear()
    try:
        BeautifulSoup(_smoke_hocr, "lxml-xml")
    except Exception as e:
        logger.error(f"  tesserocr hOCR not parseable by lxml-xml: {e}")
        raise RuntimeError(
            "tesserocr hOCR output is incompatible with lxml-xml parser. "
            "Use --force-pytesseract as workaround."
        )

    atexit.register(_tesserocr_api.End)

    # Suppress leptonica stderr warnings (boxClipToRectangle, pixScanForForeground, etc.)
    # These are non-fatal internal warnings that pytesseract hid in subprocess stderr.
    _devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(_devnull, 2)
    os.close(_devnull)


def ocr_column_tesserocr(column_image: np.ndarray) -> str:
    """Run OCR via persistent tesserocr API, return hOCR HTML string.

    Calls Clear() after recognition to free image data and results,
    preventing memory accumulation over thousands of pages.
    """
    global _tesserocr_api
    pil_img = Image.fromarray(column_image)
    _tesserocr_api.SetImage(pil_img)
    try:
        _tesserocr_api.Recognize()
        return _tesserocr_api.GetHOCRText(0)
    finally:
        _tesserocr_api.Clear()


def extract_text_from_hocr(hocr_html: str) -> str:
    """Extract plain text from hOCR by joining ocr_line text content."""
    soup = BeautifulSoup(hocr_html, "lxml-xml")
    lines = soup.find_all("span", class_="ocr_line")
    return "\n".join(_clean_hocr_text(line.get_text()) for line in lines)


def _is_noise_line(text: str) -> bool:
    """Return True if a line is OCR noise (no real words, just punctuation/symbols)."""
    alpha_chars = re.sub(r"[^a-zA-Zàâäéèêëïîôùûüÿçœæ]", "", text)
    return len(alpha_chars) < 3


def separate_footnotes(
    hocr_html: str, column_height: int
) -> tuple[list[str], list[str]]:
    """Separate body lines from footnote lines using hOCR position and font size.

    Footnote detection uses x_size from ocr_line title attributes (NOT ocrx_word).
    Strategy: scan bottom-up from column end to find footnote block boundary.
    A footnote block starts with a marker like (1) and extends to the bottom.
    """
    try:
        soup = BeautifulSoup(hocr_html, "lxml-xml")
    except Exception:
        return ([extract_text_from_hocr(hocr_html)], [])

    lines = soup.find_all("span", class_="ocr_line")
    if not lines:
        return ([extract_text_from_hocr(hocr_html)], [])

    # Collect per-line data — x_size is on the ocr_line title, not ocrx_word
    line_data = []
    all_x_sizes = []

    for line in lines:
        text = _clean_hocr_text(line.get_text())
        line_title = line.get("title", "")
        line_bbox = _extract_bbox(line_title)
        line_x_size = _extract_x_size(line_title)

        if line_x_size is not None:
            all_x_sizes.append(line_x_size)

        line_data.append({
            "text": text,
            "bbox": line_bbox,
            "x_size": line_x_size,
        })

    # Guard: if no x_sizes found, treat all as body
    if not all_x_sizes:
        return ([ld["text"] for ld in line_data], [])

    median_size = statistics.median(all_x_sizes)
    # Footnotes are typically ~85-90% of body font size in these JO documents
    footnote_size_threshold = median_size * 0.90

    # Footnote marker patterns (superscript digits or parenthesized numbers)
    # Allow missing closing paren — OCR often drops it: "(1 Cette" instead of "(1) Cette"
    footnote_marker_re = re.compile(
        r"^\s*([¹²³⁴⁵⁶⁷⁸⁹⁰]|\(\d+\)?\s)"
    )

    # Two-pass strategy to find footnote block:
    # Pass 1: Find the highest footnote marker in the bottom 50% of the column.
    # Pass 2: Everything from that marker to the end = footnotes.
    marker_idx = None
    for idx in range(len(line_data) - 1, -1, -1):
        ld = line_data[idx]
        bbox = ld["bbox"]
        # Only consider lines in the bottom 50% of the column
        in_lower_half = bbox is not None and bbox[1] > column_height * 0.50
        if not in_lower_half:
            break  # Reached upper half, stop scanning

        if footnote_marker_re.match(ld["text"]):
            marker_idx = idx
            # Keep scanning upward — there might be a higher marker

    # If no marker found, try font-size-only detection in bottom 20%
    if marker_idx is None:
        for idx in range(len(line_data) - 1, -1, -1):
            ld = line_data[idx]
            bbox = ld["bbox"]
            in_bottom = bbox is not None and bbox[1] > column_height * 0.80
            if not in_bottom:
                break
            if ld["x_size"] is not None and ld["x_size"] < footnote_size_threshold:
                marker_idx = idx

    # Split into body and footnotes — everything from marker to end is footnote
    if marker_idx is not None:
        body_lines = [ld["text"] for ld in line_data[:marker_idx]]
        footnote_lines = [
            ld["text"] for ld in line_data[marker_idx:]
            if not _is_noise_line(ld["text"])
        ]
    else:
        body_lines = [ld["text"] for ld in line_data]
        footnote_lines = []

    return (body_lines, footnote_lines)


def process_page(
    gray_or_pil: np.ndarray | Image.Image, page_num: int, dpi: int = DEFAULT_DPI
) -> dict:
    """Process a single page: preprocess, split columns, OCR, separate footnotes.

    Accepts either a grayscale numpy array (from fitz) or a PIL RGB image (sequential mode).
    """
    if isinstance(gray_or_pil, Image.Image):
        gray = cv2.cvtColor(np.array(gray_or_pil), cv2.COLOR_RGB2GRAY)
    else:
        gray = gray_or_pil
    binary = preprocess_image(gray)
    columns, is_two_column = detect_and_split_columns(binary, dpi=dpi)

    all_body = []
    all_footnotes = []

    for column_img in columns:
        if _USE_TESSEROCR and _tesserocr_api is not None:
            hocr = ocr_column_tesserocr(column_img)
        else:
            hocr = ocr_column(column_img)
        body_lines, footnote_lines = separate_footnotes(hocr, column_img.shape[0])
        all_body.extend(body_lines)
        all_footnotes.extend(footnote_lines)

    body_text = "\n".join(all_body)
    footnote_text = "\n".join(all_footnotes)

    col_label = "2-col" if is_two_column else "1-col"
    logger.debug(
        f"  Page {page_num}: {col_label}, "
        f"{len(all_body)} body lines, {len(all_footnotes)} footnote lines"
    )

    return {
        "body": body_text,
        "footnotes": footnote_text,
        "is_two_column": is_two_column,
    }


# ---------------------------------------------------------------------------
# Multi-process page worker (top-level for pickling)
# ---------------------------------------------------------------------------


def _render_page_grayscale(pdf_path: str, page_num: int, dpi: int) -> np.ndarray:
    """Render a single PDF page to a grayscale numpy array using PyMuPDF.

    Uses fitz.Pixmap directly — no subprocess fork (unlike pdftoppm),
    and renders in grayscale to skip the RGB→gray conversion step.
    """
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_num - 1]  # 0-indexed
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        # cs=fitz.csGRAY renders directly as grayscale (1 channel)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
        gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
        return gray
    finally:
        doc.close()


def _process_page_worker(pdf_path: str, page_num: int, dpi: int) -> dict:
    """Process a single page in a worker process.

    This is a top-level function so it can be pickled by ProcessPoolExecutor.
    Each worker renders the page via PyMuPDF (in-process, no fork) then OCRs it.

    Returns dict with page_num, body, footnotes, or error info.
    """
    try:
        gray = _render_page_grayscale(pdf_path, page_num, dpi)
        result = process_page(gray, page_num, dpi=dpi)
        return {
            "page_num": page_num,
            "body": result["body"],
            "footnotes": result["footnotes"],
            "error": None,
        }
    except Exception as e:
        return {
            "page_num": page_num,
            "body": "",
            "footnotes": "",
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Discovery & download
# ---------------------------------------------------------------------------


def list_pdfs_from_url(collector: ArchiveANCollector, url: str) -> list[str]:
    """List PDF URLs from a session page using the exact URL from list_sessions().

    Workaround for ArchiveANCollector.list_pdfs() which reconstructs the URL
    as .html, causing 404s on .htm pages.
    """
    resp = collector._get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    pdf_urls = []
    seen = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if href.lower().endswith(".pdf"):
            full_url = urljoin(url, href)
            if full_url not in seen:
                seen.add(full_url)
                pdf_urls.append(full_url)

    return pdf_urls


def discover_and_download(legislatures: list[int], pdf_dir: Path) -> list[dict]:
    """Discover sessions and download PDFs for given legislatures.

    Returns list of {"legislature": int, "session": str, "pdf_path": Path, "pdf_url": str}.
    """
    all_pdfs = []

    with ArchiveANCollector(pdf_dir) as collector:
        # No fixed delay — the collector's adaptive backoff (429/503)
        # handles rate limiting automatically if the server pushes back.
        collector._delay = 0
        for leg in legislatures:
            sessions = collector.list_sessions(leg)
            logger.info(f"Legislature {leg}: {len(sessions)} sessions found")

            for session_info in sessions:
                pdf_urls = list_pdfs_from_url(collector, session_info["url"])
                logger.info(
                    f"  Session {session_info['session']}: {len(pdf_urls)} PDFs"
                )

                for url in pdf_urls:
                    filename = url.rsplit("/", 1)[-1]
                    output_path = pdf_dir / f"leg{leg}" / session_info["session"] / filename
                    try:
                        collector.download_pdf(url, output_path)
                    except Exception as e:
                        logger.error(f"  Failed to download {filename}: {e}")
                        continue

                    all_pdfs.append({
                        "legislature": leg,
                        "session": session_info["session"],
                        "pdf_path": output_path,
                        "pdf_url": url,
                    })

    logger.info(f"Total: {len(all_pdfs)} PDFs across {len(legislatures)} legislature(s)")
    return all_pdfs


# ---------------------------------------------------------------------------
# Local filesystem scan (--skip-download)
# ---------------------------------------------------------------------------


def scan_local_pdfs(legislatures: list[int], pdf_dir: Path) -> list[dict]:
    """Scan local filesystem for existing PDFs instead of scraping the website.

    Returns list of {"legislature": int, "session": str, "pdf_path": Path, "pdf_url": None}.
    """
    all_pdfs = []

    for leg in legislatures:
        leg_dir = pdf_dir / f"leg{leg}"
        if not leg_dir.exists():
            logger.warning(f"Legislature {leg}: directory not found ({leg_dir})")
            continue

        for session_dir in sorted(leg_dir.iterdir()):
            if not session_dir.is_dir() or session_dir.name.startswith("."):
                continue
            for pdf_file in sorted(session_dir.glob("*.pdf")):
                all_pdfs.append({
                    "legislature": leg,
                    "session": session_dir.name,
                    "pdf_path": pdf_file,
                    "pdf_url": None,
                })

    logger.info(f"Local scan: {len(all_pdfs)} PDFs found across {len(legislatures)} legislature(s)")
    return all_pdfs


# ---------------------------------------------------------------------------
# Resume filtering
# ---------------------------------------------------------------------------


def filter_pending(all_pdfs: list[dict], reocr_dir: Path) -> list[dict]:
    """Filter out already-processed PDFs (resume support).

    A PDF is considered processed if its _body.txt exists and has size > 0.
    Also reads page count for each pending PDF (needed for two-pass scheduling).
    """
    pending = []
    for pdf_info in all_pdfs:
        body_path = (
            reocr_dir
            / f"leg{pdf_info['legislature']}"
            / pdf_info["session"]
            / f"{pdf_info['pdf_path'].stem}_body.txt"
        )
        if not body_path.exists() or body_path.stat().st_size == 0:
            doc = fitz.open(str(pdf_info["pdf_path"]))
            pdf_info["page_count"] = len(doc)
            doc.close()
            pending.append(pdf_info)

    logger.info(f"PDFs total: {len(all_pdfs)}")
    logger.info(f"PDFs already processed (resume): {len(all_pdfs) - len(pending)}")
    logger.info(f"PDFs to process: {len(pending)}")
    return pending


# ---------------------------------------------------------------------------
# Single-PDF re-OCR with atomic write
# ---------------------------------------------------------------------------


def reocr_pdf(
    pdf_path: Path,
    output_dir: Path,
    dpi: int,
    executor: ProcessPoolExecutor | None = None,
) -> dict:
    """Re-OCR a single PDF and write body + footnotes text files.

    Uses atomic write (write to .tmp, then os.replace) to ensure resume
    reliability — partial files are never visible as completed.

    Args:
        executor: A persistent ProcessPoolExecutor for parallel page OCR.
                  If None, pages are processed sequentially.
    """
    doc = fitz.open(str(pdf_path))
    try:
        total_pages = len(doc)
    finally:
        doc.close()

    output_dir.mkdir(parents=True, exist_ok=True)

    body_path = output_dir / f"{pdf_path.stem}_body.txt"
    footnotes_path = output_dir / f"{pdf_path.stem}_footnotes.txt"
    body_tmp = output_dir / f"{pdf_path.stem}_body.txt.tmp"
    footnotes_tmp = output_dir / f"{pdf_path.stem}_footnotes.txt.tmp"

    # Clean up stale .tmp files from previous interrupted runs
    for tmp in (body_tmp, footnotes_tmp):
        if tmp.exists():
            tmp.unlink()

    all_body = [""] * total_pages
    all_footnotes = [""] * total_pages

    if executor is not None and total_pages > 1:
        # Parallel mode: submit pages to the persistent pool
        completed = 0
        futures = {
            executor.submit(
                _process_page_worker, str(pdf_path), page_idx + 1, dpi
            ): page_idx
            for page_idx in range(total_pages)
        }
        for future in as_completed(futures):
            page_idx = futures[future]
            page_num = page_idx + 1
            completed += 1
            try:
                result = future.result()
            except BrokenExecutor as e:
                logger.error(f"  Worker pool crashed: {e}")
                raise
            if result["error"]:
                logger.warning(
                    f"  Page {page_num}/{total_pages} FAILED: {result['error']}"
                )
            else:
                all_body[page_idx] = result["body"]
                all_footnotes[page_idx] = result["footnotes"]
            logger.info(f"  Page {page_num}/{total_pages} done ({completed}/{total_pages})")
    else:
        # Sequential mode
        for page_idx in range(total_pages):
            page_num = page_idx + 1
            logger.info(f"  Page {page_num}/{total_pages}")
            try:
                gray = _render_page_grayscale(str(pdf_path), page_num, dpi)
                result = process_page(gray, page_num, dpi=dpi)
                all_body[page_idx] = result["body"]
                all_footnotes[page_idx] = result["footnotes"]
                del gray
            except Exception as e:
                logger.warning(f"  Page {page_num}/{total_pages} FAILED: {e}")

    # Atomic write body
    body_text = "\n\n".join(all_body)
    body_tmp.write_text(body_text, encoding="utf-8")
    os.replace(str(body_tmp), str(body_path))

    # Atomic write footnotes (only if any found, clean up stale file otherwise)
    footnotes_text = "\n\n".join(fn for fn in all_footnotes if fn.strip())
    if footnotes_text.strip():
        footnotes_tmp.write_text(footnotes_text, encoding="utf-8")
        os.replace(str(footnotes_tmp), str(footnotes_path))
    elif footnotes_path.exists():
        footnotes_path.unlink()

    return {
        "pages": total_pages,
        "body_chars": len(body_text),
        "has_footnotes": bool(footnotes_text.strip()),
    }


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Batch re-OCR for Assemblée Nationale historical archive PDFs"
    )
    parser.add_argument(
        "--legislature", type=int, choices=range(1, 12), default=None,
        help="Specific legislature to process (1-11)",
    )
    parser.add_argument(
        "--all", action="store_true", dest="all_legislatures",
        help="Process all legislatures (1-11)",
    )
    parser.add_argument(
        "--dpi", type=int, default=DEFAULT_DPI,
        help=f"DPI for PDF to image conversion (default: {DEFAULT_DPI})",
    )
    parser.add_argument(
        "--download-only", action="store_true",
        help="Only download PDFs, skip re-OCR",
    )
    parser.add_argument(
        "--skip-download", action="store_true",
        help="Skip discovery/download, scan local filesystem for PDFs",
    )
    parser.add_argument(
        "--workers", type=int, default=LARGE_PDF_WORKERS,
        help=f"Number of parallel processes for page OCR (default: {LARGE_PDF_WORKERS}). "
             f"PDFs with <={SMALL_PDF_THRESHOLD} pages use adaptive workers (1 per page).",
    )
    parser.add_argument(
        "--force-pytesseract", action="store_true",
        help="Force pytesseract backend even if tesserocr is available (for benchmarking)",
    )
    args = parser.parse_args()

    # Validation
    if not args.legislature and not args.all_legislatures:
        parser.error("Either --legislature N or --all is required")
    if args.download_only and args.skip_download:
        parser.error("--download-only and --skip-download are mutually exclusive")

    legislatures = [args.legislature] if args.legislature else LEGISLATURES

    # Check Tesseract environment
    check_tesseract_environment()

    # Configure tesserocr backend
    global _USE_TESSEROCR, _tessdata_dir
    if args.force_pytesseract:
        _USE_TESSEROCR = False
        logger.info("  Forced pytesseract backend (--force-pytesseract)")
    if _USE_TESSEROCR:
        _tessdata_dir = _discover_tessdata_dir()
        if not _tessdata_dir:
            logger.warning("  tessdata directory not found, falling back to pytesseract")
            _USE_TESSEROCR = False

    # Sequential mode: initialize tesserocr in main process
    if _USE_TESSEROCR and args.workers <= 1:
        logger.info("  Sequential mode: initializing tesserocr in main process")
        _init_tesserocr_worker(tessdata_dir=_tessdata_dir, limit_omp=False)

    # Phase 1: Discovery & Download
    logger.info("=" * 60)
    logger.info("PHASE 1: Discovery & Download")
    logger.info("=" * 60)

    if args.skip_download:
        all_pdfs = scan_local_pdfs(legislatures, PDF_DIR)
    else:
        all_pdfs = discover_and_download(legislatures, PDF_DIR)

    if args.download_only:
        logger.info("Download-only mode. Exiting.")
        return

    # Phase 2: Re-OCR
    logger.info("=" * 60)
    logger.info("PHASE 2: Re-OCR")
    logger.info("=" * 60)

    pending = filter_pending(all_pdfs, REOCR_DIR)
    if not pending:
        logger.info("All PDFs already processed. Nothing to do.")
        return

    # Split into two batches: small PDFs (adaptive workers) and large PDFs (fixed workers)
    small_pdfs = [p for p in pending if p["page_count"] <= SMALL_PDF_THRESHOLD]
    large_pdfs = [p for p in pending if p["page_count"] > SMALL_PDF_THRESHOLD]

    if small_pdfs:
        small_max_pages = max(p["page_count"] for p in small_pdfs)
        logger.info(
            f"  Pass 1: {len(small_pdfs)} small PDFs (≤{SMALL_PDF_THRESHOLD}p), "
            f"pool of {small_max_pages} workers"
        )
    if large_pdfs:
        logger.info(
            f"  Pass 2: {len(large_pdfs)} large PDFs (>{SMALL_PDF_THRESHOLD}p), "
            f"pool of {LARGE_PDF_WORKERS} workers"
        )

    start_time = time.time()
    success_times = []
    success = 0
    errors = 0
    interrupted = False
    total_pending = len(pending)

    def _create_executor(workers):
        if _USE_TESSEROCR:
            os.environ["OMP_THREAD_LIMIT"] = "1"
            return ProcessPoolExecutor(
                max_workers=workers,
                initializer=_init_tesserocr_worker,
                initargs=(_tessdata_dir, True),
            )
        else:
            return ProcessPoolExecutor(max_workers=workers)

    def _process_batch(batch, executor, batch_offset):
        nonlocal success, errors, interrupted
        for i, pdf_info in enumerate(batch):
            pdf_start = time.time()
            pdf_path = pdf_info["pdf_path"]
            output_dir = REOCR_DIR / f"leg{pdf_info['legislature']}" / pdf_info["session"]
            global_idx = batch_offset + i

            logger.info(
                f"[{global_idx + 1}/{total_pending}] {pdf_path.name} "
                f"({pdf_info['page_count']}p, leg {pdf_info['legislature']}, "
                f"{pdf_info['session']})"
            )

            try:
                stats = reocr_pdf(pdf_path, output_dir, args.dpi, executor=executor)
                elapsed = time.time() - pdf_start
                success_times.append(elapsed)
                success += 1
                logger.info(
                    f"  Done: {stats['pages']} pages, {stats['body_chars']} chars, "
                    f"{elapsed:.1f}s"
                )
            except BrokenExecutor:
                errors += 1
                logger.error("  Pool crashed (segfault in worker). Restarting pool.")
                executor.shutdown(wait=False)
                executor = _create_executor(executor._max_workers)
            except Exception as e:
                errors += 1
                logger.error(f"  FAILED: {e}")

            # ETA after first 3 successful PDFs
            if len(success_times) >= 3:
                avg_time = sum(success_times) / len(success_times)
                remaining = total_pending - (global_idx + 1)
                eta_seconds = avg_time * remaining
                eta_h = int(eta_seconds // 3600)
                eta_m = int((eta_seconds % 3600) // 60)
                logger.info(
                    f"  ETA: {eta_h}h{eta_m:02d}m "
                    f"({remaining} remaining, avg {avg_time:.1f}s/PDF)"
                )
        return executor

    try:
        batch_offset = 0

        # Pass 1: Small PDFs with adaptive worker count
        if small_pdfs and args.workers > 1:
            small_workers = max(p["page_count"] for p in small_pdfs)
            logger.info(f"  Starting pass 1: {small_workers} workers for small PDFs")
            if _USE_TESSEROCR:
                logger.info("  Using tesserocr (CAPI binding) — persistent API per worker")
            executor = _create_executor(small_workers)
            try:
                executor = _process_batch(small_pdfs, executor, batch_offset)
            finally:
                executor.shutdown(wait=True)
            batch_offset += len(small_pdfs)
        elif small_pdfs:
            executor = None
            _process_batch(small_pdfs, executor, batch_offset)
            batch_offset += len(small_pdfs)

        # Pass 2: Large PDFs with fixed worker count
        if large_pdfs and args.workers > 1:
            logger.info(f"  Starting pass 2: {LARGE_PDF_WORKERS} workers for large PDFs")
            executor = _create_executor(LARGE_PDF_WORKERS)
            try:
                executor = _process_batch(large_pdfs, executor, batch_offset)
            finally:
                executor.shutdown(wait=True)
        elif large_pdfs:
            executor = None
            _process_batch(large_pdfs, executor, batch_offset)

    except KeyboardInterrupt:
        interrupted = True
        logger.info("")
        logger.info("Interrupted by user (Ctrl+C). Shutting down workers...")

    # Final summary
    total_elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info("INTERRUPTED" if interrupted else "COMPLETE")
    logger.info(f"  Processed: {success}/{total_pending} PDFs")
    logger.info(f"  Errors: {errors}")
    logger.info(f"  Total time: {total_elapsed / 3600:.1f}h")
    logger.info(f"  Output: {REOCR_DIR}/")
    if interrupted:
        logger.info("  Resume: re-run the same command to continue where you left off.")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
