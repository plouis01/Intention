#!/usr/bin/env python3
"""
Count the exact number of PDFs available on archives.assemblee-nationale.fr
for legislatures 1 through 11 (1958-2002).

Structure:
  - CRI index: https://archives.assemblee-nationale.fr/{leg}/cri/
  - Session pages: https://archives.assemblee-nationale.fr/{leg}/cri/{leg}-{session}.html
  - PDFs linked from session pages
"""

import httpx
import time
import sys
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE_URL = "https://archives.assemblee-nationale.fr"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}
DELAY = 0.3  # seconds between requests


def fetch_page(client: httpx.Client, url: str, retries: int = 3) -> str | None:
    """Fetch a page with retries."""
    for attempt in range(retries):
        try:
            resp = client.get(url, headers=HEADERS, timeout=30, follow_redirects=True)
            if resp.status_code == 200:
                # Try to decode with different encodings
                try:
                    return resp.text
                except Exception:
                    return resp.content.decode("latin-1", errors="replace")
            elif resp.status_code == 404:
                print(f"  [404] {url}")
                return None
            else:
                print(f"  [HTTP {resp.status_code}] {url} (attempt {attempt+1})")
        except Exception as e:
            print(f"  [ERROR] {url}: {e} (attempt {attempt+1})")
        if attempt < retries - 1:
            time.sleep(1)
    return None


def get_session_links(client: httpx.Client, leg: int) -> list[tuple[str, str]]:
    """Get all session page links from a legislature's CRI index page.

    Returns list of (session_name, url) tuples.
    """
    index_url = f"{BASE_URL}/{leg}/cri/"
    html = fetch_page(client, index_url)
    if not html:
        print(f"  Could not fetch CRI index for legislature {leg}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    sessions = []

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        # Session pages are HTML files, typically like {leg}-{session}.html or similar
        if href.endswith(".html") or href.endswith(".htm"):
            full_url = urljoin(index_url, href)
            # Only include links that are under the CRI directory for this legislature
            if f"/{leg}/cri/" in full_url:
                session_name = a_tag.get_text(strip=True) or href
                sessions.append((session_name, full_url))

    # Deduplicate by URL
    seen = set()
    unique_sessions = []
    for name, url in sessions:
        if url not in seen:
            seen.add(url)
            unique_sessions.append((name, url))

    return unique_sessions


def count_pdfs_on_page(client: httpx.Client, url: str) -> tuple[int, list[str]]:
    """Count PDF links on a session page.

    Returns (count, list_of_pdf_urls).
    """
    html = fetch_page(client, url)
    if not html:
        return 0, []

    soup = BeautifulSoup(html, "html.parser")
    pdf_urls = []

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if href.lower().endswith(".pdf"):
            full_url = urljoin(url, href)
            pdf_urls.append(full_url)

    # Deduplicate
    unique_pdfs = list(dict.fromkeys(pdf_urls))
    return len(unique_pdfs), unique_pdfs


def main():
    grand_total_pdfs = 0
    grand_total_sessions = 0

    results = {}  # leg -> {sessions: int, pdfs: int, details: [...]}

    with httpx.Client() as client:
        for leg in range(1, 12):
            print(f"\n{'='*70}")
            print(f"LEGISLATURE {leg}")
            print(f"{'='*70}")

            session_links = get_session_links(client, leg)
            time.sleep(DELAY)

            print(f"  Found {len(session_links)} session page(s)")

            leg_total_pdfs = 0
            leg_details = []

            for i, (session_name, session_url) in enumerate(session_links):
                pdf_count, pdf_urls = count_pdfs_on_page(client, session_url)
                time.sleep(DELAY)

                leg_total_pdfs += pdf_count
                leg_details.append({
                    "session": session_name,
                    "url": session_url,
                    "pdf_count": pdf_count,
                })

                # Truncate session name for display
                display_name = session_name[:60] if len(session_name) > 60 else session_name
                print(f"  [{i+1}/{len(session_links)}] {display_name}: {pdf_count} PDFs")
                sys.stdout.flush()

            results[leg] = {
                "sessions": len(session_links),
                "pdfs": leg_total_pdfs,
                "details": leg_details,
            }

            grand_total_pdfs += leg_total_pdfs
            grand_total_sessions += len(session_links)

            print(f"\n  Legislature {leg} TOTAL: {len(session_links)} sessions, {leg_total_pdfs} PDFs")

    # Final summary
    print(f"\n\n{'='*70}")
    print("FINAL SUMMARY")
    print(f"{'='*70}")

    for leg in range(1, 12):
        r = results.get(leg, {"sessions": 0, "pdfs": 0})
        print(f"  Legislature {leg:2d}: {r['sessions']:4d} sessions, {r['pdfs']:6d} PDFs")

    print(f"\n  GRAND TOTAL: {grand_total_sessions} sessions, {grand_total_pdfs} PDFs")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
