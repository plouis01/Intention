#!/usr/bin/env python3
"""
Test OCR quality of French National Assembly archive PDFs (1958-2002).

This script downloads sample PDFs from different legislatures and evaluates:
- Presence of OCR text layer
- Quality of extracted text
- Consistency across different time periods
"""

import tempfile
from pathlib import Path
import httpx
import fitz  # PyMuPDF

# Test URLs covering different decades and positions in sessions
TEST_PDFS = [
    # First PDFs from each legislature (001.pdf)
    {
        "url": "https://archives.assemblee-nationale.fr/1/cri/1958-1959-ordinaire1/001.pdf",
        "legislature": 1,
        "year": 1958,
        "session": "1958-1959-ordinaire1",
        "number": "001"
    },
    {
        "url": "https://archives.assemblee-nationale.fr/2/cri/1963-1964-ordinaire1/001.pdf",
        "legislature": 2,
        "year": 1963,
        "session": "1963-1964-ordinaire1",
        "number": "001"
    },
    {
        "url": "https://archives.assemblee-nationale.fr/3/cri/1967-1968-ordinaire1/001.pdf",
        "legislature": 3,
        "year": 1967,
        "session": "1967-1968-ordinaire1",
        "number": "001"
    },
    {
        "url": "https://archives.assemblee-nationale.fr/4/cri/1968-1969-ordinaire1/001.pdf",
        "legislature": 4,
        "year": 1969,
        "session": "1968-1969-ordinaire1",
        "number": "001"
    },
    {
        "url": "https://archives.assemblee-nationale.fr/5/cri/1974-1975-ordinaire1/001.pdf",
        "legislature": 5,
        "year": 1974,
        "session": "1974-1975-ordinaire1",
        "number": "001"
    },
    {
        "url": "https://archives.assemblee-nationale.fr/6/cri/1979-1980-ordinaire1/001.pdf",
        "legislature": 6,
        "year": 1979,
        "session": "1979-1980-ordinaire1",
        "number": "001"
    },
    {
        "url": "https://archives.assemblee-nationale.fr/7/cri/1982-1983-ordinaire1/001.pdf",
        "legislature": 7,
        "year": 1982,
        "session": "1982-1983-ordinaire1",
        "number": "001"
    },
    {
        "url": "https://archives.assemblee-nazionale.fr/8/cri/1986-1987-ordinaire1/001.pdf",
        "legislature": 8,
        "year": 1986,
        "session": "1986-1987-ordinaire1",
        "number": "001"
    },
    {
        "url": "https://archives.assemblee-nationale.fr/9/cri/1988-1989-ordinaire1/001.pdf",
        "legislature": 9,
        "year": 1988,
        "session": "1988-1989-ordinaire1",
        "number": "001"
    },
    {
        "url": "https://archives.assemblee-nationale.fr/10/cri/1994-1995-ordinaire1/001.pdf",
        "legislature": 10,
        "year": 1994,
        "session": "1994-1995-ordinaire1",
        "number": "001"
    },
    {
        "url": "https://archives.assemblee-nationale.fr/11/cri/1998-1999-ordinaire1/001.pdf",
        "legislature": 11,
        "year": 1998,
        "session": "1998-1999-ordinaire1",
        "number": "001"
    },
    # Last PDFs from select sessions to test consistency
    {
        "url": "https://archives.assemblee-nationale.fr/1/cri/1958-1959-ordinaire1/003.pdf",
        "legislature": 1,
        "year": 1958,
        "session": "1958-1959-ordinaire1",
        "number": "003"
    },
    {
        "url": "https://archives.assemblee-nationale.fr/9/cri/1988-1989-ordinaire1/010.pdf",
        "legislature": 9,
        "year": 1988,
        "session": "1988-1989-ordinaire1",
        "number": "010"
    },
    {
        "url": "https://archives.assemblee-nationale.fr/11/cri/1998-1999-ordinaire1/050.pdf",
        "legislature": 11,
        "year": 1998,
        "session": "1998-1999-ordinaire1",
        "number": "050"
    },
]

