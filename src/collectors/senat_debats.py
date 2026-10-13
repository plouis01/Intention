"""Sénat debate collector."""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup, NavigableString

logger = logging.getLogger(__name__)


class SenatDebatCollector:
    """Collector for Sénat floor debates from senat.fr."""

    BASE_URL = "https://www.senat.fr"

    def __init__(self, output_dir: Optional[Path] = None):
        self.output_dir = output_dir or Path("data/raw/senat/seances")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(timeout=60.0, follow_redirects=True)

    def fetch_seance(self, url: str) -> dict:
        """
        Fetch a Sénat debate session page and extract interventions.

        Args:
            url: Full URL to the séance page (e.g., https://www.senat.fr/seances/s202311/s20231124/s20231124020.html)

        Returns:
            Dict with date, titre, interventions list
        """
        logger.info(f"Fetching séance: {url}")

        try:
            response = self._client.get(url)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            # Extract date from URL
            date_match = re.search(r"/s(\d{8})/", url)
            date = None
            if date_match:
                date = datetime.strptime(date_match.group(1), "%Y%m%d")

            # Extract title
            title_elem = soup.find("h1") or soup.find("title")
            titre = title_elem.get_text(strip=True) if title_elem else "Séance"

            # Extract interventions
            interventions = []

            # Method 1: Modern Sénat structure with div.intervenant
            intervenant_divs = soup.find_all("div", class_="intervenant")
            if intervenant_divs:
                for div in intervenant_divs:
                    # Find speaker name in orateur_nom span
                    orateur_spans = div.find_all("span", class_="orateur_nom")
                    if orateur_spans:
                        # Take first span only (duplicates are common)
                        speaker = orateur_spans[0].get_text(strip=True)
                        # Clean up speaker name
                        speaker = re.sub(r"[.\s]+$", "", speaker)
                        speaker = re.sub(r"\s+", " ", speaker)
                        # Fix missing space after M./Mme/Mlle
                        speaker = re.sub(r"^(M\.|Mme|Mlle)([A-Z])", r"\1 \2", speaker)
                    else:
                        continue

                    # Find function/role if present in HTML comment
                    fonction = None
                    div_str = str(div)
                    qua_match = re.search(r'qua="([^"]+)"', div_str)
                    if qua_match and qua_match.group(1):
                        fonction = qua_match.group(1)

                    # Get content from paragraph(s) within this div
                    content_parts = []
                    for p in div.find_all("p"):
                        # Clone and remove orateur_nom spans to get clean content
                        p_copy = p.__copy__()
                        for span in p_copy.find_all("span", class_="orateur_nom"):
                            span.decompose()
                        p_text = p_copy.get_text(strip=True)
                        # Clean leading punctuation
                        p_text = re.sub(r"^[.\s]+", "", p_text)
                        if p_text and len(p_text) > 5:
                            content_parts.append(p_text)

                    content = " ".join(content_parts)
                    if content and len(content) > 20:
                        interventions.append({
                            "orateur": speaker,
                            "fonction": fonction,
                            "contenu": content[:5000],
                        })

            # Method 2: Fallback for older page structure
            # Pre-2002 pages have multiple speakers per <p>, separated by <strong> tags
            if not interventions:
                speaker_pattern = re.compile(
                    r"^\s*(M\.|Mme|Mlle)\s+"
                    r"("
                    r"l[ea]\s+(?:président(?:e)?|ministre|secrétaire|rapporteur)"
                    r"|"
                    r"(?:[A-ZÀ-Ý][a-zà-ÿ]+|d'[A-ZÀ-Ý][a-zà-ÿ]+|(?:de|du|le|la|des)\s+[A-ZÀ-Ý])[a-zà-ÿ]*"
                    r"(?:-[A-ZÀ-Ý][a-zà-ÿ]+)*"
                    r"(?:\s+(?:[A-ZÀ-Ý][a-zà-ÿ]+(?:-[A-ZÀ-Ý][a-zà-ÿ]+)*|d'[A-ZÀ-Ý][a-zà-ÿ]+|(?:de|du|le|la|des)))*"
                    r")"
                    r"(?:\s*\([^)]+\))?"
                    r"(?:\s*,\s*([^\.]+))?\s*[\.:,–—]"
                )
                _reject_first_words = {
                    "Celles", "Ceux", "Celle", "Celui",
                    "Cette", "Ces", "Certains", "Certaines",
                }

                for strong in soup.find_all("strong"):
                    strong_text = strong.get_text(strip=True)
                    match = speaker_pattern.match(strong_text)
                    if not match:
                        continue

                    # Reject pronoun-starting false speakers
                    name = match.group(2)
                    if not name.startswith("l"):
                        first_word = name.split()[0].split("-")[0].replace("d'", "")
                        if first_word in _reject_first_words:
                            continue

                    # Parse speaker name and optional function
                    speaker = f"{match.group(1)} {match.group(2)}".strip()
                    fonction = match.group(3).strip().rstrip(".") if match.group(3) else None

                    # Check for <em> immediately after <strong> (contains role)
                    next_sib = strong.next_sibling
                    while next_sib and isinstance(next_sib, NavigableString):
                        if next_sib.strip():
                            break
                        next_sib = next_sib.next_sibling
                    if next_sib and hasattr(next_sib, "name") and next_sib.name == "em":
                        fonction = next_sib.get_text(strip=True).rstrip(".")

                    # Collect text after this <strong> until next <strong>
                    content_parts = []
                    node = strong.next_sibling
                    while node:
                        if hasattr(node, "name") and node.name == "strong":
                            break
                        if hasattr(node, "name") and node.name == "em" and node == next_sib and fonction:
                            node = node.next_sibling
                            continue
                        text = node.get_text(strip=True) if hasattr(node, "get_text") else str(node).strip()
                        if text:
                            content_parts.append(text)
                        node = node.next_sibling

                    content = " ".join(content_parts)
                    content = re.sub(r"^[.\s]+", "", content)
                    if content and len(content) > 20:
                        interventions.append({
                            "orateur": speaker,
                            "fonction": fonction,
                            "contenu": content[:5000],
                        })

                # Deduplicate (old pages can have TOC + body with same speakers)
                seen = set()
                unique = []
                for interv in interventions:
                    key = (interv["orateur"], interv["contenu"][:100])
                    if key not in seen:
                        seen.add(key)
                        unique.append(interv)
                interventions = unique

            interventions = self._enrich_fonctions(interventions)

            return {
                "url": url,
                "date": date,
                "titre": titre,
                "interventions": interventions,
            }

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error fetching {url}: {e}")
            return None
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            return None

    def list_sessions_for_month(self, year: int, month: int) -> list[dict]:
        """
        Scrape the monthly index page to discover actual session dates.

        Args:
            year: Year (e.g. 1997)
            month: Month (1-12)

        Returns:
            List of dicts with 'date' (datetime) and 'url' (str) for each session.
        """
        month_str = f"{year}{month:02d}"
        index_url = f"{self.BASE_URL}/seances/s{month_str}/s{month_str}.html"

        try:
            response = self._client.get(index_url)
            response.raise_for_status()
        except httpx.HTTPStatusError:
            return []
        except Exception as e:
            logger.debug(f"No index for {year}-{month:02d}: {e}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        sessions = []
        seen_dates = set()

        # Look for links matching the session date pattern
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"s(\d{8})", href)
            if not m:
                continue
            date_str = m.group(1)
            if date_str in seen_dates:
                continue
            seen_dates.add(date_str)

            try:
                date = datetime.strptime(date_str, "%Y%m%d")
            except ValueError:
                continue

            # Build the mono URL
            mono_url = (
                f"{self.BASE_URL}/seances/s{month_str}/"
                f"s{date_str}/s{date_str}_mono.html"
            )
            sessions.append({"date": date, "url": mono_url})

        sessions.sort(key=lambda s: s["date"])
        return sessions

    def fetch_amendement(self, url: str) -> dict:
        """
        Fetch a Sénat amendment page.

        Args:
            url: Full URL to the amendment page

        Returns:
            Dict with amendment details
        """
        logger.info(f"Fetching amendement: {url}")

        try:
            response = self._client.get(url)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            # Extract amendment number from URL
            numero_match = re.search(r"Amdt_([^\.]+)\.html", url)
            numero = numero_match.group(1) if numero_match else None

            # Find author
            auteur = None
            for elem in soup.find_all(["p", "div"]):
                text = elem.get_text(strip=True)
                if "présenté par" in text.lower() or "auteur" in text.lower():
                    auteur = text
                    break

            # Find exposé des motifs
            expose = None
            objet_section = soup.find(string=re.compile("objet|exposé", re.I))
            if objet_section:
                parent = objet_section.find_parent()
                if parent:
                    expose = parent.get_text(strip=True)

            # Find sort (adopté, rejeté, etc.)
            sort = None
            for status in ["adopté", "rejeté", "retiré", "tombé"]:
                if soup.find(string=re.compile(status, re.I)):
                    sort = status
                    break

            return {
                "url": url,
                "numero": numero,
                "auteur": auteur,
                "expose_motifs": expose,
                "sort": sort,
            }

        except Exception as e:
            logger.error(f"Error fetching amendement {url}: {e}")
            return None

    @staticmethod
    def _enrich_fonctions(interventions: list[dict]) -> list[dict]:
        """Enrich fonction field within a single debate's interventions.

        Three passes:
        1. Name-based: orateur name implies a function
        2. President introductions: "La parole est à M. X, rapporteur..."
        3. Propagation: spread known functions to other interventions of same speaker
        """
        if not interventions:
            return interventions

        intro_pattern = re.compile(
            r"La parole est à (M\.|Mme|Mlle)\s+"
            r"([A-Za-zÀ-ü][A-Za-zÀ-ü\s\-]+?)"
            r",\s*([^\.]{5,80})\.",
            re.UNICODE,
        )
        skip_phrases = (
            "pour défendre", "pour présenter", "pour explication",
            "contre l'", "pour l'", "sur l'", "sur l'",
            "dont c'est", "avec l'autorisation",
        )

        speaker_functions: dict[str, str] = {}

        for interv in interventions:
            name = interv["orateur"]
            name_lower = name.lower()

            # Pass 1: name-based detection
            if interv.get("fonction") is None:
                if name in ("M. le président", "Mme la présidente"):
                    interv["fonction"] = "président de séance"
                elif "président" in name_lower or "présidente" in name_lower:
                    interv["fonction"] = "président de séance"
                elif "rapporteur" in name_lower:
                    m = re.search(r"(rapporteur\w*(?:\s+.+)?)", name, re.IGNORECASE)
                    interv["fonction"] = m.group(1).strip().rstrip(".") if m else "rapporteur"
                elif any(k in name_lower for k in ("ministre", "secrétaire d", "garde des sceaux")):
                    m = re.search(
                        r"(?:le|la)\s+(ministre.+|secrétaire\s+d.+|garde\s+des\s+sceaux.+)",
                        name, re.IGNORECASE,
                    )
                    interv["fonction"] = m.group(1).strip().rstrip(".") if m else "ministre"

            # Collect known functions for propagation
            if interv.get("fonction"):
                speaker_functions.setdefault(name, interv["fonction"])

            # Pass 2: parse president introductions
            if name in ("M. le président", "Mme la présidente"):
                normalized = re.sub(r"\s+", " ", interv.get("contenu", ""))
                for match in intro_pattern.finditer(normalized):
                    prefix = match.group(1)
                    speaker_name = match.group(2).strip()
                    role = match.group(3).strip().rstrip(",;")
                    if any(s in role.lower() for s in skip_phrases):
                        continue
                    full_name = f"{prefix} {speaker_name}"
                    speaker_functions.setdefault(full_name, role)

        # Pass 3: propagation
        for interv in interventions:
            if interv.get("fonction") is None and interv["orateur"] in speaker_functions:
                interv["fonction"] = speaker_functions[interv["orateur"]]

        return interventions

    def close(self):
        """Close HTTP client."""
        self._client.close()


def main():
    """Test the collector."""
    collector = SenatDebatCollector()

    # Test with LF 2024 debate
    result = collector.fetch_seance(
        "https://www.senat.fr/seances/s202311/s20231124/s20231124020.html"
    )

    if result:
        print(f"Date: {result['date']}")
        print(f"Titre: {result['titre']}")
        print(f"Interventions: {len(result['interventions'])}")
        for i, interv in enumerate(result["interventions"][:3]):
            print(f"  {i+1}. {interv['orateur']}: {interv['contenu'][:100]}...")

    collector.close()


if __name__ == "__main__":
    main()
