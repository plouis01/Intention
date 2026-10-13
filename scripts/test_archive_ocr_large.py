#!/usr/bin/env python3
"""
Large-scale OCR quality test of French National Assembly archive PDFs (1958-2002).

Tests 100 PDFs spread across all 11 legislatures to validate that OCR text layers
are consistently present across the entire archive.

Sampling strategy: ~9 PDFs per legislature across different sessions and PDF numbers.
"""

import sys
import tempfile
import time
from pathlib import Path

import httpx
import fitz  # PyMuPDF

# Base URL pattern
BASE_URL = "https://archives.assemblee-nationale.fr/{leg}/cri/{session}/{number}.pdf"

# User agent to avoid blocking
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

# Thresholds
MIN_CHARS_PER_PAGE = 100
DELAY_BETWEEN_DOWNLOADS = 0.5  # seconds


def build_test_urls():
    """Build exactly 100 test URLs spread across all 11 legislatures."""
    urls = []

    def add(leg, session, number):
        num_str = f"{number:03d}"
        url = BASE_URL.format(leg=leg, session=session, number=num_str)
        urls.append({
            "url": url,
            "legislature": leg,
            "session": session,
            "number": num_str,
            "session_type": "extraordinaire" if "extraordinaire" in session else "ordinaire",
        })

    # ── Legislature 1 (9 PDFs) ──
    add(1, "1958-1959-ordinaire1", 1)
    add(1, "1958-1959-ordinaire1", 5)
    add(1, "1958-1959-ordinaire1", 30)
    add(1, "1959-1960-ordinaire1", 1)
    add(1, "1959-1960-ordinaire1", 15)
    add(1, "1960-1961-ordinaire1", 1)
    add(1, "1960-1961-ordinaire1", 40)
    add(1, "1961-1962-ordinaire1", 1)
    add(1, "1961-1962-ordinaire1", 20)

    # ── Legislature 2 (9 PDFs) ──
    add(2, "1962-1963-ordinaire1", 1)
    add(2, "1962-1963-ordinaire1", 25)
    add(2, "1963-1964-ordinaire1", 1)
    add(2, "1963-1964-ordinaire1", 50)
    add(2, "1964-1965-ordinaire1", 1)
    add(2, "1964-1965-ordinaire1", 30)
    add(2, "1965-1966-ordinaire1", 1)
    add(2, "1965-1966-ordinaire1", 15)
    add(2, "1965-1966-ordinaire1", 60)

    # ── Legislature 3 (9 PDFs) ──
    add(3, "1967-1968-ordinaire1", 1)
    add(3, "1967-1968-ordinaire1", 5)
    add(3, "1967-1968-ordinaire1", 10)
    add(3, "1967-1968-ordinaire1", 20)
    add(3, "1967-1968-ordinaire1", 30)
    add(3, "1967-1968-ordinaire1", 40)
    add(3, "1967-1968-ordinaire1", 50)
    add(3, "1967-1968-ordinaire1", 60)
    add(3, "1967-1968-ordinaire1", 80)

    # ── Legislature 4 (9 PDFs) ──
    add(4, "1968-1969-ordinaire1", 1)
    add(4, "1968-1969-ordinaire1", 15)
    add(4, "1968-1969-ordinaire1", 40)
    add(4, "1969-1970-ordinaire1", 1)
    add(4, "1969-1970-ordinaire1", 25)
    add(4, "1970-1971-ordinaire1", 1)
    add(4, "1970-1971-ordinaire1", 50)
    add(4, "1971-1972-ordinaire1", 1)
    add(4, "1971-1972-ordinaire1", 30)

    # ── Legislature 5 (9 PDFs) ──
    add(5, "1973-1974-ordinaire1", 1)
    add(5, "1973-1974-ordinaire1", 60)
    add(5, "1974-1975-ordinaire1", 1)
    add(5, "1974-1975-ordinaire1", 40)
    add(5, "1975-1976-ordinaire1", 1)
    add(5, "1975-1976-ordinaire1", 80)
    add(5, "1976-1977-ordinaire1", 1)
    add(5, "1976-1977-ordinaire1", 50)
    add(5, "1977-1978-extraordinaire1", 1)  # extraordinaire

    # ── Legislature 6 (9 PDFs) ──
    add(6, "1978-1979-ordinaire1", 1)
    add(6, "1978-1979-ordinaire1", 30)
    add(6, "1978-1979-ordinaire1", 100)
    add(6, "1979-1980-ordinaire1", 1)
    add(6, "1979-1980-ordinaire1", 40)
    add(6, "1979-1980-ordinaire1", 80)
    add(6, "1980-1981-ordinaire1", 1)
    add(6, "1980-1981-ordinaire1", 25)
    add(6, "1980-1981-ordinaire1", 60)

    # ── Legislature 7 (9 PDFs) ──
    add(7, "1981-1982-ordinaire1", 1)
    add(7, "1981-1982-ordinaire1", 50)
    add(7, "1981-1982-ordinaire1", 120)
    add(7, "1982-1983-ordinaire1", 1)
    add(7, "1982-1983-ordinaire1", 40)
    add(7, "1983-1984-ordinaire1", 1)
    add(7, "1984-1985-ordinaire1", 1)
    add(7, "1984-1985-ordinaire1", 60)
    add(7, "1981-1982-extraordinaire1", 1)  # extraordinaire

    # ── Legislature 8 (9 PDFs) ──
    add(8, "1986-1987-ordinaire1", 1)
    add(8, "1986-1987-ordinaire1", 10)
    add(8, "1986-1987-ordinaire1", 30)
    add(8, "1986-1987-ordinaire1", 60)
    add(8, "1986-1987-ordinaire1", 100)
    add(8, "1987-1988-ordinaire1", 1)
    add(8, "1987-1988-ordinaire1", 20)
    add(8, "1987-1988-ordinaire1", 50)
    add(8, "1987-1988-ordinaire1", 80)

    # ── Legislature 9 (9 PDFs) ──
    add(9, "1988-1989-ordinaire1", 1)
    add(9, "1988-1989-ordinaire1", 30)
    add(9, "1989-1990-ordinaire1", 1)
    add(9, "1989-1990-ordinaire1", 50)
    add(9, "1990-1991-ordinaire1", 1)
    add(9, "1990-1991-ordinaire1", 40)
    add(9, "1991-1992-ordinaire1", 1)
    add(9, "1991-1992-ordinaire1", 60)
    add(9, "1992-1993-extraordinaire1", 1)  # extraordinaire

    # ── Legislature 10 (9 PDFs) ──
    add(10, "1993-1994-ordinaire1", 1)
    add(10, "1993-1994-ordinaire1", 20)
    add(10, "1993-1994-ordinaire1", 50)
    add(10, "1993-1994-ordinaire1", 100)
    add(10, "1994-1995-ordinaire1", 1)
    add(10, "1994-1995-ordinaire1", 40)
    add(10, "1994-1995-ordinaire1", 80)
    add(10, "1995-1996-ordinaire1", 1)
    add(10, "1995-1996-ordinaire1", 30)

    # ── Legislature 11 (10 PDFs) - extra one to reach 100 ──
    add(11, "1997-1998-ordinaire1", 1)
    add(11, "1997-1998-ordinaire1", 40)
    add(11, "1997-1998-ordinaire1", 100)
    add(11, "1998-1999-ordinaire1", 1)
    add(11, "1998-1999-ordinaire1", 50)
    add(11, "1999-2000-ordinaire1", 1)
    add(11, "1999-2000-ordinaire1", 80)
    add(11, "2000-2001-ordinaire1", 1)
    add(11, "2000-2001-ordinaire1", 60)
    add(11, "2001-2002-extraordinaire1", 1)  # extraordinaire

    assert len(urls) == 100, f"Expected 100 URLs, got {len(urls)}"
    return urls


