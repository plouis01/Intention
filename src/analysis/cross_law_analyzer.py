"""Cross-law reference detection for temporal context analysis.

Detects explicit references (regex), implicit references (Groq LLM),
and semantic thematic links (embedding similarity) between laws.
"""

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum

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

# Sentence boundary pattern for citation extraction
_SENTENCE_END = re.compile(r'[.!?]')


class ReferenceType(str, Enum):
    """Type of cross-law reference detected."""
    EXPLICIT_NUMBER = "explicit_number"    # "loi n° 2023-1322"
    EXPLICIT_YEAR = "explicit_year"        # "loi de finances pour 2024"
    IMPLICIT_LLM = "implicit_llm"          # Detected by LLM (Groq)
    SEMANTIC_THEME = "semantic_theme"       # Thematic link via embedding similarity


# Confidence weight per reference type
_TYPE_WEIGHTS = {
    ReferenceType.EXPLICIT_NUMBER: 1.0,
    ReferenceType.EXPLICIT_YEAR: 0.8,
    ReferenceType.IMPLICIT_LLM: 0.7,
    ReferenceType.SEMANTIC_THEME: 0.6,
}


@dataclass
class CrossReference:
    """A detected cross-reference between laws."""
    source_law_year: int
    source_law_type: str
    target_law_ref: str
    citation: str
    chunk_score: float = 0.0
    ref_type: ReferenceType = ReferenceType.EXPLICIT_NUMBER
    confidence: float = 0.0

    def __post_init__(self):
        if self.confidence == 0.0:
            weight = _TYPE_WEIGHTS.get(self.ref_type, 0.5)
            self.confidence = self.chunk_score * weight


# Prompt for Groq-based implicit reference detection
CROSS_REF_DETECTION_PROMPT = """\
Tu analyses des extraits de débats parlementaires \
sur la {law_type} {law_year}.
Ces extraits discutent de l'article {article_ref} du CGI.

Identifie si ces textes font référence (explicitement ou implicitement) à des dispositions
d'AUTRES lois de finances (LF, LFR, LFSS) d'années différentes de {law_year}.

Cherche en particulier :
- Références temporelles ("l'an dernier", "en 2019", "la réforme précédente")
- Comparaisons législatives ("contrairement à", "comme nous l'avions voté")
- Mentions de dispositifs antérieurs ("le dispositif instauré par", "la mesure de")
- Renvois à d'autres textes financiers

Textes à analyser :
---
{chunk_text}
---

Réponds UNIQUEMENT en JSON valide (pas de texte avant/après) :
{{"references": [
  {{"target_year": 2019, "target_type": "LF", "context": "phrase clé du texte", "confidence": 0.8}},
  ...
], "has_references": true}}

Si aucune référence détectée :
{{"references": [], "has_references": false}}"""


