#!/usr/bin/env python3
"""
Re-OCR quality test for French National Assembly archive PDFs.

Compares baseline text (PyMuPDF pre-existing OCR layer) with fresh re-OCR
using column pre-splitting and footnote detection.

Uses Tesseract 5 with hOCR-based footnote detection.

Usage:
    python scripts/test_reocr_quality.py                              # Full document
    python scripts/test_reocr_quality.py --pages 1-5                  # Pages 1-5 only
    python scripts/test_reocr_quality.py --pages 2,4-5                # Specific pages
    python scripts/test_reocr_quality.py --dpi 300                    # Lower DPI (faster)
"""

import argparse
import difflib
import os
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

import cv2
import fitz
import httpx
import numpy as np
import pytesseract
from bs4 import BeautifulSoup
from pdf2image import convert_from_path
from PIL import Image

# --- Constants ---

DEFAULT_PDF_URL = (
    "https://archives.assemblee-nationale.fr/7/cri/1981-1982-ordinaire1/001.pdf"
)
DEFAULT_OUTPUT_DIR = "data/raw/reocr_test"
DEFAULT_DPI = 400
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}
TESSERACT_LANG = "fra"
TESSERACT_CONFIG = "--oem 1 --psm 4 -c preserve_interword_spaces=1"

# Expected tessdata_best fra.traineddata size (± 10%) for validation
_TESSDATA_BEST_FRA_MIN_SIZE = 3_500_000  # ~3.5 MB (tessdata_fast is ~1.3 MB)


def check_tesseract_environment():
    """Verify Tesseract version and tessdata_best model at startup."""
    # Check Tesseract version
    try:
        version = pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError:
        print("ERROR: Tesseract not found. Install with: brew install tesseract")
        sys.exit(1)

    if version.major < 5:
        print(f"WARNING: Tesseract {version} detected. Version 5+ recommended.")

    # Check tessdata_best fra model
    tessdata_dir = os.environ.get("TESSDATA_PREFIX", "")
    if not tessdata_dir:
        # Try common locations
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
                print(
                    f"WARNING: fra.traineddata is {size / 1e6:.1f} MB — "
                    f"likely tessdata_fast, not tessdata_best. "
                    f"For best quality, install tessdata_best model."
                )
        else:
            print(
                f"WARNING: fra.traineddata not found in {tessdata_dir}. "
                f"Install with: curl -L -o {tessdata_dir}/fra.traineddata "
                f"https://github.com/tesseract-ocr/tessdata_best/raw/main/fra.traineddata"
            )

    print(f"  Tesseract {version}, lang={TESSERACT_LANG}")


# --- Task 2: Core structure, PDF download, baseline extraction ---


def parse_page_range(pages_str: str | None, total_pages: int) -> list[int]:
    """Parse --pages argument into 0-indexed page indices.

    Args:
        pages_str: Comma-separated parts, each being a single page or range.
            Examples: "5", "1-10", "2,4-5", "1,3,7-10". None for all pages.
        total_pages: Total number of pages in the PDF.

    Returns:
        List of 0-indexed page indices (sorted, deduplicated).

    Raises:
        ValueError: If page numbers are out of range or invalid.
    """
    if pages_str is None:
        return list(range(total_pages))

    indices = []
    try:
        for part in pages_str.split(","):
            part = part.strip()
            if "-" in part:
                start, end = part.split("-", 1)
                indices.extend(range(int(start) - 1, int(end)))
            else:
                indices.append(int(part) - 1)
    except ValueError:
        raise ValueError(
            f"Invalid page range: '{pages_str}'. Use '5', '1-10', or '2,4-5'."
        )

    indices = sorted(set(indices))

    if not indices:
        raise ValueError(f"Empty page range: '{pages_str}'. Start must be <= end.")

    for idx in indices:
        if idx < 0 or idx >= total_pages:
            raise ValueError(
                f"Page {idx + 1} out of range (PDF has {total_pages} pages)."
            )

    return indices


