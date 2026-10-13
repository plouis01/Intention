"""Parser for the Sénat AMELI PostgreSQL dump.

Extracts amendments from the AMELI open data dump (PostgreSQL COPY format)
for texts related to CGI-modifying laws (lois de finances, LFSS, etc.).

The dump uses LATIN1 encoding with tab-separated COPY blocks terminated by \\.
"""

import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any, Generator

logger = logging.getLogger(__name__)

# Default paths
DEFAULT_SQL_PATH = Path("data/raw/senat/ameli/var/opt/opendata/ameli.sql")
DEFAULT_CGI_LAWS_PATH = Path("data/lois_cgi_1958_2026.json")
DEFAULT_OUTPUT_DIR = Path("data/raw/senat/ameli/amendments")

# AMELI sort lookup: id -> (label, code, type)
# Pre-defined since the table is only 13 rows and rarely changes
SORT_SESSION_MAP = {
    "A": "Adopté",
    "B": "Adopté - vote unique",
    "J": "Rejeté",
    "K": "Rejeté - vote unique",
    "R": "Retiré",
    "N": "Non soutenu",
    "S": "Tombé",
}

SORT_COMMISSION_MAP = {
    "1": "Adopté",
    "2": "Adopté avec modification",
    "3": "Rejeté",
    "4": "Retiré",
    "5": "Satisfait ou sans objet",
    "6": "",
}

# AMELI avis commission lookup: id -> label
AVIS_COMMISSION_MAP = {
    "G": "Avis du Gouvernement",
    "F": "Favorable",
    "D": "Défavorable",
    "S": "Sagesse du Sénat",
    "N": "Néant",
    "R": "Demande de retrait",
}

# AMELI avis gouvernement lookup: id -> label
AVIS_GOUVERNEMENT_MAP = {
    "F": "Favorable",
    "D": "Défavorable",
    "S": "Sagesse du Sénat",
    "N": "Néant",
    "R": "Demande de retrait",
}

# Normalized sort values (matching src/parsers/amendments.py SORT_MAPPING)
SORT_NORMALIZE = {
    "Adopté": "adopte",
    "Adopté - vote unique": "adopte",
    "Adopté avec modification": "adopte",
    "Rejeté": "rejete",
    "Rejeté - vote unique": "rejete",
    "Retiré": "retire",
    "Tombé": "tombe",
    "Non soutenu": "non_soutenu",
    "Satisfait ou sans objet": "non_soutenu",
    "": None,
}


def _unescape_pg_copy(value: str) -> str:
    """Unescape PostgreSQL COPY format backslash sequences.

    In COPY format:
    - \\N means NULL (handled separately)
    - \\\\ means literal backslash
    - \\n means newline
    - \\r means carriage return
    - \\t means tab (inside a field value)
    """
    result = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            next_char = value[i + 1]
            if next_char == "\\":
                result.append("\\")
                i += 2
            elif next_char == "n":
                result.append("\n")
                i += 2
            elif next_char == "r":
                result.append("\r")
                i += 2
            elif next_char == "t":
                result.append("\t")
                i += 2
            else:
                # Unknown escape, keep as-is
                result.append(value[i])
                i += 1
        else:
            result.append(value[i])
            i += 1
    return "".join(result)


def parse_copy_block(
    file_path: str | Path,
    table_name: str,
    columns: list[str],
) -> list[dict[str, Any]]:
    """Parse a COPY block from a PostgreSQL dump file.

    Scans the file line-by-line for the COPY statement matching the given
    table name, then reads tab-separated data rows until the \\. terminator.

    Args:
        file_path: Path to the SQL dump file.
        table_name: Name of the table to extract (e.g. 'amd', 'txt_ameli').
        columns: List of column names in the order they appear in the COPY statement.

    Returns:
        List of dicts mapping column names to values.
        \\N values are converted to None, text fields are backslash-unescaped.
    """
    file_path = Path(file_path)
    # Build the pattern to match the COPY line for this table
    copy_prefix = f"COPY {table_name} ("
    rows: list[dict[str, Any]] = []
    in_block = False
    num_cols = len(columns)

    logger.info(f"Parsing table '{table_name}' from {file_path.name}...")

    with open(file_path, encoding="latin-1") as f:
        for line in f:
            if not in_block:
                if line.startswith(copy_prefix):
                    in_block = True
                    logger.debug(f"Found COPY block for '{table_name}'")
                continue

            # Check for block terminator
            stripped = line.rstrip("\n").rstrip("\r")
            if stripped == "\\.":
                break

            # Parse tab-separated values
            parts = stripped.split("\t")
            if len(parts) != num_cols:
                logger.warning(
                    f"Table '{table_name}': expected {num_cols} columns, "
                    f"got {len(parts)} — skipping row"
                )
                continue

            row = {}
            for col, val in zip(columns, parts):
                if val == "\\N":
                    row[col] = None
                else:
                    row[col] = _unescape_pg_copy(val)
            rows.append(row)

    if not in_block:
        logger.warning(f"COPY block for '{table_name}' not found in {file_path}")
    else:
        logger.info(f"Parsed {len(rows)} rows from '{table_name}'")

    return rows


