"""Collector for Assemblée Nationale historical archives (legislatures 1-11, 1958-2002)."""

import logging
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

ARCHIVE_AN_BASE = "https://archives.assemblee-nationale.fr"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}
DEFAULT_DELAY = 2.0
BACKOFF_WAIT = 60
MAX_RETRIES = 3


class ArchiveANCollector:
    """Collector for AN historical archive PDFs (legislatures 1-11)."""

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

    def list_sessions(self, legislature: int) -> list[dict]:
        """Fetch CRI index page and extract session links.

        Returns list of {"legislature": int, "session": str, "url": str}.
        """
        index_url = f"{ARCHIVE_AN_BASE}/{legislature}/cri/"
        resp = self._get(index_url)
        soup = BeautifulSoup(resp.text, "html.parser")

        sessions = []
        seen_urls = set()

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if href.endswith(".html") or href.endswith(".htm"):
                full_url = urljoin(index_url, href)
                if f"/{legislature}/cri/" in full_url and full_url not in seen_urls:
                    seen_urls.add(full_url)
                    # Extract session name from URL: {leg}-{session}.html
                    filename = full_url.rsplit("/", 1)[-1]
                    session_name = filename.replace(".html", "").replace(".htm", "")
                    # Strip legislature prefix if present
                    prefix = f"{legislature}-"
                    if session_name.startswith(prefix):
                        session_name = session_name[len(prefix):]
                    sessions.append({
                        "legislature": legislature,
                        "session": session_name,
                        "url": full_url,
                    })

        logger.info(f"Legislature {legislature}: found {len(sessions)} sessions")
        return sessions

    def list_pdfs(self, legislature: int, session: str) -> list[str]:
        """Fetch session HTML page and extract PDF links."""
        session_url = f"{ARCHIVE_AN_BASE}/{legislature}/cri/{legislature}-{session}.html"
        try:
            resp = self._get(session_url)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning(f"Session page not found: {session_url}")
                return []
            raise

        soup = BeautifulSoup(resp.text, "html.parser")
        pdf_urls = []
        seen = set()

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if href.lower().endswith(".pdf"):
                full_url = urljoin(session_url, href)
                if full_url not in seen:
                    seen.add(full_url)
                    pdf_urls.append(full_url)

        logger.info(f"Legislature {legislature}, session {session}: {len(pdf_urls)} PDFs")
        return pdf_urls

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

    def download_session(self, legislature: int, session: str) -> list[Path]:
        """Download all PDFs for a session."""
        pdf_urls = self.list_pdfs(legislature, session)
        session_dir = self.output_dir / f"leg{legislature}" / session

        paths = []
        for url in pdf_urls:
            filename = url.rsplit("/", 1)[-1]
            output_path = session_dir / filename
            try:
                path = self.download_pdf(url, output_path)
                paths.append(path)
            except Exception as e:
                logger.error(f"Failed to download {url}: {e}")

        logger.info(
            f"Legislature {legislature}, session {session}: "
            f"downloaded {len(paths)}/{len(pdf_urls)} PDFs"
        )
        return paths
