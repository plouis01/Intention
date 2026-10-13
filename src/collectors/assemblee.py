"""Assemblée Nationale open data collector."""

import json
import logging
import zipfile
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Base URLs for AN open data
AN_DATA_BASE = "https://data.assemblee-nationale.fr"
AN_OPENDATA_BASE = "https://data.assemblee-nationale.fr/static/openData"


class AssembleeCollector:
    """Collector for Assemblée Nationale open data."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(timeout=120.0, follow_redirects=True)

    def download_dossiers_archive(self, legislature: int = 17) -> Path:
        """
        Download the legislative dossiers archive for a legislature.

        Args:
            legislature: Legislature number (14, 15, 16, 17, etc.)

        Returns:
            Path to the downloaded/extracted data
        """
        # Archive URLs vary by legislature: 14 and 15 use roman numeral suffixes
        roman = {14: "XIV", 15: "XV"}
        if legislature in roman:
            suffix = f"_{roman[legislature]}"
        else:
            suffix = ""
        archive_url = f"{AN_OPENDATA_BASE}/repository/{legislature}/loi/dossiers_legislatifs/Dossiers_Legislatifs{suffix}.json.zip"

        output_path = self.output_dir / f"dossiers_leg{legislature}"
        output_path.mkdir(exist_ok=True)

        archive_file = output_path / "dossiers.json.zip"

        # Skip if already extracted (any JSON besides the zip itself)
        existing = list(output_path.rglob("*.json"))
        if existing:
            logger.info(f"Dossiers for legislature {legislature} already collected ({len(existing)} files), skipping")
            return output_path

        logger.info(f"Downloading dossiers for legislature {legislature}")
        try:
            response = self._client.get(archive_url)
            response.raise_for_status()

            with open(archive_file, "wb") as f:
                f.write(response.content)

            # Extract the archive
            with zipfile.ZipFile(archive_file, "r") as zf:
                zf.extractall(output_path)

            logger.info(f"Extracted dossiers to {output_path}")
            return output_path

        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to download dossiers archive: {e}")
            raise

    def download_amendements_archive(self, legislature: int = 17) -> Path:
        """
        Download the amendments archive for a legislature.

        Args:
            legislature: Legislature number

        Returns:
            Path to the extracted data
        """
        # URL patterns differ by legislature:
        # - Leg 14: /amendements_legis_XIV/Amendements_XIV.json.zip
        # - Leg 15: /amendements_legis/Amendements_XV.json.zip
        # - Leg 16+: /amendements_div_legis/Amendements.json.zip
        if legislature == 14:
            urls_to_try = [
                f"{AN_OPENDATA_BASE}/repository/14/loi/amendements_legis_XIV/Amendements_XIV.json.zip",
            ]
        elif legislature == 15:
            urls_to_try = [
                f"{AN_OPENDATA_BASE}/repository/15/loi/amendements_legis/Amendements_XV.json.zip",
            ]
        else:
            urls_to_try = [
                f"{AN_OPENDATA_BASE}/repository/{legislature}/loi/amendements_div_legis/Amendements.json.zip",
            ]

        output_path = self.output_dir / f"amendements_leg{legislature}"
        output_path.mkdir(exist_ok=True)

        archive_file = output_path / "amendements.json.zip"

        # Skip if already extracted (individual JSON files in json/ subdir)
        json_subdir = output_path / "json"
        if json_subdir.exists() and any(json_subdir.rglob("*.json")):
            count = sum(1 for _ in json_subdir.rglob("*.json"))
            logger.info(f"Amendements for legislature {legislature} already collected ({count} files), skipping")
            return output_path

        logger.info(f"Downloading amendements for legislature {legislature}")
        last_error = None
        for archive_url in urls_to_try:
            try:
                response = self._client.get(archive_url)
                response.raise_for_status()

                with open(archive_file, "wb") as f:
                    f.write(response.content)

                with zipfile.ZipFile(archive_file, "r") as zf:
                    zf.extractall(output_path)

                logger.info(f"Extracted amendements to {output_path}")
                return output_path

            except httpx.HTTPStatusError as e:
                logger.warning(f"URL failed for legislature {legislature}: {archive_url} ({e})")
                last_error = e

        logger.error(f"Failed to download amendements archive for legislature {legislature}")
        raise last_error

    def download_comptes_rendus(self, legislature: int = 17) -> Path:
        """
        Download debate transcripts (comptes rendus) for a legislature.

        Args:
            legislature: Legislature number

        Returns:
            Path to the extracted data
        """
        # SYCERON (Système de Comptes Rendus) archives
        archive_url = f"{AN_OPENDATA_BASE}/repository/{legislature}/vp/syceronbrut/syseron.xml.zip"

        output_path = self.output_dir / f"comptes_rendus_leg{legislature}"
        output_path.mkdir(exist_ok=True)

        archive_file = output_path / "syceron.xml.zip"

        # Skip if already extracted
        existing = list(output_path.rglob("*.xml"))
        if existing:
            logger.info(f"Comptes rendus for legislature {legislature} already collected ({len(existing)} files), skipping")
            return output_path

        logger.info(f"Downloading comptes rendus for legislature {legislature}")
        try:
            response = self._client.get(archive_url)
            response.raise_for_status()

            with open(archive_file, "wb") as f:
                f.write(response.content)

            with zipfile.ZipFile(archive_file, "r") as zf:
                zf.extractall(output_path)

            logger.info(f"Extracted comptes rendus to {output_path}")
            return output_path

        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to download comptes rendus archive: {e}")
            raise

    def get_dossier_by_ref(self, reference: str, legislature: int = 17) -> dict | None:
        """
        Get a specific legislative dossier by reference.

        Args:
            reference: Dossier reference (e.g., "DLR5L17N49106")
            legislature: Legislature number

        Returns:
            Dossier data or None if not found
        """
        dossiers_path = self.output_dir / f"dossiers_leg{legislature}"

        if not dossiers_path.exists():
            self.download_dossiers_archive(legislature)

        # Search through JSON files
        for json_file in dossiers_path.glob("**/*.json"):
            try:
                with open(json_file, encoding="utf-8") as f:
                    data = json.load(f)

                # Check if this is the right dossier
                if isinstance(data, dict):
                    dossier = data.get("dossierParlementaire", data)
                    if dossier.get("uid") == reference or reference in str(dossier):
                        return dossier

            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

        return None

    def get_amendements_for_dossier(
        self, dossier_ref: str, legislature: int = 17
    ) -> list[dict]:
        """
        Get all amendments for a specific legislative dossier.

        Args:
            dossier_ref: Dossier reference
            legislature: Legislature number

        Returns:
            List of amendments
        """
        amendements_path = self.output_dir / f"amendements_leg{legislature}"

        if not amendements_path.exists():
            self.download_amendements_archive(legislature)

        amendments = []

        for json_file in amendements_path.glob("**/*.json"):
            try:
                with open(json_file, encoding="utf-8") as f:
                    data = json.load(f)

                # Check if amendment belongs to this dossier
                if isinstance(data, dict):
                    amend = data.get("amendement", data)
                    if amend.get("dossierRef") == dossier_ref:
                        amendments.append(amend)

            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

        return amendments

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def parse_dossier(data: dict) -> dict[str, Any]:
    """
    Parse a legislative dossier from AN format.

    Args:
        data: Raw dossier data

    Returns:
        Parsed dossier with normalized fields
    """
    dossier = data.get("dossierParlementaire", data)

    return {
        "uid": dossier.get("uid"),
        "titre": dossier.get("titreDossier", {}).get("titre"),
        "legislature": dossier.get("legislature"),
        "etapes": [
            {
                "code": etape.get("codeStade"),
                "libelle": etape.get("libelleStade"),
                "date": etape.get("dateStade"),
            }
            for etape in dossier.get("actesLegislatifs", {}).get("acteLegislatif", [])
            if isinstance(etape, dict)
        ],
        "url_source": f"https://www.assemblee-nationale.fr/dyn/{dossier.get('legislature')}/dossiers/{dossier.get('uid')}",
    }


def parse_amendement(data: dict) -> dict[str, Any]:
    """
    Parse an amendment from AN format.

    Args:
        data: Raw amendment data

    Returns:
        Parsed amendment with normalized fields
    """
    amend = data.get("amendement", data)

    # Determine sort (outcome)
    sort_map = {
        "Adopté": "adopte",
        "Rejeté": "rejete",
        "Retiré": "retire",
        "Tombé": "tombe",
        "Non soutenu": "non_soutenu",
    }

    sort_raw = amend.get("sort", {})
    if isinstance(sort_raw, dict):
        sort_libelle = sort_raw.get("libelle", "")
    else:
        sort_libelle = str(sort_raw)

    sort = sort_map.get(sort_libelle, sort_libelle.lower() if sort_libelle else None)

    return {
        "numero": amend.get("numeroLong") or amend.get("numero"),
        "auteur": amend.get("signataires", {}).get("auteur", {}).get("acteurRef"),
        "objet": amend.get("corps", {}).get("contenuAuteur", {}).get("exposeSommaire"),
        "contenu": amend.get("corps", {}).get("contenuAuteur", {}).get("dispositif"),
        "expose_motifs": amend.get("corps", {}).get("contenuAuteur", {}).get("exposeSommaire"),
        "sort": sort,
        "article_ref": amend.get("pointeurFragmentTexte", {}).get("divisionArticle", {}).get("numeroArticle"),
        "date_depot": amend.get("dateDepot"),
        "dossier_ref": amend.get("dossierRef"),
        "uid": amend.get("uid"),
    }


def collect_an_data(laws_file: Path, output_dir: Path) -> None:
    """
    Collect AN data for all laws in the reference file.

    Args:
        laws_file: Path to laws reference JSON
        output_dir: Output directory for raw data
    """
    with open(laws_file) as f:
        data = json.load(f)

    with AssembleeCollector(output_dir / "assemblee") as collector:
        # Determine which legislatures we need
        # 15e: 2017-2022, 16e: 2022-2024, 17e: 2024-
        legislatures = [14, 15, 16, 17]

        for leg in legislatures:
            # Try each component separately to avoid skipping all data if one fails
            try:
                collector.download_dossiers_archive(leg)
            except Exception as e:
                logger.warning(f"Could not download dossiers for legislature {leg}: {e}")

            try:
                collector.download_amendements_archive(leg)
            except Exception as e:
                logger.warning(f"Could not download amendements for legislature {leg}: {e}")

            try:
                collector.download_comptes_rendus(leg)
            except Exception as e:
                logger.warning(f"Could not download comptes rendus for legislature {leg}: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    laws_file = Path("data/lois_finances_2012_2025.json")
    output_dir = Path("data/raw")

    if not laws_file.exists():
        print(f"Laws file not found: {laws_file}")
    else:
        collect_an_data(laws_file, output_dir)
