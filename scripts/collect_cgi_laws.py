#!/usr/bin/env python3
"""Collect all laws that modify the Code Général des Impôts (CGI) since 1958.

Uses the Légifrance PISTE API to:
  1. Fetch the CGI table of contents (all sections/articles)
  2. For each article, get the modification history (source laws)
  3. Deduplicate and output a comprehensive reference file

Usage:
    python scripts/collect_cgi_laws.py                    # Full run
    python scripts/collect_cgi_laws.py --resume            # Resume from checkpoint
    python scripts/collect_cgi_laws.py --sections-only     # Just list sections (test)
    python scripts/collect_cgi_laws.py --max-articles 100  # Limit for testing

Output:
    data/lois_cgi_1958_2026.json

Requires:
    PISTE_CLIENT_ID and PISTE_CLIENT_SECRET in .env
"""

import argparse
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx

from src.utils.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

CGI_TEXT_ID = "LEGITEXT000006069577"
CHECKPOINT_FILE = PROJECT_ROOT / "data" / "cgi_laws_checkpoint.json"
OUTPUT_FILE = PROJECT_ROOT / "data" / "lois_cgi_1958_2026.json"


class LegifranceCodeClient:
    """Client for querying Légifrance API about code structure and modifications."""

    def __init__(self):
        self.settings = get_settings()
        self._token: str | None = None
        self._token_expires: datetime | None = None
        self._client = httpx.Client(timeout=60.0)
        self._request_count = 0
        self._delay = 0.05  # Small delay between requests to avoid 403 bans
        self._now_ms = int(datetime.now().timestamp() * 1000)
        import threading
        self._lock = threading.Lock()

    def _get_token(self) -> str:
        if self._token and self._token_expires and datetime.now() < self._token_expires:
            return self._token

        if not self.settings.piste_client_id or not self.settings.piste_client_secret:
            raise ValueError(
                "PISTE credentials not configured. "
                "Set PISTE_CLIENT_ID and PISTE_CLIENT_SECRET in .env"
            )

        response = self._client.post(
            self.settings.piste_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.settings.piste_client_id,
                "client_secret": self.settings.piste_client_secret,
                "scope": "openid",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()

        data = response.json()
        self._token = data["access_token"]
        expires_in = data.get("expires_in", 3600) - 300
        self._token_expires = datetime.now() + timedelta(seconds=expires_in)
        logger.info("Obtained Légifrance API token")
        return self._token

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        if self._delay:
            time.sleep(self._delay)
        with self._lock:
            self._request_count += 1

        token = self._get_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        headers["Content-Type"] = "application/json"

        url = f"{self.settings.piste_api_base}{endpoint}"

        for attempt in range(5):
            try:
                response = self._client.request(method, url, headers=headers, **kwargs)
                if response.status_code in (429, 403):
                    wait = int(response.headers.get("Retry-After", 30 * (attempt + 1)))
                    logger.warning(f"Rate limited ({response.status_code}), "
                                   f"waiting {wait}s (attempt {attempt + 1}/5)")
                    time.sleep(wait)
                    # Increase delay to slow down future requests
                    self._delay = max(self._delay, 0.1 * (attempt + 1))
                    continue
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError:
                raise
            except httpx.HTTPError as e:
                if attempt < 4:
                    logger.warning(f"Request error: {e}, retrying...")
                    time.sleep(5 * (attempt + 1))
                else:
                    raise

        raise httpx.HTTPError(f"Max retries exceeded for {url}")

    def get_code_toc(self, text_id: str, sct_id: str | None = None,
                     date: int | None = None) -> dict:
        """Get table of contents for a code.

        Args:
            text_id: Code identifier (e.g. LEGITEXT000006069577 for CGI)
            sct_id: Section ID to drill into (None for root)
            date: Timestamp in ms (None for current)
        """
        body = {"textId": text_id}
        if sct_id:
            body["sctId"] = sct_id
        body["date"] = date or self._now_ms

        return self._request("POST", "/consult/code/tableMatieres", json=body)

    def get_article_with_history(self, article_id: str) -> dict:
        """Get article with ancestors (version history)."""
        return self._request(
            "POST",
            "/consult/getArticleWithAncestorsAndChildren",
            json={"id": article_id},
        )

    def get_article(self, article_id: str) -> dict:
        """Get a single article (without ancestors)."""
        return self._request(
            "POST",
            "/consult/getArticle",
            json={"id": article_id},
        )

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def collect_all_article_ids(client: LegifranceCodeClient,
                            text_id: str) -> list[dict]:
    """Collect all article IDs from the CGI table of contents.

    The root API response already contains the full tree (~3700+ articles).
    We walk it recursively, then drill into empty leaf sections that may
    contain additional articles not included in the root response.
    """
    seen_articles: set[str] = set()
    articles: list[dict] = []
    empty_leaves: list[str] = []

    def _walk(node: dict, collect_leaves: bool = True) -> None:
        """Recursively walk the TOC tree from a single API response."""
        for entry in node.get("articles", []):
            article_id = entry.get("id")
            if article_id and article_id not in seen_articles:
                seen_articles.add(article_id)
                articles.append({
                    "id": article_id,
                    "num": entry.get("num", ""),
                    "cid": entry.get("cid"),
                })
        for section in node.get("sections", []):
            child_sections = section.get("sections", [])
            child_articles = section.get("articles", [])
            if collect_leaves and not child_sections and not child_articles:
                sct_id = section.get("id")
                if sct_id:
                    empty_leaves.append(sct_id)
            _walk(section, collect_leaves=collect_leaves)

    # Step 1: fetch root and walk the full tree
    logger.info("Fetching CGI root table of contents...")
    toc = client.get_code_toc(text_id)
    _walk(toc, collect_leaves=True)
    logger.info(f"Root tree: {len(articles)} articles, "
                f"{len(empty_leaves)} empty leaf sections")

    # Step 2: drill into empty leaves to find additional articles
    # Snapshot the list so it doesn't grow during iteration
    leaves_to_drill = list(empty_leaves)
    if leaves_to_drill:
        visited = set()
        for i, sct_id in enumerate(leaves_to_drill):
            if sct_id in visited:
                continue
            visited.add(sct_id)
            try:
                child_toc = client.get_code_toc(text_id, sct_id=sct_id)
                # Don't collect new empty leaves — only extract articles
                _walk(child_toc, collect_leaves=False)
            except (httpx.HTTPStatusError, json.JSONDecodeError):
                pass
            if (i + 1) % 100 == 0:
                logger.info(f"  Drilled {i + 1}/{len(leaves_to_drill)} leaves "
                            f"({len(articles)} articles)")

        logger.info(f"After drilling leaves: {len(articles)} articles")

    logger.info(f"Found {len(articles)} unique articles in CGI")
    return articles


def extract_laws_from_article(client: LegifranceCodeClient,
                              article: dict,
                              deep: bool = False) -> list[dict]:
    """Extract all modifying laws from an article via getArticle endpoint.

    getArticle returns fields: lienModifications, lienCitations, lienAutres
    with keys: linkType, textTitle, textCid, numTexte, natureText, datePubli

    If deep=True, also fetches all historical versions of the article
    and extracts their lienModifications (captures older modifications
    not linked from the current version).
    """
    article_id = article["id"]
    laws = []

    try:
        data = client.get_article(article_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (404, 400, 403):
            logger.debug(f"Article {article['num']}: {e.response.status_code}")
            return []
        raise

    art_data = data.get("article")
    if not art_data or not isinstance(art_data, dict):
        return []

    # Extract from lienModifications (CREATION, MODIFIE, etc.)
    for lien in (art_data.get("lienModifications") or []):
        law_info = _parse_law_from_lien(lien)
        if law_info:
            laws.append(law_info)

    # Also scan lienCitations for LOI/ORDONNANCE references
    for lien in (art_data.get("lienCitations") or []):
        nature = (lien.get("natureText") or "").upper()
        if nature in ("LOI", "ORDONNANCE"):
            law_info = _parse_law_from_lien(lien)
            if law_info:
                laws.append(law_info)

    # Deep mode: fetch all historical versions' modifications
    if deep:
        versions = art_data.get("articleVersions", [])
        seen_ids = {article_id}
        for version in versions:
            vid = version.get("id")
            if not vid or vid in seen_ids:
                continue
            seen_ids.add(vid)
            try:
                vdata = client.get_article(vid)
            except httpx.HTTPStatusError:
                continue
            vart = vdata.get("article")
            if not vart or not isinstance(vart, dict):
                continue
            for lien in (vart.get("lienModifications") or []):
                law_info = _parse_law_from_lien(lien)
                if law_info:
                    laws.append(law_info)

    return laws


def _parse_law_from_lien(lien: dict) -> dict | None:
    """Parse law information from a getArticle lien object.

    Fields: textTitle, textCid, numTexte, natureText, datePubli, linkType
    """
    titre = lien.get("textTitle", "")
    if not titre:
        return None

    # Extract numero: prefer title extraction (more reliable for old laws)
    # numTexte often contains YYYY-MM (date) instead of real law number for pre-1998 texts
    numero_from_title = _extract_numero(titre)
    num_texte = lien.get("numTexte") or ""

    if numero_from_title:
        numero = numero_from_title
    elif num_texte and re.search(r"\d{4}-\d{3,}", num_texte):
        # numTexte with 3+ digit suffix is a real law number (e.g. 2023-1322)
        numero = num_texte
    else:
        return None

    text_cid = lien.get("textCid", "")

    # Convert datePubli (timestamp ms) to date string
    date_pub = ""
    date_ms = lien.get("datePubli")
    if date_ms and isinstance(date_ms, (int, float)):
        try:
            date_pub = datetime.fromtimestamp(date_ms / 1000).strftime("%Y-%m-%d")
        except (OSError, ValueError):
            pass
    if not date_pub:
        date_pub = _extract_date_from_titre(titre)

    return {
        "numero": numero,
        "type": _classify_law_type(titre),
        "titre": _clean_titre(titre),
        "date_promulgation": date_pub,
        "jorf": text_cid if text_cid.startswith("JORFTEXT") else "",
        "url_legifrance": (
            f"https://www.legifrance.gouv.fr/jorf/id/{text_cid}"
            if text_cid.startswith("JORFTEXT") else ""
        ),
    }


def _extract_numero(text: str) -> str | None:
    """Extract law number from text (e.g. 'n°2023-1322' or 'Loi 59-1472')."""
    # Modern format: n°2023-1322
    m = re.search(r"n[°o]?\s*(\d{4}-\d+)", text)
    if m:
        return m.group(1)
    # Old format in title: "Loi 59-1472" or "Ordonnance 67-830" or "LOI 62-873"
    # Pattern: text type + optional "CEE/CE" + number (YY-NNN or NNNN-NN)
    m = re.search(
        r"(?:loi|ordonnance|décret|arrêté|règlement)"
        r"(?:\s+(?:CEE?|CE))?"
        r"\s+(?:n[°o]?\s*)?(\d{2,4}-\d{2,})",
        text, re.IGNORECASE,
    )
    if m:
        return m.group(1)
    # Fallback: YYYY-NNN (but not YYYY-MM which is just a date)
    m = re.search(r"(\d{4}-\d{3,})", text)
    if m:
        return m.group(1)
    return None


def _classify_law_type(titre: str) -> str:
    """Classify law type from title text."""
    t = titre.lower()
    if "qpc" in t and "décision" in t:
        return "QPC"
    if "finances pour" in t or "budget" in t:
        return "LF"
    if "finances rectificative" in t or "fin de gestion" in t:
        return "LFR"
    if "financement de la sécurité sociale" in t:
        return "LFSS"
    if "ordonnance" in t:
        return "ORDONNANCE"
    if "décret" in t:
        return "DECRET"
    return "LOI"


def _clean_titre(titre: str) -> str:
    """Clean up a law title."""
    # Remove common prefixes
    titre = re.sub(r"^Loi\s+n[°o]\s*\d{4}-\d+\s+du\s+", "Loi du ", titre)
    return titre.strip()[:300]


def _extract_date_from_titre(titre: str) -> str:
    """Try to extract a date from a law title."""
    MONTHS = {
        "janvier": "01", "février": "02", "mars": "03", "avril": "04",
        "mai": "05", "juin": "06", "juillet": "07", "août": "08",
        "septembre": "09", "octobre": "10", "novembre": "11", "décembre": "12",
    }
    m = re.search(
        r"(\d{1,2})\s+(janvier|février|mars|avril|mai|juin|juillet|"
        r"août|septembre|octobre|novembre|décembre)\s+(\d{4})",
        titre, re.IGNORECASE,
    )
    if m:
        day = m.group(1).zfill(2)
        month = MONTHS.get(m.group(2).lower(), "01")
        year = m.group(3)
        return f"{year}-{month}-{day}"
    return ""


def _load_finance_law_types() -> dict[str, tuple[str, int]]:
    """Build a lookup of law numero -> (type, annee_budgetaire).

    Combines the curated reference file (2012-2025) with known historical
    LF/LFR/LFSS numbers for complete coverage.
    """
    lookup: dict[str, tuple[str, int]] = {}

    # 1. Load curated reference if available
    ref_file = Path(__file__).resolve().parent.parent / "data" / "lois_finances_2012_2025.json"
    if ref_file.exists():
        import json as _json
        with open(ref_file) as f:
            ref = _json.load(f)
        for loi in ref.get("lois", []):
            num = loi.get("numero", "")
            typ = loi.get("type", "")
            annee = loi.get("annee_budgetaire", 0)
            if num and typ:
                lookup[num] = (typ, annee)

    # 2. Historical LF (loi de finances initiale) — from 1959 to 2011
    # format: (numero, annee_budgetaire)
    historical_lf = [
        ("58-1374", 1959), ("59-1454", 1960),
        ("60-1384", 1961), ("61-1396", 1962), ("62-1529", 1963),
        ("63-1241", 1964), ("64-1279", 1965), ("65-997", 1966),
        ("66-935", 1967), ("67-1114", 1968), ("68-1172", 1969),
        ("69-1161", 1970), ("70-1199", 1971), ("71-1061", 1972),
        ("72-1121", 1973), ("73-1150", 1974), ("74-1129", 1975),
        ("75-1278", 1976), ("76-1232", 1977), ("77-1467", 1978),
        ("78-1239", 1979), ("79-1102", 1980), ("80-1094", 1981),
        ("81-1160", 1982), ("82-1126", 1983), ("83-1179", 1984),
        ("84-1208", 1985), ("85-1403", 1986), ("86-1317", 1987),
        ("87-1060", 1988), ("88-1149", 1989),
        ("89-935", 1990), ("90-1168", 1991), ("91-1322", 1992),
        ("92-1376", 1993), ("93-1352", 1994), ("94-1162", 1995),
        ("95-1346", 1996), ("96-1181", 1997), ("97-1269", 1998),
        ("98-1266", 1999), ("99-1172", 2000), ("2000-1352", 2001),
        ("2001-1275", 2002),
        ("2002-1575", 2003), ("2003-1311", 2004), ("2004-1484", 2005),
        ("2005-1719", 2006), ("2006-1666", 2007), ("2007-1822", 2008),
        ("2008-1425", 2009), ("2009-1673", 2010), ("2010-1657", 2011),
        ("2011-1977", 2012),
    ]
    for num, annee in historical_lf:
        if num not in lookup:
            lookup[num] = ("LF", annee)

    # 3. Historical LFR (loi de finances rectificative) — major ones
    historical_lfr = [
        # Pre-1982 LFR
        ("60-706", 1960), ("60-859", 1960),
        ("61-825", 1961),
        ("62-873", 1962),
        ("63-628", 1963), ("63-1293", 1963),
        ("64-1278", 1964),
        ("65-1154", 1965),
        ("66-948", 1966),
        ("69-1160", 1969),
        ("71-1025", 1971),
        ("72-1147", 1972),
        ("74-1114", 1974),
        ("75-408", 1975),
        ("76-978", 1976),
        ("78-653", 1978),
        ("80-1039", 1980),
        ("81-734", 1981), ("81-1179", 1981),
        # Post-1982
        ("82-540", 1982), ("85-1404", 1985), ("86-824", 1986),
        ("88-1193", 1988), ("91-1323", 1991), ("92-1476", 1992),
        ("93-859", 1993), ("93-1353", 1993), ("94-1163", 1994),
        ("95-885", 1995), ("95-1347", 1995),
        ("96-1182", 1996), ("97-1239", 1997),
        ("98-1267", 1998), ("99-1173", 1999),
        ("2000-656", 2000), ("2000-1353", 2000),
        ("2001-1276", 2001), ("2002-1050", 2002), ("2002-1576", 2002),
        ("2003-1312", 2003), ("2004-1485", 2004),
        ("2005-1720", 2005), ("2006-1771", 2006),
        ("2007-1824", 2007), ("2008-1443", 2008),
        ("2009-122", 2009), ("2009-1674", 2009),
        ("2010-237", 2010), ("2010-1658", 2010),
        ("2011-900", 2011), ("2011-1978", 2011),
        ("2017-1775", 2017),
        ("2020-289", 2020), ("2020-1473", 2020),
        ("2022-1499", 2022), ("2024-1167", 2024),
    ]
    for num, annee in historical_lfr:
        if num not in lookup:
            lookup[num] = ("LFR", annee)

    # 4. Historical LFSS (loi de financement de la sécurité sociale) — from 1996
    historical_lfss = [
        ("96-1160", 1997), ("97-1164", 1998), ("98-1194", 1999),
        ("99-1140", 2000), ("2000-1257", 2001), ("2001-1246", 2002),
        ("2002-1487", 2003), ("2003-1199", 2004), ("2004-1370", 2005),
        ("2005-1579", 2006), ("2006-1640", 2007), ("2007-1786", 2008),
        ("2008-1330", 2009), ("2009-1646", 2010), ("2010-1594", 2011),
        ("2011-1906", 2012), ("2012-1404", 2013),
        ("2013-1203", 2014), ("2014-1554", 2015),
        ("2015-1702", 2016), ("2016-1827", 2017),
        ("2017-1836", 2018), ("2018-1203", 2019),
        ("2019-1446", 2020), ("2020-1576", 2021),
        ("2021-1754", 2022), ("2022-1616", 2023),
        ("2023-1250", 2024), ("2025-199", 2025),
    ]
    for num, annee in historical_lfss:
        if num not in lookup:
            lookup[num] = ("LFSS", annee)

    return lookup


def deduplicate_laws(raw_laws: list[dict]) -> list[dict]:
    """Deduplicate laws by numero and enrich with type and annee_budgetaire."""
    seen = {}
    for law in raw_laws:
        numero = law.get("numero")
        if not numero:
            continue
        if numero not in seen:
            seen[numero] = law.copy()
        else:
            # Merge: keep the most complete entry
            existing = seen[numero]
            for key in ("titre", "date_promulgation", "jorf", "url_legifrance"):
                if not existing.get(key) and law.get(key):
                    existing[key] = law[key]

    # Reclassify using known finance law numbers
    finance_lookup = _load_finance_law_types()

    # Enrich with annee_budgetaire
    laws = []
    for numero, law in seen.items():
        # Override type from lookup if available
        if numero in finance_lookup:
            law["type"], known_annee = finance_lookup[numero]
            if known_annee:
                law["annee_budgetaire"] = known_annee
                laws.append(law)
                continue

        # Extract year from numero (YYYY-NNN or YY-NNN)
        m = re.match(r"(\d{4})-", numero)
        if m:
            year = int(m.group(1))
        else:
            # Old format: YY-NNN — derive year from date_promulgation
            dp = law.get("date_promulgation", "")
            if dp and len(dp) >= 4:
                year = int(dp[:4])
            else:
                year = 0

        # For LF, the budget year is usually year+1
        if law["type"] == "LF" and law.get("titre"):
            budget_m = re.search(r"pour\s+(\d{4})", law["titre"])
            if budget_m:
                law["annee_budgetaire"] = int(budget_m.group(1))
            else:
                law["annee_budgetaire"] = year + 1
        elif law["type"] == "LFR":
            budget_m = re.search(r"pour\s+(\d{4})", law["titre"])
            if budget_m:
                law["annee_budgetaire"] = int(budget_m.group(1))
            else:
                law["annee_budgetaire"] = year
        elif law["type"] == "LFSS":
            budget_m = re.search(r"pour\s+(\d{4})", law["titre"])
            if budget_m:
                law["annee_budgetaire"] = int(budget_m.group(1))
            else:
                law["annee_budgetaire"] = year + 1
        else:
            law["annee_budgetaire"] = year

        laws.append(law)

    # Fix aberrant dates from Légifrance (e.g. 2999-01-01)
    for law in laws:
        dp = law.get("date_promulgation", "")
        if dp and dp > "2030":
            # Derive date from titre instead
            from_titre = _extract_date_from_titre(law.get("titre", ""))
            if from_titre:
                law["date_promulgation"] = from_titre

    # Fix known data issues
    _fix_known_data_issues(laws)

    # Sort by date
    laws.sort(key=lambda x: x.get("date_promulgation", "0000"))
    return laws


def _fix_known_data_issues(laws: list[dict]) -> None:
    """Fix specific data issues found during verification."""
    by_num = {law["numero"]: law for law in laws}

    # 1. Swap 89-935 / 89-936 JORF IDs
    if "89-935" in by_num and "89-936" in by_num:
        lf = by_num["89-935"]
        lfr = by_num["89-936"]
        lf["jorf"], lfr["jorf"] = lfr["jorf"], lf["jorf"]
        lf["url_legifrance"] = f"https://www.legifrance.gouv.fr/jorf/id/{lf['jorf']}"
        lfr["url_legifrance"] = f"https://www.legifrance.gouv.fr/jorf/id/{lfr['jorf']}"

    # 2. Fix 97-34: wrong JORF ID
    if "97-34" in by_num:
        law = by_num["97-34"]
        law["jorf"] = "JORFTEXT000000198986"
        law["url_legifrance"] = "https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000000198986"
        law["titre"] = (
            "Décret n° 97-34 du 15 janvier 1997 relatif à la déconcentration "
            "des décisions administratives individuelles"
        )

    # 3. Fix EU regulations: use year from date_promulgation
    for law in laws:
        titre = law.get("titre", "")
        if "Règlement CE" in titre or "Règlement CEE" in titre or "JOCE" in titre:
            dp = law.get("date_promulgation", "")
            if dp and len(dp) >= 4:
                law["annee_budgetaire"] = int(dp[:4])

    # 4. Fix any remaining aberrant annee_budgetaire
    for law in laws:
        ab = law.get("annee_budgetaire", 0)
        dp = law.get("date_promulgation", "")
        if ab > 2030 and dp and len(dp) >= 4:
            law["annee_budgetaire"] = int(dp[:4])

    # 5. Remove misparsed duplicates and arrêtés (not laws)
    nums_to_remove = {
        "82-1179", "83-1317", "82-1202", "93-63",  # misparsed numbers
        "1986-05", "1988-03", "2002-03", "2006-12",  # arrêtés
    }
    laws[:] = [l for l in laws if l["numero"] not in nums_to_remove]

    # 6. Reclassify laws incorrectly typed as LF
    for numero in ("83-25", "84-1203"):
        if numero in by_num:
            by_num[numero]["type"] = "LOI"
    if "83-25" in by_num:
        by_num["83-25"]["annee_budgetaire"] = 1983

    # 7. Reclassify QPC decisions (not laws, but do modify the CGI)
    for law in laws:
        titre = law.get("titre", "")
        if "QPC" in titre and ("Décision" in titre or "DÉCISION" in titre):
            law["type"] = "QPC"


# Known finance laws (LF/LFR/LFSS) that modify the CGI but are not always
# discovered by crawling individual article modification links.
# Each entry: (numero, type, annee_budgetaire, titre, date_promulgation, jorf)
_KNOWN_FINANCE_LAWS = [
    # Historical LF not always linked from CGI articles
    ("58-1374", "LF", 1959,
     "Ordonnance n° 58-1374 du 30 décembre 1958 portant loi de finances pour 1959",
     "1958-12-30", "JORFTEXT000000886689"),
    ("59-1454", "LF", 1960,
     "Loi de finances pour 1960 (n° 59-1454 du 26 décembre 1959)",
     "1959-12-26", "JORFTEXT000000874213"),
    ("60-1384", "LF", 1961,
     "Loi n°60-1384 du 23 décembre 1960 portant loi de finances pour 1961",
     "1960-12-23", "JORFTEXT000000313949"),
    ("61-1396", "LF", 1962,
     "Loi de finances pour 1962 (n° 61-1396 du 21 décembre 1961)",
     "1961-12-21", "JORFTEXT000000314905"),
    ("62-1529", "LF", 1963,
     "Loi n°62-1529 du 22 décembre 1962 portant loi de finances pour 1963",
     "1962-12-22", "JORFTEXT000000875162"),
    ("63-1241", "LF", 1964,
     "Loi n°63-1241 du 19 décembre 1963 de finances pour 1964",
     "1963-12-19", "JORFTEXT000000875392"),
    ("64-1279", "LF", 1965,
     "Loi n° 64-1279 du 23 décembre 1964 portant loi de finances pour 1965",
     "1964-12-23", "JORFTEXT000000880293"),
    ("66-935", "LF", 1967,
     "LOI de finances pour 1967 (n° 66-935 du 17 décembre 1966)",
     "1966-12-17", "JORFTEXT000000320872"),
    ("68-1172", "LF", 1969,
     "Loi n°68-1172 du 27 décembre 1968 de finances pour 1969",
     "1968-12-27", "JORFTEXT000000693170"),
    ("69-1161", "LF", 1970,
     "Loi n°69-1161 du 24 décembre 1969 portant loi de finances pour 1970",
     "1969-12-24", "JORFTEXT000000521285"),
    ("71-1061", "LF", 1972,
     "LOI de finances pour 1972 n° 71-1061 du 29 décembre 1971",
     "1971-12-29", "JORFTEXT000000687436"),
    ("73-1150", "LF", 1974,
     "Loi de finances pour 1974 (n° 73-1150 du 27 décembre 1973)",
     "1973-12-27", "JORFTEXT000000509756"),
    ("74-1129", "LF", 1975,
     "Loi n°74-1129 du 30 décembre 1974 de finances pour 1975",
     "1974-12-30", "JORFTEXT000000889172"),
    ("95-1346", "LF", 1996,
     "LOI de finances pour 1996 (n° 95-1346 du 30 décembre 1995)",
     "1995-12-30", "JORFTEXT000000191068"),
    # LFSS not always linked from CGI articles
    ("96-1160", "LFSS", 1997,
     "LOI n° 96-1160 du 27 décembre 1996 de financement de la sécurité sociale pour 1997",
     "1996-12-27", "JORFTEXT000000198309"),
    ("99-1140", "LFSS", 2000,
     "LOI n° 99-1140 du 29 décembre 1999 de financement de la sécurité sociale pour 2000",
     "1999-12-29", "JORFTEXT000000214048"),
    ("2000-1257", "LFSS", 2001,
     "LOI n° 2000-1257 du 23 décembre 2000 de financement de la sécurité sociale pour 2001",
     "2000-12-23", "JORFTEXT000000579088"),
    ("2001-1246", "LFSS", 2002,
     "LOI n° 2001-1246 du 21 décembre 2001 de financement de la sécurité sociale pour 2002",
     "2001-12-21", "JORFTEXT000000396224"),
    ("2002-1487", "LFSS", 2003,
     "LOI n° 2002-1487 du 20 décembre 2002 de financement de la sécurité sociale pour 2003",
     "2002-12-20", "JORFTEXT000000235196"),
    ("2004-1370", "LFSS", 2005,
     "LOI n° 2004-1370 du 20 décembre 2004 de financement de la sécurité sociale pour 2005",
     "2004-12-20", "JORFTEXT000000790552"),
    ("2006-1640", "LFSS", 2007,
     "Loi n° 2006-1640 du 21 décembre 2006 de financement de la sécurité sociale pour 2007",
     "2006-12-21", "JORFTEXT000000817095"),
    # Pre-1982 LFR not always linked from CGI articles
    ("60-706", "LFR", 1960,
     "Loi n° 60-706 du 21 juillet 1960 de finances rectificative pour 1960",
     "1960-07-21", "JORFTEXT000000512208"),
    ("61-825", "LFR", 1961,
     "Loi n° 61-825 du 29 juillet 1961 de finances rectificative pour 1961",
     "1961-07-29", ""),
    ("63-1293", "LFR", 1963,
     "Loi n° 63-1293 du 21 décembre 1963 de finances rectificative pour 1963",
     "1963-12-21", ""),
    ("64-1278", "LFR", 1964,
     "Loi n° 64-1278 du 23 décembre 1964 de finances rectificative pour 1964",
     "1964-12-23", ""),
    ("65-1154", "LFR", 1965,
     "Loi n° 65-1154 du 30 décembre 1965 de finances rectificative pour 1965",
     "1965-12-30", ""),
    ("66-948", "LFR", 1966,
     "Loi n° 66-948 du 22 décembre 1966 de finances rectificative pour 1966",
     "1966-12-22", ""),
    ("69-1160", "LFR", 1969,
     "Loi n° 69-1160 du 24 décembre 1969 de finances rectificative pour 1969",
     "1969-12-24", ""),
    ("72-1147", "LFR", 1972,
     "Loi n° 72-1147 du 23 décembre 1972 de finances rectificative pour 1972",
     "1972-12-23", ""),
    ("74-1114", "LFR", 1974,
     "Loi n° 74-1114 du 27 décembre 1974 de finances rectificative pour 1974",
     "1974-12-27", ""),
    ("76-978", "LFR", 1976,
     "Loi n° 76-978 du 29 octobre 1976 de finances rectificative pour 1976",
     "1976-10-29", ""),
    ("78-653", "LFR", 1978,
     "Loi n° 78-653 du 22 juin 1978 de finances rectificative pour 1978",
     "1978-06-22", ""),
    ("80-1039", "LFR", 1980,
     "Loi n° 80-1039 du 23 décembre 1980 de finances rectificative pour 1980",
     "1980-12-23", "JORFTEXT000000339354"),
    ("81-734", "LFR", 1981,
     "Loi n° 81-734 du 3 août 1981 de finances rectificative pour 1981",
     "1981-08-03", ""),
    # Post-2002 LFR not always linked from CGI articles
    ("2020-289", "LFR", 2020,
     "LOI n° 2020-289 du 23 mars 2020 de finances rectificative pour 2020 (1)",
     "2020-03-23", "JORFTEXT000041746298"),
    ("2020-1473", "LFR", 2020,
     "LOI n° 2020-1473 du 30 novembre 2020 de finances rectificative pour 2020 (4)",
     "2020-11-30", "JORFTEXT000042580124"),
    ("2022-1499", "LFR", 2022,
     "LOI n° 2022-1499 du 1er décembre 2022 de finances rectificative pour 2022 (2)",
     "2022-12-01", "JORFTEXT000046672407"),
    ("2024-1167", "LFR", 2024,
     "LOI n° 2024-1167 du 6 décembre 2024 de finances de fin de gestion pour 2024",
     "2024-12-06", "JORFTEXT000050754939"),
]


def inject_known_finance_laws(laws: list[dict]) -> list[dict]:
    """Inject known finance laws that the crawl may not have discovered.

    Some finance laws (especially old LFs and LFSSs) are not referenced
    in individual CGI article modification links, so they are missed by
    the article-based crawl. This function adds them if missing.
    """
    existing = {law["numero"] for law in laws}
    injected = 0

    for numero, ltype, annee, titre, date_prom, jorf in _KNOWN_FINANCE_LAWS:
        if numero in existing:
            continue
        laws.append({
            "numero": numero,
            "type": ltype,
            "titre": titre,
            "date_promulgation": date_prom,
            "jorf": jorf,
            "url_legifrance": f"https://www.legifrance.gouv.fr/jorf/id/{jorf}",
            "annee_budgetaire": annee,
        })
        injected += 1

    if injected:
        logger.info(f"Injected {injected} known finance laws not found by crawl")
        laws.sort(key=lambda x: x.get("date_promulgation", "0000"))

    return laws


def save_checkpoint(articles_done: list[str], raw_laws: list[dict]):
    """Save progress to checkpoint file."""
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "articles_done": articles_done,
            "raw_laws": raw_laws,
            "timestamp": datetime.now().isoformat(),
        }, f, ensure_ascii=False)
    logger.debug(f"Checkpoint saved: {len(articles_done)} articles, {len(raw_laws)} laws")