def download_pdf(url: str, client: httpx.Client, temp_dir: Path) -> Path | None:
    """Download PDF from URL. Returns path or None on failure."""
    filename = url.split("/")[-3] + "_" + url.split("/")[-2] + "_" + url.split("/")[-1]
    pdf_path = temp_dir / filename

    response = client.get(url)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    pdf_path.write_bytes(response.content)
    return pdf_path


def extract_text_from_pdf(pdf_path: Path) -> dict:
    """Extract text from PDF and analyze OCR quality."""
    doc = fitz.open(pdf_path)

    total_chars = 0
    pages_with_text = 0
    total_pages = len(doc)

    for page_num in range(total_pages):
        page = doc[page_num]
        text = page.get_text()
        char_count = len(text.strip())
        total_chars += char_count
        if char_count >= MIN_CHARS_PER_PAGE:
            pages_with_text += 1

    doc.close()

    ocr_coverage = (pages_with_text / total_pages * 100) if total_pages > 0 else 0
    avg_chars = total_chars / total_pages if total_pages > 0 else 0

    return {
        "total_pages": total_pages,
        "total_chars": total_chars,
        "pages_with_text": pages_with_text,
        "ocr_coverage_pct": ocr_coverage,
        "avg_chars_per_page": avg_chars,
        "has_ocr": ocr_coverage > 50,
    }


def quality_label(avg_chars: float, coverage: float) -> str:
    """Classify OCR quality."""
    if coverage < 50:
        return "NO_OCR"
    elif avg_chars < 100:
        return "NO_OCR"
    elif avg_chars < 500:
        return "POOR"
    elif avg_chars < 1000:
        return "FAIR"
    elif avg_chars < 3000:
        return "GOOD"
    else:
        return "EXCELLENT"


