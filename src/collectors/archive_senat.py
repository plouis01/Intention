"""Collector for Sénat historical archives (5ème République, 1958-1996)."""

import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

ARCHIVE_SENAT_BASE = "https://www.senat.fr"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}
DEFAULT_DELAY = 2.0
BACKOFF_WAIT = 60
MAX_RETRIES = 3


class ArchiveSenatCollector:
    """Collector for Sénat historical archive PDFs (1958-1996)."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._client: httpx.Client | None = None
        self._delay = DEFAULT_DELAY

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

    def _get(self, url: str) -> httpx.Response:
        """GET with adaptive rate limiting and retries on 429/503."""
        for attempt in range(MAX_RETRIES):
            time.sleep(self._delay)
            try:
                resp = self._client.get(url)
                if resp.status_code in (429, 503):
                    try:
                        retry_after = int(resp.headers.get("Retry-After", BACKOFF_WAIT))
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

    def list_available_months(
        self, start_year: int = 1958, end_year: int = 1996
    ) -> list[tuple[int, int]]:
        """Scrape the archive index to discover available year/month pairs.

        Returns list of (year, month) tuples.
        """
        archive_url = (
            f"{ARCHIVE_SENAT_BASE}/comptes-rendus-seances/5eme/seances/archiveSeances.html"
        )
        try:
            resp = self._get(archive_url)
        except httpx.HTTPStatusError:
            logger.warning("Could not fetch Sénat archive index, falling back to range scan")
            return self._fallback_month_list(start_year, end_year)

        soup = BeautifulSoup(resp.text, "html.parser")
        months = []
        seen = set()

        # Look for links to monthly session pages: s{YYYYMM}/s{YYYYMM}.html
        pattern = re.compile(r"s(\d{4})(\d{2})")
        for a_tag in soup.find_all("a", href=True):
            match = pattern.search(a_tag["href"])
            if match:
                year, month = int(match.group(1)), int(match.group(2))
                if start_year <= year <= end_year and (year, month) not in seen:
                    seen.add((year, month))
                    months.append((year, month))

        months.sort()
        logger.info(f"Sénat archives: found {len(months)} available months")
        return months

    def _fallback_month_list(
        self, start_year: int, end_year: int
    ) -> list[tuple[int, int]]:
        """Generate all possible year/month pairs as fallback."""
        months = []
        for year in range(start_year, end_year + 1):
            start_month = 12 if year == 1958 else 1
            end_month = 5 if year == 1996 else 12
            for month in range(start_month, end_month + 1):
                months.append((year, month))
        return months

    def list_pdfs(self, year: int, month: int) -> list[dict]:
        """Fetch session index page and extract PDF links.

        Returns list of {"date": str, "url": str}.
        """
        ym = f"{year}{month:02d}"
        session_url = (
            f"{ARCHIVE_SENAT_BASE}/comptes-rendus-seances/5eme/seances/s{ym}/s{ym}.html"
        )
        try:
            resp = self._get(session_url)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug(f"No session page for {year}/{month:02d}")
                return []
            raise

        soup = BeautifulSoup(resp.text, "html.parser")
        pdfs = []
        seen = set()

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if href.lower().endswith(".pdf"):
                full_url = urljoin(session_url, href)
                if full_url not in seen:
                    seen.add(full_url)
                    # Try to extract date from filename: s{YYYYMMDD}_{start}_{end}.pdf
                    date_match = re.search(r"s(\d{8})", href)
                    date_str = date_match.group(1) if date_match else f"{year}{month:02d}01"
                    pdfs.append({"date": date_str, "url": full_url})

        logger.debug(f"Sénat {year}/{month:02d}: {len(pdfs)} PDFs")
        return pdfs

    def download_pdf(self, url: str, output_path: Path) -> Path:
        """Download a single PDF. Skips if file already exists (idempotent)."""
        if output_path.exists() and output_path.stat().st_size > 0:
            logger.debug(f"Already exists: {output_path}")
            return output_path

        output_path.parent.mkdir(parents=True, exist_ok=True)
        resp = self._get(url)

        with open(output_path, "wb") as f:
            f.write(resp.content)

        logger.debug(f"Downloaded: {output_path} ({len(resp.content)} bytes)")
        return output_path

    def download_month(self, year: int, month: int) -> list[Path]:
        """Download all PDFs for a month."""
        pdf_infos = self.list_pdfs(year, month)
        month_dir = self.output_dir / f"senat/{year}/{month:02d}"

        paths = []
        for info in pdf_infos:
            filename = info["url"].rsplit("/", 1)[-1]
            output_path = month_dir / filename
            try:
                path = self.download_pdf(info["url"], output_path)
                paths.append(path)
            except Exception as e:
                logger.error(f"Failed to download {info['url']}: {e}")

        if pdf_infos:
            logger.info(
                f"Sénat {year}/{month:02d}: downloaded {len(paths)}/{len(pdf_infos)} PDFs"
            )
        return paths
