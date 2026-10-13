"""Sénat open data collector."""

import csv
import json
import logging
import zipfile
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Base URL for Sénat open data
SENAT_DATA_BASE = "https://data.senat.fr"


class SenatCollector:
    """Collector for Sénat open data."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(timeout=120.0, follow_redirects=True)

    def download_dosleg(self) -> Path:
        """
        Download the DOSLEG dataset (parliamentary documents).

        Returns:
            Path to the downloaded data
        """
        output_path = self.output_dir / "dosleg"
        output_path.mkdir(exist_ok=True)

        # Skip if already extracted
        dosleg_sql = output_path / "dosleg.sql"
        if dosleg_sql.exists() and dosleg_sql.stat().st_size > 0:
            logger.info(f"DOSLEG already downloaded ({dosleg_sql.stat().st_size / 1e6:.0f} MB), skipping")
            return output_path

        # DOSLEG is available as ZIP archive containing SQL dump
        zip_url = f"{SENAT_DATA_BASE}/data/dosleg/dosleg.zip"

        logger.info("Downloading DOSLEG dataset")
        try:
            response = self._client.get(zip_url)
            response.raise_for_status()

            zip_file = output_path / "dosleg.zip"
            with open(zip_file, "wb") as f:
                f.write(response.content)

            # Extract the archive
            with zipfile.ZipFile(zip_file, "r") as zf:
                zf.extractall(output_path)

            logger.info(f"Extracted DOSLEG to {output_path}")
            return output_path

        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to download DOSLEG: {e}")
            raise

    def download_ameli(self) -> Path:
        """
        Download the AMELI dataset (amendments).

        Returns:
            Path to the downloaded data
        """
        output_path = self.output_dir / "ameli"
        output_path.mkdir(exist_ok=True)

        # Skip if already extracted (look for the SQL dump inside)
        ameli_sql = output_path / "var" / "opt" / "opendata" / "ameli.sql"
        if ameli_sql.exists() and ameli_sql.stat().st_size > 0:
            logger.info(f"AMELI already downloaded ({ameli_sql.stat().st_size / 1e6:.0f} MB), skipping")
            return output_path

        # AMELI is available as ZIP archive
        zip_url = f"{SENAT_DATA_BASE}/data/ameli/ameli.zip"

        logger.info("Downloading AMELI (amendments) dataset")
        try:
            response = self._client.get(zip_url)
            response.raise_for_status()

            zip_file = output_path / "ameli.zip"
            with open(zip_file, "wb") as f:
                f.write(response.content)

            # Extract the archive
            with zipfile.ZipFile(zip_file, "r") as zf:
                zf.extractall(output_path)

            logger.info(f"Extracted AMELI to {output_path}")
            return output_path

        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to download AMELI: {e}")
            raise

    def download_cri_archive(self) -> Path:
        """Download the bulk CRI archive (all debates since Jan 2003).

        The archive ``cri.zip`` contains individual XML debate files.
        Downloads via streaming to avoid memory issues (~500 MB),
        extracts CRI_*.xml files and flattens them into ``<output_dir>/cri/``.

        Returns:
            Path to the extracted CRI directory
        """
        output_path = self.output_dir / "cri"
        output_path.mkdir(exist_ok=True)

        # Idempotency marker
        marker = output_path / ".cri_downloaded"
        if marker.exists():
            logger.info("CRI archive already downloaded, skipping")
            return output_path

        zip_url = f"{SENAT_DATA_BASE}/data/debats/cri.zip"
        archive_file = output_path / "cri.zip"

        logger.info("Downloading Sénat CRI bulk archive (~500 MB) …")
        # Stream download to avoid memory issues
        with self._client.stream("GET", zip_url) as response:
            response.raise_for_status()
            with open(archive_file, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)

        logger.info(f"Downloaded CRI archive ({archive_file.stat().st_size / 1e6:.0f} MB)")

        # Extract XML files, flattening nested directories
        # Files are named d20YYMMDD.xml (e.g., cri/d20130409.xml)
        xml_count = 0
        with zipfile.ZipFile(archive_file, "r") as zf:
            for name in zf.namelist():
                basename = name.split("/")[-1]
                if basename.endswith(".xml"):
                    # Flatten: extract directly into output_path
                    target = output_path / basename
                    if not target.exists():
                        with zf.open(name) as src, open(target, "wb") as dst:
                            dst.write(src.read())
                        xml_count += 1

        logger.info(f"Extracted {xml_count} XML files from CRI archive")

        # Create marker file
        marker.touch()

        return output_path

    def download_comptes_rendus(self, year_start: int = 2020, year_end: int = 2025) -> Path:
        """
        Download debate transcripts (comptes rendus) for a date range.

        Args:
            year_start: Start year
            year_end: End year

        Returns:
            Path to the downloaded data
        """
        output_path = self.output_dir / "comptes_rendus"
        output_path.mkdir(exist_ok=True)

        # Comptes rendus are typically organized by session
        for year in range(year_start, year_end + 1):
            session = f"{year}-{year + 1}"
            self._download_session_comptes_rendus(session, output_path)

        return output_path

    def _download_session_comptes_rendus(self, session: str, output_path: Path) -> None:
        """Download comptes rendus for a specific session."""
        session_path = output_path / session
        session_path.mkdir(exist_ok=True)

        # Skip if already downloaded
        xml_file = session_path / "comptes_rendus.xml"
        if xml_file.exists() and xml_file.stat().st_size > 0:
            logger.debug(f"Sénat CR {session} already downloaded, skipping")
            return

        # Try XML format (Akoma Ntoso)
        xml_url = f"{SENAT_DATA_BASE}/data/comptes-rendus/seance/{session}/cr.xml"

        try:
            response = self._client.get(xml_url)
            response.raise_for_status()

            with open(xml_file, "wb") as f:
                f.write(response.content)

            logger.info(f"Downloaded comptes rendus for session {session}")

        except httpx.HTTPStatusError as e:
            logger.warning(f"Could not download comptes rendus for session {session}: {e}")

    def get_dossier_by_ref(self, reference: str) -> dict | None:
        """
        Get a specific dossier by reference from downloaded data.

        Args:
            reference: Dossier reference (e.g., "pjl19-139")

        Returns:
            Dossier data or None if not found
        """
        dosleg_path = self.output_dir / "dosleg"

        if not dosleg_path.exists():
            self.download_dosleg()

        # Search CSV files
        for csv_file in dosleg_path.glob("*.csv"):
            try:
                with open(csv_file, encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if reference in str(row.values()):
                            return dict(row)
            except Exception as e:
                logger.warning(f"Error reading {csv_file}: {e}")

        return None

    def get_amendements_for_dossier(self, dossier_ref: str) -> list[dict]:
        """
        Get all amendments for a specific dossier.

        Args:
            dossier_ref: Dossier reference

        Returns:
            List of amendments
        """
        ameli_path = self.output_dir / "ameli"

        if not ameli_path.exists():
            self.download_ameli()

        amendments = []

        # Try JSON
        json_file = ameli_path / "ameli.json"
        if json_file.exists():
            try:
                with open(json_file, encoding="utf-8") as f:
                    data = json.load(f)

                for amend in data if isinstance(data, list) else data.get("amendements", []):
                    if dossier_ref in str(amend.values()):
                        amendments.append(amend)

            except Exception as e:
                logger.warning(f"Error reading AMELI JSON: {e}")

        # Try CSV
        csv_file = ameli_path / "ameli.csv"
        if csv_file.exists() and not amendments:
            try:
                with open(csv_file, encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if dossier_ref in str(row.values()):
                            amendments.append(dict(row))
            except Exception as e:
                logger.warning(f"Error reading AMELI CSV: {e}")

        return amendments

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def parse_senat_amendement(data: dict) -> dict[str, Any]:
    """
    Parse an amendment from Sénat format.

    Args:
        data: Raw amendment data

    Returns:
        Parsed amendment with normalized fields
    """
    # Map Sénat field names to our schema
    sort_map = {
        "Adopté": "adopte",
        "Rejeté": "rejete",
        "Retiré": "retire",
        "Tombé": "tombe",
        "Non soutenu": "non_soutenu",
        "adopte": "adopte",
        "rejete": "rejete",
        "retire": "retire",
    }

    sort_raw = data.get("sort", data.get("etat", ""))
    sort = sort_map.get(sort_raw, sort_raw.lower() if sort_raw else None)

    return {
        "numero": data.get("numero") or data.get("num"),
        "auteur": data.get("auteur") or data.get("signataire"),
        "objet": data.get("objet") or data.get("expose"),
        "contenu": data.get("dispositif") or data.get("contenu"),
        "expose_motifs": data.get("expose") or data.get("motifs"),
        "sort": sort,
        "article_ref": data.get("article") or data.get("subdivision"),
        "date_depot": data.get("date_depot") or data.get("dateDepot"),
        "dossier_ref": data.get("dossier") or data.get("textRef"),
        "uid": data.get("id") or data.get("uid"),
    }


def collect_senat_data(laws_file: Path, output_dir: Path) -> None:
    """
    Collect Sénat data for all laws in the reference file.

    Args:
        laws_file: Path to laws reference JSON
        output_dir: Output directory for raw data
    """
    with open(laws_file) as f:
        data = json.load(f)

    with SenatCollector(output_dir / "senat") as collector:
        try:
            collector.download_dosleg()
        except Exception as e:
            logger.warning(f"Could not download DOSLEG: {e}")

        try:
            collector.download_ameli()
        except Exception as e:
            logger.warning(f"Could not download AMELI: {e}")

        try:
            collector.download_cri_archive()
        except Exception as e:
            logger.warning(f"Could not download CRI archive: {e}")

        try:
            collector.download_comptes_rendus(2002, 2026)
        except Exception as e:
            logger.warning(f"Could not download comptes rendus: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    laws_file = Path("data/lois_finances_2012_2025.json")
    output_dir = Path("data/raw")

    if not laws_file.exists():
        print(f"Laws file not found: {laws_file}")
    else:
        collect_senat_data(laws_file, output_dir)