def _parse_copy_block_to_dict(
    file_path: str | Path,
    table_name: str,
    columns: list[str],
    key_column: str,
) -> dict[str, dict[str, Any]]:
    """Parse a COPY block and index by a key column.

    Convenience wrapper around parse_copy_block that returns a dict
    keyed by the specified column value for fast lookups.

    Args:
        file_path: Path to the SQL dump file.
        table_name: Name of the table to extract.
        columns: List of column names.
        key_column: Column name to use as dictionary key.

    Returns:
        Dict mapping key values to row dicts.
    """
    rows = parse_copy_block(file_path, table_name, columns)
    return {row[key_column]: row for row in rows if row.get(key_column) is not None}


def _strip_html(text: str | None) -> str | None:
    """Remove HTML tags and unescape HTML entities from text.

    Args:
        text: Raw HTML text or None.

    Returns:
        Clean plain text or None.
    """
    if not text:
        return None
    # Unescape HTML entities first (&#233; -> é, etc.)
    text = unescape(text)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text if text else None


def _load_cgi_law_numeros(cgi_laws_file: str | Path) -> set[str]:
    """Load CGI law numeros from the reference JSON file.

    Args:
        cgi_laws_file: Path to lois_cgi_1958_2026.json.

    Returns:
        Set of law numero strings (e.g. {'2023-1322', '58-1374', ...}).
    """
    path = Path(cgi_laws_file)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    numeros = set()
    for loi in data.get("lois", []):
        numero = loi.get("numero")
        if numero:
            numeros.add(numero)

    logger.info(f"Loaded {len(numeros)} CGI law numeros from {path.name}")
    return numeros


def _match_text_to_cgi(
    text: dict[str, Any],
    cgi_numeros: set[str],
) -> bool:
    """Check if an AMELI text record is related to a CGI-modifying law.

    Matches on:
    1. loifin='O' (finance law flag set in AMELI)
    2. doslegsignet or title matching a known CGI law numero

    Args:
        text: Row dict from txt_ameli table.
        cgi_numeros: Set of CGI law numero strings.

    Returns:
        True if the text is relevant.
    """
    # Direct finance law flag
    if text.get("loifin") == "O":
        return True

    # Check doslegsignet for law numero patterns
    signet = text.get("doslegsignet") or ""
    title = text.get("int") or ""
    full_title = text.get("inl") or ""

    for field in (signet, title, full_title):
        for numero in cgi_numeros:
            if numero in field:
                return True

    return False


def _normalize_sort_value(sor_id: str | None) -> str | None:
    """Convert an AMELI sort ID to a normalized sort value.

    Args:
        sor_id: The sorid value from the amd table (single char).

    Returns:
        Normalized sort string matching the project convention.
    """
    if not sor_id:
        return None
    label = SORT_SESSION_MAP.get(sor_id)
    if label is None:
        label = SORT_COMMISSION_MAP.get(sor_id, "")
    return SORT_NORMALIZE.get(label)


