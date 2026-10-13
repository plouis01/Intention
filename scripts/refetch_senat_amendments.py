"""Re-fetch Sénat amendments from senat.fr CSV and update NULL records in DB."""

import csv
import io
import logging
import os
import re
import sys

import httpx
import psycopg2

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

PG_URL = os.environ["DATABASE_URL"]
SENAT_CSV_URL = "https://www.senat.fr/amendements/{session}/{text}/jeu_complet_{session}_{text}.csv"

# Known text numbers for pjlfYYYY-format dossiers (discovered from senat.fr)
PJLF_TEXT_NUMBERS = {
    2003: [("2002-2003", "67"), ("2002-2003", "96")],
    2004: [("2003-2004", "72"), ("2003-2004", "115")],
    2005: [("2004-2005", "73"), ("2004-2005", "125")],
    2006: [("2005-2006", "98"), ("2005-2006", "134")],
    2007: [("2006-2007", "77"), ("2006-2007", "124")],
    2008: [("2007-2008", "90"), ("2007-2008", "139")],
    2009: [("2008-2009", "98"), ("2008-2009", "138")],
    2010: [("2009-2010", "100"), ("2009-2010", "160")],
    2011: [("2010-2011", "110"), ("2010-2011", "170")],
    2012: [("2011-2012", "106")],
    2013: [("2012-2013", "147")],
    2014: [("2013-2014", "155")],
    2015: [("2014-2015", "107")],
    2016: [("2015-2016", "163")],
    2017: [("2016-2017", "139")],
    2018: [("2017-2018", "107"), ("2017-2018", "172")],
    2019: [("2018-2019", "146")],
}


def download_senat_csv(client: httpx.Client, session: str, text: str) -> list[dict]:
    """Download and parse a Sénat amendment CSV."""
    url = SENAT_CSV_URL.format(session=session, text=text)
    log.info(f"  Downloading {url}")

    csv.field_size_limit(sys.maxsize)
    r = client.get(url)
    if r.status_code != 200:
        log.warning(f"    HTTP {r.status_code}")
        return []

    text_content = r.content.decode("latin-1")
    lines = text_content.split("\n")
    if lines[0].startswith("sep="):
        text_content = "\n".join(lines[1:])

    reader = csv.DictReader(io.StringIO(text_content), delimiter="\t")
    rows = list(reader)
    log.info(f"    Got {len(rows)} amendments")
    return rows


def strip_rect(numero: str) -> str:
    """Strip 'rect.' / 'rect. bis' suffixes for base matching."""
    return re.sub(r"\s*rect\..*$", "", numero).strip()


def normalize_numero(numero: str) -> str:
    """Normalize amendment numero for matching."""
    if not numero:
        return ""
    n = numero.strip()
    if n.lower().startswith("amt "):
        n = n[4:]
    return n.strip()


def parse_sort(sort_raw: str) -> str | None:
    """Parse sort value from free-text Sénat format."""
    if not sort_raw:
        return None
    s = sort_raw.lower()
    if "adopt" in s:
        return "adopte"
    if "rejet" in s:
        return "rejete"
    if "retir" in s:
        return "retire"
    if "tomb" in s:
        return "tombe"
    if "non soutenu" in s or "non-soutenu" in s:
        return "non_soutenu"
    return None


def normalize_auteur(auteur: str) -> str:
    """Normalize auteur name for fuzzy matching."""
    if not auteur:
        return ""
    # Take first name listed (before comma), uppercase, strip titles
    first = auteur.split(",")[0].strip()
    first = re.sub(r"^(M\.\s*|Mme\s*|Mlle\s*)", "", first)
    return first.upper().strip()