# User agent to avoid blocking
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

# Thresholds for OCR quality assessment
MIN_CHARS_PER_PAGE = 100  # Minimum chars to consider page has text layer
SAMPLE_LENGTH = 500  # Characters to display for quality check


def download_pdf(url: str, temp_dir: Path) -> Path:
    """Download PDF from URL to temp directory."""
    filename = url.split("/")[-1]
    pdf_path = temp_dir / filename

    print(f"  Downloading {url}...")
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        pdf_path.write_bytes(response.content)

    return pdf_path


def extract_text_from_pdf(pdf_path: Path) -> dict:
    """Extract text from PDF and analyze OCR quality."""
    doc = fitz.open(pdf_path)

    total_chars = 0
    page_details = []
    all_text = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text()
        char_count = len(text.strip())

        total_chars += char_count
        page_details.append({
            "page": page_num + 1,
            "chars": char_count,
            "has_text": char_count >= MIN_CHARS_PER_PAGE
        })
        all_text.append(text)

    full_text = "\n".join(all_text)

    # Calculate OCR presence metrics
    pages_with_text = sum(1 for p in page_details if p["has_text"])
    total_pages = len(doc)
    ocr_coverage = (pages_with_text / total_pages * 100) if total_pages > 0 else 0
    avg_chars_per_page = total_chars / total_pages if total_pages > 0 else 0

    doc.close()

    return {
        "total_pages": total_pages,
        "total_chars": total_chars,
        "pages_with_text": pages_with_text,
        "ocr_coverage_pct": ocr_coverage,
        "avg_chars_per_page": avg_chars_per_page,
        "has_ocr": ocr_coverage > 50,  # Consider it has OCR if >50% pages have text
        "sample_text": full_text[:SAMPLE_LENGTH].strip(),
        "page_details": page_details[:5]  # First 5 pages details
    }


def assess_quality(result: dict) -> str:
    """Assess OCR quality based on metrics."""
    avg_chars = result["avg_chars_per_page"]
    coverage = result["ocr_coverage_pct"]

    if coverage < 50:
        return "NO_OCR"
    elif avg_chars < 500:
        return "POOR"
    elif avg_chars < 1500:
        return "FAIR"
    elif avg_chars < 2500:
        return "GOOD"
    else:
        return "EXCELLENT"