def main():
    test_urls = build_test_urls()

    print("=" * 100)
    print("LARGE-SCALE OCR TEST: French National Assembly Archive PDFs (1958-2002)")
    print(f"Testing {len(test_urls)} PDFs across 11 legislatures")
    print("=" * 100)
    print()

    # Header for running output
    header = f"{'#':<4} {'Leg':<4} {'Session':<35} {'Num':<5} {'Status':<8} {'Pages':<7} {'Chars':<9} {'Avg/Pg':<9} {'OCR%':<7} {'Quality':<10}"
    print(header)
    print("-" * 100)

    results = []
    skipped_404 = 0
    errors = []

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
            for idx, pdf_info in enumerate(test_urls, 1):
                try:
                    pdf_path = download_pdf(pdf_info["url"], client, temp_path)

                    if pdf_path is None:
                        skipped_404 += 1
                        print(f"{idx:<4} {pdf_info['legislature']:<4} {pdf_info['session']:<35} {pdf_info['number']:<5} {'404':<8} {'-':<7} {'-':<9} {'-':<9} {'-':<7} {'SKIPPED':<10}")
                        sys.stdout.flush()
                        time.sleep(DELAY_BETWEEN_DOWNLOADS)
                        continue

                    extraction = extract_text_from_pdf(pdf_path)
                    quality = quality_label(extraction["avg_chars_per_page"], extraction["ocr_coverage_pct"])

                    result = {
                        **pdf_info,
                        **extraction,
                        "quality": quality,
                    }
                    results.append(result)

                    print(
                        f"{idx:<4} {pdf_info['legislature']:<4} {pdf_info['session']:<35} {pdf_info['number']:<5} "
                        f"{'OK':<8} {extraction['total_pages']:<7} {extraction['total_chars']:<9,} "
                        f"{extraction['avg_chars_per_page']:<9.0f} {extraction['ocr_coverage_pct']:<7.1f} {quality:<10}"
                    )
                    sys.stdout.flush()

                    # Cleanup to save disk space
                    pdf_path.unlink(missing_ok=True)

                except Exception as e:
                    error_msg = str(e)[:60]
                    errors.append({"idx": idx, **pdf_info, "error": error_msg})
                    print(f"{idx:<4} {pdf_info['legislature']:<4} {pdf_info['session']:<35} {pdf_info['number']:<5} {'ERROR':<8} {error_msg}")
                    sys.stdout.flush()

                time.sleep(DELAY_BETWEEN_DOWNLOADS)

    # ════════════════════════════════════════════════════════════════════
    # AGGREGATE STATISTICS
    # ════════════════════════════════════════════════════════════════════
    print()
    print()
    print("=" * 100)
    print("AGGREGATE STATISTICS")
    print("=" * 100)

    total_tested = len(results)
    if total_tested == 0:
        print("No PDFs were successfully tested.")
        return

    with_ocr = sum(1 for r in results if r["has_ocr"])
    good_quality = sum(1 for r in results if r["avg_chars_per_page"] >= 1000)
    excellent_quality = sum(1 for r in results if r["avg_chars_per_page"] >= 3000)

    print()
    print(f"Total URLs attempted:         {len(test_urls)}")
    print(f"  Skipped (404):              {skipped_404}")
    print(f"  Errors:                     {len(errors)}")
    print(f"  Successfully tested:        {total_tested}")
    print()
    print(f"PDFs with OCR text layer:     {with_ocr}/{total_tested} ({with_ocr/total_tested*100:.1f}%)")
    print(f"  >100 chars/page avg")
    print(f"PDFs with GOOD quality:       {good_quality}/{total_tested} ({good_quality/total_tested*100:.1f}%)")
    print(f"  >1000 chars/page avg")
    print(f"PDFs with EXCELLENT quality:  {excellent_quality}/{total_tested} ({excellent_quality/total_tested*100:.1f}%)")
    print(f"  >3000 chars/page avg")
    print()

    # Quality distribution
    quality_counts = {}
    for r in results:
        q = r["quality"]
        quality_counts[q] = quality_counts.get(q, 0) + 1

    print("Quality distribution:")
    for q in ["EXCELLENT", "GOOD", "FAIR", "POOR", "NO_OCR"]:
        count = quality_counts.get(q, 0)
        bar = "#" * (count * 2)
        print(f"  {q:<12} {count:>3} ({count/total_tested*100:5.1f}%)  {bar}")
    print()

    # Average metrics
    avg_chars_all = sum(r["avg_chars_per_page"] for r in results) / total_tested
    avg_coverage_all = sum(r["ocr_coverage_pct"] for r in results) / total_tested
    print(f"Overall average chars/page:   {avg_chars_all:,.0f}")
    print(f"Overall average OCR coverage: {avg_coverage_all:.1f}%")
    print()

    # ── Breakdown by legislature ──
    print("-" * 100)
    print("BREAKDOWN BY LEGISLATURE")
    print("-" * 100)
    print(f"{'Leg':<5} {'Count':<7} {'OCR%':<7} {'Avg Chars/Pg':<15} {'Quality Distribution'}")
    print(f"{'---':<5} {'-----':<7} {'----':<7} {'------------':<15} {'--------------------'}")

    for leg in range(1, 12):
        leg_results = [r for r in results if r["legislature"] == leg]
        if not leg_results:
            print(f"{leg:<5} {'0':<7} {'-':<7} {'-':<15}")
            continue
        leg_ocr = sum(1 for r in leg_results if r["has_ocr"])
        leg_avg = sum(r["avg_chars_per_page"] for r in leg_results) / len(leg_results)
        leg_qualities = {}
        for r in leg_results:
            leg_qualities[r["quality"]] = leg_qualities.get(r["quality"], 0) + 1
        q_str = ", ".join(f"{k}:{v}" for k, v in sorted(leg_qualities.items()))
        print(f"{leg:<5} {len(leg_results):<7} {leg_ocr/len(leg_results)*100:5.1f}% {leg_avg:<15,.0f} {q_str}")

    # ── Breakdown by session type ──
    print()
    print("-" * 100)
    print("BREAKDOWN BY SESSION TYPE")
    print("-" * 100)
    print(f"{'Type':<20} {'Count':<7} {'OCR%':<7} {'Avg Chars/Pg':<15}")
    print(f"{'----':<20} {'-----':<7} {'----':<7} {'------------':<15}")

    for stype in ["ordinaire", "extraordinaire"]:
        st_results = [r for r in results if r["session_type"] == stype]
        if not st_results:
            continue
        st_ocr = sum(1 for r in st_results if r["has_ocr"])
        st_avg = sum(r["avg_chars_per_page"] for r in st_results) / len(st_results)
        print(f"{stype:<20} {len(st_results):<7} {st_ocr/len(st_results)*100:5.1f}% {st_avg:<15,.0f}")

    # ── Outliers (low char count) ──
    print()
    print("-" * 100)
    print("OUTLIERS (avg chars/page < 500)")
    print("-" * 100)
    outliers = [r for r in results if r["avg_chars_per_page"] < 500]
    if outliers:
        for r in outliers:
            print(f"  Leg {r['legislature']:>2}, {r['session']}/{r['number']}.pdf - avg {r['avg_chars_per_page']:.0f} chars/page, {r['total_pages']} pages")
    else:
        print("  None found - all PDFs have >= 500 avg chars/page")

    # ── Errors ──
    if errors:
        print()
        print("-" * 100)
        print(f"ERRORS ({len(errors)})")
        print("-" * 100)
        for e in errors:
            print(f"  #{e['idx']} Leg {e['legislature']}, {e['session']}/{e['number']}.pdf - {e['error']}")

    # ── Conclusion ──
    print()
    print("=" * 100)
    print("CONCLUSION")
    print("=" * 100)
    if with_ocr == total_tested:
        print("ALL tested archive PDFs have an existing OCR text layer.")
    elif with_ocr >= total_tested * 0.95:
        print(f"NEARLY ALL ({with_ocr}/{total_tested}) tested archive PDFs have an existing OCR text layer.")
    elif with_ocr >= total_tested * 0.8:
        print(f"MOST ({with_ocr}/{total_tested}) tested archive PDFs have an existing OCR text layer.")
    else:
        print(f"ONLY {with_ocr}/{total_tested} tested archive PDFs have an existing OCR text layer.")

    if excellent_quality >= total_tested * 0.5:
        print("The majority of PDFs have EXCELLENT text extraction quality (>3000 chars/page).")
    elif good_quality >= total_tested * 0.5:
        print("The majority of PDFs have GOOD text extraction quality (>1000 chars/page).")

    poor_or_none = quality_counts.get("POOR", 0) + quality_counts.get("NO_OCR", 0)
    if poor_or_none == 0:
        print("No PDFs with poor or missing OCR were found.")
        print("RECOMMENDATION: The existing OCR layers are sufficient - no re-OCR needed.")
    elif poor_or_none < total_tested * 0.1:
        print(f"Only {poor_or_none} PDFs had poor/no OCR - minimal issues.")
        print("RECOMMENDATION: The existing OCR layers are largely sufficient.")
    else:
        print(f"{poor_or_none} PDFs had poor/no OCR - consider selective re-processing.")

    print("=" * 100)


if __name__ == "__main__":
    main()
