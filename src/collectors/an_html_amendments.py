"""Collector for AN HTML amendments (legislatures 12-13, 2002-2012).

Downloads amendments from the AN archive search interface for legislatures
12 (2002-2007) and 13 (2007-2012). These older amendments are not available
via the modern AN open data API (which starts at legislature 14).

The collector discovers available texts by scraping the search form dropdown,
then paginates through results for each text.

URL patterns:
  Search form:  https://www2.assemblee-nationale.fr/recherche/archives_amendements
                ?LEGISLATURE={leg}
  Results:      https://www2.assemblee-nationale.fr/recherche/archives_resultats_amendements
                ?LEGISLATURE={leg}&NUM_INIT={text}&offset={n}
  Detail pages: https://www.assemblee-nationale.fr/{leg}/amendements/{text}/{doc_id}.asp

Encoding: detail pages are typically ISO-8859-1 / windows-1252.
"""

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

AN_ARCHIVE_BASE = "https://www2.assemblee-nationale.fr"
AN_DETAIL_BASE = "https://www.assemblee-nationale.fr"
AN_SEARCH_URL = f"{AN_ARCHIVE_BASE}/recherche/archives_amendements"
AN_RESULTS_URL = f"{AN_ARCHIVE_BASE}/recherche/archives_resultats_amendements"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}
DEFAULT_DELAY = 0.5
BACKOFF_WAIT = 60
MAX_RETRIES = 3
RESULTS_PER_PAGE = 500

# Normalized sort values
SORT_MAPPING = {
    "adopté": "adopte",
    "adoptée": "adopte",
    "adopte": "adopte",
    "rejeté": "rejete",
    "rejetée": "rejete",
    "rejete": "rejete",
    "retiré": "retire",
    "retirée": "retire",
    "retire": "retire",
    "tombé": "tombe",
    "tombée": "tombe",
    "tombe": "tombe",
    "non soutenu": "non_soutenu",
    "non-soutenu": "non_soutenu",
    "non_soutenu": "non_soutenu",
    "non renseigné": None,
    "retiré avant séance": "retire",
    "irrecevable 40c": "irrecevable",
    "irrecevable 44c": "irrecevable",
}


def _normalize_sort(sort_value: str | None) -> str | None:
    """Normalize a sort value from the AN HTML pages."""
    if not sort_value:
        return None
    normalized = sort_value.lower().strip()
    if normalized in SORT_MAPPING:
        return SORT_MAPPING[normalized]
    return normalized


def _strip_html(text: str | None) -> str | None:
    """Remove HTML tags and collapse whitespace."""
    if not text:
        return None
    from html import unescape

    text = unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if text else None


def _decode_response(resp: httpx.Response) -> str:
    """Decode response content, handling ISO-8859-1 / windows-1252."""
    content_type = resp.headers.get("content-type", "")
    if "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=")[-1].split(";")[0].strip()
        try:
            return resp.content.decode(charset)
        except (UnicodeDecodeError, LookupError):
            pass

    head = resp.content[:2048]
    meta_match = re.search(
        rb'charset=["\']?([^"\'\s;>]+)', head, re.IGNORECASE
    )
    if meta_match:
        charset = meta_match.group(1).decode("ascii", errors="ignore")
        try:
            return resp.content.decode(charset)
        except (UnicodeDecodeError, LookupError):
            pass

    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        return resp.content.decode("latin-1")


DEFAULT_WORKERS = 4
MIN_GLOBAL_DELAY = 0.15  # Minimum delay between requests across all threads


