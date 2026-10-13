#!/usr/bin/env python3
"""Extract amendment mentions from pre-2001 debate interventions.

No structured digital source exists for amendments before October 2001.
This script uses regex patterns to extract amendment records from the
text of parliamentary debate interventions already in the database.

Focuses on debates linked to finance law dossiers (LF, LFR, LFSS).
Extracts: numero, auteur, sort (adopté/rejeté/retiré), article visé.
"""

import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from src.db.models import Amendement, Debat, DossierLegislatif, Loi, get_session

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# --- Regex patterns for amendment extraction ---

# Amendment number: "amendement n° 123", "amendement no 123", "sous-amendement n° 45"
AMEND_NUM_RE = re.compile(
    r"(?:sous-)?amendement\s+n[°o\s]+\s*(\d+(?:\s*(?:rectifié|rect\.?))?)",
    re.IGNORECASE,
)

# Author pattern: "de M. Dupont", "de Mme Martin", "de la commission", "du Gouvernement"
AUTHOR_RE = re.compile(
    r"(?:présenté|déposé|défendu)?\s*(?:par\s+)?"
    r"(?:de\s+|du\s+)"
    r"(M(?:me|lle)?\.?\s+[A-ZÀ-Ý][a-zà-ÿ]+(?:[-\s][A-ZÀ-Ý][a-zà-ÿ]+)*"
    r"|la\s+commission(?:\s+\w+)?"
    r"|[Gg]ouvernement)",
    re.IGNORECASE,
)

# Sort (outcome) patterns
SORT_PATTERNS = [
    # Formal vote results (most reliable — in president interventions)
    (re.compile(r"\(L'amendement\s+(?:est\s+)?adopt[ée]\b", re.IGNORECASE), "adopté"),
    (re.compile(r"\(L'amendement\s+n'est\s+pas\s+adopt[ée]\b", re.IGNORECASE), "rejeté"),
    (re.compile(r"\(L[ea]\s+sous-amendement\s+(?:est\s+)?adopt[ée]\b", re.IGNORECASE), "adopté"),
    (re.compile(r"\(L[ea]\s+sous-amendement\s+n'est\s+pas\s+adopt[ée]\b", re.IGNORECASE), "rejeté"),
    (re.compile(r"amendement\s+est\s+retir[ée]\b", re.IGNORECASE), "retiré"),
    (re.compile(r"\bRetrait\b"), "retiré"),
    (re.compile(r"\bAdoption\b"), "adopté"),
    (re.compile(r"\bRejet\b"), "rejeté"),
    (re.compile(r"\btomb[ée]\b", re.IGNORECASE), "tombé"),
]

# Article reference near amendment: "article 12", "Art. 54 bis"
ARTICLE_RE = re.compile(
    r"[Aa]rt(?:icle)?\.?\s+(\d+(?:\s*(?:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies))?)",
)


def extract_amendments_from_text(content: str, debate_id: int) -> list[dict]:
    """Extract amendment mentions from a single intervention text.

    Returns list of dicts with keys: numero, auteur, sort, article_vise, context.
    """
    results = []

    # Find all amendment number mentions
    for match in AMEND_NUM_RE.finditer(content):
        numero = match.group(1).strip()
        # Normalize: remove extra spaces
        numero = re.sub(r"\s+", " ", numero)

        pos = match.start()
        # Context window: 200 chars before, 300 chars after the mention
        ctx_start = max(0, pos - 200)
        ctx_end = min(len(content), match.end() + 300)
        context = content[ctx_start:ctx_end]

        # Extract author (look in context around the mention)
        auteur = None
        # Narrow window for author: just after the amendment number mention
        author_window = content[pos:min(len(content), match.end() + 150)]
        author_match = AUTHOR_RE.search(author_window)
        if author_match:
            auteur = author_match.group(1).strip()

        # Extract sort (look in context after the mention)
        sort = None
        sort_window = content[pos:min(len(content), match.end() + 200)]
        for pattern, sort_value in SORT_PATTERNS:
            if pattern.search(sort_window):
                sort = sort_value
                break

        # Extract article reference (look in wider context)
        article_vise = None
        article_match = ARTICLE_RE.search(context)
        if article_match:
            article_vise = article_match.group(1).strip()

        results.append({
            "numero": numero,
            "auteur": auteur,
            "sort": sort,
            "article_vise": article_vise,
            "context": context.replace("\n", " ").strip()[:300],
        })

    return results


