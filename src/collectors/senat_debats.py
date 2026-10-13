"""Sénat debate collector."""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup

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
                        # Combine all orateur_nom spans
                        speaker = " ".join(s.get_text(strip=True) for s in orateur_spans)
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
            if not interventions:
                for p in soup.find_all(["p", "div"]):
                    text = p.get_text(strip=True)

                    # Look for speaker patterns
                    speaker_match = re.match(
                        r"^(M\.|Mme|Mlle)\s+([^,\.\(]+)(?:,\s*([^\.]+))?", text
                    )

                    if speaker_match:
                        speaker = f"{speaker_match.group(1)} {speaker_match.group(2)}".strip()
                        fonction = speaker_match.group(3) if speaker_match.group(3) else None

                        # Content is everything after the speaker identification
                        content = text[speaker_match.end():].strip()
                        if content.startswith("."):
                            content = content[1:].strip()

                        if content and len(content) > 20:
                            interventions.append({
                                "orateur": speaker,
                                "fonction": fonction,
                                "contenu": content[:5000],
                            })

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