def extract_cgi_amendments(
    sql_path: str | Path,
    cgi_laws_file: str | Path,
) -> dict[str, list[dict[str, Any]]]:
    """Extract amendments related to CGI-modifying laws from the AMELI dump.

    Parses all necessary tables, filters texts by CGI relevance, joins
    amendments with texts, subdivisions, sort lookups, and authors.

    Args:
        sql_path: Path to ameli.sql dump file.
        cgi_laws_file: Path to lois_cgi_1958_2026.json.

    Returns:
        Dict mapping text ID (str) to list of amendment dicts.
    """
    sql_path = Path(sql_path)
    cgi_numeros = _load_cgi_law_numeros(cgi_laws_file)

    # --- Parse lookup tables (small) ---

    logger.info("Parsing lookup tables...")

    sor_rows = parse_copy_block(sql_path, "sor", ["id", "lib", "cod", "typ"])
    sor_map = {row["id"]: row for row in sor_rows}
    logger.info(f"  sor: {len(sor_map)} entries")

    # --- Parse texts ---

    txt_columns = [
        "id", "natid", "lecid", "sesinsid", "sesdepid", "fbuid",
        "num", "int", "inl", "datdep", "urg", "dis", "secdel",
        "loifin", "loifinpar", "txtamd", "datado", "numado", "txtexa",
        "pubdellim", "numabs", "libdelim", "libcplnat", "doslegsignet",
        "proacc", "txttyp", "ordsnddelib", "txtetaid", "fusderid",
        "fusder", "fusderord", "fusdertyp",
    ]
    txt_rows = parse_copy_block(sql_path, "txt_ameli", txt_columns)

    # Filter to CGI-relevant texts and build lookup
    txt_map: dict[str, dict[str, Any]] = {}
    for row in txt_rows:
        if _match_text_to_cgi(row, cgi_numeros):
            txt_id = row["id"]
            if txt_id is not None:
                txt_map[txt_id] = row

    logger.info(
        f"Filtered {len(txt_map)} CGI-relevant texts "
        f"from {len(txt_rows)} total texts"
    )
    del txt_rows  # Free memory

    if not txt_map:
        logger.warning("No CGI-relevant texts found — returning empty result")
        return {}

    # Build set of relevant text IDs for fast lookup
    relevant_txt_ids = set(txt_map.keys())

    # --- Parse subdivisions (articles) ---

    sub_columns = [
        "id", "txtid", "merid", "typid", "lic", "lib", "pos",
        "sig", "posder", "prires", "dupl", "subamd", "sorid",
        "txtidder", "style", "islec", "comdelid",
    ]
    sub_rows = parse_copy_block(sql_path, "sub", sub_columns)

    # Build lookup: sub_id -> sub row (only for relevant texts)
    sub_map: dict[str, dict[str, Any]] = {}
    for row in sub_rows:
        if row.get("txtid") in relevant_txt_ids:
            sub_id = row["id"]
            if sub_id is not None:
                sub_map[sub_id] = row
    logger.info(
        f"Indexed {len(sub_map)} subdivisions for relevant texts "
        f"(from {len(sub_rows)} total)"
    )
    del sub_rows

    # --- Parse amendment-senator join ---

    amdsen_columns = [
        "amdid", "senid", "rng", "qua", "nomuse", "prenomuse", "hom", "grpid",
    ]
    amdsen_rows = parse_copy_block(sql_path, "amdsen", amdsen_columns)

    # Build lookup: amd_id -> list of authors (sorted by rng)
    amdsen_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in amdsen_rows:
        amd_id = row.get("amdid")
        if amd_id is not None:
            amdsen_map[amd_id].append(row)

    # Sort each author list by rng (rank)
    for amd_id in amdsen_map:
        amdsen_map[amd_id].sort(key=lambda r: int(r.get("rng") or 0))

    logger.info(f"Indexed {len(amdsen_map)} amendment-senator links")
    del amdsen_rows

    # --- Parse amendments ---

    amd_columns = [
        "id", "subid", "amdperid", "motid", "etaid", "nomentid",
        "sorid", "avcid", "avgid", "irrid", "txtid", "opmid",
        "ocmid", "ideid", "discomid", "num", "rev", "typ", "dis",
        "obj", "datdep", "obs", "ord", "autext", "subpos", "mot",
        "numabs", "subidder", "libgrp", "alinea", "accgou", "colleg",
        "typrectid", "motposexa", "irrlo1113valid", "irrsaisiepar",
    ]
    amd_rows = parse_copy_block(sql_path, "amd", amd_columns)

    # --- Join and build output ---

    amendments_by_text: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped = 0

    for amd in amd_rows:
        txt_id = amd.get("txtid")
        if txt_id not in relevant_txt_ids:
            skipped += 1
            continue

        txt = txt_map.get(txt_id, {})
        sub_id = amd.get("subid")
        sub = sub_map.get(sub_id, {}) if sub_id else {}

        # Build author string from amdsen join
        amd_id = amd.get("id")
        authors = amdsen_map.get(amd_id, [])
        if authors:
            author_parts = []
            for a in authors:
                qua = a.get("qua") or ""
                prenom = a.get("prenomuse") or ""
                nom = a.get("nomuse") or ""
                author_parts.append(f"{qua} {prenom} {nom}".strip())
            auteur = ", ".join(author_parts)
        else:
            # Fallback to autext field
            auteur = amd.get("autext")

        # Build group label from first author's group or libgrp
        group_label = amd.get("libgrp")
        if not group_label and authors:
            group_label = authors[0].get("grpid")

        # Parse date
        datdep = amd.get("datdep")
        date_depot = None
        if datdep:
            try:
                date_depot = datetime.fromisoformat(datdep.strip())
            except (ValueError, AttributeError):
                pass

        # Resolve sort
        sor_id = amd.get("sorid")
        sort_normalized = _normalize_sort_value(sor_id)
        sort_label = None
        if sor_id and sor_id in sor_map:
            sort_label = sor_map[sor_id].get("lib")

        # Resolve avis
        avis_com = AVIS_COMMISSION_MAP.get(amd.get("avcid") or "", None)
        avis_gouv = AVIS_GOUVERNEMENT_MAP.get(amd.get("avgid") or "", None)

        # Build article reference from subdivision
        article_ref = sub.get("lic") or sub.get("lib")

        # Clean HTML from dispositif and objet
        dispositif = _strip_html(amd.get("dis"))
        objet = _strip_html(amd.get("obj"))

        amendment_dict = {
            "id_ameli": amd_id,
            "numero": amd.get("num"),
            "revision": amd.get("rev"),
            "type": amd.get("typ"),
            "auteur": auteur,
            "groupe": group_label,
            "dispositif": dispositif,
            "objet": objet,
            "sort": sort_normalized,
            "sort_label": sort_label,
            "avis_commission": avis_com,
            "avis_gouvernement": avis_gouv,
            "date_depot": date_depot.isoformat() if date_depot else None,
            "article_ref": article_ref,
            "article_lic": sub.get("lic"),
            "article_lib": sub.get("lib"),
            "sub_id": sub_id,
            "txt_id": txt_id,
            "txt_num": txt.get("num"),
            "txt_titre": txt.get("int"),
            "txt_titre_long": txt.get("inl"),
            "txt_doslegsignet": txt.get("doslegsignet"),
            "txt_loifin": txt.get("loifin"),
            "txt_datdep": txt.get("datdep"),
            "alinea": amd.get("alinea"),
            "ordre": amd.get("ord"),
            "chambre": "SENAT",
            "reference_externe": f"AMELI-{amd_id}",
        }
        amendments_by_text[txt_id].append(amendment_dict)

    logger.info(
        f"Extracted {sum(len(v) for v in amendments_by_text.values())} amendments "
        f"across {len(amendments_by_text)} texts "
        f"({skipped} amendments skipped for non-CGI texts)"
    )

    return dict(amendments_by_text)


