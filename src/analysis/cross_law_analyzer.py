"""Cross-law reference detection for temporal context analysis."""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Patterns for detecting law references in debate text
LAW_NUMBER_PATTERN = re.compile(
    r'loi\s+n[°o]\s*(\d{4}[-–]\d+)', re.IGNORECASE
)
LAW_YEAR_PATTERN = re.compile(
    r'loi\s+de\s+finances\s+(?:rectificative\s+)?pour\s+(\d{4})', re.IGNORECASE
)
LFSS_PATTERN = re.compile(
    r'loi\s+de\s+financement\s+de\s+la\s+sécurité\s+sociale\s+(?:pour\s+)?(\d{4})',
    re.IGNORECASE,
)


@dataclass
class CrossReference:
    """A detected cross-reference between laws."""
    source_law_year: int  # Law year where the reference was found
    source_law_type: str  # LF, LFR, LFSS
    target_law_ref: str  # Referenced law (number or "LF 2019")
    citation: str  # Text excerpt containing the reference
    chunk_score: float = 0.0  # Relevance score of the source chunk


class CrossLawAnalyzer:
    """Detect cross-references between laws in debate chunks."""

    def detect_cross_references(
        self,
        sections_by_law: dict[str, dict],
        debate_chunks_by_law: dict[str, list],
    ) -> list[CrossReference]:
        """
        Scan debate chunks for mentions of OTHER law numbers.

        Args:
            sections_by_law: Mapping of "LF_2019" -> {"law_type": ..., "law_year": ...}
            debate_chunks_by_law: Mapping of "LF_2019" -> [(chunk, score), ...]

        Returns:
            List of CrossReference objects
        """
        # Build set of known law identifiers
        known_laws = set()
        for key, info in sections_by_law.items():
            known_laws.add(key)

        cross_refs = []

        for source_key, chunks in debate_chunks_by_law.items():
            source_info = sections_by_law.get(source_key, {})
            source_year = source_info.get("law_year", 0)
            source_type = source_info.get("law_type", "")

            for chunk, score in chunks:
                text = chunk.content

                # Find law number references
                for match in LAW_NUMBER_PATTERN.finditer(text):
                    ref = match.group(1).replace("–", "-")
                    # Check if this references a different law in our analysis
                    target_key = self._match_law_number(ref, sections_by_law)
                    if target_key and target_key != source_key:
                        # Extract context (100 chars around match)
                        start = max(0, match.start() - 50)
                        end = min(len(text), match.end() + 50)
                        citation = text[start:end].strip()
                        cross_refs.append(CrossReference(
                            source_law_year=source_year,
                            source_law_type=source_type,
                            target_law_ref=target_key,
                            citation=citation,
                            chunk_score=score,
                        ))

                # Find "loi de finances pour YYYY" references
                for match in LAW_YEAR_PATTERN.finditer(text):
                    year = int(match.group(1))
                    # Check for LF or LFR matching this year
                    for target_key, target_info in sections_by_law.items():
                        if (
                            target_key != source_key
                            and target_info.get("law_year") == year
                            and target_info.get("law_type") in ("LF", "LFR")
                        ):
                            start = max(0, match.start() - 50)
                            end = min(len(text), match.end() + 50)
                            citation = text[start:end].strip()
                            cross_refs.append(CrossReference(
                                source_law_year=source_year,
                                source_law_type=source_type,
                                target_law_ref=target_key,
                                citation=citation,
                                chunk_score=score,
                            ))

                # Find LFSS references
                for match in LFSS_PATTERN.finditer(text):
                    year = int(match.group(1))
                    for target_key, target_info in sections_by_law.items():
                        if (
                            target_key != source_key
                            and target_info.get("law_year") == year
                            and target_info.get("law_type") == "LFSS"
                        ):
                            start = max(0, match.start() - 50)
                            end = min(len(text), match.end() + 50)
                            citation = text[start:end].strip()
                            cross_refs.append(CrossReference(
                                source_law_year=source_year,
                                source_law_type=source_type,
                                target_law_ref=target_key,
                                citation=citation,
                                chunk_score=score,
                            ))

        # Deduplicate by (source, target, citation_prefix)
        seen = set()
        deduped = []
        for cr in cross_refs:
            key = (cr.source_law_year, cr.target_law_ref, cr.citation[:50])
            if key not in seen:
                seen.add(key)
                deduped.append(cr)

        return deduped

    def _match_law_number(
        self, law_number: str, sections_by_law: dict[str, dict]
    ) -> str | None:
        """Match a law number to a known law key."""
        for key, info in sections_by_law.items():
            if info.get("law_numero") == law_number:
                return key
        return None