def update_amendments_from_csv(pg_conn, dossier_id: int, csv_rows: list[dict]):
    """Match CSV rows to DB records and update NULL fields."""
    cur = pg_conn.cursor()

    cur.execute(
        """SELECT id, numero, auteur, reference_externe
           FROM amendements
           WHERE dossier_id = %s AND objet IS NULL""",
        (dossier_id,),
    )
    null_amendments = cur.fetchall()
    if not null_amendments:
        return 0

    log.info(f"    {len(null_amendments)} NULL amendments in DB")

    # Build lookup: base_numero → list of DB rows
    db_by_numero: dict[str, list[tuple]] = {}
    for row in null_amendments:
        n = normalize_numero(row[1])
        base = strip_rect(n)
        db_by_numero.setdefault(base, []).append(row)
        if base != n:
            db_by_numero.setdefault(n, []).append(row)

    updated = 0
    matched_ids = set()

    for csv_row in csv_rows:
        numero_raw = normalize_numero(csv_row.get("Numéro ", ""))
        numero_base = strip_rect(numero_raw)
        objet = (csv_row.get("Objet ") or "").strip()
        dispositif = (csv_row.get("Dispositif ") or "").strip()
        sort_raw = (csv_row.get("Sort ") or "").strip()
        url = (csv_row.get("Url amendement ") or "").strip()
        csv_auteur = (csv_row.get("Auteur ") or "").strip()

        if not objet and not dispositif:
            continue

        sort = parse_sort(sort_raw)

        # Try exact match first, then base match
        candidates = []
        for key in [numero_raw, numero_base]:
            if key in db_by_numero:
                candidates = [r for r in db_by_numero[key] if r[0] not in matched_ids]
                if candidates:
                    break

        if not candidates:
            continue

        # If multiple candidates, try matching by auteur
        best = candidates[0]
        if len(candidates) > 1 and csv_auteur:
            csv_auteur_norm = normalize_auteur(csv_auteur)
            for c in candidates:
                if csv_auteur_norm and csv_auteur_norm in (c[2] or "").upper():
                    best = c
                    break

        # Build update
        update_fields = []
        update_vals = []

        if objet:
            update_fields.append("objet = %s")
            update_vals.append(objet)
            update_fields.append("expose_motifs = %s")
            update_vals.append(objet)
        if dispositif:
            update_fields.append("contenu = %s")
            update_vals.append(dispositif)
        if sort:
            update_fields.append("sort = %s")
            update_vals.append(sort)
        if url:
            update_fields.append("url_source = %s")
            update_vals.append(url)

        if update_fields:
            update_vals.append(best[0])
            cur.execute(
                f"UPDATE amendements SET {', '.join(update_fields)} WHERE id = %s",
                update_vals,
            )
            matched_ids.add(best[0])
            updated += 1

    pg_conn.commit()
    log.info(f"    Updated {updated}/{len(null_amendments)} NULL amendments")
    return updated


def discover_csv_url(client: httpx.Client, url_source: str, annee: int) -> list[tuple[str, str]]:
    """Discover (session, text) pairs for a dossier."""
    results = []

    # Try pjlXX-NNN format
    m = re.search(r"pjl(\d{2})-(\d+)", url_source or "")
    if m:
        year_short = int(m.group(1))
        text = m.group(2)
        year_full = 2000 + year_short if year_short < 50 else 1900 + year_short
        session = f"{year_full}-{year_full + 1}"
        results.append((session, text))
        return results

    # Try pjlfYYYY format - use known text numbers
    m = re.search(r"pjlf(\d{4})", url_source or "")
    if m:
        budget_year = int(m.group(1))
        if budget_year in PJLF_TEXT_NUMBERS:
            return PJLF_TEXT_NUMBERS[budget_year]

        # Try to scrape the dossier page for amendment links
        try:
            r = client.get(url_source)
            if r.status_code == 200:
                amend_refs = re.findall(r"/amendements/(\d{4}-\d{4})/(\d+)/", r.text)
                seen = set()
                for session, text in amend_refs:
                    key = (session, text)
                    if key not in seen:
                        seen.add(key)
                        results.append(key)
        except Exception:
            pass

    return results


def main():
    log.info("=== Re-fetching Sénat amendments from senat.fr CSVs ===\n")

    pg_conn = psycopg2.connect(PG_URL)
    client = httpx.Client(timeout=120, follow_redirects=True)

    cur = pg_conn.cursor()
    cur.execute("""
        SELECT dl.id, dl.url_source, l.numero, l.annee,
               COUNT(CASE WHEN a.objet IS NULL THEN 1 END) as null_count,
               COUNT(*) as total
        FROM dossiers_legislatifs dl
        JOIN lois l ON l.id = dl.loi_id
        JOIN amendements a ON a.dossier_id = dl.id
        WHERE dl.chambre = 'SENAT'
        GROUP BY dl.id, dl.url_source, l.numero, l.annee
        HAVING COUNT(CASE WHEN a.objet IS NULL THEN 1 END) > 0
        ORDER BY null_count DESC
    """)
    dossiers = cur.fetchall()
    log.info(f"Found {len(dossiers)} Sénat dossiers with NULL amendments\n")

    total_updated = 0

    for dossier_id, url_source, loi_numero, annee, null_count, total_count in dossiers:
        log.info(f"--- Dossier {dossier_id}: loi {loi_numero} (année {annee}) ---")
        log.info(f"  URL: {url_source}")
        log.info(f"  {null_count}/{total_count} NULL amendments")

        pairs = discover_csv_url(client, url_source, annee)
        if not pairs:
            log.warning(f"  Could not discover text references, skipping")
            continue

        for session, text in pairs:
            # Check if CSV exists
            csv_url = SENAT_CSV_URL.format(session=session, text=text)
            try:
                r = client.head(csv_url)
                if r.status_code != 200:
                    log.info(f"  No CSV at {session}/{text}")
                    continue
            except Exception:
                continue

            rows = download_senat_csv(client, session, text)
            if rows:
                updated = update_amendments_from_csv(pg_conn, dossier_id, rows)
                total_updated += updated

        log.info("")

    # Final summary
    cur.execute("SELECT COUNT(*) FROM amendements WHERE objet IS NULL")
    remaining = cur.fetchone()[0]

    log.info(f"=== Summary ===")
    log.info(f"Total updated: {total_updated}")
    log.info(f"Remaining NULL objet: {remaining}")

    pg_conn.close()
    client.close()


if __name__ == "__main__":
    main()
