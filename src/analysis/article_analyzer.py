"""Article analyzer service for generating comprehensive analysis documents."""

import dataclasses
import json
import logging
import re
from datetime import UTC, date, datetime, timedelta
from itertools import groupby
from pathlib import Path

from sqlalchemy import String, or_

from src.analysis.amendment_tracker import AmendmentTracker
from src.analysis.article_index import ArticleIndex, get_article_index
from src.analysis.cross_law_analyzer import CrossLawAnalyzer
from src.analysis.models import (
    ArticleAnalysisResult,
    ArticleInfo,
    CertitudeLevel,
    ConsolidationPeriod,
    ForwardLookingSection,
    LawModification,
    OperationalParameters,
    QuotedSource,
    SectionAnalysis,
    SynthesisRow,
)
from src.analysis.prompts import (
    CONSOLIDATION_PROMPT,
    SECTION_EXTRACTION_PROMPT,
    format_amendment_chunks_for_prompt,
    format_debate_chunks_for_prompt,
)
from src.db.models import Amendement, Article, DossierLegislatif, Loi, SourceExterne, get_session
from src.indexing.chunking import Chunk, chunk_amendment
from src.indexing.search import SearchService
from src.utils.config import get_settings
from src.utils.groq_pool import GroqClientPool, get_groq_pool

logger = logging.getLogger(__name__)