def download_pdf(url: str, output_dir: Path) -> Path:
    """Download PDF from URL, skip if already exists."""
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"Invalid URL scheme: {url}")
    pdf_path = output_dir / "original.pdf"
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        print(f"  PDF already downloaded: {pdf_path}")
        return pdf_path

    print(f"  Downloading {url}...")
    with httpx.Client(headers=HEADERS, timeout=60.0, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        pdf_path.write_bytes(response.content)
    print(f"  Saved to {pdf_path} ({pdf_path.stat().st_size / 1024:.0f} KB)")
    return pdf_path


def extract_baseline_text(pdf_path: Path, page_indices: list[int]) -> list[str]:
    """Extract text via PyMuPDF get_text() as baseline for comparison."""
    doc = fitz.open(str(pdf_path))
    try:
        pages = []
        for idx in page_indices:
            page = doc[idx]
            pages.append(page.get_text())
        return pages
    finally:
        doc.close()


# --- Task 3: Image preprocessing ---


def preprocess_image(pil_image: Image.Image) -> np.ndarray:
    """Convert PIL image to preprocessed binary numpy array for OCR.

    Steps: grayscale -> deskew -> denoise -> Otsu binarization.
    """
    img = np.array(pil_image)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)

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

    # Denoise
    denoised = cv2.fastNlMeansDenoising(gray, h=10)

    # Binarize (Otsu)
    _, binary = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    return binary


# --- Task 4: Column detection and splitting ---


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


# --- Task 5: Tesseract OCR with hOCR footnote detection ---


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


def extract_text_from_hocr(hocr_html: str) -> str:
    """Extract plain text from hOCR by joining ocr_line text content."""
    soup = BeautifulSoup(hocr_html, "lxml-xml")
    lines = soup.find_all("span", class_="ocr_line")
    return "\n".join(_clean_hocr_text(line.get_text()) for line in lines)


def _is_noise_line(text: str) -> bool:
    """Return True if a line is OCR noise (no real words, just punctuation/symbols)."""
    # Strip all non-alphabetic characters and check if any real word remains
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


# --- Task 6: Page processing pipeline ---


def process_page(pil_image: Image.Image, page_num: int, dpi: int = DEFAULT_DPI) -> dict:
    """Process a single page: preprocess, split columns, OCR, separate footnotes."""
    binary = preprocess_image(pil_image)
    columns, is_two_column = detect_and_split_columns(binary, dpi=dpi)

    all_body = []
    all_footnotes = []

    for column_img in columns:
        hocr = ocr_column(column_img)
        body_lines, footnote_lines = separate_footnotes(hocr, column_img.shape[0])
        all_body.extend(body_lines)
        all_footnotes.extend(footnote_lines)

    body_text = "\n".join(all_body)
    footnote_text = "\n".join(all_footnotes)

    col_label = "2-col" if is_two_column else "1-col"
    print(
        f"  Page {page_num}: {col_label}, "
        f"{len(all_body)} body lines, {len(all_footnotes)} footnote lines"
    )

    return {
        "body": body_text,
        "footnotes": footnote_text,
        "is_two_column": is_two_column,
    }


# --- Task 7: Comparison and report generation ---


def compare_texts(baseline: str, reocr: str) -> dict:
    """Compare baseline and re-OCR text, return metrics and examples."""
    baseline_lines = baseline.splitlines()
    reocr_lines = reocr.splitlines()

    # Unified diff
    diff_lines = list(
        difflib.unified_diff(baseline_lines, reocr_lines, lineterm="")
    )
    additions = sum(1 for d in diff_lines if d.startswith("+") and not d.startswith("+++"))
    removals = sum(1 for d in diff_lines if d.startswith("-") and not d.startswith("---"))

    # Character-level similarity
    char_similarity = difflib.SequenceMatcher(None, baseline, reocr).ratio()

    # Bag-of-words similarity (order-independent)
    baseline_words = Counter(re.findall(r"\w+", baseline.lower()))
    reocr_words = Counter(re.findall(r"\w+", reocr.lower()))
    common = sum((baseline_words & reocr_words).values())
    total = max(sum(baseline_words.values()), sum(reocr_words.values()), 1)
    bow_similarity = common / total

    # Extract correction examples via SequenceMatcher on lines
    examples = []
    sm = difflib.SequenceMatcher(None, baseline_lines, reocr_lines)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace":
            for bl, rl in zip(baseline_lines[i1:i2], reocr_lines[j1:j2]):
                if bl.strip() and rl.strip() and bl != rl:
                    examples.append((bl.strip(), rl.strip()))
    # Sort by length (shorter = more focused corrections), limit to 20
    examples.sort(key=lambda x: len(x[0]))
    examples = examples[:20]

    return {
        "char_similarity": char_similarity,
        "bow_similarity": bow_similarity,
        "additions": additions,
        "removals": removals,
        "examples": examples,
        "diff_lines": diff_lines,
    }