def write_ameli_json(
    amendments_by_text: dict[str, list[dict[str, Any]]],
    output_dir: str | Path,
) -> int:
    """Write parsed amendments to JSON files, one per text.

    Args:
        amendments_by_text: Dict mapping text ID to list of amendment dicts.
        output_dir: Directory to write JSON files to.

    Returns:
        Number of files written.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for txt_id, amendments in amendments_by_text.items():
        if not amendments:
            continue

        # Build metadata from the first amendment's text info
        first = amendments[0]
        output = {
            "source": "ameli",
            "txt_id": txt_id,
            "txt_num": first.get("txt_num"),
            "txt_titre": first.get("txt_titre"),
            "txt_titre_long": first.get("txt_titre_long"),
            "txt_doslegsignet": first.get("txt_doslegsignet"),
            "txt_loifin": first.get("txt_loifin"),
            "txt_datdep": first.get("txt_datdep"),
            "chambre": "SENAT",
            "total_amendments": len(amendments),
            "amendments": amendments,
        }

        out_path = output_dir / f"text_{txt_id}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        written += 1

    logger.info(f"Wrote {written} JSON files to {output_dir}")
    return written


def main(
    sql_path: str | Path | None = None,
    cgi_laws_file: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Parse the AMELI dump and write amendment JSONs.

    Can be called from the pipeline or as a standalone script.

    Args:
        sql_path: Path to ameli.sql (defaults to DEFAULT_SQL_PATH).
        cgi_laws_file: Path to CGI laws JSON (defaults to DEFAULT_CGI_LAWS_PATH).
        output_dir: Output directory (defaults to DEFAULT_OUTPUT_DIR).

    Returns:
        Dict of amendments by text ID.
    """
    sql_path = Path(sql_path) if sql_path else DEFAULT_SQL_PATH
    cgi_laws_file = Path(cgi_laws_file) if cgi_laws_file else DEFAULT_CGI_LAWS_PATH
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR

    if not sql_path.exists():
        logger.error(f"AMELI dump not found: {sql_path}")
        return {}

    if not cgi_laws_file.exists():
        logger.error(f"CGI laws file not found: {cgi_laws_file}")
        return {}

    logger.info(f"Starting AMELI parser — dump: {sql_path}")
    logger.info(f"CGI laws reference: {cgi_laws_file}")

    amendments_by_text = extract_cgi_amendments(sql_path, cgi_laws_file)

    if amendments_by_text:
        write_ameli_json(amendments_by_text, output_dir)
    else:
        logger.warning("No amendments extracted — nothing to write")

    # Summary stats
    total = sum(len(v) for v in amendments_by_text.values())
    by_sort = defaultdict(int)
    for amds in amendments_by_text.values():
        for a in amds:
            by_sort[a.get("sort") or "unknown"] += 1

    logger.info(f"AMELI parser complete: {total} amendments, {len(amendments_by_text)} texts")
    for sort_val, count in sorted(by_sort.items(), key=lambda x: -x[1]):
        logger.info(f"  {sort_val}: {count}")

    return amendments_by_text


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    main()
