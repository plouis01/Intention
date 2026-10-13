#!/usr/bin/env python3
"""Import DILA debate files (CRI - Comptes Rendus Intégraux) into the database."""

import re
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.models import Debat, Intervention, get_session
from src.parsers.debates import extract_article_mentions


def parse_date_from_filename(filename: str) -> datetime:
    """Extract date from DILA filename like CRI_20121205_089.xml."""
    match = re.search(r"(\d{8})", filename)
    if match:
        date_str = match.group(1)
        return datetime.strptime(date_str, "%Y%m%d")
    return None


def extract_interventions_from_cri(xml_path: Path) -> list:
    """
    Extract speaker interventions from a CRI XML file.

    Returns list of dicts with: orateur, fonction, contenu
    """
    interventions = []

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        # Find all Para elements that might contain interventions
        # DILA format uses various elements - we look for patterns with speaker names
        all_text = ET.tostring(root, encoding="unicode", method="text")

        # Split by common speaker patterns
        # Pattern: "M. Name" or "Mme Name" followed by content
        speaker_pattern = re.compile(
            r"((?:M\.|Mme|Mlle)\s+[A-ZÉÈÊËÀÂÄÙÛÜÔÖÎÏÇ][a-zéèêëàâäùûüôöîïç\-]+(?:\s+[A-ZÉÈÊËÀÂÄÙÛÜÔÖÎÏÇ][a-zéèêëàâäùûüôöîïç\-]+)*)"
            r"(?:,\s*([^\.]+))?"  # Optional function
            r"\.\s*"
        )

        # Extract metadata for date
        date_elem = root.find(".//dateSeance")
        if date_elem is not None and date_elem.text:
            date_str = date_elem.text[:10]  # Take YYYY-MM-DD part

        # For now, extract raw text content
        # This is a simplified extraction - full parsing would need more work
        content_blocks = []
        for elem in root.iter():
            if elem.text and len(elem.text.strip()) > 50:
                content_blocks.append(elem.text.strip())

        # Combine and try to split by speakers
        full_text = " ".join(content_blocks)

        # Find speaker mentions
        matches = list(speaker_pattern.finditer(full_text))

        for i, match in enumerate(matches):
            speaker = match.group(1).strip()
            function = match.group(2).strip() if match.group(2) else None

            # Get content until next speaker
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
            content = full_text[start:end].strip()

            if content and len(content) > 20:
                interventions.append(
                    {"orateur": speaker, "fonction": function, "contenu": content[:10000]}  # Limit content size
                )

    except ET.ParseError as e:
        print(f"  XML parse error in {xml_path.name}: {e}")
    except Exception as e:
        print(f"  Error processing {xml_path.name}: {e}")

    return interventions


def import_dila_debates(dila_dir: Path, year: int):
    """Import all CRI files from a DILA year directory."""

    session = get_session()
    year_dir = dila_dir / f"debats_{year}"

    if not year_dir.exists():
        print(f"Directory not found: {year_dir}")
        return

    cri_files = list(year_dir.glob("CRI_*.xml"))
    print(f"\nProcessing {len(cri_files)} CRI files from {year}...")

    debates_added = 0
    interventions_added = 0

    for cri_file in cri_files:
        # Check if debate already exists
        reference = cri_file.stem  # e.g., "CRI_20121205_089"
        existing = session.query(Debat).filter_by(reference_externe=reference).first()

        if existing:
            continue

        # Parse date from filename
        debate_date = parse_date_from_filename(cri_file.name)
        if not debate_date:
            continue

        # Create debate record
        debat = Debat(
            chambre="AN",
            date=debate_date,
            etape="seance_publique",
            url_source=f"file://{cri_file}",
            reference_externe=reference,
        )
        session.add(debat)
        session.flush()  # Get the ID

        # Extract interventions
        interventions = extract_interventions_from_cri(cri_file)

        for i, interv in enumerate(interventions):
            intervention = Intervention(
                debat_id=debat.id,
                orateur=interv["orateur"],
                fonction=interv.get("fonction"),
                contenu=interv["contenu"],
                articles_mentionnes=extract_article_mentions(interv["contenu"]),
                ordre=i + 1,
            )
            session.add(intervention)
            interventions_added += 1

        debates_added += 1

        if debates_added % 10 == 0:
            print(f"  Processed {debates_added} debates...")
            session.commit()

    session.commit()
    print(f"  Added {debates_added} debates, {interventions_added} interventions from {year}")

    session.close()


def main():
    """Import all DILA debates."""
    dila_dir = Path("data/raw/dila")

    if not dila_dir.exists():
        print(f"DILA directory not found: {dila_dir}")
        return

    years = [2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019]

    for year in years:
        import_dila_debates(dila_dir, year)


if __name__ == "__main__":
    print("Importing DILA debate files...")
    main()
    print("\nDone!")