class ArticleAnalyzer:
    """
    Generate comprehensive article analysis documents.

    This service orchestrates the multi-stage analysis pipeline:
    1. Load article modification history from index
    2. For each modification, perform hybrid search
    3. Extract intent using LLM for each modification
    4. Build synthesis across all modifications
    5. Return structured result for rendering

    Uses GroqClientPool for automatic API key rotation on rate limits.
    """

    def __init__(
        self,
        search_service: SearchService | None = None,
        article_index: ArticleIndex | None = None,
        groq_pool: GroqClientPool | None = None,
    ):
        self.settings = get_settings()
        self.search_service = search_service
        self.article_index = article_index or get_article_index()
        self._anthropic_client = None
        self._groq_pool = groq_pool

        if self.settings.llm_provider == "groq":
            pool = self._get_groq_pool()
            logger.info(f"ArticleAnalyzer using GroqClientPool with {pool.total_keys_count} API keys")

    def _get_groq_pool(self) -> GroqClientPool:
        """Get Groq client pool, using singleton if not provided."""
        if self._groq_pool is None:
            self._groq_pool = get_groq_pool()
        return self._groq_pool

    @property
    def anthropic_client(self):
        """Lazy-load Anthropic client."""
        if self._anthropic_client is None:
            from anthropic import Anthropic
            self._anthropic_client = Anthropic(api_key=self.settings.anthropic_api_key)
        return self._anthropic_client

    def _ensure_search_service(self) -> None:
        """Ensure search service is loaded using configured provider."""
        if self.search_service is None:
            self.search_service = SearchService.create_configured()

    def analyze_article(
        self,
        article_ref: str,
        question: str,
        include_jurisprudence: bool = True,
        max_modifications: int | None = None,
        include_consolidation_periods: bool = True,
    ) -> ArticleAnalysisResult:
        """
        Generate full analysis for an article.

        Args:
            article_ref: Article reference (e.g., "150-0 B ter")
            question: The question to answer
            include_jurisprudence: Whether to include jurisprudence section
            max_modifications: Limit number of modifications to analyze (None = all)
            include_consolidation_periods: Whether to detect and include consolidation periods

        Returns:
            Complete ArticleAnalysisResult
        """
        self._ensure_search_service()

        # Stage 1: Get article info from index
        article_info = self.article_index.get_article(article_ref)
        if not article_info:
            raise ValueError(f"Article '{article_ref}' not found in index")

        logger.info(f"Analyzing article {article_ref} with {len(article_info.modifications)} modifications")

        # Stage 2-4: Analyze each modification
        sections = []
        modifications = article_info.modifications
        if max_modifications:
            modifications = modifications[:max_modifications]

        total_debates = 0
        total_interventions = 0

        # Group modifications by law_numero for single search per law
        # Sort first — groupby only groups consecutive elements
        sorted_mods = sorted(modifications, key=lambda m: m.law_numero)
        grouped_mods = []
        for key, group in groupby(sorted_mods, key=lambda m: m.law_numero):
            grouped_mods.append(list(group))

        # Track curated citations used across sections for dedup
        used_curated_citations: set[str] = set()

        for mod_group in grouped_mods:
            primary_mod = mod_group[0]
            logger.info(f"Analyzing {primary_mod.short_ref} ({len(mod_group)} article(s))...")
            section, debates_count, interventions_count = self._analyze_modification(
                article_info, primary_mod
            )
            # Record all articles in the group
            if len(mod_group) > 1:
                all_articles = ", ".join(m.article_in_law for m in mod_group)
                section.modification = dataclasses.replace(
                    primary_mod, article_in_law=all_articles
                )
            # Augment with curated quotes (with cross-section dedup + relevance check)
            sources_before = len(section.sources_an) + len(section.sources_senat)
            section = self._augment_with_curated_quotes(
                article_info.reference, primary_mod, section,
                article_keywords=article_info.keywords,
                used_curated_citations=used_curated_citations,
            )
            sources_after = len(section.sources_an) + len(section.sources_senat)
            sections.append(section)
            total_debates += debates_count
            total_interventions += interventions_count + (sources_after - sources_before)

        # Stage 4.5: Deduplicate citations across modifications by date proximity
        sections = self._dedup_citations_by_date_proximity(sections)

        # Stage 5: Build synthesis
        synthese = self._build_synthesis(article_info, sections)

        # Detect consolidation periods
        consolidation_periods = []
        if include_consolidation_periods:
            consolidation_periods = self._detect_consolidation_periods(
                article_info, modifications
            )

        # Build parliamentary source references
        an_sources, senat_sources = self._build_parliamentary_sources(sections)

        # Get jurisprudence
        jurisprudence = []
        if include_jurisprudence:
            jurisprudence = self.article_index.get_jurisprudence(article_ref)

        # Get forward-looking analysis
        forward_looking = self._get_forward_looking(article_info)

        # Get enhanced methodology references
        rapports, dila_sources = self._get_methodology_references(article_info)

        # Cross-law reference detection (Phase 5A)
        cross_references = []
        try:
            cross_analyzer = CrossLawAnalyzer()
            # Build debate chunks per law and sections_by_law mappings
            # from sections analyzed above (lightweight, no extra search needed)
            sections_by_law: dict[str, dict] = {}
            debate_chunks_by_law: dict[str, list] = {}
            for section in sections:
                mod = section.modification
                law_key = f"{mod.law_type}_{mod.law_year}"
                sections_by_law[law_key] = {
                    "law_type": mod.law_type,
                    "law_year": mod.law_year,
                    "law_numero": mod.law_numero,
                }
                # Re-search to get chunks for cross-law detection
                # Only if we have the search service loaded
                if self.search_service:
                    if mod.law_type in ("LF", "LFSS"):
                        ds = mod.date - timedelta(days=450)
                    else:
                        ds = mod.date - timedelta(days=180)
                    de = mod.date + timedelta(days=30)
                    try:
                        sr = self.search_service.search_article_modification(
                            article_ref=article_info.reference,
                            article_keywords=article_info.keywords,
                            law_type=mod.law_type,
                            law_year=mod.law_year,
                            date_start=ds, date_end=de,
                            top_k=10, min_relevant=1,
                            article_in_law=mod.article_in_law,
                        )
                        debate_chunks_by_law[law_key] = sr.get("debates", [])
                    except Exception:
                        pass

            if sections_by_law and debate_chunks_by_law:
                raw_refs = cross_analyzer.detect_cross_references(
                    sections_by_law, debate_chunks_by_law
                )
                cross_references = [
                    {
                        "source_law": f"{cr.source_law_type} {cr.source_law_year}",
                        "target_law": cr.target_law_ref,
                        "citation": cr.citation,
                    }
                    for cr in raw_refs
                ]
        except Exception as e:
            logger.debug(f"Cross-law detection failed: {e}")

        # PLF-LF delta analysis (Phase 5B)
        plf_lf_deltas = self._compute_plf_lf_delta(article_info, sections)

        # Build truncation note if max_modifications was applied
        truncation_note = ""
        if max_modifications and len(modifications) < len(article_info.modifications):
            truncation_note = (
                f"Note: Seules {len(modifications)} des "
                f"{len(article_info.modifications)} modifications ont été analysées."
            )

        # Build result — historique matches actually-analyzed modifications
        return ArticleAnalysisResult(
            article_ref=article_ref,
            article_titre=article_info.titre,
            article_code=article_info.code,
            question=question if not truncation_note else f"{question}\n\n> *{truncation_note}*",
            historique=modifications,
            sections=sections,
            consolidation_periods=consolidation_periods,
            synthese_objectif=synthese.get("objectif", ""),
            synthese_evolution=synthese.get("evolution", []),
            synthese_preoccupations=synthese.get("preoccupations", []),
            forward_looking=forward_looking,
            jurisprudence=jurisprudence,
            bofip_url=article_info.bofip_url or "",
            legifrance_url=article_info.legifrance_url or "",
            rapports_parlementaires=rapports,
            dila_sources=dila_sources,
            cross_references=cross_references,
            plf_lf_deltas=plf_lf_deltas,
            sources_parlementaires_an=an_sources,
            sources_parlementaires_senat=senat_sources,
            total_debates_searched=total_debates,
            total_interventions_found=total_interventions,
            generated_date=datetime.now(UTC).strftime("%Y-%m-%d"),
        )

    def _filter_chunks_by_article_relevance(
        self, chunks: list, article_ref: str, article_keywords: list[str]
    ) -> list:
        """Remove chunks that don't mention the article or its keywords.

        For short article refs (e.g. "39"), uses contextualized regex patterns
        to avoid matching neighbors like "39 ter", "39 decies", or substrings
        like "239".
        """
        from src.indexing.search import _build_contextualized_patterns, _is_short_article_ref

        ref_lower = article_ref.lower()
        ref_variants = [ref_lower, ref_lower.replace("-", " ")]

        # For short refs, use strict regex to avoid "39 ter", "239", etc.
        ctx_patterns = None
        if _is_short_article_ref(article_ref):
            ctx_patterns = _build_contextualized_patterns(article_ref)

        filtered = []
        for chunk, score in chunks:
            content_lower = chunk.content.lower()
            # Keep if mentions article reference (with strict matching for short refs)
            if ctx_patterns:
                if any(p.search(chunk.content) for p in ctx_patterns):
                    filtered.append((chunk, score))
                    continue
            else:
                if any(v in content_lower for v in ref_variants):
                    filtered.append((chunk, score))
                    continue
            # Keep if mentions at least 2 article keywords
            kw_matches = sum(1 for kw in article_keywords if kw.lower() in content_lower)
            if kw_matches >= 2:
                filtered.append((chunk, score))
        return filtered

    def _llm_relevance_filter(
        self,
        chunks: list[tuple[Chunk, float]],
        article_ref: str,
        law_type: str,
        law_year: int,
    ) -> list[tuple[Chunk, float]]:
        """Stage 2: LLM-based relevance filtering.

        Asks a fast LLM whether each chunk is actually about the target article.
        Filters out false positives that passed keyword/semantic search.
        Only called when settings.multi_stage_retrieval is True.
        """
        if not chunks:
            return []

        # Batch chunks into a single prompt for efficiency (avoid N API calls)
        # Max ~10 chunks per batch to stay within token limits
        BATCH_SIZE = 10
        filtered = []

        for i in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[i:i + BATCH_SIZE]
            chunk_descriptions = []
            for idx, (chunk, score) in enumerate(batch):
                # Truncate content to ~300 chars for the relevance check
                content_preview = chunk.content[:300].replace("\n", " ").strip()
                chunk_descriptions.append(
                    f"[EXTRAIT {idx + 1}] (score: {score:.2f})\n{content_preview}"
                )

            chunks_text = "\n\n".join(chunk_descriptions)

            prompt = (
                f"Tu es un filtre de pertinence pour l'analyse législative.\n\n"
                f"Article cible: article {article_ref} du CGI\n"
                f"Loi: {law_type} {law_year}\n\n"
                f"ATTENTION: Ne confonds PAS l'article {article_ref} du CGI avec:\n"
                f"- L'article {article_ref} de la loi (numéro d'article dans le texte)\n"
                f"- Les articles voisins du CGI ({article_ref} bis, {article_ref} ter, "
                f"{article_ref} quater, {article_ref} decies, etc.)\n\n"
                f"Pour chaque extrait ci-dessous, indique s'il concerne RÉELLEMENT "
                f"l'article {article_ref} du CGI lui-même "
                f"(pas un article voisin, pas un numéro d'article dans la loi, "
                f"pas un sujet différent).\n\n"
                f"Extraits:\n{chunks_text}\n\n"
                f"Réponds en JSON: {{\"relevant\": [liste des numéros d'extraits pertinents]}}\n"
                f"Exemple: {{\"relevant\": [1, 3, 5]}}\n"
                f"Si aucun extrait n'est pertinent: {{\"relevant\": []}}"
            )

            try:
                response = self._call_llm(prompt, temperature=0.0)
                # Parse response
                text = response.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                result = json.loads(text)
                relevant_ids = set(result.get("relevant", []))

                for idx, (chunk, score) in enumerate(batch):
                    if (idx + 1) in relevant_ids:
                        filtered.append((chunk, score))
            except Exception as e:
                logger.warning(f"LLM relevance filter failed for batch: {e}. Keeping all chunks.")
                filtered.extend(batch)

        logger.info(
            f"  LLM relevance filter: {len(chunks)} → {len(filtered)} chunks "
            f"for article {article_ref}"
        )
        return filtered

    def _analyze_modification(
        self,
        article_info: ArticleInfo,
        mod: LawModification,
    ) -> tuple[SectionAnalysis, int, int]:
        """
        Analyze a single law modification.

        Returns:
            Tuple of (SectionAnalysis, debates_searched, interventions_found)
        """
        # Define date range for search based on law type:
        # LF/LFSS: PLF filed ~Sep N-1, debates Oct-Dec N-1, promulgation Dec N-1 to Jan N.
        # Must look back 15 months from promulgation to capture PLF examination debates.
        # LFR has a shorter legislative process.
        if mod.law_type in ("LF", "LFSS"):
            date_start = mod.date - timedelta(days=450)  # 15 months: captures PLF examination
        else:
            date_start = mod.date - timedelta(days=180)  # 6 months for LFR
        date_end = mod.date + timedelta(days=30)

        # Stage 1: Search — broader recall when multi-stage retrieval is enabled
        multi_stage = self.settings.multi_stage_retrieval
        search_top_k = 25 if multi_stage else 15
        min_sim_override = self.settings.multi_stage_min_similarity if multi_stage else None

        search_results = self.search_service.search_article_modification(
            article_ref=article_info.reference,
            article_keywords=article_info.keywords,
            law_type=mod.law_type,
            law_year=mod.law_year,
            date_start=date_start,
            date_end=date_end,
            top_k=search_top_k,
            min_relevant=5,
            min_similarity_override=min_sim_override,
            article_in_law=mod.article_in_law,
        )

        debate_chunks = search_results.get("debates", [])
        amendment_chunks = search_results.get("amendments", [])

        # Supplement with amendments directly linked to the article in DB
        linked_amendments = self._fetch_linked_amendments(
            mod, article_ref=article_info.reference, article_keywords=article_info.keywords
        )
        if linked_amendments:
            existing_ids = {chunk.source_id for chunk, _ in amendment_chunks}
            for chunk, score in linked_amendments:
                if chunk.source_id not in existing_ids:
                    amendment_chunks.append((chunk, score))
                    existing_ids.add(chunk.source_id)

        # Pre-filter chunks by article relevance before LLM
        debate_chunks = self._filter_chunks_by_article_relevance(
            debate_chunks, article_info.reference, article_info.keywords
        )
        amendment_chunks = self._filter_chunks_by_article_relevance(
            amendment_chunks, article_info.reference, article_info.keywords
        )

        # Stage 2: LLM-based relevance filter (when multi_stage_retrieval is enabled)
        if multi_stage and (debate_chunks or amendment_chunks):
            logger.info("  Multi-stage Stage 2: LLM relevance filtering...")
            debate_chunks = self._llm_relevance_filter(
                debate_chunks, article_info.reference, mod.law_type, mod.law_year
            )
            amendment_chunks = self._llm_relevance_filter(
                amendment_chunks, article_info.reference, mod.law_type, mod.law_year
            )

        # Fetch exposé des motifs / rapport content for this law
        expose_text = self._fetch_expose_motifs(
            article_ref=article_info.reference,
            law_numero=mod.law_ref,
        )
        if expose_text:
            logger.info(f"  Found exposé des motifs ({len(expose_text)} chars)")

        logger.info(
            f"  Found {len(debate_chunks)} debate chunks, "
            f"{len(amendment_chunks)} amendment chunks (after relevance filter)"
        )

        # Extract intent using LLM
        if debate_chunks or amendment_chunks:
            section = self._extract_section_with_llm(article_info, mod, debate_chunks, amendment_chunks, expose_text)
        else:
            section = self._create_empty_section(mod)

        section.debates_analyzed = len(debate_chunks)
        section.amendments_analyzed = len(amendment_chunks)

        # Enrich with amendment genealogy insights
        try:
            tracker = AmendmentTracker()
            chains = tracker.find_genealogy(mod.law_numero, article_info.reference)
            for chain in chains:
                if len(chain.stages) > 1:
                    section.points_vigilance.append(
                        f"Généalogie d'amendement: {chain.summary}"
                    )
        except Exception as e:
            logger.debug(f"Amendment genealogy failed: {e}")

        # Note: curated quote augmentation moved to analyze_article() loop
        # for cross-section deduplication support

        return section, len(debate_chunks), len(amendment_chunks)

    def _augment_with_curated_quotes(
        self,
        article_ref: str,
        mod: LawModification,
        section: SectionAnalysis,
        article_keywords: list[str] | None = None,
        used_curated_citations: set[str] | None = None,
    ) -> SectionAnalysis:
        """
        Augment section with curated quotes if search yielded few results.

        Curated quotes are used when:
        - No sources were found, or
        - Less than 2 sources per chamber were found

        Args:
            article_ref: CGI article reference (e.g., "199 terdecies-0 A")
            mod: The law modification being analyzed
            section: Section to augment
            article_keywords: Keywords for relevance filtering
            used_curated_citations: Set of citation prefixes already used in prior
                sections (for cross-section deduplication). Updated in-place.
        """
        if used_curated_citations is None:
            used_curated_citations = set()

        total_sources = len(section.sources_an) + len(section.sources_senat)

        # Get year for curated quotes lookup
        # For LF 2019, the modification happened in 2018, so we may need to check both
        year = mod.law_year
        if mod.law_type == "LF":
            # LF years are off by one (LF 2019 = passed in 2018)
            year_to_check = mod.law_year - 1
        else:
            year_to_check = mod.law_year

        # Only augment if we have few sources
        if total_sources >= self.settings.curated_augmentation_min_sources:
            return section

        # Try to get curated quotes
        curated = self._get_curated_quotes(article_ref, year)
        if not curated:
            # Try the alternative year
            curated = self._get_curated_quotes(article_ref, year_to_check)

        if not curated:
            return section

        logger.info(f"Augmenting {mod.short_ref} with {len(curated)} curated quotes")

        # Add curated quotes that aren't duplicates
        existing_citations = {s.citation[:50].lower() for s in section.sources_an + section.sources_senat}

        for quote in curated:
            citation_key = quote.citation[:80].lower()

            # Cross-section dedup: skip if already used in a prior section
            if citation_key in used_curated_citations:
                continue

            if quote.citation[:50].lower() in existing_citations:
                continue

            # B12: Reject quotes that start mid-word (truncated extraction)
            stripped = quote.citation.lstrip('"\'  «')
            if stripped and stripped[0].islower():
                logger.warning(f"Rejecting mid-word curated quote: {quote.citation[:60]}...")
                continue

            # Apply procedural filter even to curated quotes (catches data issues)
            if self._is_procedural_quote(quote.citation):
                logger.warning(
                    f"Rejecting procedural curated quote: {quote.citation[:80]}..."
                )
                continue
            # Relevance check: curated quotes require stricter matching (3 keywords)
            if article_keywords and not self._quote_is_relevant(
                quote.citation, article_ref, article_keywords, min_keywords=3,
            ):
                logger.warning(
                    f"Rejecting irrelevant curated quote (doesn't match {article_ref}): "
                    f"{quote.citation[:80]}..."
                )
                continue
            if quote.chambre == "Sénat":
                section.sources_senat.append(quote)
            else:
                section.sources_an.append(quote)
            existing_citations.add(quote.citation[:50].lower())
            used_curated_citations.add(citation_key)

        # B10: Update intention text if curated quotes filled an empty section
        added_sources = (len(section.sources_an) + len(section.sources_senat)) - total_sources
        if added_sources > 0 and "n'ont pas été identifiés" in (section.intention or ""):
            section.intention = (
                "L'intention du législateur est éclairée par les sources "
                "parlementaires identifiées ci-dessous."
            )

        # Recalculate certitude with weighted sources after augmentation
        if len(section.sources_an) + len(section.sources_senat) > total_sources:
            all_sources = section.sources_an + section.sources_senat
            new_niveau = CertitudeLevel.from_weighted_sources(all_sources)
            if new_niveau.score > section.niveau_certitude.score:
                section.niveau_certitude = new_niveau
                section.certitude_justification = (
                    "Sources complétées avec des citations vérifiées des débats parlementaires."
                )

        return section

    def _extract_section_with_langextract(
        self,
        article_info: ArticleInfo,
        mod: LawModification,
        debate_chunks: list,
        amendment_chunks: list,
        expose_motifs: str = "",
    ) -> SectionAnalysis:
        """Extract section using LangExtract for source-grounded citations.

        No _validate_quotes() needed — LangExtract guarantees source grounding,
        so citations are verifiable by construction.
        """
        from src.analysis.langextract_provider import LangExtractProvider

        provider = LangExtractProvider(
            api_key=self.settings.langextract_api_key,
            model_id=self.settings.langextract_model,
        )

        debate_text = format_debate_chunks_for_prompt(debate_chunks, max_chars=40000)
        amendment_text = format_amendment_chunks_for_prompt(amendment_chunks, max_chars=12000)
        expose_section = expose_motifs if expose_motifs else ""

        return provider.extract_section(article_info, mod, debate_text, amendment_text, expose_section)

    def _extract_section_with_llm(
        self,
        article_info: ArticleInfo,
        mod: LawModification,
        debate_chunks: list,
        amendment_chunks: list,
        expose_motifs: str = "",
    ) -> SectionAnalysis:
        """Extract section analysis using LLM."""
        # Route to LangExtract if configured
        if self.settings.extraction_provider == "langextract":
            return self._extract_section_with_langextract(
                article_info, mod, debate_chunks, amendment_chunks, expose_motifs
            )

        # Route to two-step extraction if configured
        if self.settings.extraction_mode == "two_step":
            return self._extract_section_two_step(
                article_info, mod, debate_chunks, amendment_chunks, expose_motifs
            )

        # Format chunks for prompt — use more context for Anthropic (200K) vs Groq (128K)
        if self.settings.llm_provider == "anthropic":
            debate_text = format_debate_chunks_for_prompt(debate_chunks, max_chars=40000)
            amendment_text = format_amendment_chunks_for_prompt(
                amendment_chunks, max_chars=12000, debate_chunks=debate_chunks
            )
        else:
            debate_text = format_debate_chunks_for_prompt(debate_chunks, max_chars=20000)
            amendment_text = format_amendment_chunks_for_prompt(
                amendment_chunks, max_chars=6000, debate_chunks=debate_chunks
            )

        # Categorize exposé sections with structural markers (Phase 2D)
        if expose_motifs:
            expose_section = self._categorize_expose(expose_motifs)
        else:
            expose_section = "[Aucun exposé des motifs trouvé]"

        # Build prompt with article keywords for validation
        prompt = SECTION_EXTRACTION_PROMPT.format(
            article_ref=article_info.reference,
            article_code=article_info.code,
            article_titre=article_info.titre,
            article_keywords=", ".join(article_info.keywords[:5]),
            law_ref=mod.law_ref,
            law_date=mod.date.isoformat(),
            article_in_law=mod.article_in_law,
            nature=mod.nature.value,
            debate_chunks=debate_text,
            amendments=amendment_text,
            expose_motifs=expose_section,
        )

        # Call LLM with retry on empty/invalid output
        result = {}
        try:
            response_text = self._call_llm(prompt)
            result = self._parse_section_json(response_text)
        except Exception as e:
            logger.warning(f"LLM extraction failed (attempt 1): {e}")
            error_str = str(e).lower()
            if any(k in error_str for k in ("403", "forbidden", "unauthorized")):
                logger.error("Permanent LLM auth error, skipping retry")
                return self._create_empty_section(mod)

        # Retry once if result is empty or missing critical fields
        if not result.get("intention") and not result.get("contexte_legislatif"):
            logger.info("Empty or invalid LLM response, retrying with temperature=0.1...")
            try:
                response_text = self._call_llm(prompt, temperature=0.1)
                result = self._parse_section_json(response_text)
            except Exception as e:
                logger.error(f"LLM extraction failed (retry): {e}")

        if not result.get("intention") and not result.get("contexte_legislatif"):
            logger.error("LLM extraction failed after retry, creating empty section")
            return self._create_empty_section(mod)

        # Retry if single-chamber bias detected and source material has both chambers
        has_an_sources = result.get("sources_an") and len(result["sources_an"]) > 0
        has_senat_sources = result.get("sources_senat") and len(result["sources_senat"]) > 0
        if result.get("intention") and (has_an_sources != has_senat_sources):
            # Check if debate text actually contains both chambers
            if "assemblée" in debate_text.lower() and "sénat" in debate_text.lower():
                logger.info("Single-chamber bias detected, retrying with explicit instruction...")
                enhanced_prompt = (
                    prompt
                    + "\n\nIMPORTANT: Cite sources from BOTH chambers"
                    " (Assemblée nationale AND Sénat)."
                    " The debate sources contain contributions from both."
                )
                try:
                    response_text = self._call_llm(enhanced_prompt, temperature=0.1)
                    retry_result = self._parse_section_json(response_text)
                    if retry_result.get("sources_an") and retry_result.get("sources_senat"):
                        result = retry_result
                except Exception as retry_err:
                    logger.debug(f"Chamber bias retry failed: {retry_err}")

        # Build sources from chamber-specific lists (new format)
        sources_an = []
        for s in result.get("sources_an", []):
            sources_an.append(
                QuotedSource(
                    citation=s.get("citation", ""),
                    orateur=s.get("orateur", ""),
                    fonction=s.get("fonction", ""),
                    date=s.get("date", ""),
                    chambre="AN",
                )
            )

        sources_senat = []
        for s in result.get("sources_senat", []):
            sources_senat.append(
                QuotedSource(
                    citation=s.get("citation", ""),
                    orateur=s.get("orateur", ""),
                    fonction=s.get("fonction", ""),
                    date=s.get("date", ""),
                    chambre="Sénat",
                )
            )

        # Fallback to old format if needed (backwards compatibility)
        if not sources_an and not sources_senat:
            for s in result.get("sources", []):
                source = QuotedSource(
                    citation=s.get("citation", ""),
                    orateur=s.get("orateur", ""),
                    fonction=s.get("fonction", ""),
                    date=s.get("date", ""),
                    chambre=s.get("chambre", ""),
                )
                if source.chambre == "Sénat":
                    sources_senat.append(source)
                else:
                    sources_an.append(source)

        niveau = CertitudeLevel.MOYEN
        niveau_str = result.get("niveau_certitude", "moyen").lower()
        if "élevé" in niveau_str or "eleve" in niveau_str:
            niveau = CertitudeLevel.ELEVE
        elif "faible" in niveau_str:
            niveau = CertitudeLevel.FAIBLE

        # Parse operational parameters if present (dynamic per article)
        operational_params = None
        if "operational_parameters" in result:
            op = result["operational_parameters"]
            if isinstance(op, dict):
                # Filter out null/none values
                params = {k: str(v) for k, v in op.items() if v and str(v).lower() not in ("null", "none", "n/a")}
                if params:
                    operational_params = OperationalParameters(parameters=params)

        # Parse rejected amendment rationale (2C)
        points_vigilance = list(result.get("points_vigilance", []))
        for amend_rej in result.get("amendements_rejetes_detailles", []):
            numero = amend_rej.get("numero", "?")
            auteur = amend_rej.get("auteur", "inconnu")
            objet = amend_rej.get("objet", "")
            raison = amend_rej.get("raison_rejet", "")
            implication = amend_rej.get("implication_intention", "")
            # Normalize: strip any existing "N°" prefix to avoid "N°N°"
            numero_clean = numero.lstrip("N°").strip()
            # Use the actual status from LLM output (rejeté/retiré/tombé/non soutenu)
            sort_reel = amend_rej.get("sort_reel", "rejeté")
            points_vigilance.append(
                f"Amendement N° {numero_clean} ({auteur}) {sort_reel}: {objet}. "
                f"Raison: {raison}. Implication: {implication}"
            )

        section = SectionAnalysis(
            modification=mod,
            contexte=result.get("contexte_legislatif", ""),
            intention=result.get("intention", ""),
            sources_an=sources_an,
            sources_senat=sources_senat,
            evolution_debats=result.get("evolution_debats", ""),
            points_vigilance=points_vigilance,
            niveau_certitude=niveau,
            certitude_justification=result.get("justification_certitude", ""),
            operational_params=operational_params,
        )

        # Validate quotes against source chunks
        section = self._validate_quotes(
            section,
            debate_chunks + amendment_chunks,
            article_info.keywords,
            article_ref=article_info.reference,
        )

        # Validate certitude reasoning if present
        certitude_reasoning = result.get("certitude_reasoning")
        if certitude_reasoning:
            validated_niveau = self._compute_certitude_from_reasoning(certitude_reasoning)
            if validated_niveau.score < section.niveau_certitude.score:
                section.niveau_certitude = validated_niveau

        # Recalculate confidence from validated sources weighted by speaker importance
        all_sources = section.sources_an + section.sources_senat
        weighted_niveau = CertitudeLevel.from_weighted_sources(all_sources)
        # Take the lower of LLM's assessment and weighted source quality
        # (prevents LLM from claiming "élevé" with no good sources)
        if weighted_niveau.score < section.niveau_certitude.score:
            section.niveau_certitude = weighted_niveau

        section = self._cleanup_section_text(section)
        return section

    @staticmethod
    def _dedup_citations_by_date_proximity(
        sections: list[SectionAnalysis],
    ) -> list[SectionAnalysis]:
        """Remove duplicate citations across sections, keeping each in the
        section whose law date is closest to the citation date.

        Two citations are considered duplicates when the first 80 chars of
        their text AND their speaker match.
        """
        if len(sections) < 2:
            return sections

        # Build index: citation_key → list of (section_idx, list_name, source_idx, distance_days)
        from collections import defaultdict

        citation_locations: dict[str, list[tuple[int, str, int, int]]] = defaultdict(list)

        for sec_idx, section in enumerate(sections):
            law_date = section.modification.date  # date object
            for list_name in ("sources_an", "sources_senat"):
                sources: list = getattr(section, list_name)
                for src_idx, source in enumerate(sources):
                    key = (source.citation[:80].lower().strip(), source.orateur.lower().strip())
                    # Parse citation date
                    try:
                        cite_date = date.fromisoformat(source.date)
                        distance = abs((law_date - cite_date).days)
                    except (ValueError, TypeError):
                        distance = 9999  # no date → lowest priority
                    citation_locations[key].append(
                        (sec_idx, list_name, src_idx, distance, source.is_curated)
                    )

        # For each duplicate group, find the best section (smallest distance)
        # and mark all others for removal
        to_remove: set[tuple[int, str, int]] = set()

        for key, locations in citation_locations.items():
            if len(locations) < 2:
                continue
            # Prefer curated, then smallest distance, then first chronologically
            best = min(locations, key=lambda x: (not x[4], x[3], x[0]))
            for loc in locations:
                if loc is not best:
                    to_remove.add((loc[0], loc[1], loc[2]))

        if not to_remove:
            return sections

        # Remove marked sources (iterate in reverse index order to preserve indices)
        for sec_idx, section in enumerate(sections):
            for list_name in ("sources_an", "sources_senat"):
                sources: list = getattr(section, list_name)
                indices_to_drop = sorted(
                    [src_idx for s, l, src_idx in to_remove if s == sec_idx and l == list_name],
                    reverse=True,
                )
                for idx in indices_to_drop:
                    if idx < len(sources):
                        sources.pop(idx)

        removed_count = len(to_remove)
        if removed_count:
            logger.info(f"Cross-modification dedup: removed {removed_count} duplicate citation(s)")

        return sections

    @staticmethod
    def _cleanup_section_text(section: SectionAnalysis) -> SectionAnalysis:
        """Post-process section text to fix common LLM output artifacts."""
        def _clean(text: str) -> str:
            if not text:
                return text
            # Fix "N°N°" → "N°" (LLM duplicates the prefix)
            text = re.sub(r'N°\s*N°', 'N°', text)
            # Fix "n'porte" and similar apostrophe-collisions
            text = re.sub(r"n'porte", "n'porte", text)
            return text

        section.intention = _clean(section.intention)
        section.evolution_debats = _clean(section.evolution_debats)
        section.certitude_justification = _clean(section.certitude_justification)
        section.contexte = _clean(section.contexte)
        section.points_vigilance = [_clean(pv) for pv in section.points_vigilance]

        # Suppress intent on faible sections that self-admit off-topic/irrelevance
        if section.niveau_certitude == CertitudeLevel.FAIBLE and section.intention:
            section = ArticleAnalyzer._maybe_suppress_faible_intent(section)

        # Fallback message for empty intents
        if not section.intention or not section.intention.strip():
            section.intention = (
                "Les débats retrouvés ne permettent pas d'identifier de manière fiable "
                "l'intention du législateur pour cette modification."
            )

        return section

    @staticmethod
    def _maybe_suppress_faible_intent(section: SectionAnalysis) -> SectionAnalysis:
        """Suppress intent on faible sections that self-admit irrelevance.

        Keep intent if the section has on-topic sources or the justification
        explains limited-but-relevant evidence.
        """
        justification = (section.certitude_justification or "").lower()

        # Patterns that indicate the section is off-topic or has no real evidence
        OFF_TOPIC_PATTERNS = [
            r"aucun débat pertinent identifié",
            r"aucun extrait pertinent",
            r"aucun débat substantiel",
            r"ne (porte|concerne|traite) pas",
            r"ne concerne pas l['\u2019]article",
            r"confusion.{0,30}(cgi|code|loi)",
            r"exclusivement d['\u2019]autres sujets",
            r"ne documente pas l['\u2019]article",
            r"sans rapport.{0,20}avec",
            r"traitent exclusivement d['\u2019]autres",
            r"sans lien direct avec",
            r"sans éclairer l['\u2019]intention",
        ]

        is_off_topic = any(
            re.search(pat, justification) for pat in OFF_TOPIC_PATTERNS
        )

        if not is_off_topic:
            return section

        # Even if justification says off-topic, keep intent if there are real sources
        has_sources = bool(section.sources_an or section.sources_senat)
        if has_sources:
            return section

        # Suppress: replace intent and derived fields with standardized message
        section.intention = ""
        section.evolution_debats = ""
        section.points_vigilance = []
        return section

    def _create_empty_section(
        self,
        mod: LawModification,
    ) -> SectionAnalysis:
        """Create an empty section when no debates are found."""
        return SectionAnalysis(
            modification=mod,
            contexte=f"Modification apportée par {mod.law_ref}",
            intention=(
                "Les débats parlementaires relatifs à cette modification n'ont pas été "
                "identifiés dans la base de données. Une recherche complémentaire dans "
                "les archives parlementaires serait nécessaire."
            ),
            sources_an=[],
            sources_senat=[],
            evolution_debats="",
            points_vigilance=[],
            niveau_certitude=CertitudeLevel.FAIBLE,
            certitude_justification=(
                "Aucun débat parlementaire pertinent trouvé dans la base de données "
                "pour cette modification."
            ),
        )

    def _compute_certitude_from_reasoning(
        self, reasoning: dict
    ) -> CertitudeLevel:
        """Validate LLM's certitude claim against its own reasoning.

        Overrides certitude if reasoning contradicts the claim.
        """
        try:
            explicit_count = int(reasoning.get("extraits_explicites", 0))
            authoritative = bool(reasoning.get("source_autoritative", False))
        except (TypeError, ValueError):
            return CertitudeLevel.MOYEN

        # Apply rules from the prompt
        if explicit_count >= 2 and authoritative:
            return CertitudeLevel.ELEVE
        elif explicit_count >= 1 or authoritative:
            return CertitudeLevel.MOYEN
        else:
            return CertitudeLevel.FAIBLE

    def _extract_section_two_step(
        self,
        article_info: ArticleInfo,
        mod: LawModification,
        debate_chunks: list,
        amendment_chunks: list,
        expose_motifs: str = "",
    ) -> SectionAnalysis:
        """Extract section using two-step approach: fact extraction then intent synthesis."""
        from src.analysis.two_step_prompts import (
            FACT_EXTRACTION_PROMPT,
            INTENT_SYNTHESIS_PROMPT,
        )

        # Format chunks
        _debate_max = 40000 if self.settings.llm_provider == "anthropic" else 20000
        _amend_max = 12000 if self.settings.llm_provider == "anthropic" else 6000
        debate_text = format_debate_chunks_for_prompt(debate_chunks, max_chars=_debate_max)
        amendment_text = format_amendment_chunks_for_prompt(amendment_chunks, max_chars=_amend_max)
        expose_section = expose_motifs if expose_motifs else "[Aucun exposé des motifs trouvé]"

        # Step 1: Extract facts (temperature 0.0)
        fact_prompt = FACT_EXTRACTION_PROMPT.format(
            article_ref=article_info.reference,
            article_code=article_info.code,
            law_ref=mod.law_ref,
            article_in_law=mod.article_in_law,
            debate_chunks=debate_text,
            amendments=amendment_text,
            expose_motifs=expose_section,
        )
        try:
            facts_text = self._call_llm(fact_prompt, temperature=0.0)
            facts_result = self._parse_section_json(facts_text)
        except Exception as e:
            logger.warning(f"Two-step fact extraction failed: {e}, falling back to single")
            return self._extract_section_with_llm(
                article_info, mod, debate_chunks, amendment_chunks, expose_motifs
            )

        if not facts_result.get("facts"):
            logger.warning("No facts extracted, falling back to single-step")
            return self._extract_section_with_llm(
                article_info, mod, debate_chunks, amendment_chunks, expose_motifs
            )

        # Step 2: Synthesize intent from facts (temperature 0.2)
        facts_formatted = json.dumps(facts_result, ensure_ascii=False, indent=2)
        synth_prompt = INTENT_SYNTHESIS_PROMPT.format(
            article_ref=article_info.reference,
            article_code=article_info.code,
            article_titre=article_info.titre,
            law_ref=mod.law_ref,
            law_date=mod.date.isoformat(),
            article_in_law=mod.article_in_law,
            nature=mod.nature.value,
            extracted_facts=facts_formatted,
        )
        try:
            synth_text = self._call_llm(synth_prompt, temperature=0.2)
            result = self._parse_section_json(synth_text)
        except Exception as e:
            logger.warning(f"Two-step synthesis failed: {e}, falling back to single")
            return self._extract_section_with_llm(
                article_info, mod, debate_chunks, amendment_chunks, expose_motifs
            )

        # Build section from synthesis result (same parsing as single-step)
        sources_an = [
            QuotedSource(
                citation=s.get("citation", ""), orateur=s.get("orateur", ""),
                fonction=s.get("fonction", ""), date=s.get("date", ""), chambre="AN",
            )
            for s in result.get("sources_an", [])
        ]
        sources_senat = [
            QuotedSource(
                citation=s.get("citation", ""), orateur=s.get("orateur", ""),
                fonction=s.get("fonction", ""), date=s.get("date", ""), chambre="Sénat",
            )
            for s in result.get("sources_senat", [])
        ]

        niveau = CertitudeLevel.MOYEN
        niveau_str = result.get("niveau_certitude", "moyen").lower()
        if "élevé" in niveau_str or "eleve" in niveau_str:
            niveau = CertitudeLevel.ELEVE
        elif "faible" in niveau_str:
            niveau = CertitudeLevel.FAIBLE

        # Validate certitude reasoning
        certitude_reasoning = result.get("certitude_reasoning")
        if certitude_reasoning:
            validated = self._compute_certitude_from_reasoning(certitude_reasoning)
            if validated.score < niveau.score:
                niveau = validated

        operational_params = None
        if "operational_parameters" in result:
            op = result["operational_parameters"]
            if isinstance(op, dict):
                params = {k: str(v) for k, v in op.items() if v and str(v).lower() not in ("null", "none", "n/a")}
                if params:
                    operational_params = OperationalParameters(parameters=params)

        points_vigilance = list(result.get("points_vigilance", []))
        for amend_rej in result.get("amendements_rejetes_detailles", []):
            numero = amend_rej.get("numero", "?")
            auteur = amend_rej.get("auteur", "inconnu")
            objet = amend_rej.get("objet", "")
            raison = amend_rej.get("raison_rejet", "")
            implication = amend_rej.get("implication_intention", "")
            # Normalize: strip any existing "N°" prefix to avoid "N°N°"
            numero_clean = numero.lstrip("N°").strip()
            # Use the actual status from LLM output (rejeté/retiré/tombé/non soutenu)
            sort_reel = amend_rej.get("sort_reel", "rejeté")
            points_vigilance.append(
                f"Amendement N° {numero_clean} ({auteur}) {sort_reel}: {objet}. "
                f"Raison: {raison}. Implication: {implication}"
            )

        section = SectionAnalysis(
            modification=mod,
            contexte=result.get("contexte_legislatif", ""),
            intention=result.get("intention", ""),
            sources_an=sources_an,
            sources_senat=sources_senat,
            evolution_debats=result.get("evolution_debats", ""),
            points_vigilance=points_vigilance,
            niveau_certitude=niveau,
            certitude_justification=result.get("justification_certitude", ""),
            operational_params=operational_params,
        )

        # Validate quotes against source chunks
        section = self._validate_quotes(
            section, debate_chunks + amendment_chunks,
            article_info.keywords, article_ref=article_info.reference,
        )

        # Weighted source confidence
        all_sources = section.sources_an + section.sources_senat
        weighted_niveau = CertitudeLevel.from_weighted_sources(all_sources)
        if weighted_niveau.score < section.niveau_certitude.score:
            section.niveau_certitude = weighted_niveau

        section = self._cleanup_section_text(section)
        return section

    def _find_amendment_discussion(
        self, amendment_chunks: list, debate_chunks: list
    ) -> list:
        """Link amendments to their debate discussion passages.

        For each amendment, searches debate chunks for mentions of the amendment number.
        Returns amendment chunks with linked debate passages passed to the formatter.
        """
        # This linking is now handled in format_amendment_chunks_for_prompt
        # via the debate_chunks parameter. Just return the chunks as-is.
        return amendment_chunks

    def _compute_plf_lf_delta(
        self,
        article_info: ArticleInfo,
        sections: list[SectionAnalysis],
    ) -> list[dict]:
        """Compare forward_looking PLF propositions against enacted sections.

        Returns list of deltas: [{"plf_year": 2025, "proposed": ..., "enacted": ..., "status": ...}]
        """
        forward_data = self.article_index.get_forward_looking(article_info.reference)
        if not forward_data:
            return []

        deltas = []
        # Build lookup of enacted intentions by year
        enacted_by_year: dict[int, str] = {}
        for section in sections:
            year = section.modification.law_year
            if section.intention:
                enacted_by_year[year] = section.intention

        for fw in forward_data:
            plf_year = fw.get("plf_year") or fw.get("annee")
            if not plf_year:
                continue

            proposed = " ; ".join(fw.get("propositions", []))
            if not proposed:
                continue

            # Check if this PLF year was enacted
            enacted = enacted_by_year.get(plf_year, "")

            if enacted:
                # Simple keyword overlap comparison (no LLM)
                proposed_words = set(re.findall(r'\w+', proposed.lower()))
                enacted_words = set(re.findall(r'\w+', enacted.lower()))
                if proposed_words and enacted_words:
                    overlap = len(proposed_words & enacted_words) / len(proposed_words | enacted_words)
                    if overlap > 0.5:
                        status = "conforme"
                    elif overlap > 0.2:
                        status = "modifié"
                    else:
                        status = "substantiellement modifié"
                else:
                    status = "inconnu"

                deltas.append({
                    "plf_year": plf_year,
                    "proposed": proposed[:300],
                    "enacted": enacted[:300],
                    "status": status,
                })
            else:
                deltas.append({
                    "plf_year": plf_year,
                    "proposed": proposed[:300],
                    "enacted": "",
                    "status": "non adopté ou non trouvé",
                })

        return deltas

    def _call_llm(self, prompt: str, temperature: float = 0.2) -> str:
        """
        Call LLM API and return response text.

        For Groq, uses GroqClientPool for automatic key rotation on rate limits.
        """
        if self.settings.llm_provider == "groq":
            return self._get_groq_pool().chat_completion(
                model=self.settings.groq_model,
                messages=[
                    {
                        "role": "system",
                        "content": "Tu es un assistant juridique expert en droit fiscal français et en analyse des travaux parlementaires. Tu réponds uniquement en JSON valide.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=3500,
                temperature=temperature,
                timeout=90.0,
            )
        else:
            response = self.anthropic_client.messages.create(
                model=self.settings.claude_model,
                max_tokens=4000,
                temperature=temperature,
                messages=[{"role": "user", "content": prompt}],
                timeout=90.0,
            )
            return response.content[0].text

    def _parse_section_json(self, response_text: str) -> dict:
        """Parse JSON from LLM response."""
        # Try to extract JSON from response
        text = response_text.strip()

        # Remove markdown code blocks if present
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]

        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON object in text
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass

            logger.error(f"Failed to parse JSON from: {text[:200]}...")
            return {}

    def _build_synthesis(
        self,
        article_info: ArticleInfo,
        sections: list[SectionAnalysis],
    ) -> dict:
        """Build synthesis from analyzed sections, preferring predefined data."""
        # Get predefined synthesis from index
        index_synthese = self.article_index.get_synthese(article_info.reference)

        # Check if predefined evolution data exists (preferred)
        predefined_evolution = self.article_index.get_synthese_evolution(article_info.reference)

        # Collect parameter evolution timeline from sections
        param_timeline = self._collect_parameter_timeline(sections)

        if predefined_evolution:
            # Use predefined synthesis data — convert to dynamic format
            evolution = []
            for row in predefined_evolution:
                params = {k: v for k, v in row.items() if k != "periode" and v}
                evolution.append(
                    SynthesisRow(
                        periode=row.get("periode", ""),
                        parameters=params,
                    )
                )
            logger.info(f"Using predefined synthesis evolution ({len(evolution)} periods)")
        else:
            # Build evolution table from per-section operational parameters
            evolution = []
            for section in sections:
                periode = f"{section.modification.law_year}"
                params = {"nature": section.modification.nature.value}

                if section.operational_params and section.operational_params.parameters:
                    params.update(section.operational_params.parameters)

                evolution.append(
                    SynthesisRow(periode=periode, parameters=params)
                )

        return {
            "objectif": index_synthese.get("objectif_constant", ""),
            "evolution": evolution,
            "preoccupations": (
                [p] if isinstance(p := index_synthese.get("preoccupations_recurrentes", []), str)
                else p
            ),
            "parameter_timeline": param_timeline,
        }

    def _collect_parameter_timeline(
        self,
        sections: list[SectionAnalysis],
    ) -> dict[str, list[tuple[str, str]]]:
        """Collect operational parameters from all sections into a timeline.

        Returns:
            Dict mapping parameter names to lists of (period, value) tuples.
        """
        timeline: dict[str, list[tuple[str, str]]] = {}
        for section in sections:
            if not section.operational_params or not section.operational_params.parameters:
                continue
            periode = str(section.modification.law_year)
            for param_name, value in section.operational_params.parameters.items():
                if param_name not in timeline:
                    timeline[param_name] = []
                timeline[param_name].append((periode, value))
        return timeline

    def _detect_consolidation_periods(
        self,
        article_info: ArticleInfo,
        modifications: list[LawModification],
        min_gap_years: int = 2,
    ) -> list[ConsolidationPeriod]:
        """
        Detect gaps between modifications and analyze debates in those periods.

        Args:
            article_info: Article information
            modifications: List of modifications (sorted by date)
            min_gap_years: Minimum years between modifications to create a consolidation period

        Returns:
            List of ConsolidationPeriod objects with analyzed content
        """
        consolidation_periods = []

        # Deduplicate: track unique (current_year, next_year) transitions
        seen_gaps = set()
        for i in range(len(modifications) - 1):
            current_year = modifications[i].law_year
            next_year = modifications[i + 1].law_year
            gap = next_year - current_year

            if gap >= min_gap_years:
                gap_key = (current_year, next_year)
                if gap_key in seen_gaps:
                    continue
                seen_gaps.add(gap_key)

                start_year = current_year + 1
                end_year = next_year - 1

                if start_year >= end_year:
                    continue  # Skip single-year "periods" like 2013-2013

                logger.info(
                    f"Analyzing consolidation period: {start_year}-{end_year} "
                    f"({gap - 1} years without modification)"
                )

                # Search for debates mentioning the article during the consolidation period
                date_start = date(start_year, 1, 1)
                date_end = date(end_year, 12, 31)

                # Use article-specific search with strict filtering
                search_results = self.search_service.search_article_modification(
                    article_ref=article_info.reference,
                    article_keywords=article_info.keywords,
                    law_type="",  # No specific law type for consolidation
                    law_year=start_year,
                    date_start=date_start,
                    date_end=date_end,
                    top_k=10,
                    min_relevant=2,  # Only analyze if we have meaningful results
                )

                debate_chunks = search_results.get("debates", [])

                # If we found relevant debates, analyze them with LLM
                if debate_chunks:
                    consolidation = self._analyze_consolidation_period(
                        article_info, start_year, end_year, debate_chunks
                    )
                else:
                    # Search for rejected amendments mentioning the article
                    rejected_info = []
                    ref_pattern = f"%{article_info.reference}%"
                    try:
                        with get_session() as session:
                            rejected_amendments = (
                                session.query(Amendement)
                                .join(DossierLegislatif, Amendement.dossier_id == DossierLegislatif.id)
                                .join(Loi, DossierLegislatif.loi_id == Loi.id)
                                .filter(
                                    Amendement.sort == "rejete",
                                    Loi.annee >= start_year,
                                    Loi.annee <= end_year,
                                    or_(
                                        Amendement.objet.ilike(ref_pattern),
                                        Amendement.expose_motifs.ilike(ref_pattern),
                                    ),
                                )
                                .all()
                            )
                            for amend in rejected_amendments:
                                rejected_info.append(
                                    f"Amendement N°{amend.numero} "
                                    f"({amend.auteur or 'inconnu'}): "
                                    f"{(amend.objet or '')[:200]}"
                                )
                    except Exception as e:
                        logger.debug(f"Could not search rejected amendments in consolidation: {e}")

                    doctrine_text = (
                        f"Entre {start_year} et {end_year}, le dispositif n'a pas été modifié. "
                    )
                    if rejected_info:
                        doctrine_text += (
                            f"Cependant, {len(rejected_info)} amendement(s) rejeté(s) mentionnant "
                            f"l'article ont été identifié(s) durant cette période."
                        )
                    else:
                        doctrine_text += (
                            "Aucun débat parlementaire mentionnant l'article n'a été identifié "
                            "dans cette période."
                        )

                    consolidation = ConsolidationPeriod(
                        start_year=start_year,
                        end_year=end_year,
                        title=f"Consolidation du dispositif ({start_year}-{end_year})",
                        doctrine_evolution=doctrine_text,
                        amendements_rejetes=rejected_info[:5],
                        citations=[],
                    )

                consolidation_periods.append(consolidation)

        return consolidation_periods

    def _analyze_consolidation_period(
        self,
        article_info: ArticleInfo,
        start_year: int,
        end_year: int,
        debate_chunks: list,
    ) -> ConsolidationPeriod:
        """Analyze debates during a consolidation period using LLM."""
        # Format chunks for prompt
        max_chars = 20000 if self.settings.llm_provider == "anthropic" else 12000
        debate_text = format_debate_chunks_for_prompt(debate_chunks, max_chars=max_chars)

        # Build prompt
        prompt = CONSOLIDATION_PROMPT.format(
            article_ref=article_info.reference,
            article_code=article_info.code,
            start_year=start_year,
            end_year=end_year,
            debate_chunks=debate_text,
        )

        # Call LLM
        try:
            response_text = self._call_llm(prompt)
            result = self._parse_section_json(response_text)
        except Exception as e:
            logger.error(f"Consolidation analysis failed: {e}")
            return ConsolidationPeriod(
                start_year=start_year,
                end_year=end_year,
                title=f"Consolidation du dispositif ({start_year}-{end_year})",
                doctrine_evolution=(
                    f"Entre {start_year} et {end_year}, le dispositif n'a pas été modifié. "
                    "L'analyse détaillée des débats n'a pas pu être réalisée."
                ),
                amendements_rejetes=[],
                citations=[],
            )

        # Build citations from result
        citations = []
        for c in result.get("citations", []):
            citations.append(
                QuotedSource(
                    citation=c.get("citation", ""),
                    orateur=c.get("orateur", ""),
                    fonction=c.get("fonction", ""),
                    date=c.get("date", ""),
                    chambre=c.get("chambre", ""),
                )
            )

        return ConsolidationPeriod(
            start_year=start_year,
            end_year=end_year,
            title=result.get("titre", f"Consolidation du dispositif ({start_year}-{end_year})"),
            doctrine_evolution=result.get("doctrine_evolution", ""),
            amendements_rejetes=result.get("amendements_rejetes", []),
            citations=citations,
        )

    # Procedural phrases to reject in quotes
    PROCEDURAL_QUOTE_PATTERNS = [
        "l'article est ainsi modifié",
        "l'article est ainsi rédigé",
        "la parole est à",
        "je mets aux voix",
        "l'amendement est adopté",
        "l'amendement est rejeté",
        "il en est ainsi décidé",
        "la séance est levée",
        "la séance est suspendue",
        "je vous remercie",
        "rappel au règlement",
        "avis défavorable",
        "avis favorable",
        "sagesse",
        "avis du gouvernement",
        "avis de la commission",
        "est ainsi modifié",
        "est ainsi rédigé",
        "est complété par",
        "est remplacé par",
        "les mots",
        "sont remplacés par les mots",
        "sanctions, garanties, sûretés",
        "aucune citation pertinente",
    ]

    def _is_procedural_quote(self, citation: str) -> bool:
        """Check if a quote is primarily procedural content."""
        citation_lower = citation.lower()
        for pattern in self.PROCEDURAL_QUOTE_PATTERNS:
            if pattern in citation_lower:
                return True
        # Also reject very short quotes that likely lack substance
        if len(citation) < self.settings.quote_min_length:
            return True
        return False

    def _quote_is_relevant(
        self, citation: str, article_ref: str, article_keywords: list[str],
        min_keywords: int = 2,
    ) -> bool:
        """Check if a quote is about the article, not some unrelated provision."""
        citation_lower = citation.lower()
        # Check for article reference (with variants)
        ref_lower = article_ref.lower()
        ref_variants = [ref_lower, ref_lower.replace("-", " "), ref_lower.replace(" ", "")]
        if any(v in citation_lower for v in ref_variants):
            return True
        # Check for article-specific mechanism keywords
        matches = sum(1 for kw in article_keywords if kw.lower() in citation_lower)
        return matches >= min_keywords

    def _validate_quotes(
        self,
        section: SectionAnalysis,
        source_chunks: list,
        article_keywords: list[str],
        article_ref: str = "",
    ) -> SectionAnalysis:
        """
        Validate that quotes in the section exist in source chunks and are substantive.

        Args:
            section: SectionAnalysis with quotes to validate
            source_chunks: Original chunks from search
            article_keywords: Keywords that should appear in relevant quotes

        Returns:
            SectionAnalysis with invalid quotes removed or flagged
        """
        # Extract text content from chunks
        chunk_texts = []
        for chunk, _ in source_chunks:
            chunk_texts.append(chunk.content.lower())
        combined_text = " ".join(chunk_texts)

        original_an_count = len(section.sources_an)
        original_senat_count = len(section.sources_senat)

        # Validate AN sources
        validated_an = []
        for source in section.sources_an:
            if self._is_procedural_quote(source.citation):
                logger.warning(f"Rejecting procedural quote: {source.citation[:80]}...")
                continue

            if article_ref and not self._quote_is_relevant(
                source.citation, article_ref, article_keywords,
            ):
                logger.warning(f"Rejecting irrelevant quote: {source.citation[:80]}...")
                continue

            if self._quote_matches_sources(source.citation, combined_text):
                validated_an.append(source)
            else:
                logger.warning(f"Quote not found in sources: {source.citation[:100]}...")

        # Validate Sénat sources
        validated_senat = []
        for source in section.sources_senat:
            if self._is_procedural_quote(source.citation):
                logger.warning(f"Rejecting procedural quote: {source.citation[:80]}...")
                continue

            if article_ref and not self._quote_is_relevant(
                source.citation, article_ref, article_keywords,
            ):
                logger.warning(f"Rejecting irrelevant quote: {source.citation[:80]}...")
                continue

            if self._quote_matches_sources(source.citation, combined_text):
                validated_senat.append(source)
            else:
                logger.warning(f"Quote not found in sources: {source.citation[:100]}...")

        # Update section with validated sources
        section.sources_an = validated_an
        section.sources_senat = validated_senat

        # Downgrade confidence if many quotes were removed
        original_count = original_an_count + original_senat_count
        validated_count = len(validated_an) + len(validated_senat)
        if original_count > 0 and validated_count < original_count / 2:
            section.niveau_certitude = CertitudeLevel.FAIBLE

        return section

    def _quote_matches_sources(self, citation: str, combined_text: str) -> bool:
        """Check if a citation matches source text using tiered validation.

        Tier 1: Full exact substring match (strictest)
        Tier 2: Contiguous 8-word sliding window match (catches real quotes with minor truncation)
        Tier 3: Word overlap threshold at 75% (catches paraphrases)
        """
        citation_lower = citation.lower().strip()
        if not citation_lower:
            return False

        # Tier 1: Full exact substring match
        if citation_lower in combined_text:
            return True

        # Tier 2: Contiguous 8-word sliding window match
        citation_words = re.findall(r'\w+', citation_lower)
        window_size = 8
        if len(citation_words) >= window_size:
            for i in range(len(citation_words) - window_size + 1):
                window = " ".join(citation_words[i:i + window_size])
                if window in combined_text:
                    return True

        # Tier 3: Word overlap at 75% threshold
        words = [w for w in citation_words if len(w) > 3]
        if not words:
            return False
        matched = sum(1 for w in words if w in combined_text)
        return matched / len(words) >= self.settings.quote_word_overlap_threshold

    def _build_parliamentary_sources(
        self,
        sections: list[SectionAnalysis],
    ) -> tuple[list[str], list[str]]:
        """
        Build lists of parliamentary sources from analyzed sections.

        Returns:
            Tuple of (AN sources, Sénat sources)
        """
        an_sources = set()
        senat_sources = set()

        for section in sections:
            mod = section.modification

            # Add AN sources
            for source in section.sources_an:
                if source.date:
                    an_sources.add(
                        f"Débats AN {source.date} ({mod.law_type} {mod.law_year})"
                    )

            # Add Sénat sources
            for source in section.sources_senat:
                if source.date:
                    senat_sources.add(
                        f"Débats Sénat {source.date} ({mod.law_type} {mod.law_year})"
                    )

        return sorted(list(an_sources)), sorted(list(senat_sources))

    def _get_forward_looking(
        self,
        article_info: ArticleInfo,
    ) -> list[ForwardLookingSection]:
        """
        Get forward-looking analysis from predefined data.

        Filters out citations and propositions that don't mention the article
        or its keywords, to prevent cross-contamination from unrelated provisions.

        Returns:
            List of ForwardLookingSection for PLF discussions
        """
        forward_data = self.article_index.get_forward_looking(article_info.reference)

        if not forward_data:
            return []

        # Build relevance check data
        ref_lower = article_info.reference.lower()
        ref_variants = [ref_lower, ref_lower.replace("-", " "), ref_lower.replace(" ", "")]
        keywords = article_info.keywords or []

        def _text_is_relevant(text: str) -> bool:
            """Check if text mentions the article ref or at least 2 keywords."""
            text_lower = text.lower()
            if any(v in text_lower for v in ref_variants):
                return True
            kw_matches = sum(1 for kw in keywords if kw.lower() in text_lower)
            return kw_matches >= 2

        sections = []
        for item in forward_data:
            # Filter citations by relevance
            citations = []
            for c in item.get("citations", []):
                citation_text = c.get("citation", "")
                if not citation_text:
                    continue
                if not _text_is_relevant(citation_text):
                    logger.warning(
                        f"Rejecting irrelevant forward-looking citation: "
                        f"{citation_text[:80]}..."
                    )
                    continue
                citations.append(
                    QuotedSource(
                        citation=citation_text,
                        orateur=c.get("orateur", ""),
                        fonction=c.get("fonction", ""),
                        date=c.get("date", ""),
                        chambre=c.get("chambre", ""),
                    )
                )

            # Filter propositions by relevance
            raw_propositions = item.get("propositions", [])
            propositions = [p for p in raw_propositions if _text_is_relevant(p)]
            if not propositions and not citations:
                # Nothing relevant survived filtering — skip or add placeholder
                logger.info(
                    f"Forward-looking PLF {item.get('plf_year')}: "
                    f"no relevant propositions/citations for {article_info.reference}"
                )
                # Still include the section but with empty content and low confidence
                propositions = [
                    f"Aucun extrait explicite sur l'article {article_info.reference} "
                    f"n'a ete identifie dans les debats PLF {item.get('plf_year', '')} "
                    f"indexes localement."
                ]
                niveau = CertitudeLevel.FAIBLE
            else:
                # Parse certitude level
                niveau = CertitudeLevel.MOYEN
                niveau_str = item.get("niveau_certitude", "moyen").lower()
                if "élevé" in niveau_str or "eleve" in niveau_str:
                    niveau = CertitudeLevel.ELEVE
                elif "faible" in niveau_str:
                    niveau = CertitudeLevel.FAIBLE

            sections.append(
                ForwardLookingSection(
                    plf_year=item.get("plf_year", 0),
                    status=item.get("status", "en cours"),
                    propositions=propositions,
                    citations=citations,
                    niveau_certitude=niveau,
                )
            )

        return sections

    def _get_methodology_references(
        self,
        article_info: ArticleInfo,
    ) -> tuple[list[dict], list[str]]:
        """
        Get enhanced methodology references from predefined data.

        Returns:
            Tuple of (rapports_parlementaires, dila_sources)
        """
        rapports = self.article_index.get_rapports_parlementaires(article_info.reference)
        dila = self.article_index.get_dila_sources(article_info.reference)
        return rapports, dila

    def _categorize_expose(self, expose_text: str) -> str:
        """Categorize expose sections with structural markers.

        Distinguishes between 'Exposé des motifs du PLF' and 'Rapport de la commission'
        based on content patterns, and adds clear section markers.
        """
        sections = expose_text.split("\n\n---\n\n")
        formatted_parts = []

        for section in sections:
            section_lower = section.lower()
            # Detect section type from content
            if any(
                kw in section_lower
                for kw in ("exposé des motifs", "projet de loi", "le gouvernement propose")
            ):
                label = "### Exposé des motifs du PLF"
            elif any(
                kw in section_lower
                for kw in ("rapport", "commission des finances", "rapporteur général")
            ):
                label = "### Rapport de la commission"
            elif any(
                kw in section_lower
                for kw in ("sénat", "commission des finances du sénat")
            ):
                label = "### Rapport parlementaire (Sénat)"
            else:
                label = "### Source parlementaire"

            formatted_parts.append(f"{label}\n{section}")

        return "\n\n".join(formatted_parts)

    def _fetch_expose_motifs(
        self,
        article_ref: str,
        law_numero: str,
        max_chars: int = 3000,
    ) -> str:
        """
        Fetch exposé des motifs / rapport content from SourceExterne for a law.

        Queries for rapports linked to the law or article, and returns their
        content (or resume) formatted for inclusion in the LLM prompt.
        """
        try:
            with get_session() as session:
                sources = (
                    session.query(SourceExterne)
                    .filter(SourceExterne.type == "rapport")
                    .filter(
                        or_(
                            SourceExterne.lois_liees.cast(String).contains(law_numero),
                            SourceExterne.articles_cgi_lies.cast(String).contains(article_ref),
                        )
                    )
                    .limit(50)
                    .all()
                )

                parts = []
                current_chars = 0
                for src in sources:

                    text = src.contenu or src.resume
                    if not text:
                        continue

                    # Filter to paragraphs mentioning the CGI article
                    article_patterns = [
                        article_ref.lower(),
                        article_ref.lower().replace("-", " "),
                        f"article {article_ref}".lower(),
                    ]
                    paragraphs = text.split("\n\n")
                    relevant = [p for p in paragraphs if any(pat in p.lower() for pat in article_patterns)]
                    if relevant:
                        text = "\n\n".join(relevant)
                    elif len(text) > 500:
                        text = text[:500] + "..."

                    header = f"[{src.titre}]" if src.titre else ""
                    entry = f"{header}\n{text}" if header else text

                    if current_chars + len(entry) > max_chars:
                        break
                    parts.append(entry)
                    current_chars += len(entry)

                return "\n\n---\n\n".join(parts) if parts else ""
        except Exception as e:
            logger.debug(f"Could not fetch exposé des motifs: {e}")
            return ""

    def _fetch_linked_amendments(
        self,
        mod: LawModification,
        article_ref: str = "",
        article_keywords: list[str] | None = None,
        max_amendments: int = 20,
    ) -> list[tuple[Chunk, float]]:
        """
        Fetch amendments directly linked to the article in this law via DB foreign key.

        Supplements vector search results to guarantee recall of all
        amendments that were formally linked to the article during import.
        Prioritizes adopted amendments, then rejected (useful for points de vigilance).
        """
        try:
            with get_session() as session:
                # Extract article number from article_in_law (e.g., "Art. 24" -> "24")
                article_num = re.sub(r"^Art\.?\s*", "", mod.article_in_law).strip()
                if not article_num:
                    return []

                amendments = (
                    session.query(Amendement)
                    .join(DossierLegislatif, Amendement.dossier_id == DossierLegislatif.id)
                    .join(Loi, DossierLegislatif.loi_id == Loi.id)
                    .filter(Loi.numero == mod.law_numero)
                    .filter(Amendement.article_id != None)  # noqa: E711 - SQLAlchemy IS NOT NULL
                    .join(Article, Amendement.article_id == Article.id)
                    .filter(Article.numero_article == article_num)
                    .filter(Amendement.expose_motifs != None, Amendement.expose_motifs != "")  # noqa: E711
                    .all()
                )

                # Sort: adopted first, then rejected, then others
                sort_priority = {"adopte": 0, "rejete": 1, "retire": 2, "tombe": 3}
                amendments.sort(key=lambda a: sort_priority.get(a.sort or "", 4))
                amendments = amendments[:max_amendments]

                # Filter amendments by CGI article relevance
                if article_ref and article_keywords:
                    ref_lower = article_ref.lower()
                    ref_variants = [ref_lower, ref_lower.replace("-", " ")]

                    def _amendment_is_relevant(amend):
                        text = ((amend.objet or "") + " " + (amend.expose_motifs or "")).lower()
                        if any(v in text for v in ref_variants):
                            return True
                        kw_matches = sum(1 for kw in article_keywords if kw.lower() in text)
                        return kw_matches >= 2

                    before_count = len(amendments)
                    amendments = [a for a in amendments if _amendment_is_relevant(a)]
                    if before_count != len(amendments):
                        logger.info(f"  Filtered {before_count - len(amendments)} irrelevant linked amendments")

                results = []
                for amend in amendments:
                    chambre = amend.dossier.chambre if amend.dossier else "AN"
                    chunk = chunk_amendment(
                        numero=amend.numero,
                        auteur=amend.auteur,
                        objet=amend.objet,
                        contenu=amend.contenu,
                        expose_motifs=amend.expose_motifs,
                        sort=amend.sort,
                        article_ref=article_num,
                        chambre=chambre,
                        dossier_ref=str(amend.dossier_id) if amend.dossier_id else None,
                        date=amend.date_depot.isoformat() if amend.date_depot else None,
                    )
                    # Score 1.0: directly linked via DB foreign key
                    results.append((chunk, 1.0))

                logger.info(f"  Found {len(results)} directly linked amendments for {mod.short_ref} art. {article_num}")
                return results
        except Exception as e:
            logger.debug(f"Could not fetch linked amendments: {e}")
            return []

    def _get_curated_quotes(
        self,
        article_ref: str,
        year: int,
    ) -> list[QuotedSource]:
        """
        Get curated quotes for a specific modification year.

        Returns:
            List of QuotedSource from predefined data
        """
        curated = self.article_index.get_curated_quotes(article_ref, str(year))

        if not curated:
            return []

        sources = []
        for q in curated:
            sources.append(
                QuotedSource(
                    citation=q.get("citation", ""),
                    orateur=q.get("orateur", ""),
                    fonction=q.get("fonction", ""),
                    date=q.get("date", ""),
                    chambre=q.get("chambre", ""),
                    is_curated=True,
                )
            )
        return sources
