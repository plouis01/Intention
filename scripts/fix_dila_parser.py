#!/usr/bin/env python3
"""Re-import DILA debates with improved XML parsing."""

import re
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.models import Debat, Intervention, get_session
from src.parsers.debates import extract_article_mentions


def parse_date_from_filename(filename: str) -> datetime:
    """Extract date from DILA filename like CRI_20121205_089.xml."""
    match = re.search(r"(\d{8})", filename)
    if match:
        return datetime.strptime(match.group(1), "%Y%m%d")
    return None


def extract_interventions_improved(xml_path: Path) -> list:
    """
    Extract speaker interventions using proper XML structure.

    DILA CRI format has:
    - <interlocuteur> elements with speaker info
    - <paragraphe> elements with speech content
    - Often nested in <point> or <intervention> elements
    """
    interventions = []

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        current_speaker = None
        current_function = None
        current_content = []

        def save_intervention():
            nonlocal current_speaker, current_content
            if current_speaker and current_content:
                full_content = " ".join(current_content).strip()
                if len(full_content) > 20:
                    interventions.append({
                        "orateur": current_speaker,
                        "fonction": current_function,
                        "contenu": full_content[:10000]
                    })
            current_content = []

        # Try different DILA XML structures
        # Structure 1: Look for interlocuteur/paragraphe pairs
        for elem in root.iter():
            tag = elem.tag.lower() if isinstance(elem.tag, str) else ""

            # Speaker identification
            if "interlocuteur" in tag or "orateur" in tag:
                save_intervention()
                speaker_text = "".join(elem.itertext()).strip()

                # Parse speaker name and function
                match = re.match(
                    r"(M\.|Mme|Mlle)\s+([^,\(\)]+)(?:,\s*([^\(\)]+))?",
                    speaker_text
                )
                if match:
                    current_speaker = f"{match.group(1)} {match.group(2)}".strip()
                    current_function = match.group(3).strip() if match.group(3) else None
                else:
                    current_speaker = speaker_text[:100]
                    current_function = None

            # Content paragraphs
            elif "paragraphe" in tag or tag == "p":
                text = "".join(elem.itertext()).strip()
                if text and current_speaker:
                    current_content.append(text)

            # Alternative: intervention blocks
            elif tag == "intervention":
                save_intervention()
                # Look for speaker within intervention
                for child in elem:
                    child_tag = child.tag.lower() if isinstance(child.tag, str) else ""
                    if "interlocuteur" in child_tag or "orateur" in child_tag:
                        speaker_text = "".join(child.itertext()).strip()
                        match = re.match(
                            r"(M\.|Mme|Mlle)\s+([^,\(\)]+)(?:,\s*([^\(\)]+))?",
                            speaker_text
                        )
                        if match:
                            current_speaker = f"{match.group(1)} {match.group(2)}".strip()
                            current_function = match.group(3).strip() if match.group(3) else None

        # Save last intervention
        save_intervention()

        # Fallback: if no structured interventions, try text-based extraction
        if not interventions:
            all_text = ET.tostring(root, encoding="unicode", method="text")

            # Split by speaker patterns
            speaker_pattern = re.compile(
                r"((?:M\.|Mme|Mlle)\s+[A-ZÉÈÊËÀÂÄÙÛÜÔÖÎÏÇ][a-zéèêëàâäùûüôöîïç\-']+(?:\s+[A-Za-zÉÈÊËÀÂÄÙÛÜÔÖÎÏÇéèêëàâäùûüôöîïç\-']+)*)"
                r"(?:,\s*([^\.]+))?"
                r"\.\s*[-–—]?\s*"
            )

            matches = list(speaker_pattern.finditer(all_text))

            for i, match in enumerate(matches):
                speaker = match.group(1).strip()
                function = match.group(2).strip() if match.group(2) else None

                start = match.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else min(start + 5000, len(all_text))
                content = all_text[start:end].strip()

                # Clean up content
                content = re.sub(r"\s+", " ", content)

                if content and len(content) > 30:
                    interventions.append({
                        "orateur": speaker,
                        "fonction": function,
                        "contenu": content[:10000]
                    })

    except ET.ParseError as e:
        print(f"  XML parse error in {xml_path.name}: {e}")
    except Exception as e:
        print(f"  Error processing {xml_path.name}: {e}")

    return interventions


def reimport_dila_debates(dila_dir: Path, years: list):
    """Re-import DILA debates with improved parser."""

    session = get_session()

    for year in years:
        year_dir = dila_dir / f"debats_{year}"

        if not year_dir.exists():
            print(f"Directory not found: {year_dir}")
            continue

        cri_files = list(year_dir.glob("CRI_*.xml"))
        print(f"\nProcessing {len(cri_files)} CRI files from {year}...")

        debates_updated = 0
        interventions_added = 0

        for cri_file in cri_files:
            reference = cri_file.stem

            # Find existing debate
            debat = session.query(Debat).filter_by(reference_externe=reference).first()

            if not debat:
                # Create new debate
                debate_date = parse_date_from_filename(cri_file.name)
                if not debate_date:
                    continue

                debat = Debat(
                    chambre="AN",
                    date=debate_date,
                    etape="seance_publique",
                    url_source=f"file://{cri_file}",
                    reference_externe=reference,
                )
                session.add(debat)
                session.flush()
            else:
                # Only re-import if the debate has no interventions or no contenu_integral
                existing_count = session.query(Intervention).filter_by(debat_id=debat.id).count()
                if existing_count > 0 and debat.contenu_integral:
                    continue
                # Delete existing interventions to re-import with better content
                session.query(Intervention).filter_by(debat_id=debat.id).delete()

            # Extract interventions with improved parser
            interventions = extract_interventions_improved(cri_file)

            # Build contenu_integral from interventions
            full_text_parts = []
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
                full_text_parts.append(f"{interv['orateur']}: {interv['contenu']}")

            # Update debate with contenu_integral
            if full_text_parts:
                debat.contenu_integral = "\n\n".join(full_text_parts)

            debates_updated += 1

            if debates_updated % 20 == 0:
                print(f"  Processed {debates_updated} debates...")
                session.commit()

        session.commit()
        print(f"  Updated {debates_updated} debates, {interventions_added} interventions from {year}")

    # Verify quality
    print("\n--- Quality check ---")
    apport_cession = session.query(Intervention).filter(
        Intervention.contenu.contains("apport-cession")
    ).count()
    report_imposition = session.query(Intervention).filter(
        Intervention.contenu.contains("report d'imposition")
    ).count()
    b_ter = session.query(Intervention).filter(
        Intervention.contenu.contains("150-0 B ter")
    ).count()

    print(f"Interventions mentioning 'apport-cession': {apport_cession}")
    print(f"Interventions mentioning 'report d'imposition': {report_imposition}")
    print(f"Interventions mentioning '150-0 B ter': {b_ter}")

    session.close()


if __name__ == "__main__":
    print("Re-importing DILA debates with improved parser...\n")

    dila_dir = Path("data/raw/dila")
    years = [2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019]

    reimport_dila_debates(dila_dir, years)
    print("\nDone!")
