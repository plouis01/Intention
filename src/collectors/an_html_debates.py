"""Collector for AN HTML debates (legislatures 12-13, 2002-2012).

Downloads raw HTML debate pages from assemblee-nationale.fr with
idempotent checks and adaptive rate limiting.
"""

import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

AN_BASE_URL = "https://www.assemblee-nationale.fr"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}
DEFAULT_DELAY = 0.0
BACKOFF_WAIT = 60
MAX_RETRIES = 3

# Session registry — all known sessions per legislature
SESSIONS = {
    12: [
        "2001-2002", "2001-2002-extra",
        "2002-2003", "2002-2003-extra",
        "2003-2004", "2003-2004-extra",
        "2004-2005", "2004-2005-extra",
        "2005-2006", "2005-2006-extra",
        "2006-2007",
    ],
    13: [
        "2006-2007", "2006-2007-extra", "2006-2007-extra2",
        "2007-2008", "2007-2008-extra", "2007-2008-extra2",
        "2008-2009", "2008-2009-extra", "2008-2009-extra2",
        "2009-2010", "2009-2010-extra", "2009-2010-extra2",
        "2010-2011", "2010-2011-extra", "2010-2011-extra2", "2010-2011-extra3",
        "2011-2012",
    ],
}

# Content validation markers — at least one must be present in first 8KB
# (leg12 pages have ~6KB of boilerplate CSS/JS before content)
# Note: use ISO-8859-1 encoding for accented characters (é = \xe9)
_CONTENT_MARKERS = [b"compte rendu", b"rspeak", b"s\xe9ance", b"seance", b"<strong>"]


