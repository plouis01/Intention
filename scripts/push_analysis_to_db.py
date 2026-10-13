#!/usr/bin/env python3
"""
Parse a generated analysis markdown file and push it into the Railway DB analysis_cache.

Usage:
    python scripts/push_analysis_to_db.py "39"
    python scripts/push_analysis_to_db.py "39" --file data/analyse_39.md
    python scripts/push_analysis_to_db.py "150-0 B ter"
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.analysis.article_index import get_article_index
from src.api.cgi_routes import compute_modifications_hash, get_article_index as get_api_article_index
from src.db.models import AnalysisCache, get_session
from src.utils.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def parse_historique_table(lines: list[str], start: int) -> list[dict]:
    """Parse the historique markdown table into modification dicts."""
    modifications = []
    i = start
    # Skip to table rows (past header and separator)
    while i < len(lines) and not lines[i].startswith("|"):
        i += 1
    if i < len(lines):
        i += 2  # skip header row and separator row

    while i < len(lines) and lines[i].startswith("|"):
        cols = [c.strip() for c in lines[i].split("|")[1:-1]]
        if len(cols) >= 4:
            date_str = cols[0].strip()
            law_ref = cols[1].strip()
            article = cols[2].strip()
            nature = cols[3].strip().replace("**", "")
            # Parse date
            try:
                parts = date_str.split("/")
                iso_date = f"{parts[2]}-{parts[1]}-{parts[0]}"
            except (IndexError, ValueError):
                iso_date = date_str
            modifications.append({
                "date": iso_date,
                "law_ref": law_ref,
                "article_in_law": article,
                "nature": nature,
            })
        i += 1
    return modifications


def parse_law_ref(law_ref: str) -> dict:
    """Parse 'LFR 2002 (n°2002-1576)' into components."""
    m = re.match(r"(LF|LFR|LFSS)\s+(\d{4})\s+\(n°([\d-]+)\)", law_ref)
    if m:
        return {
            "law_type": m.group(1),
            "law_year": int(m.group(2)),
            "law_numero": m.group(3),
            "law_ref": law_ref,
        }
    return {"law_type": "", "law_year": 0, "law_numero": "", "law_ref": law_ref}


def parse_quoted_sources(lines: list[str], start: int, end: int) -> list[dict]:
    """Parse blockquoted sources like:
    > "citation text"
    > — **Orateur**, (fonction), date, chambre
    """
    sources = []
    i = start
    while i < end:
        line = lines[i].strip()
        if line.startswith("> \"") or line.startswith("> «"):
            # Collect multi-line citation
            citation_lines = [line[2:].strip()]  # remove "> "
            i += 1
            while i < end and lines[i].strip().startswith(">") and not lines[i].strip().startswith("> —"):
                citation_lines.append(lines[i].strip()[2:].strip())
                i += 1
            citation = " ".join(citation_lines)
            # Remove surrounding quotes
            citation = re.sub(r'^["\u201c«]\s*', '', citation)
            citation = re.sub(r'\s*["\u201d»]$', '', citation)

            # Parse attribution line
            if i < end and lines[i].strip().startswith("> —"):
                attr = lines[i].strip()[4:].strip()  # remove "> — "
                orateur_m = re.match(r"\*\*(.+?)\*\*(?:,\s*\((.+?)\))?,?\s*(\d{4}-\d{2}-\d{2})?,?\s*(AN|Sénat)?", attr)
                if orateur_m:
                    sources.append({
                        "citation": citation,
                        "orateur": orateur_m.group(1) or "",
                        "fonction": orateur_m.group(2) or "",
                        "date": orateur_m.group(3) or "",
                        "chambre": orateur_m.group(4) or "",
                    })
                else:
                    sources.append({
                        "citation": citation,
                        "orateur": attr,
                        "fonction": "",
                        "date": "",
                        "chambre": "",
                    })
                i += 1
            else:
                sources.append({
                    "citation": citation,
                    "orateur": "",
                    "fonction": "",
                    "date": "",
                    "chambre": "",
                })
        else:
            i += 1
    return sources


def find_section_boundaries(lines: list[str]) -> list[tuple[int, int]]:
    """Find start/end line indices for each numbered section."""
    boundaries = []
    section_starts = []
    for i, line in enumerate(lines):
        if re.match(r"^## <a id=\"\d+-", line):
            section_starts.append(i)

    for j, start in enumerate(section_starts):
        if j + 1 < len(section_starts):
            end = section_starts[j + 1]
        else:
            # Find next ## that isn't a subsection
            end = len(lines)
            for k in range(start + 1, len(lines)):
                if lines[k].startswith("## ") and not lines[k].startswith("### "):
                    # Check it's not a consolidation section within this section
                    if not lines[k].startswith("## Consolidation"):
                        end = k
                        break
        boundaries.append((start, end))
    return boundaries


def parse_section(lines: list[str], start: int, end: int, index: int) -> dict:
    """Parse a single section into the JSON format expected by the cache."""
    section_lines = lines[start:end]
    section_text = "\n".join(section_lines)

    # Parse header: "## <a id="1-modification-lfr-2002"></a>1. Modification (LFR 2002)"
    header = lines[start]
    nature_m = re.search(r"\d+\.\s+(Modification|Création|Abrogation)", header)
    nature = nature_m.group(1) if nature_m else "Modification"

    # Parse law info
    law_ref = ""
    date_str = ""
    article_in_law = ""
    for i in range(start, min(start + 15, end)):
        line = lines[i]
        if line.startswith("**Loi**:"):
            law_ref = line.replace("**Loi**:", "").strip()
        elif line.startswith("**Date de promulgation**:"):
            date_str = line.replace("**Date de promulgation**:", "").strip()
            # Convert DD/MM/YYYY to ISO
            try:
                parts = date_str.split("/")
                date_str = f"{parts[2]}-{parts[1]}-{parts[0]}"
            except (IndexError, ValueError):
                pass
        elif line.startswith("**Article dans la loi**:"):
            article_in_law = line.replace("**Article dans la loi**:", "").strip()

    law_info = parse_law_ref(law_ref)

    # Parse contexte (text between "### Contexte législatif" header info and "### Intention")
    contexte = ""
    intention = ""
    evolution = ""
    points_vigilance = []
    niveau_certitude = "moyen"
    certitude_justification = ""
    debates_analyzed = 0
    amendments_analyzed = 0
    sources_an = []
    sources_senat = []

    # Find sub-section positions
    subsections = {}
    for i in range(start, end):
        line = lines[i].strip()
        if line.startswith("### Contexte législatif"):
            subsections["contexte"] = i
        elif line.startswith("### Intention du législateur"):
            subsections["intention"] = i
        elif line.startswith("### Sources principales"):
            chambre = "AN" if "Assemblée" in line or "AN" in line else "Sénat"
            subsections.setdefault("sources", []).append((i, chambre))
        elif line.startswith("### Évolution lors des débats"):
            subsections["evolution"] = i
        elif line.startswith("### Points de vigilance"):
            subsections["vigilance"] = i
        elif line.startswith("### Niveau de certitude:"):
            subsections["certitude"] = i

    # Extract contexte text
    if "contexte" in subsections:
        ctx_start = subsections["contexte"] + 1
        ctx_end = subsections.get("intention", subsections.get("sources", [(end,)])[0][0] if "sources" in subsections else end)
        # Skip the metadata lines (**Loi**: ..., **Date**: ..., **Article**: ...) and blanks
        ctx_lines = []
        for i in range(ctx_start, ctx_end):
            line = lines[i].strip()
            if line.startswith("**Loi**:") or line.startswith("**Date de promulgation**:") or line.startswith("**Article dans la loi**:"):
                continue
            if line.startswith("###"):
                break
            if line == "" and not ctx_lines:
                continue  # skip leading blanks
            ctx_lines.append(line)
        contexte = "\n".join(ctx_lines).strip()

    # Extract intention text
    if "intention" in subsections:
        int_start = subsections["intention"] + 1
        # Find next subsection
        int_end = end
        for key in ["sources", "evolution", "vigilance", "certitude"]:
            if key in subsections:
                val = subsections[key]
                if key == "sources":
                    candidate = val[0][0]
                else:
                    candidate = val
                if candidate > subsections["intention"]:
                    int_end = min(int_end, candidate)
        int_lines = []
        for i in range(int_start, int_end):
            line = lines[i].strip()
            if line.startswith("###"):
                break
            int_lines.append(line)
        intention = "\n".join(int_lines).strip()

    # Extract sources
    if "sources" in subsections:
        for src_start, chambre in subsections["sources"]:
            # Find end of sources section
            src_end = end
            for i in range(src_start + 1, end):
                if lines[i].strip().startswith("### "):
                    src_end = i
                    break
            parsed = parse_quoted_sources(lines, src_start, src_end)
            # Set chambre for all sources
            for s in parsed:
                if not s["chambre"]:
                    s["chambre"] = chambre
            if chambre == "AN":
                sources_an.extend(parsed)
            else:
                sources_senat.extend(parsed)

    # Extract evolution
    if "evolution" in subsections:
        ev_start = subsections["evolution"] + 1
        ev_end = end
        for key in ["vigilance", "certitude"]:
            if key in subsections and subsections[key] > subsections["evolution"]:
                ev_end = min(ev_end, subsections[key])
        ev_lines = []
        for i in range(ev_start, ev_end):
            line = lines[i].strip()
            if line.startswith("###"):
                break
            ev_lines.append(line)
        evolution = "\n".join(ev_lines).strip()

    # Extract points de vigilance
    if "vigilance" in subsections:
        vig_start = subsections["vigilance"] + 1
        vig_end = end
        if "certitude" in subsections and subsections["certitude"] > subsections["vigilance"]:
            vig_end = subsections["certitude"]
        for i in range(vig_start, vig_end):
            line = lines[i].strip()
            if line.startswith("###"):
                break
            if line.startswith("- "):
                points_vigilance.append(line[2:])

    # Extract certitude
    if "certitude" in subsections:
        cert_line = lines[subsections["certitude"]].strip()
        cert_m = re.search(r"certitude:\s*(\w+)", cert_line)
        if cert_m:
            niveau_certitude = cert_m.group(1)
        # Justification is the paragraph after
        just_lines = []
        for i in range(subsections["certitude"] + 1, end):
            line = lines[i].strip()
            if line.startswith("*Débats analysés"):
                m = re.match(r"\*Débats analysés:\s*(\d+)\s*\|\s*Amendements analysés:\s*(\d+)\*", line)
                if m:
                    debates_analyzed = int(m.group(1))
                    amendments_analyzed = int(m.group(2))
                break
            if line == "" or line.startswith("---"):
                continue
            just_lines.append(line)
        certitude_justification = "\n".join(just_lines).strip()

    return {
        "index": index,
        "modification": {
            "date": date_str,
            "law_numero": law_info["law_numero"],
            "law_type": law_info["law_type"],
            "law_year": law_info["law_year"],
            "article_in_law": article_in_law,
            "nature": nature,
            "law_ref": law_ref,
        },
        "contexte": contexte,
        "intention": intention,
        "sources_an": sources_an,
        "sources_senat": sources_senat,
        "evolution_debats": evolution,
        "points_vigilance": points_vigilance,
        "niveau_certitude": niveau_certitude,
        "certitude_justification": certitude_justification,
        "debates_analyzed": debates_analyzed,
        "amendments_analyzed": amendments_analyzed,
        "operational_params": None,
    }


def parse_consolidation_sections(lines: list[str]) -> list[dict]:
    """Parse consolidation sections like '## Consolidation du dispositif (2009-2010)'."""
    consolidations = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("## Consolidation du dispositif"):
            m = re.search(r"\((\d{4})-(\d{4})\)", lines[i])
            start_year = int(m.group(1)) if m else 0
            end_year = int(m.group(2)) if m else 0
            title = lines[i].strip().replace("## ", "")

            # Find end of consolidation section
            end = len(lines)
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("## ") and not lines[j].startswith("### "):
                    end = j
                    break

            # Parse context paragraph
            doctrine = ""
            for j in range(i + 1, end):
                if lines[j].strip().startswith("### Contexte"):
                    # Grab paragraphs after
                    ctx_lines = []
                    for k in range(j + 1, end):
                        if lines[k].strip().startswith("### "):
                            break
                        if lines[k].strip():
                            ctx_lines.append(lines[k].strip())
                    doctrine = "\n".join(ctx_lines)
                    break

            # Parse citations
            citations = parse_quoted_sources(lines, i, end)

            consolidations.append({
                "start_year": start_year,
                "end_year": end_year,
                "title": title,
                "doctrine_evolution": doctrine,
                "amendements_rejetes": [],
                "citations": citations,
            })
        i += 1
    return consolidations


def parse_synthesis(lines: list[str]) -> dict:
    """Parse the synthesis section."""
    synth = {"objectif": "", "evolution": [], "preoccupations": []}

    synth_start = None
    for i, line in enumerate(lines):
        if line.strip() == "## Synthèse de l'évolution de l'intention législative":
            synth_start = i
            break
    if synth_start is None:
        return synth

    # Find end
    synth_end = len(lines)
    for i in range(synth_start + 1, len(lines)):
        if lines[i].startswith("## ") and "Synthèse" not in lines[i]:
            synth_end = i
            break

    # Parse objectif
    for i in range(synth_start, synth_end):
        if lines[i].strip() == "### Objectif constant":
            obj_lines = []
            for j in range(i + 1, synth_end):
                if lines[j].strip().startswith("### "):
                    break
                if lines[j].strip():
                    obj_lines.append(lines[j].strip())
            synth["objectif"] = "\n".join(obj_lines)
            break

    # Parse evolution table
    for i in range(synth_start, synth_end):
        if lines[i].strip() == "### Évolution des conditions":
            # Find table
            for j in range(i + 1, synth_end):
                if lines[j].startswith("|") and "Période" in lines[j]:
                    # Skip header and separator
                    k = j + 2
                    while k < synth_end and lines[k].startswith("|"):
                        cols = [c.strip() for c in lines[k].split("|")[1:-1]]
                        if len(cols) >= 4:
                            synth["evolution"].append({
                                "periode": cols[0],
                                "nature": cols[1],
                                "loi": cols[2],
                                "article": cols[3],
                            })
                        k += 1
                    break
            break

    # Parse preoccupations
    for i in range(synth_start, synth_end):
        if lines[i].strip() == "### Préoccupations récurrentes du législateur":
            for j in range(i + 1, synth_end):
                line = lines[j].strip()
                if line.startswith("### ") or line.startswith("## "):
                    break
                m = re.match(r"^\d+\.\s+(.+)", line)
                if m:
                    synth["preoccupations"].append(m.group(1))
            break

    return synth


def parse_forward_looking(lines: list[str]) -> list[dict]:
    """Parse forward-looking / PLF sections."""
    sections = []
    fl_start = None
    for i, line in enumerate(lines):
        if line.strip() == "## Discussions PLF en cours (projets de modification)":
            fl_start = i
            break
    if fl_start is None:
        return sections

    fl_end = len(lines)
    for i in range(fl_start + 1, len(lines)):
        if lines[i].startswith("## ") and "PLF" not in lines[i] and "Discussions" not in lines[i]:
            fl_end = i
            break

    # Find PLF sub-sections
    plf_starts = []
    for i in range(fl_start, fl_end):
        m = re.match(r"### PLF (\d{4})", lines[i].strip())
        if m:
            plf_starts.append((i, int(m.group(1))))

    for idx, (ps, year) in enumerate(plf_starts):
        pe = plf_starts[idx + 1][0] if idx + 1 < len(plf_starts) else fl_end

        propositions = []
        citations = []
        niveau_certitude = "moyen"

        # Parse propositions (between "Propositions" and "Citations" headers)
        in_propositions = False
        for i in range(ps, pe):
            line = lines[i].strip()
            if "Propositions de modification" in line:
                in_propositions = True
                continue
            if "Citations des débats" in line:
                in_propositions = False
                continue
            if in_propositions and line.startswith("- "):
                propositions.append(line[2:])

        # Parse citations
        citations = parse_quoted_sources(lines, ps, pe)

        # Parse certitude
        for i in range(ps, pe):
            m = re.match(r"\*Niveau de certitude:\s*(\w+)\*", lines[i].strip())
            if m:
                niveau_certitude = m.group(1)

        sections.append({
            "plf_year": year,
            "status": "en cours",
            "propositions": propositions,
            "citations": citations,
            "niveau_certitude": niveau_certitude,
        })

    return sections


def parse_jurisprudence(lines: list[str], article_ref: str) -> list[dict]:
    """Get jurisprudence from the article index (more complete than markdown)."""
    index = get_article_index()
    juris = index.get_jurisprudence(article_ref)
    if juris:
        return [
            {
                "juridiction": j.juridiction,
                "date": j.date,
                "reference": j.reference,
                "titre": j.titre,
                "faits": getattr(j, "faits", ""),
                "solution": getattr(j, "solution", ""),
                "portee": getattr(j, "portee", ""),
            }
            for j in juris
        ]
    return []


def parse_parliamentary_sources(lines: list[str]) -> tuple[list[str], list[str]]:
    """Parse AN and Sénat debate source lists from methodology section."""
    an_sources = []
    senat_sources = []
    in_an = False
    in_senat = False

    for line in lines:
        stripped = line.strip()
        if stripped == "### Débats parlementaires (Assemblée Nationale)":
            in_an = True
            in_senat = False
            continue
        elif stripped == "### Débats parlementaires (Sénat)":
            in_senat = True
            in_an = False
            continue
        elif stripped.startswith("### ") or stripped.startswith("## "):
            in_an = False
            in_senat = False
            continue

        if stripped.startswith("- Débats"):
            if in_an:
                an_sources.append(stripped[2:])
            elif in_senat:
                senat_sources.append(stripped[2:])

    return an_sources, senat_sources


def parse_dila_sources(lines: list[str]) -> list[str]:
    """Parse DILA sources from methodology section."""
    sources = []
    in_dila = False
    for line in lines:
        stripped = line.strip()
        if stripped == "### Sources DILA (archives parlementaires)":
            in_dila = True
            continue
        elif stripped.startswith("### ") or stripped.startswith("## "):
            in_dila = False
            continue
        if in_dila and stripped.startswith("- "):
            sources.append(stripped[2:])
    return sources


def parse_markdown_to_cache_json(md_path: Path, article_ref: str) -> dict:
    """Parse a full analysis markdown file into the JSON format expected by analysis_cache."""
    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()
    lines = content.split("\n")

    # Parse header info
    titre = ""
    question = ""
    for line in lines[:10]:
        if line.startswith("**Objet**:"):
            titre = line.replace("**Objet**:", "").strip()
        elif line.startswith("**Question posée**:"):
            question = line.replace("**Question posée**:", "").strip()

    # Parse historique table
    hist_start = 0
    for i, line in enumerate(lines):
        if line.strip() == "## Historique des modifications":
            hist_start = i
            break
    modifications = parse_historique_table(lines, hist_start)

    # Parse sections
    boundaries = find_section_boundaries(lines)
    sections = []
    for idx, (s, e) in enumerate(boundaries):
        section = parse_section(lines, s, e, idx)
        sections.append(section)

    # Parse consolidation
    consolidation = parse_consolidation_sections(lines)

    # Parse synthesis
    synthesis = parse_synthesis(lines)

    # Parse forward-looking
    forward_looking = parse_forward_looking(lines)

    # Parse jurisprudence (from index, not markdown — more complete)
    jurisprudence = parse_jurisprudence(lines, article_ref)

    # Parse parliamentary sources
    an_sources, senat_sources = parse_parliamentary_sources(lines)

    # Parse DILA sources
    dila_sources = parse_dila_sources(lines)

    # Build article_info
    article_info = {
        "reference": article_ref,
        "titre": titre,
        "code": "CGI",
        "question": question,
        "total_modifications": len(modifications),
        "total_sections": len(sections),
        "modifications": modifications,
    }

    settings = get_settings()
    collected = {
        "article_info": article_info,
        "sections": sections,
        "consolidation": consolidation,
        "synthesis": synthesis,
        "forward_looking": forward_looking,
        "jurisprudence": jurisprudence,
        "cross_references": [],
        "plf_lf_deltas": [],
        "rapports_parlementaires": [],
        "dila_sources": dila_sources,
        "sources_parlementaires_an": an_sources,
        "sources_parlementaires_senat": senat_sources,
        "metadata": {
            "total_sections": len(sections),
            "total_debates_searched": sum(s.get("debates_analyzed", 0) for s in sections),
            "total_interventions_found": 0,
            "generated_date": datetime.now(UTC).strftime("%Y-%m-%d"),
            "llm_model": settings.claude_model if settings.llm_provider == "anthropic" else settings.groq_model,
            "embedding_model": settings.local_embedding_model,
        },
    }

    return collected


def push_to_db(article_ref: str, analysis: dict) -> bool:
    """Insert or update the analysis in the Railway DB analysis_cache table."""
    # Compute modifications hash from article index
    index_data = get_api_article_index()
    raw_article = None
    for ref, data in index_data.get("articles", {}).items():
        if ref.lower() == article_ref.lower():
            raw_article = data
            break

    raw_modifications = raw_article.get("modifications", []) if raw_article else []
    mod_hash = compute_modifications_hash(raw_modifications)
    last_mod_date = raw_modifications[-1]["date"] if raw_modifications else ""

    # Compute quality metrics
    certitude_scores = {"élevé": 1.0, "moyen": 0.5, "faible": 0.0}
    section_scores = []
    total_sources = 0
    min_certitude = "élevé"
    for s in analysis["sections"]:
        score = certitude_scores.get(s.get("niveau_certitude", "moyen"), 0.5)
        source_count = len(s.get("sources_an", [])) + len(s.get("sources_senat", []))
        section_scores.append(score)
        total_sources += source_count
        if certitude_scores.get(s.get("niveau_certitude", "moyen"), 0.5) < certitude_scores.get(min_certitude, 0.5):
            min_certitude = s.get("niveau_certitude", "moyen")

    overall_quality = sum(section_scores) / len(section_scores) if section_scores else 0.0

    settings = get_settings()
    ttl_days = 30
    now = datetime.now(UTC).replace(tzinfo=None)
    expires = now + timedelta(days=ttl_days)
    llm_model = settings.claude_model if settings.llm_provider == "anthropic" else settings.groq_model

    # Add invalidation data
    analysis["invalidation"] = {
        "modification_count": len(raw_modifications),
        "last_modification_date": last_mod_date,
        "modifications_hash": mod_hash,
    }

    with get_session() as session:
        existing = (
            session.query(AnalysisCache)
            .filter(AnalysisCache.cgi_article == article_ref)
            .first()
        )

        if existing:
            existing.analysis_json = analysis
            existing.modifications_hash = mod_hash
            existing.modification_count = len(raw_modifications)
            existing.last_modification_date = last_mod_date
            existing.quality_score = int(overall_quality * 100)
            existing.sources_count = total_sources
            existing.confidence_level = min_certitude
            existing.created_at = now
            existing.expires_at = expires
            existing.llm_model = llm_model
            session.commit()
            logger.info(f"Updated existing cache entry for {article_ref}")
        else:
            cache = AnalysisCache(
                cgi_article=article_ref,
                analysis_json=analysis,
                modifications_hash=mod_hash,
                modification_count=len(raw_modifications),
                last_modification_date=last_mod_date,
                quality_score=int(overall_quality * 100),
                sources_count=total_sources,
                confidence_level=min_certitude,
                created_at=now,
                expires_at=expires,
                llm_model=llm_model,
            )
            session.add(cache)
            session.commit()
            logger.info(f"Created new cache entry for {article_ref}")

    return True


def main():
    parser = argparse.ArgumentParser(description="Push analysis markdown to Railway DB cache")
    parser.add_argument("article", help="CGI article reference (e.g., '39', '150-0 B ter')")
    parser.add_argument("--file", type=Path, help="Path to markdown file (auto-detected if omitted)")
    parser.add_argument("--dry-run", action="store_true", help="Parse and print JSON without pushing to DB")
    args = parser.parse_args()

    article_ref = args.article
    md_path = args.file
    if md_path is None:
        safe_ref = article_ref.replace(" ", "_").replace("-", "_")
        md_path = project_root / "data" / f"analyse_{safe_ref}.md"

    if not md_path.exists():
        print(f"ERROR: File not found: {md_path}")
        return 1

    print(f"Parsing {md_path}...")
    analysis = parse_markdown_to_cache_json(md_path, article_ref)

    print(f"  Sections: {len(analysis['sections'])}")
    print(f"  Consolidation periods: {len(analysis['consolidation'])}")
    print(f"  Forward-looking: {len(analysis['forward_looking'])}")
    print(f"  Jurisprudence: {len(analysis['jurisprudence'])}")
    print(f"  Sources AN: {len(analysis['sources_parlementaires_an'])}")
    print(f"  Sources Sénat: {len(analysis['sources_parlementaires_senat'])}")

    if args.dry_run:
        print("\n--- DRY RUN: JSON output ---")
        print(json.dumps(analysis, indent=2, ensure_ascii=False)[:5000])
        print("...")
        return 0

    print(f"\nPushing to DB...")
    push_to_db(article_ref, analysis)
    print("Done!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