class CrossLawAnalyzer:
    """Detect cross-references between laws in debate chunks."""

    def detect_cross_references(
        self,
        sections_by_law: dict[str, dict],
        debate_chunks_by_law: dict[str, list],
        min_chunk_score: float = 0.35,
        article_ref: str = "",
    ) -> list[CrossReference]:
        """
        Scan debate chunks for mentions of OTHER law numbers.

        Args:
            sections_by_law: Mapping of "LF_2019" -> {"law_type": ..., "law_year": ...}
            debate_chunks_by_law: Mapping of "LF_2019" -> [(chunk, score), ...]
            min_chunk_score: Minimum chunk score to consider (filters noise)

        Returns:
            List of CrossReference objects sorted by confidence descending
        """
        cross_refs = []
        # Build article reference variants for relevance check
        ref_lower = article_ref.lower() if article_ref else ""
        ref_variants = [ref_lower, ref_lower.replace("-", " ")] if ref_lower else []

        for source_key, chunks in debate_chunks_by_law.items():
            source_info = sections_by_law.get(source_key, {})
            source_year = source_info.get("law_year", 0)
            source_type = source_info.get("law_type", "")

            for chunk, score in chunks:
                if score < min_chunk_score:
                    continue

                text = chunk.content

                # Check if the chunk text mentions the target article
                text_lower = text.lower()
                chunk_mentions_article = (
                    not ref_variants
                    or any(v in text_lower for v in ref_variants)
                )

                # Find law number references (e.g., "loi n° 2023-1322")
                for match in LAW_NUMBER_PATTERN.finditer(text):
                    ref = match.group(1).replace("–", "-")
                    target_key = self._match_law_number(ref, sections_by_law)
                    if target_key and target_key != source_key:
                        if not chunk_mentions_article:
                            continue
                        cross_refs.append(CrossReference(
                            source_law_year=source_year,
                            source_law_type=source_type,
                            target_law_ref=target_key,
                            citation=citation,
                            chunk_score=score,
                            ref_type=ReferenceType.EXPLICIT_NUMBER,
                        ))

                # Find "loi de finances pour YYYY" references
                for match in LAW_YEAR_PATTERN.finditer(text):
                    year = int(match.group(1))
                    for target_key, target_info in sections_by_law.items():
                        if (
                            target_key != source_key
                            and target_info.get("law_year") == year
                            and target_info.get("law_type") in ("LF", "LFR")
                        ):
                            if not chunk_mentions_article:
                                continue
                            cross_refs.append(CrossReference(
                                source_law_year=source_year,
                                source_law_type=source_type,
                                target_law_ref=target_key,
                                citation=citation,
                                chunk_score=score,
                                ref_type=ReferenceType.EXPLICIT_YEAR,
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
                            if not chunk_mentions_article:
                                continue
                            cross_refs.append(CrossReference(
                                source_law_year=source_year,
                                source_law_type=source_type,
                                target_law_ref=target_key,
                                citation=citation,
                                chunk_score=score,
                                ref_type=ReferenceType.EXPLICIT_YEAR,
                            ))

        # Deduplicate by (source, target, citation_prefix)
        seen = set()
        deduped = []
        for cr in cross_refs:
            key = (cr.source_law_year, cr.target_law_ref, cr.citation[:80])
            if key not in seen:
                seen.add(key)
                deduped.append(cr)

        deduped.sort(key=lambda x: x.confidence, reverse=True)
        return deduped

    def detect_implicit_references(
        self,
        sections_by_law: dict[str, dict],
        debate_chunks_by_law: dict[str, list],
        article_ref: str,
        min_chunk_score: float = 0.45,
        batch_size: int = 5,
    ) -> list[CrossReference]:
        """
        Use Groq LLM to detect implicit cross-law references in high-quality chunks.

        Args:
            sections_by_law: Mapping of law keys to law info
            debate_chunks_by_law: Mapping of law keys to (chunk, score) lists
            article_ref: CGI article reference (e.g., "150-0 B ter")
            min_chunk_score: Only process chunks above this score threshold
            batch_size: Number of chunks per LLM call

        Returns:
            List of CrossReference with ref_type=IMPLICIT_LLM
        """
        try:
            from src.utils.groq_pool import get_groq_pool
        except ImportError:
            logger.debug("Groq pool not available, skipping implicit reference detection")
            return []

        pool = get_groq_pool(anthropic_fallback=False)
        implicit_refs = []

        # Build known law years for validation
        known_law_years: dict[tuple[int, str], str] = {}
        for key, info in sections_by_law.items():
            year = info.get("law_year", 0)
            law_type = info.get("law_type", "")
            known_law_years[(year, law_type)] = key

        for source_key, chunks in debate_chunks_by_law.items():
            source_info = sections_by_law.get(source_key, {})
            source_year = source_info.get("law_year", 0)
            source_type = source_info.get("law_type", "")

            # Filter to high-score chunks only
            top_chunks = [(c, s) for c, s in chunks if s >= min_chunk_score]
            top_chunks.sort(key=lambda x: x[1], reverse=True)
            top_chunks = top_chunks[:10]

            if not top_chunks:
                continue

            # Batch chunks for efficient LLM calls
            for i in range(0, len(top_chunks), batch_size):
                batch = top_chunks[i:i + batch_size]
                combined_text = "\n---\n".join(
                    c.content[:1500] for c, _ in batch
                )
                avg_score = sum(s for _, s in batch) / len(batch)

                prompt = CROSS_REF_DETECTION_PROMPT.format(
                    law_type=source_type,
                    law_year=source_year,
                    article_ref=article_ref,
                    chunk_text=combined_text,
                )

                try:
                    response = pool.chat_completion(
                        model="llama-3.3-70b-versatile",
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=500,
                        temperature=0.1,
                    )
                    refs = self._parse_llm_references(
                        response, source_year, source_type,
                        known_law_years, source_key, avg_score,
                    )
                    implicit_refs.extend(refs)
                except Exception as e:
                    logger.debug(f"Groq implicit ref detection failed for {source_key}: {e}")
                    continue

        # Deduplicate
        seen = set()
        deduped = []
        for cr in implicit_refs:
            key = (cr.source_law_year, cr.target_law_ref, cr.ref_type)
            if key not in seen:
                seen.add(key)
                deduped.append(cr)

        deduped.sort(key=lambda x: x.confidence, reverse=True)
        return deduped

    def detect_semantic_links(
        self,
        sections_by_law: dict[str, dict],
        debate_chunks_by_law: dict[str, list],
        embedding_service,
        similarity_threshold: float = 0.80,
    ) -> list[CrossReference]:
        """
        Detect thematic links between laws by comparing embedding centroids.

        Computes the mean embedding of top chunks per law, then finds law pairs
        with high centroid similarity, indicating shared legislative themes.

        Args:
            sections_by_law: Mapping of law keys to law info
            debate_chunks_by_law: Mapping of law keys to (chunk, score) lists
            embedding_service: EmbeddingService for generating embeddings
            similarity_threshold: Minimum centroid similarity to report

        Returns:
            List of CrossReference with ref_type=SEMANTIC_THEME
        """
        try:
            import numpy as np
        except ImportError:
            logger.debug("numpy not available, skipping semantic link detection")
            return []

        if len(debate_chunks_by_law) < 2:
            return []

        # Compute centroid embedding for each law's top chunks
        law_centroids: dict[str, tuple[list, "np.ndarray"]] = {}
        for law_key, chunks in debate_chunks_by_law.items():
            top_chunks = sorted(chunks, key=lambda x: x[1], reverse=True)[:5]
            if not top_chunks:
                continue
            texts = [c.content[:2000] for c, _ in top_chunks]
            try:
                embeddings = embedding_service.generate_embeddings(texts)
                centroid = np.mean(embeddings, axis=0)
                law_centroids[law_key] = (top_chunks, centroid)
            except Exception as e:
                logger.debug(f"Failed to embed chunks for {law_key}: {e}")
                continue

        # Compare each pair of laws
        links = []
        keys = list(law_centroids.keys())
        for i, k1 in enumerate(keys):
            info1 = sections_by_law.get(k1, {})
            for k2 in keys[i + 1:]:
                c1 = law_centroids[k1][1]
                c2 = law_centroids[k2][1]

                # Cosine similarity
                dot = np.dot(c1, c2)
                norm = np.linalg.norm(c1) * np.linalg.norm(c2)
                if norm == 0:
                    continue
                sim = float(dot / norm)

                if sim >= similarity_threshold:
                    # Find best matching chunk for citation
                    best_chunk1 = law_centroids[k1][0][0][0]  # highest score chunk
                    citation = (
                        f"Thème commun détecté (similarité: {sim:.2f}). "
                        f"Ex: «{best_chunk1.content[:100]}...»"
                    )
                    links.append(CrossReference(
                        source_law_year=info1.get("law_year", 0),
                        source_law_type=info1.get("law_type", ""),
                        target_law_ref=k2,
                        citation=citation,
                        chunk_score=sim,
                        ref_type=ReferenceType.SEMANTIC_THEME,
                        confidence=sim * _TYPE_WEIGHTS[ReferenceType.SEMANTIC_THEME],
                    ))

        links.sort(key=lambda x: x.confidence, reverse=True)
        return links

    def detect_all(
        self,
        sections_by_law: dict[str, dict],
        debate_chunks_by_law: dict[str, list],
        article_ref: str = "",
        min_chunk_score: float = 0.35,
        enable_llm: bool = True,
    ) -> list[CrossReference]:
        """
        Run all detection methods and merge results.

        Args:
            sections_by_law: Mapping of law keys to law info
            debate_chunks_by_law: Mapping of law keys to (chunk, score) lists
            article_ref: CGI article reference for LLM context
            min_chunk_score: Minimum chunk score for regex detection
            enable_llm: Whether to run Groq-based implicit detection

        Returns:
            Merged, deduplicated list of CrossReference sorted by confidence
        """
        # Regex-based detection
        refs = self.detect_cross_references(
            sections_by_law, debate_chunks_by_law,
            min_chunk_score=min_chunk_score,
            article_ref=article_ref,
        )

        # LLM-based implicit detection
        if enable_llm and article_ref:
            try:
                implicit = self.detect_implicit_references(
                    sections_by_law, debate_chunks_by_law,
                    article_ref=article_ref,
                    min_chunk_score=max(min_chunk_score, 0.45),
                )
                refs.extend(implicit)
            except Exception as e:
                logger.debug(f"Implicit reference detection failed: {e}")

        # Final dedup across all methods
        seen = set()
        deduped = []
        for cr in refs:
            key = (cr.source_law_year, cr.target_law_ref)
            if key not in seen:
                seen.add(key)
                deduped.append(cr)

        deduped.sort(key=lambda x: x.confidence, reverse=True)
        return deduped

    @staticmethod
    def _extract_citation(text: str, match: re.Match, max_len: int = 300) -> str:
        """Extract the full sentence containing the match, capped at max_len."""
        # Find sentence boundaries around the match
        sentence_start = 0
        for m in _SENTENCE_END.finditer(text[:match.start()]):
            sentence_start = m.end()

        sentence_end = len(text)
        end_match = _SENTENCE_END.search(text, match.end())
        if end_match:
            sentence_end = end_match.end()

        citation = text[sentence_start:sentence_end].strip()

        if len(citation) > max_len:
            # Center around the match
            center = match.start() - sentence_start
            half = max_len // 2
            start = max(0, center - half)
            end = min(len(citation), start + max_len)
            start = max(0, end - max_len)
            result = citation[start:end].strip()
            if start > 0:
                result = "..." + result
            if end < len(citation):
                result = result + "..."
            return result

        return citation

    @staticmethod
    def _match_law_number(
        law_number: str, sections_by_law: dict[str, dict]
    ) -> str | None:
        """Match a law number to a known law key."""
        for key, info in sections_by_law.items():
            if info.get("law_numero") == law_number:
                return key
        return None

    @staticmethod
    def _parse_llm_references(
        response: str,
        source_year: int,
        source_type: str,
        known_law_years: dict[tuple[int, str], str],
        source_key: str,
        avg_chunk_score: float,
    ) -> list[CrossReference]:
        """Parse Groq JSON response into CrossReference objects."""
        refs = []
        try:
            # Clean response (strip markdown fences if present)
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1]
                if cleaned.endswith("```"):
                    cleaned = cleaned.rsplit("```", 1)[0]
            data = json.loads(cleaned)

            if not data.get("has_references"):
                return []

            for ref in data.get("references", []):
                target_year = ref.get("target_year")
                target_type = ref.get("target_type", "LF")
                context = ref.get("context", "")
                llm_confidence = ref.get("confidence", 0.5)

                if not target_year or target_year == source_year:
                    continue

                # Validate against known laws
                target_key = known_law_years.get((target_year, target_type))
                if not target_key or target_key == source_key:
                    # Try matching just the year with any type
                    matched = False
                    for (y, _), k in known_law_years.items():
                        if y == target_year and k != source_key:
                            target_key = k
                            matched = True
                            break
                    if not matched:
                        # Reference to a law not in our analysis set - still record it
                        target_key = f"{target_type}_{target_year}"

                refs.append(CrossReference(
                    source_law_year=source_year,
                    source_law_type=source_type,
                    target_law_ref=target_key,
                    citation=context[:300] if context else "",
                    chunk_score=avg_chunk_score,
                    ref_type=ReferenceType.IMPLICIT_LLM,
                    confidence=(
                        avg_chunk_score * llm_confidence
                        * _TYPE_WEIGHTS[ReferenceType.IMPLICIT_LLM]
                    ),
                ))
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.debug(f"Failed to parse LLM cross-ref response: {e}")

        return refs