def main():
    """Main test function."""
    print("=" * 80)
    print("Testing OCR Quality of French National Assembly Archive PDFs (1958-2002)")
    print("=" * 80)
    print()

    results = []

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        for idx, pdf_info in enumerate(TEST_PDFS, 1):
            print(f"\n[{idx}/{len(TEST_PDFS)}] Testing Legislature {pdf_info['legislature']} ({pdf_info['year']}) - {pdf_info['session']}/{pdf_info['number']}.pdf")
            print("-" * 80)

            try:
                # Download PDF
                pdf_path = download_pdf(pdf_info["url"], temp_path)

                # Extract and analyze text
                print(f"  Extracting text...")
                extraction_result = extract_text_from_pdf(pdf_path)

                # Assess quality
                quality = assess_quality(extraction_result)

                # Store result
                result = {
                    **pdf_info,
                    **extraction_result,
                    "quality": quality
                }
                results.append(result)

                # Display summary
                print(f"  Pages: {extraction_result['total_pages']}")
                print(f"  Total characters: {extraction_result['total_chars']:,}")
                print(f"  Avg chars/page: {extraction_result['avg_chars_per_page']:.0f}")
                print(f"  OCR coverage: {extraction_result['ocr_coverage_pct']:.1f}%")
                print(f"  Has OCR layer: {'YES' if extraction_result['has_ocr'] else 'NO'}")
                print(f"  Quality assessment: {quality}")

                # Display sample text
                if extraction_result['sample_text']:
                    print(f"\n  Sample text (first {SAMPLE_LENGTH} chars):")
                    print("  " + "-" * 76)
                    for line in extraction_result['sample_text'].split('\n')[:10]:
                        print(f"  {line}")
                    print("  " + "-" * 76)

            except Exception as e:
                print(f"  ERROR: {e}")
                results.append({
                    **pdf_info,
                    "error": str(e),
                    "quality": "ERROR"
                })

    # Final summary
    print("\n\n" + "=" * 80)
    print("SUMMARY REPORT")
    print("=" * 80)
    print()

    # Summary table
    print(f"{'Leg':<4} {'Year':<6} {'Doc':<10} {'Pages':<7} {'Chars':<10} {'Avg/Page':<10} {'OCR%':<8} {'Quality':<12} {'Has OCR':<8}")
    print("-" * 80)

    for r in results:
        if "error" in r:
            print(f"{r['legislature']:<4} {r['year']:<6} {r['number']:<10} {'ERROR':<7} {'-':<10} {'-':<10} {'-':<8} {r['quality']:<12} {'ERROR':<8}")
        else:
            print(f"{r['legislature']:<4} {r['year']:<6} {r['number']:<10} {r['total_pages']:<7} {r['total_chars']:<10,} {r['avg_chars_per_page']:<10.0f} {r['ocr_coverage_pct']:<8.1f} {r['quality']:<12} {'YES' if r['has_ocr'] else 'NO':<8}")

    # Overall statistics
    print()
    print("OVERALL STATISTICS")
    print("-" * 80)

    valid_results = [r for r in results if "error" not in r]
    if valid_results:
        total_tested = len(valid_results)
        with_ocr = sum(1 for r in valid_results if r['has_ocr'])

        quality_counts = {}
        for r in valid_results:
            quality_counts[r['quality']] = quality_counts.get(r['quality'], 0) + 1

        print(f"Total PDFs tested: {total_tested}")
        print(f"PDFs with OCR layer: {with_ocr} ({with_ocr/total_tested*100:.1f}%)")
        print(f"PDFs without OCR layer: {total_tested - with_ocr} ({(total_tested-with_ocr)/total_tested*100:.1f}%)")
        print()
        print("Quality distribution:")
        for quality in ["EXCELLENT", "GOOD", "FAIR", "POOR", "NO_OCR"]:
            count = quality_counts.get(quality, 0)
            if count > 0:
                print(f"  {quality}: {count} ({count/total_tested*100:.1f}%)")

        # Average metrics for PDFs with OCR
        ocr_results = [r for r in valid_results if r['has_ocr']]
        if ocr_results:
            avg_chars_all = sum(r['avg_chars_per_page'] for r in ocr_results) / len(ocr_results)
            avg_coverage = sum(r['ocr_coverage_pct'] for r in ocr_results) / len(ocr_results)
            print()
            print(f"Average metrics for PDFs with OCR:")
            print(f"  Average chars/page: {avg_chars_all:.0f}")
            print(f"  Average coverage: {avg_coverage:.1f}%")

        print()
        print("CONCLUSION")
        print("-" * 80)
        if with_ocr == total_tested:
            print("ALL tested archive PDFs have an existing OCR text layer.")
        elif with_ocr >= total_tested * 0.8:
            print("MOST tested archive PDFs have an existing OCR text layer.")
        elif with_ocr >= total_tested * 0.5:
            print("SOME tested archive PDFs have an existing OCR text layer.")
        else:
            print("FEW tested archive PDFs have an existing OCR text layer.")

        # Quality recommendation
        poor_quality = quality_counts.get("POOR", 0) + quality_counts.get("NO_OCR", 0)
        if poor_quality >= total_tested * 0.5:
            print("RECOMMENDATION: Re-OCR is strongly recommended due to poor quality.")
        elif poor_quality >= total_tested * 0.2:
            print("RECOMMENDATION: Consider selective re-OCR for low-quality documents.")
        else:
            print("RECOMMENDATION: Existing OCR appears usable for most documents.")


if __name__ == "__main__":
    main()