class ANHtmlAmendmentCollector:
    """Collector for AN HTML amendments (legislatures 12-13)."""

    def __init__(self, output_dir: Path, max_workers: int = DEFAULT_WORKERS):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max_workers
        self._client: httpx.Client | None = None
        self._delay = DEFAULT_DELAY
        self._rate_lock = threading.Lock()
        self._last_request_time = 0.0

    def __enter__(self):
        self._client = httpx.Client(
            timeout=60.0,
            follow_redirects=True,
            headers=HEADERS,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

    def _rate_limit(self):
        """Thread-safe rate limiter ensuring MIN_GLOBAL_DELAY between requests."""
        with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            wait = max(MIN_GLOBAL_DELAY - elapsed, 0)
            if wait > 0:
                time.sleep(wait)
            self._last_request_time = time.monotonic()

    def _get(self, url: str) -> httpx.Response:
        """GET with thread-safe rate limiting and retries on 429/503."""
        for attempt in range(MAX_RETRIES):
            self._rate_limit()
            try:
                resp = self._client.get(url)
                if resp.status_code in (429, 503):
                    try:
                        retry_after = int(
                            resp.headers.get("Retry-After", BACKOFF_WAIT)
                        )
                    except (ValueError, TypeError):
                        retry_after = BACKOFF_WAIT
                    logger.warning(
                        f"Rate limited ({resp.status_code}), waiting {retry_after}s "
                        f"(attempt {attempt + 1}/{MAX_RETRIES})"
                    )
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError:
                raise
            except httpx.HTTPError as e:
                if attempt < MAX_RETRIES - 1:
                    logger.warning(f"Request error: {e}, retrying...")
                    time.sleep(BACKOFF_WAIT)
                else:
                    raise
        raise httpx.HTTPError(f"Max retries exceeded for {url}")

    # ------------------------------------------------------------------
    # Text discovery from search dropdown
    # ------------------------------------------------------------------

    def discover_texts(self, legislature: int) -> list[dict]:
        """Discover available texts from the search form dropdown.

        Scrapes the ``<select name="numInit">`` element on the search page.
        Each option has the format: ``{text_number} ({count} amdts)``.

        Args:
            legislature: Legislature number (12 or 13).

        Returns:
            List of dicts with keys: text_number, expected_count, label.
        """
        url = f"{AN_SEARCH_URL}?LEGISLATURE={legislature}"
        resp = self._get(url)
        html = _decode_response(resp)
        soup = BeautifulSoup(html, "html.parser")

        sel = soup.find("select", {"name": "numInit"})
        if not sel:
            logger.error(f"No numInit select found for legislature {legislature}")
            return []

        texts = []
        for opt in sel.find_all("option"):
            value = (opt.get("value") or "").strip()
            if not value:
                continue
            try:
                text_number = int(value)
            except ValueError:
                continue

            label = opt.get_text(strip=True)
            count_match = re.search(r"\((\d+)\s+amdts?\)", label)
            expected_count = int(count_match.group(1)) if count_match else 0

            texts.append({
                "text_number": text_number,
                "expected_count": expected_count,
                "label": label,
            })

        logger.info(
            f"Legislature {legislature}: discovered {len(texts)} texts "
            f"({sum(t['expected_count'] for t in texts)} total amendments)"
        )
        return texts

    # ------------------------------------------------------------------
    # Search results parsing
    # ------------------------------------------------------------------

    def search_amendments(
        self, legislature: int, text_number: int, offset: int = 0
    ) -> dict[str, Any]:
        """Parse one page of amendment search results.

        The AN archive returns 10 results per page in an HTML table
        with columns: [Texte, N°, Place, Date, Auteurs, Sort].

        Returns:
            Dict with amendments list, total count, and has_next flag.
        """
        url = (
            f"{AN_RESULTS_URL}"
            f"?LEGISLATURE={legislature}&NUM_INIT={text_number}"
            f"&nbres={RESULTS_PER_PAGE}&offset={offset}"
        )
        try:
            resp = self._get(url)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 400):
                return {"amendments": [], "total": 0, "has_next": False}
            raise

        html = _decode_response(resp)
        soup = BeautifulSoup(html, "html.parser")

        total = self._extract_total_count(soup)

        # Find the results table
        table = soup.find("table")
        if not table:
            return {"amendments": [], "total": total or 0, "has_next": False}

        # Parse rows (first row is header with <th>)
        amendments = []
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 5:
                continue

            amend = self._parse_result_row(cells, legislature, text_number)
            if amend:
                amendments.append(amend)

        has_next = self._has_next_page(soup, offset, total, len(amendments))

        return {
            "amendments": amendments,
            "total": total,
            "has_next": has_next,
        }

    def _parse_result_row(
        self, cells: list, legislature: int, text_number: int
    ) -> dict[str, Any] | None:
        """Parse a result row: [Texte, N°, Place, Date, Auteurs, Sort]."""
        cell_texts = [c.get_text(strip=True) for c in cells]

        # Column 0: Texte (text number, skip)
        # Column 1: N° (amendment number, contains link to detail page)
        numero = cell_texts[1] if len(cell_texts) > 1 else None

        # Extract detail URL from the N° cell
        detail_url = None
        link = cells[1].find("a", href=True) if len(cells) > 1 else None
        if link:
            href = link["href"]
            if not href.startswith("http"):
                detail_url = f"{AN_DETAIL_BASE}{href}" if href.startswith("/") else href
            else:
                detail_url = href

        # Column 2: Place (article reference)
        article = cell_texts[2] if len(cell_texts) > 2 else None

        # Column 3: Date
        date = cell_texts[3] if len(cell_texts) > 3 else None

        # Column 4: Auteurs (normalize to first author only)
        from src.parsers.amendments import normalize_amendment_author
        auteurs = normalize_amendment_author(cell_texts[4] if len(cell_texts) > 4 else None)

        # Column 5: Sort
        sort_raw = cell_texts[5] if len(cell_texts) > 5 else None

        return {
            "numero": numero,
            "article_ref": article or None,
            "date_depot": date or None,
            "auteur": auteurs or None,
            "sort": _normalize_sort(sort_raw),
            "sort_label": sort_raw or None,
            "detail_url": detail_url,
            "chambre": "AN",
            "reference_externe": f"AN-LEG{legislature}-{text_number}-{numero or '?'}",
        }

    def _extract_total_count(self, soup: BeautifulSoup) -> int | None:
        """Extract total count from "N amendements" text on the page."""
        text = soup.get_text(separator=" ", strip=True)
        m = re.search(r"(\d+)\s+amendements?", text, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return None

    def _has_next_page(
        self,
        soup: BeautifulSoup,
        current_offset: int,
        total: int | None,
        page_count: int,
    ) -> bool:
        """Check if there is a next page of results."""
        if page_count < RESULTS_PER_PAGE:
            return False

        if total is not None:
            return (current_offset + page_count) < total

        # Look for pagination links with higher offsets
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if "offset=" in href:
                m = re.search(r"offset=(\d+)", href)
                if m and int(m.group(1)) > current_offset:
                    return True

        return False

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def download_amendment_detail(self, url: str) -> dict[str, Any] | None:
        """Download and parse an individual amendment detail page (.asp).

        Returns:
            Dict with dispositif, expose_sommaire, avis_commission,
            avis_gouvernement, or None on failure.
        """
        try:
            resp = self._get(url)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug(f"Amendment detail 404: {url}")
                return None
            raise

        html = _decode_response(resp)
        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text(separator="\n", strip=True)

        detail: dict[str, Any] = {
            "dispositif": None,
            "expose_sommaire": None,
            "avis_commission": None,
            "avis_gouvernement": None,
        }

        detail["dispositif"] = self._extract_section(
            soup, page_text,
            markers=["dispositif", "texte de l'amendement", "texte de l\u2019amendement"],
            stop_markers=["exposé sommaire", "expose sommaire", "exposé des motifs", "objet"],
        )

        detail["expose_sommaire"] = self._extract_section(
            soup, page_text,
            markers=["exposé sommaire", "expose sommaire", "exposé des motifs", "objet de l'amendement"],
            stop_markers=["avis de la commission", "avis du gouvernement", "commission"],
        )

        detail["avis_commission"] = self._extract_avis(
            page_text, markers=["avis de la commission", "commission :"],
        )

        detail["avis_gouvernement"] = self._extract_avis(
            page_text,
            markers=["avis du gouvernement", "gouvernement :", "position du gouvernement"],
        )

        return detail

    def _extract_section(
        self, soup: BeautifulSoup, page_text: str,
        markers: list[str], stop_markers: list[str],
    ) -> str | None:
        """Extract a text section between markers."""
        page_lower = page_text.lower()

        # Strategy 1: heading element + following siblings
        for marker in markers:
            for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "b", "strong", "p"]):
                if marker in tag.get_text(strip=True).lower():
                    parts = []
                    sibling = tag.find_next_sibling()
                    while sibling:
                        sib_text = sibling.get_text(strip=True)
                        if any(sm in sib_text.lower() for sm in stop_markers):
                            break
                        if sib_text:
                            parts.append(sib_text)
                        sibling = sibling.find_next_sibling()
                    if parts:
                        return _strip_html(" ".join(parts))

        # Strategy 2: plain text between markers
        for marker in markers:
            idx = page_lower.find(marker)
            if idx == -1:
                continue
            start = idx + len(marker)
            while start < len(page_text) and page_text[start] in " :\n\r\t":
                start += 1
            end = len(page_text)
            for sm in stop_markers:
                sm_idx = page_lower.find(sm, start)
                if sm_idx != -1 and sm_idx < end:
                    end = sm_idx
            section = page_text[start:end].strip()
            if section and len(section) > 5:
                return _strip_html(section)

        return None

    def _extract_avis(self, page_text: str, markers: list[str]) -> str | None:
        """Extract a short avis value."""
        page_lower = page_text.lower()
        for marker in markers:
            idx = page_lower.find(marker)
            if idx == -1:
                continue
            start = idx + len(marker)
            while start < len(page_text) and page_text[start] in " :\n\r\t":
                start += 1
            end = start
            while end < len(page_text) and end - start < 200:
                if page_text[end] == "\n":
                    break
                end += 1
            value = page_text[start:end].strip()
            if value:
                value = re.sub(r"\s*[-–]\s*$", "", value)
                return value[:200]
        return None

    # ------------------------------------------------------------------
    # Full download for one text
    # ------------------------------------------------------------------

    def download_all_for_text(
        self, legislature: int, text_number: int,
        fetch_details: bool = False,
    ) -> dict[str, Any]:
        """Download all amendments for a text, paginating through results.

        Args:
            legislature: Legislature number (12 or 13).
            text_number: AN text number.
            fetch_details: Whether to fetch individual .asp detail pages.
        """
        all_amendments = []
        offset = 0
        total = None

        while True:
            result = self.search_amendments(legislature, text_number, offset=offset)

            if result["total"] is not None:
                total = result["total"]

            if not result["amendments"]:
                break

            all_amendments.extend(result["amendments"])

            if not result["has_next"]:
                break

            offset += RESULTS_PER_PAGE

        # Optionally fetch detail pages
        if fetch_details and all_amendments:
            logger.info(
                f"  Leg {legislature} text {text_number}: "
                f"fetching details for {len(all_amendments)} amendments..."
            )
            for i, amend in enumerate(all_amendments):
                detail_url = amend.get("detail_url")
                if not detail_url:
                    continue
                try:
                    detail = self.download_amendment_detail(detail_url)
                    if detail:
                        amend["dispositif"] = detail.get("dispositif")
                        amend["expose_sommaire"] = detail.get("expose_sommaire")
                        amend["avis_commission"] = detail.get("avis_commission")
                        amend["avis_gouvernement"] = detail.get("avis_gouvernement")
                except Exception as e:
                    logger.debug(
                        f"Failed detail for {amend.get('numero')}: {e}"
                    )
                if (i + 1) % 100 == 0:
                    logger.info(f"    Detail progress: {i + 1}/{len(all_amendments)}")

        return {
            "source": f"an_html_leg{legislature}",
            "legislature": legislature,
            "text_number": text_number,
            "total_amendments": len(all_amendments),
            "amendments": all_amendments,
        }

    # ------------------------------------------------------------------
    # Collect one legislature
    # ------------------------------------------------------------------

    def _collect_one_text(
        self,
        legislature: int,
        text_info: dict,
        leg_dir: Path,
        force: bool,
        fetch_details: bool,
    ) -> tuple[str, int]:
        """Collect amendments for a single text. Returns (status, count).

        Status is one of: "collected", "skipped", "failed".
        Thread-safe: rate limiting is handled by _rate_limit().
        """
        text_number = text_info["text_number"]
        out_path = leg_dir / f"text_{text_number}.json"

        # Idempotent: skip existing files
        if not force and out_path.exists() and out_path.stat().st_size > 10:
            try:
                with open(out_path, encoding="utf-8") as f:
                    existing = json.load(f)
                return "skipped", existing.get("total_amendments", 0)
            except (json.JSONDecodeError, OSError):
                pass  # Re-download if corrupted

        try:
            data = self.download_all_for_text(
                legislature, text_number, fetch_details=fetch_details,
            )
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return "collected", data["total_amendments"]
        except Exception as e:
            logger.error(f"Failed text {text_number}: {e}")
            return "failed", 0

    def collect_legislature(
        self,
        legislature: int,
        force: bool = False,
        fetch_details: bool = False,
    ) -> tuple[int, int]:
        """Collect all amendments for a legislature.

        Discovers texts from the dropdown, then downloads each using
        parallel workers (controlled by max_workers).

        Args:
            legislature: 12 or 13.
            force: Re-download existing files.
            fetch_details: Fetch individual .asp detail pages.

        Returns:
            Tuple of (texts_collected, amendments_collected).
        """
        leg_dir = self.output_dir / f"leg{legislature}"
        leg_dir.mkdir(parents=True, exist_ok=True)

        texts = self.discover_texts(legislature)
        if not texts:
            logger.warning(f"No texts found for legislature {legislature}")
            return 0, 0

        total_texts = 0
        total_amendments = 0
        skipped = 0
        failed = 0
        done = 0

        workers = max(1, self.max_workers)
        logger.info(
            f"Legislature {legislature}: collecting {len(texts)} texts "
            f"with {workers} workers"
        )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._collect_one_text,
                    legislature, text_info, leg_dir, force, fetch_details,
                ): text_info
                for text_info in texts
            }

            for future in as_completed(futures):
                text_info = futures[future]
                text_number = text_info["text_number"]
                expected = text_info["expected_count"]
                done += 1

                try:
                    status, count = future.result()
                except Exception as e:
                    logger.error(f"Unexpected error for text {text_number}: {e}")
                    failed += 1
                    continue

                total_amendments += count
                if status == "skipped":
                    total_texts += 1
                    skipped += 1
                elif status == "collected":
                    total_texts += 1
                    logger.info(
                        f"  [{done}/{len(texts)}] Leg {legislature} "
                        f"text {text_number}: {count}/{expected} amendments"
                    )
                else:
                    failed += 1

        logger.info(
            f"Legislature {legislature} complete: {total_amendments} amendments "
            f"across {total_texts} texts ({skipped} skipped, {failed} failed)"
        )
        return total_texts, total_amendments