class ANHtmlCollector:
    """Collector for AN HTML debate pages (legislatures 12-13)."""

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

    def list_debates(self, legislature: int, session: str) -> list[dict]:
        """Fetch session index page and extract debate links.

        Returns list of {"legislature": int, "session": str, "filename": str,
                         "url": str, "title": str}.
        """
        index_url = f"{AN_BASE_URL}/{legislature}/cri/{session}/"
        try:
            resp = self._get(index_url)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 403):
                logger.warning(f"Session index not found: {index_url} ({e.response.status_code})")
                return []
            raise

        soup = BeautifulSoup(resp.content, "html.parser")
        debates = []
        seen = set()

        if legislature == 13:
            # LEG13 format: <table id="listecri">
            debates = self._parse_index_leg13(soup, legislature, session)
        else:
            # LEG12: try both formats
            debates = self._parse_index_leg12(soup, legislature, session)

        # Deduplicate
        unique = []
        for d in debates:
            if d["filename"] not in seen:
                seen.add(d["filename"])
                unique.append(d)

        if not unique:
            logger.info(f"No debate links found in {index_url}")

        logger.info(f"Legislature {legislature}, session {session}: {len(unique)} debates found")
        return unique

    def _parse_index_leg13(self, soup: BeautifulSoup, legislature: int,
                           session: str) -> list[dict]:
        """Parse leg13 index format: <table id="listecri">."""
        debates = []
        table = soup.find("table", id="listecri")
        if not table:
            # Fallback to generic link parsing
            return self._parse_index_generic(soup, legislature, session)

        for a_tag in table.find_all("a", href=True):
            href = a_tag["href"]
            if href.endswith(".asp") and not href.startswith("#"):
                filename = href.rsplit("/", 1)[-1]
                title = a_tag.get_text(strip=True)
                url = urljoin(f"{AN_BASE_URL}/{legislature}/cri/{session}/", href)
                debates.append({
                    "legislature": legislature,
                    "session": session,
                    "filename": filename,
                    "url": url,
                    "title": title,
                })

        return debates

    def _parse_index_leg12(self, soup: BeautifulSoup, legislature: int,
                           session: str) -> list[dict]:
        """Parse leg12 index formats (Format A and B)."""
        debates = []

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if href.startswith("#"):
                continue

            # Remove anchor fragment before checking extension
            base_href = href.split("#")[0]
            if not base_href.endswith(".asp"):
                continue

            # Extract filename
            filename = base_href.rsplit("/", 1)[-1]

            # Skip non-debate links (e.g., navigation links)
            if not re.match(r"\d{8}\.asp", filename):
                continue

            title = a_tag.get_text(strip=True)
            url = urljoin(f"{AN_BASE_URL}/{legislature}/cri/{session}/", base_href)
            debates.append({
                "legislature": legislature,
                "session": session,
                "filename": filename,
                "url": url,
                "title": title,
            })

        return debates

    def _parse_index_generic(self, soup: BeautifulSoup, legislature: int,
                             session: str) -> list[dict]:
        """Generic fallback: find all .asp links."""
        debates = []
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if href.endswith(".asp") and not href.startswith("#"):
                filename = href.rsplit("/", 1)[-1]
                if re.match(r"\d{8}\.asp", filename):
                    title = a_tag.get_text(strip=True)
                    url = urljoin(f"{AN_BASE_URL}/{legislature}/cri/{session}/", href)
                    debates.append({
                        "legislature": legislature,
                        "session": session,
                        "filename": filename,
                        "url": url,
                        "title": title,
                    })
        return debates

    def download_debate(self, url: str, output_path: Path,
                        force: bool = False) -> Path | None:
        """Download a single debate HTML. Idempotent — skips existing files.

        Returns path on success, None on failure.
        """
        if not force and output_path.exists() and output_path.stat().st_size > 0:
            logger.debug(f"Already exists: {output_path}")
            return output_path

        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            resp = self._get(url)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning(f"Debate not found (404): {url}")
                return None
            raise

        content = resp.content

        # Content validation: check first 8KB for debate markers
        head = content[:8192].lower()
        if not any(marker in head for marker in _CONTENT_MARKERS):
            logger.warning(f"No debate content markers in {url}, skipping")
            return None

        with open(output_path, "wb") as f:
            f.write(content)

        logger.debug(f"Downloaded: {output_path} ({len(content)} bytes)")
        return output_path

    def download_session(self, legislature: int, session: str,
                         force: bool = False) -> list[Path]:
        """Download all debates for a session."""
        debates = self.list_debates(legislature, session)
        session_dir = self.output_dir / f"leg{legislature}" / session

        new_downloads = 0
        skipped = 0
        failed = 0
        paths = []

        for debate in debates:
            # Replace .asp with .html for local storage
            local_filename = debate["filename"].replace(".asp", ".html")
            output_path = session_dir / local_filename
            existed = output_path.exists() and output_path.stat().st_size > 0

            try:
                path = self.download_debate(debate["url"], output_path, force=force)
                if path:
                    paths.append(path)
                    if existed and not force:
                        skipped += 1
                    else:
                        new_downloads += 1
                else:
                    failed += 1
            except Exception as e:
                logger.error(f"Failed to download {debate['url']}: {e}")
                failed += 1

        logger.info(
            f"Session {session}: {new_downloads} new, "
            f"{skipped} skipped, {failed} failed (total {len(debates)})"
        )
        return paths

    def download_legislature(self, legislature: int,
                             force: bool = False) -> list[Path]:
        """Download all sessions for a legislature."""
        sessions = SESSIONS.get(legislature, [])
        all_paths = []
        for session in sessions:
            paths = self.download_session(legislature, session, force=force)
            all_paths.extend(paths)
        logger.info(
            f"Legislature {legislature}: {len(all_paths)} total debates downloaded"
        )
        return all_paths

    def list_commissions_elargies(self, year: int) -> list[dict]:
        """Fetch commission élargie index page and extract debate links."""
        index_url = f"{AN_BASE_URL}/13/budget/plf{year}/commissions_elargies/cr/"
        try:
            resp = self._get(index_url)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 403):
                logger.warning(f"Commission élargie index not found: {index_url}")
                return []
            raise

        soup = BeautifulSoup(resp.content, "html.parser")
        debates = []
        seen = set()

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if href.endswith(".asp") and not href.startswith("#"):
                filename = href.rsplit("/", 1)[-1]
                # Skip navigation links (aide.asp, plan.asp, etc.)
                # Real commission debate files match C###.asp pattern
                if not re.match(r"C\d+\.asp", filename):
                    continue
                if filename in seen:
                    continue
                seen.add(filename)
                title = a_tag.get_text(strip=True)
                url = urljoin(index_url, href)
                debates.append({
                    "legislature": 13,
                    "session": f"commission_elargie/plf{year}",
                    "filename": filename,
                    "url": url,
                    "title": title,
                })

        logger.info(f"Commission élargie PLF {year}: {len(debates)} debates found")
        return debates

    def download_commissions_elargies(self, year: int) -> list[Path]:
        """Download commission élargie debates for a given PLF year."""
        debates = self.list_commissions_elargies(year)
        output_dir = self.output_dir / "leg13" / "commission_elargie" / f"plf{year}"

        paths = []
        for debate in debates:
            local_filename = debate["filename"].replace(".asp", ".html")
            output_path = output_dir / local_filename
            try:
                path = self.download_debate(debate["url"], output_path)
                if path:
                    paths.append(path)
            except Exception as e:
                logger.error(f"Failed to download {debate['url']}: {e}")

        logger.info(f"Commission élargie PLF {year}: {len(paths)} debates downloaded")
        return paths