def load_checkpoint() -> tuple[list[str], list[dict]]:
    """Load progress from checkpoint."""
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        logger.info(
            f"Resuming from checkpoint: {len(data['articles_done'])} articles done, "
            f"{len(data['raw_laws'])} laws found"
        )
        return data["articles_done"], data["raw_laws"]
    return [], []


def main():
    parser = argparse.ArgumentParser(
        description="Collect all laws modifying the CGI since 1958",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--resume", action="store_true",
                        help="Resume from checkpoint")
    parser.add_argument("--sections-only", action="store_true",
                        help="Only list sections (test mode)")
    parser.add_argument("--max-articles", type=int, default=0,
                        help="Limit number of articles to process (0=all)")
    parser.add_argument("--workers", type=int, default=8,
                        help="Number of parallel workers for phase 2 (default: 8)")
    parser.add_argument("--deep", action="store_true",
                        help="Fetch all historical versions of each article "
                             "(slower but finds older modifications)")
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE,
                        help="Output file path")

    args = parser.parse_args()

    with LegifranceCodeClient() as client:
        # Phase 1: Get all article IDs
        print("=== PHASE 1: Fetching CGI table of contents ===")
        articles = collect_all_article_ids(client, CGI_TEXT_ID)
        print(f"Found {len(articles)} articles in CGI")

        if args.sections_only:
            for art in articles[:50]:
                print(f"  {art['num']}: {art['id']}")
            print(f"  ... ({len(articles)} total)")
            return

        # Phase 2: Collect laws from each article's history (parallel)
        print("\n=== PHASE 2: Collecting modification history ===")

        if args.resume:
            articles_done, raw_laws = load_checkpoint()
        else:
            articles_done = []
            raw_laws = []

        done_set = set(articles_done)
        remaining = [a for a in articles if a["id"] not in done_set]

        if args.max_articles:
            remaining = remaining[:args.max_articles]

        total = len(remaining)
        workers = args.workers
        print(f"  {total} articles to process with {workers} workers")

        processed = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(extract_laws_from_article, client, art, args.deep): art
                for art in remaining
            }
            for future in as_completed(futures):
                article = futures[future]
                processed += 1
                try:
                    laws = future.result()
                    raw_laws.extend(laws)
                    articles_done.append(article["id"])
                except Exception as e:
                    logger.error(f"Error processing {article['num']}: {e}")
                    articles_done.append(article["id"])

                if processed % 100 == 0:
                    print(f"  {processed}/{total} articles "
                          f"({len(raw_laws)} raw law entries)")
                if processed % 500 == 0:
                    save_checkpoint(articles_done, raw_laws)

        # Final checkpoint
        save_checkpoint(articles_done, raw_laws)

        # Phase 3: Deduplicate and save
        print(f"\n=== PHASE 3: Deduplicating {len(raw_laws)} raw entries ===")
        laws = deduplicate_laws(raw_laws)
        print(f"Found {len(laws)} unique laws modifying the CGI")

        # Filter: keep only laws from 1958 onwards
        laws = [law for law in laws if law.get("date_promulgation", "") >= "1958"]
        print(f"After filtering (>= 1958): {len(laws)} laws")

        # Phase 3b: Inject known finance laws not found by crawl
        laws = inject_known_finance_laws(laws)
        print(f"After injecting known finance laws: {len(laws)} laws")

        # Build output
        output = {
            "description": (
                "Liste de toutes les lois modifiant le CGI depuis 1958"
            ),
            "last_updated": datetime.now().strftime("%Y-%m-%d"),
            "source": "API Légifrance (PISTE)",
            "cgi_text_id": CGI_TEXT_ID,
            "stats": {
                "total_laws": len(laws),
                "by_type": {},
            },
            "lois": laws,
        }

        # Count by type
        for law in laws:
            t = law.get("type", "AUTRE")
            output["stats"]["by_type"][t] = output["stats"]["by_type"].get(t, 0) + 1

        # Save
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        print(f"\nSaved to {args.output}")
        print("\nStatistics:")
        print(f"  Total: {len(laws)} laws")
        for t, count in sorted(output["stats"]["by_type"].items()):
            print(f"  {t}: {count}")
        print(f"\nAPI requests made: {client._request_count}")

        # Clean up checkpoint
        if CHECKPOINT_FILE.exists():
            CHECKPOINT_FILE.unlink()
            print("Checkpoint file cleaned up.")


if __name__ == "__main__":
    main()