# ----------------------------------------------------------------------
# Top-level collection function (called from pipeline)
# ----------------------------------------------------------------------

def collect_an_html_amendments(
    output_dir: str | Path | None = None,
    legislatures: list[int] | None = None,
    force: bool = False,
    fetch_details: bool = False,
    max_workers: int = DEFAULT_WORKERS,
) -> dict[int, tuple[int, int]]:
    """Collect amendments for legislatures 12 and/or 13 from AN archives.

    Discovers texts automatically from the search dropdown - no mapping
    file needed.

    Args:
        output_dir: Base output directory (default: data/raw/an_html_amendments).
        legislatures: List of legislatures to collect (default: [12, 13]).
        force: Re-download existing files.
        fetch_details: Fetch individual amendment detail pages.
        max_workers: Number of parallel download threads (default: 4).

    Returns:
        Dict mapping legislature -> (texts_count, amendments_count).
    """
    project_root = Path(__file__).parent.parent.parent

    if output_dir is None:
        output_dir = project_root / "data" / "raw" / "an_html_amendments"
    else:
        output_dir = Path(output_dir)

    if legislatures is None:
        legislatures = [12, 13]

    results = {}

    with ANHtmlAmendmentCollector(output_dir, max_workers=max_workers) as collector:
        for leg in legislatures:
            logger.info(f"=== Collecting legislature {leg} amendments ===")
            texts, amendments = collector.collect_legislature(
                leg, force=force, fetch_details=fetch_details,
            )
            results[leg] = (texts, amendments)

    total_t = sum(t for t, _ in results.values())
    total_a = sum(a for _, a in results.values())
    logger.info(f"AN HTML collection complete: {total_a} amendments across {total_t} texts")
    return results


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(
        description="Download AN amendments for legislatures 12-13",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=None,
        help="Output directory (default: data/raw/an_html_amendments)",
    )
    parser.add_argument(
        "--legislature", type=int, nargs="+", default=[12, 13],
        help="Legislature(s) to collect (default: 12 13)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-download existing files",
    )
    parser.add_argument(
        "--fetch-details", action="store_true",
        help="Also fetch individual amendment .asp detail pages",
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"Number of parallel download threads (default: {DEFAULT_WORKERS})",
    )
    args = parser.parse_args()

    results = collect_an_html_amendments(
        output_dir=args.output_dir,
        legislatures=args.legislature,
        force=args.force,
        fetch_details=args.fetch_details,
        max_workers=args.workers,
    )

    for leg, (texts, amendments) in sorted(results.items()):
        print(f"Legislature {leg}: {amendments} amendments across {texts} texts")