def deduplicate_amendments(mentions: list[dict]) -> list[dict]:
    """Deduplicate amendment mentions by numero within a debate.

    Keeps the mention with the most information (sort > auteur > article).
    """
    by_numero: dict[str, list[dict]] = defaultdict(list)
    for m in mentions:
        by_numero[m["numero"]].append(m)

    deduped = []
    for numero, group in sorted(by_numero.items(), key=lambda x: _num_sort_key(x[0])):
        # Score each mention by completeness
        best = max(group, key=lambda m: (
            bool(m["sort"]) * 4 +
            bool(m["auteur"]) * 2 +
            bool(m["article_vise"]) * 1
        ))

        # Merge: fill in missing fields from other mentions
        for m in group:
            if not best["sort"] and m["sort"]:
                best["sort"] = m["sort"]
            if not best["auteur"] and m["auteur"]:
                best["auteur"] = m["auteur"]
            if not best["article_vise"] and m["article_vise"]:
                best["article_vise"] = m["article_vise"]

        deduped.append(best)

    return deduped


def _num_sort_key(numero: str):
    """Sort amendment numbers numerically."""
    m = re.match(r"(\d+)", numero)
    return int(m.group(1)) if m else 999999


def main():
    session = get_session()

    # Get all pre-2001 debates with dossier_id (linked to finance laws)
    debates = session.execute(text("""
        SELECT d.id, d.dossier_id, d.date, d.chambre,
               dl.loi_id, l.type, l.annee
        FROM debats d
        JOIN dossiers_legislatifs dl ON d.dossier_id = dl.id
        JOIN lois l ON dl.loi_id = l.id
        WHERE d.date < '2001-01-01'
        ORDER BY d.date
    """)).fetchall()

    logger.info(f"Pre-2001 debates with dossier link: {len(debates)}")

    # Check existing extracted amendments to avoid duplicates
    existing = session.execute(text("""
        SELECT reference_externe FROM amendements
        WHERE reference_externe LIKE 'extracted_from_debate:%'
    """)).fetchall()
    existing_refs = {r[0] for r in existing}
    logger.info(f"Already extracted amendments: {len(existing_refs)}")

    total_created = 0
    total_debates_with_amend = 0
    batch_size = 500
    batch = []

    for idx, (debate_id, dossier_id, debate_date, chambre,
              loi_id, loi_type, loi_annee) in enumerate(debates):
        # Skip if already processed
        ref_prefix = f"extracted_from_debate:{debate_id}:"
        if any(r.startswith(ref_prefix) for r in existing_refs):
            continue

        # Get all interventions for this debate
        interventions = session.execute(text("""
            SELECT id, contenu FROM interventions
            WHERE debat_id = :did AND contenu LIKE '%amendement%'
            ORDER BY ordre
        """), {"did": debate_id}).fetchall()

        if not interventions:
            continue

        # Extract amendment mentions from all interventions
        all_mentions = []
        for intv_id, contenu in interventions:
            if not contenu:
                continue
            mentions = extract_amendments_from_text(contenu, debate_id)
            all_mentions.extend(mentions)

        if not all_mentions:
            continue

        # Deduplicate within this debate
        unique = deduplicate_amendments(all_mentions)
        total_debates_with_amend += 1

        for amend in unique:
            ref = f"extracted_from_debate:{debate_id}:{amend['numero']}"
            if ref in existing_refs:
                continue

            batch.append(Amendement(
                dossier_id=dossier_id,
                numero=amend["numero"],
                auteur=amend["auteur"],
                objet=amend["context"],
                sort=amend["sort"],
                date_depot=debate_date,
                reference_externe=ref,
            ))
            existing_refs.add(ref)

        # Flush in batches
        if len(batch) >= batch_size:
            session.add_all(batch)
            session.flush()
            total_created += len(batch)
            batch = []

        if (idx + 1) % 1000 == 0:
            logger.info(f"  Processed {idx + 1}/{len(debates)} debates, {total_created + len(batch)} amendments so far")

    # Final flush
    if batch:
        session.add_all(batch)
        session.flush()
        total_created += len(batch)

    session.commit()
    session.close()

    logger.info(f"\nResults:")
    logger.info(f"  Debates processed: {len(debates)}")
    logger.info(f"  Debates with amendments: {total_debates_with_amend}")
    logger.info(f"  Amendments created: {total_created}")


if __name__ == "__main__":
    main()