def print_report(
    page_results: list[dict],
    comparisons: list[dict],
    page_indices: list[int],
    baseline_pages: list[str],
    output_dir: Path,
    pdf_url: str = DEFAULT_PDF_URL,
):
    """Print console comparison report."""
    print("\n" + "=" * 80)
    print("RE-OCR QUALITY COMPARISON REPORT")
    print("=" * 80)
    print(f"PDF: {pdf_url}")
    print("OCR Engine: Tesseract 5")
    print(f"Output: {output_dir}/")

    # Bias notice
    print("\n" + "-" * 80)
    print("NOTE: Character-level similarity may be low due to column reordering")
    print("(structural difference, not OCR quality). Bag-of-words similarity is")
    print("a better indicator of pure OCR quality improvement.")
    print("-" * 80)

    # Per-page table
    print(f"\n{'Page':<6} {'Cols':<6} {'BL Lines':<10} {'RO Lines':<10} "
          f"{'Char Sim':<10} {'BoW Sim':<10} {'Footnotes':<10}")
    print("-" * 72)

    total_footnotes = 0
    for i, (result, comp) in enumerate(zip(page_results, comparisons)):
        page_num = page_indices[i] + 1
        bl_lines = len(baseline_pages[i].splitlines()) if i < len(baseline_pages) else 0
        ro_lines = len(result["body"].splitlines())
        fn_lines = len(result["footnotes"].splitlines()) if result["footnotes"] else 0
        total_footnotes += fn_lines
        cols = "2" if result["is_two_column"] else "1"
        print(
            f"{page_num:<6} {cols:<6} "
            f"{bl_lines:<10} {ro_lines:<10} "
            f"{comp['char_similarity']:<10.3f} {comp['bow_similarity']:<10.3f} "
            f"{fn_lines:<10}"
        )

    # Overall stats
    print("\n" + "-" * 72)
    if comparisons:
        avg_char = statistics.mean(c["char_similarity"] for c in comparisons)
        avg_bow = statistics.mean(c["bow_similarity"] for c in comparisons)
        print(f"Pages processed: {len(page_results)}")
        print(f"Avg char similarity: {avg_char:.3f}")
        print(f"Avg BoW similarity:  {avg_bow:.3f}")
        print(f"Total footnote lines: {total_footnotes}")

    # Correction examples
    all_examples = []
    for i, comp in enumerate(comparisons):
        page_num = page_indices[i] + 1
        for before, after in comp["examples"]:
            all_examples.append((page_num, before, after))

    if all_examples:
        print(f"\n{'=' * 80}")
        print(f"TOP CORRECTION EXAMPLES ({min(20, len(all_examples))} shown)")
        print("=" * 80)
        for page_num, before, after in all_examples[:20]:
            print(f"\nPage {page_num}:")
            print(f"  BEFORE: {before[:120]}")
            print(f"  AFTER:  {after[:120]}")

    # Footnote samples
    all_fn = []
    for i, result in enumerate(page_results):
        if result["footnotes"].strip():
            page_num = page_indices[i] + 1
            for line in result["footnotes"].splitlines()[:2]:
                if line.strip():
                    all_fn.append((page_num, line.strip()))
    if all_fn:
        print(f"\n{'=' * 80}")
        print(f"FOOTNOTE SAMPLES ({min(5, len(all_fn))} shown)")
        print("=" * 80)
        for page_num, line in all_fn[:5]:
            print(f"  Page {page_num}: {line[:120]}")


