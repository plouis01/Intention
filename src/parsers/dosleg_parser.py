"""Parser for Sénat DOSLEG PostgreSQL dump.

Parses COPY blocks from the dosleg.sql dump to extract dossier législatif
information. The dump uses tab-separated values with \\N for NULL.
"""

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _unescape_pg(val: str) -> str | None:
    """Unescape a PostgreSQL COPY value. \\N → None, handle backslash escapes."""
    if val == "\\N":
        return None
    val = val.replace("\\\\", "\\").replace("\\n", "\n").replace("\\t", "\t")
    return val.strip()


def parse_copy_block(
    dump_path: Path,
    table_name: str,
    encoding: str = "utf-8",
) -> tuple[list[str], list[list[str | None]]]:
    """Parse a single COPY ... FROM stdin block from a PostgreSQL dump.

    Args:
        dump_path: Path to the SQL dump file
        table_name: Name of the table to extract
        encoding: File encoding

    Returns:
        Tuple of (column_names, rows) where each row is a list of values
    """
    # Build regex to match the COPY line for this table
    # Format: COPY public.table_name (col1, col2, ...) FROM stdin;
    copy_pattern = re.compile(
        rf"^COPY\s+(?:public\.)?{re.escape(table_name)}\s+\(([^)]+)\)\s+FROM\s+stdin;",
        re.IGNORECASE,
    )

    columns: list[str] = []
    rows: list[list[str | None]] = []
    in_block = False

    with open(dump_path, encoding=encoding, errors="replace") as f:
        for line in f:
            if in_block:
                stripped = line.rstrip("\n")
                if stripped == "\\.":
                    break  # End of COPY data
                parts = stripped.split("\t")
                rows.append([_unescape_pg(p) for p in parts])
            else:
                m = copy_pattern.match(line)
                if m:
                    col_str = m.group(1)
                    columns = [c.strip().strip('"') for c in col_str.split(",")]
                    in_block = True

    return columns, rows


def parse_loi_table(dump_path: Path) -> list[dict[str, Any]]:
    """Parse the `loi` table from DOSLEG dump.

    Returns a list of dicts with key fields for each legislative dossier.
    """
    columns, rows = parse_copy_block(dump_path, "loi")

    if not columns:
        logger.error("Could not find 'loi' COPY block in DOSLEG dump")
        return []

    logger.info(f"Parsed {len(rows)} rows from DOSLEG loi table")

    results = []
    for row in rows:
        d = dict(zip(columns, row))
        results.append(d)

    return results


def parse_typloi_table(dump_path: Path) -> dict[str, str]:
    """Parse the `typloi` lookup table. Returns {code: label}."""
    columns, rows = parse_copy_block(dump_path, "typloi")
    if not columns:
        return {}
    result = {}
    for row in rows:
        d = dict(zip(columns, row))
        result[d.get("typloicod", "")] = d.get("typloilib", "")
    return result


def parse_etaloi_table(dump_path: Path) -> dict[str, str]:
    """Parse the `etaloi` lookup table. Returns {code: label}."""
    columns, rows = parse_copy_block(dump_path, "etaloi")
    if not columns:
        return {}
    result = {}
    for row in rows:
        d = dict(zip(columns, row))
        result[d.get("etaloicod", "")] = d.get("etaloilib", "")
    return result


def get_senat_dossiers(dump_path: Path) -> list[dict[str, Any]]:
    """Extract Sénat dossier info from DOSLEG dump, ready for DB import.

    Returns dossiers with:
        - numero: law number (e.g. "2023-1322")
        - signet: dossier identifier (e.g. "pjlf2024")
        - url: constructed Sénat URL
        - titre: short title
        - type: law type code
        - etat: status code
        - date_loi: enactment date
    """
    lois = parse_loi_table(dump_path)

    dossiers = []
    for loi in lois:
        numero = loi.get("numero")
        signet = loi.get("signet")
        etat = loi.get("etaloicod")

        # Only promulgated laws (etat=04) with a law number
        if not numero or etat != "04":
            continue

        # Build Sénat URL from signet
        url = None
        if signet:
            url = f"https://www.senat.fr/dossier-legislatif/{signet}.html"

        dossiers.append({
            "numero": numero,
            "signet": signet,
            "url": url,
            "titre": loi.get("loitit"),
            "titre_complet": loi.get("loiint"),
            "type": loi.get("typloicod"),
            "etat": etat,
            "date_loi": loi.get("date_loi"),
            "loicod": loi.get("loicod"),
        })

    logger.info(
        f"Extracted {len(dossiers)} promulgated dossiers from DOSLEG "
        f"(out of {len(lois)} total)"
    )
    return dossiers