def export_files(
    baseline_pages: list[str],
    reocr_pages: list[str],
    footnote_pages: list[str],
    comparisons: list[dict],
    page_indices: list[int],
    output_dir: Path,
):
    """Write all output files for manual inspection."""
    # Per-page files
    for i, page_idx in enumerate(page_indices):
        page_num = page_idx + 1
        output_dir.joinpath(f"baseline_page_{page_num:03d}.txt").write_text(
            baseline_pages[i], encoding="utf-8"
        )
        output_dir.joinpath(f"reocr_page_{page_num:03d}.txt").write_text(
            reocr_pages[i], encoding="utf-8"
        )
        if footnote_pages[i].strip():
            output_dir.joinpath(f"reocr_footnotes_page_{page_num:03d}.txt").write_text(
                footnote_pages[i], encoding="utf-8"
            )

    # Full document files
    output_dir.joinpath("baseline_full.txt").write_text(
        "\n\n".join(baseline_pages), encoding="utf-8"
    )
    output_dir.joinpath("reocr_full.txt").write_text(
        "\n\n".join(reocr_pages), encoding="utf-8"
    )
    footnotes_full = "\n\n".join(fn for fn in footnote_pages if fn.strip())
    output_dir.joinpath("reocr_footnotes_full.txt").write_text(
        footnotes_full, encoding="utf-8"
    )

    # Diff report
    all_diff = []
    for i, comp in enumerate(comparisons):
        page_num = page_indices[i] + 1
        all_diff.append(f"--- Page {page_num} ---")
        all_diff.extend(comp["diff_lines"])
        all_diff.append("")
    output_dir.joinpath("diff_report.txt").write_text(
        "\n".join(all_diff), encoding="utf-8"
    )


# --- Task 8: Main orchestration ---


def main():
    parser = argparse.ArgumentParser(
        description="Re-OCR quality test for archive parliamentary PDFs"
    )
    parser.add_argument(
        "--pdf-url", default=DEFAULT_PDF_URL, help="URL of the PDF to test"
    )
    parser.add_argument(
        "--output-dir", default=DEFAULT_OUTPUT_DIR, help="Output directory"
    )
    parser.add_argument(
        "--dpi", type=int, default=DEFAULT_DPI, help="DPI for PDF to image conversion"
    )
    parser.add_argument(
        "--pages",
        default=None,
        help="Pages: '5', '1-10', '2,4-5', or omit for all",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Check OCR environment
    print("\n[0/5] Checking Tesseract environment...")
    check_tesseract_environment()

    # Download PDF
    print("\n[1/5] Downloading PDF...")
    pdf_path = download_pdf(args.pdf_url, output_dir)

    # Get total page count
    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    doc.close()
    print(f"  Total pages: {total_pages}")

    # Parse page range
    page_indices = parse_page_range(args.pages, total_pages)
    print(f"  Processing pages: {[i + 1 for i in page_indices]}")

    # Extract baseline
    print("\n[2/5] Extracting baseline text (PyMuPDF)...")
    baseline_pages = extract_baseline_text(pdf_path, page_indices)

    # Re-OCR
    print(f"\n[3/5] Re-OCR with Tesseract ({args.dpi} DPI)...")
    reocr_pages = []
    footnote_pages = []
    page_results = []

    for i, page_idx in enumerate(page_indices):
        print(f"\n[{i + 1}/{len(page_indices)}] Processing page {page_idx + 1}...")
        images = convert_from_path(
            str(pdf_path),
            dpi=args.dpi,
            first_page=page_idx + 1,
            last_page=page_idx + 1,
        )

        result = process_page(images[0], page_idx + 1, dpi=args.dpi)

        del images
        reocr_pages.append(result["body"])
        footnote_pages.append(result["footnotes"])
        page_results.append(result)

    # Compare
    print("\n[4/5] Comparing baseline vs re-OCR...")
    comparisons = [
        compare_texts(b, r) for b, r in zip(baseline_pages, reocr_pages)
    ]

    # Report
    print_report(
        page_results, comparisons, page_indices, baseline_pages, output_dir,
        pdf_url=args.pdf_url,
    )

    # Export
    print("\n[5/5] Exporting files...")
    export_files(
        baseline_pages, reocr_pages, footnote_pages, comparisons, page_indices, output_dir
    )
    print(f"\nOutput files written to: {output_dir}/")


if __name__ == "__main__":
    main()
