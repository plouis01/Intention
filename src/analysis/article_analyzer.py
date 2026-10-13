"""Article analyzer service for generating comprehensive analysis documents."""

import dataclasses
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from itertools import groupby

from sqlalchemy import String, and_, func, or_

from src.analysis.amendment_tracker import AmendmentTracker
from src.analysis.article_index import ArticleIndex, get_article_index
from src.analysis.cross_law_analyzer import CrossLawAnalyzer
from src.analysis.models import (
    ArticleAnalysisResult,
    ArticleInfo,
    CertitudeLevel,
    ConsolidationPeriod,
    LawModification,
    OperationalParameters,
    QuotedSource,
    SectionAnalysis,
    SynthesisRow,
    TokenTracker,
    TokenUsage,
)
from src.analysis.prompts import (
    CONSOLIDATION_PROMPT,
    SECTION_EXTRACTION_PROMPT,
    SYSTEM_PROMPT_LEGAL,
    SYSTEM_PROMPT_LEGAL_SHORT,
    format_amendment_chunks_for_prompt,
    format_debate_chunks_for_prompt,
)
from src.db.models import (
    Amendement,
    Article,
    Debat,
    DossierLegislatif,
    Intervention,
    Loi,
    SourceExterne,
    get_session,
)
from src.indexing.chunking import Chunk, chunk_amendment
from src.indexing.search import (
    SearchService,
    _build_contextualized_patterns,
    _is_short_article_ref,
    _keyword_matches,
)

# Keywords containing an article-ref pattern (e.g. "article 150-0 B ter",
# "l'article 39 C bis", "2° du iii de l'article …") are dropped from
# scoreable_kw: self-refs are redundant with the explicit-ref branch of
# topical cleanup; cross-refs would falsely promote chunks discussing
# OTHER CGI articles. Stricter than the previous `startswith` check
# (cf. data audit: 99.8% of articles have at least one such kw entry).
_ARTICLE_REF_IN_KW_RE = re.compile(
    r"\b(?:l['’]?\s*article|articles?|art\.)\s+\d",
    re.IGNORECASE,
)
from src.utils.config import get_settings
from src.utils.groq_pool import GroqClientPool, get_groq_pool

logger = logging.getLogger(__name__)


def _truncate_at_word(text: str, max_length: int) -> str:
    """Truncate text at a word boundary, never mid-word. Adds ellipsis."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_length:
        return text
    truncated = text[:max_length]
    last_space = truncated.rfind(' ')
    if last_space > max_length * 0.7:
        return truncated[:last_space].rstrip('.,;: ') + '…'
    return truncated.rstrip('.,;: ') + '…'


def _dedup_sources_list(sources: list[QuotedSource]) -> list[QuotedSource]:
    """Remove duplicate sources within a list by (citation[:80], orateur)."""
    seen: set[tuple[str, str]] = set()
    result = []
    for s in sources:
        key = (s.citation[:80].lower().strip(), s.orateur.lower().strip())
        if key not in seen:
            seen.add(key)
            result.append(s)
    return result


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
        self.token_tracker = TokenTracker()
        self._system_prompt_formatted: str | None = None
        self._last_anthropic_call: float = 0.0

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
        max_modifications: int | None = None,
        include_consolidation_periods: bool = True,
    ) -> ArticleAnalysisResult:
        """
        Generate full analysis for an article.

        Args:
            article_ref: Article reference (e.g., "150-0 B ter")
            question: The question to answer
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
        skipped_sections = []
        modifications = article_info.modifications

        # Era cutoff: if the article identifier has been (re-)created at some
        # point in its history, any modification BEFORE that latest "Création"
        # refers to a DIFFERENT article that previously used the same number
        # (e.g., CGI 973 was "Droit de timbre / territoires d'outre-mer" until
        # the LF 2018 — n°2017-1837 — abrogated it and re-created the
        # identifier for the new IFI regime). Feeding pre-Création entries
        # to the LLM produces structural false positives (the LLM treats
        # them as if they were the current article).
        # No-op for articles without a "Création" event (continuous history,
        # e.g., 39 C since the 1960s) and for articles whose Création is
        # already the earliest entry (e.g., 150-0 B ter, created in 2012).
        _creation_dates = [
            m.date for m in modifications
            if m.nature.value == "Création"
        ]
        if _creation_dates:
            era_cutoff = max(_creation_dates)
            pre_creation = [m for m in modifications if m.date < era_cutoff]
            if pre_creation:
                logger.warning(
                    f"[era_cutoff] {article_ref}: dropping {len(pre_creation)} "
                    f"pre-Création modifications (cutoff={era_cutoff}). "
                    f"These refer to a prior article re-using the same identifier."
                )
                modifications = [m for m in modifications if m.date >= era_cutoff]

        if max_modifications:
            modifications = modifications[:max_modifications]

        # Separate modifications into analyzable vs skippable
        _NO_DEBATE_TYPES = {"DECRET", "ORDONNANCE", "ARRETE", "LOI_ORGANIQUE"}
        _TECHNICAL_NATURES = {"Codification", "Transfert"}
        analyzable_mods = []
        for mod in modifications:
            if mod.law_type in _NO_DEBATE_TYPES or mod.nature.value in _TECHNICAL_NATURES:
                skipped_sections.append(self._create_skipped_section(
                    mod, f"{mod.law_type} - pas de débat parlementaire."
                ))
            elif mod.law_type == "INCONNU":
                skipped_sections.append(self._create_skipped_section(
                    mod, "Texte modificateur non identifié - recherche impossible."
                ))
            else:
                analyzable_mods.append(mod)

        if skipped_sections:
            logger.info(
                f"Skipped {len(skipped_sections)} modifications "
                f"(no parliamentary debates): "
                f"{len(modifications) - len(analyzable_mods)} of {len(modifications)}"
            )

        total_debates = 0
        total_interventions = 0

        # Collect debate chunks per law for cross-law detection (avoids re-searching)
        debate_chunks_by_law: dict[str, list] = {}
        sections_by_law: dict[str, dict] = {}

        # Pre-compute embedding once for the article (avoids N calls to Vertex AI)
        core_kw = article_info.keywords[:5] if len(article_info.keywords) > 5 else article_info.keywords
        article_query_embedding = None
        if self.search_service and hasattr(self.search_service, 'embedding_service'):
            try:
                kw_str = ', '.join(core_kw)
                texte_extrait = (article_info.texte_actuel or "")[:200]
                article_query_embedding = self.search_service.embedding_service.generate_embedding(
                    f"Intention du législateur pour l'article {article_info.reference} du CGI "
                    f"({kw_str}). {texte_extrait} "
                    f"Objectifs, justifications et débats parlementaires."
                )
                logger.info(f"Pre-computed article embedding ({len(article_query_embedding)}d)")
            except Exception as e:
                logger.debug(f"Could not pre-compute embedding: {e}")

        # Cache dossier resolution per law_numero
        _dossier_cache: dict[str, dict] = {}

        use_bicameral = self.settings.bicameral_search_enabled

        # Sort by DATE (chronological), then group by law_numero
        sorted_mods = sorted(analyzable_mods, key=lambda m: m.date)
        grouped_mods = []
        for key, group in groupby(sorted_mods, key=lambda m: m.law_numero):
            grouped_mods.append(list(group))

        # Merge groups that are temporally close (±60 days), same article_in_law,
        # AND same law_type. LF + LFR are distinct texts - only merge true duplicates.
        merged_groups: list[list] = []
        for group in grouped_mods:
            if merged_groups:
                prev = merged_groups[-1]
                prev_date = prev[0].date
                curr_date = group[0].date
                if (abs((curr_date - prev_date).days) <= 60
                        and prev[0].article_in_law == group[0].article_in_law
                        and prev[0].law_type == group[0].law_type):
                    merged_groups[-1] = prev + group
                    continue
            merged_groups.append(group)
        grouped_mods = merged_groups

        # --- Pipeline Stage 1: Search all sections first (fast, I/O bound) ---
        import time as _time
        _stage1_start = _time.monotonic()
        search_results_by_mod = []
        for mod_group in grouped_mods:
            primary_mod = mod_group[0]
            _mod_start = _time.monotonic()
            multi_stage = self.settings.multi_stage_retrieval
            search_top_k = 25 if multi_stage else 40

            if use_bicameral:
                # --- BICAMERAL PIPELINE ---
                # Phase 0: Resolve dossiers per chamber
                if primary_mod.law_numero not in _dossier_cache:
                    _dossier_cache[primary_mod.law_numero] = self._resolve_dossiers_for_modification(primary_mod.law_numero)
                dossiers_info = _dossier_cache[primary_mod.law_numero]

                # Phase 1+2: Search per chamber
                from src.indexing.search import SearchService as _SS
                an_info = dossiers_info.get("AN", {})
                senat_info = dossiers_info.get("SENAT", {})

                # Give each chamber the full top_k budget - merge_bicameral_results
                # will cut to total. This avoids starving the Sénat (which typically
                # has more HP) with a fixed top_k // 2.
                an_results = self.search_service.search_chamber(
                    debate_ids=an_info.get("debate_ids", []),
                    article_ref=article_info.reference,
                    article_keywords=article_info.keywords,
                    article_in_law=primary_mod.article_in_law,
                    chambre="AN",
                    law_type=primary_mod.law_type,
                    law_year=primary_mod.law_year,
                    date_start=an_info.get("date_start"),
                    date_end=an_info.get("date_end"),
                    top_k=search_top_k,
                    query_embedding=article_query_embedding,
                    modification_objet=primary_mod.objet or "",
                    mod_date=primary_mod.date,
                )

                senat_results = self.search_service.search_chamber(
                    debate_ids=senat_info.get("debate_ids", []),
                    article_ref=article_info.reference,
                    article_keywords=article_info.keywords,
                    article_in_law=primary_mod.article_in_law,
                    chambre="SENAT",
                    law_type=primary_mod.law_type,
                    law_year=primary_mod.law_year,
                    date_start=senat_info.get("date_start"),
                    date_end=senat_info.get("date_end"),
                    top_k=search_top_k,
                    query_embedding=article_query_embedding,
                    modification_objet=primary_mod.objet or "",
                    mod_date=primary_mod.date,
                )

                # Phase 3: Bicameral merge
                merged, is_bicameral = _SS.merge_bicameral_results(
                    an_results, senat_results,
                    top_k=search_top_k,
                    quality_threshold=self.settings.bicameral_quality_threshold,
                )
                debate_chunks = merged.get("debates", [])
                amendment_chunks = merged.get("amendments", [])

                _t_search = _time.monotonic() - _mod_start
            else:
                # --- LEGACY PIPELINE (feature flag off) ---
                all_mod_dates = [(m.date, m.law_type) for m in modifications]
                date_start = primary_mod.date - timedelta(days=450)
                date_end = primary_mod.date + timedelta(days=30)
                min_sim_override = self.settings.multi_stage_min_similarity if multi_stage else None

                law_debate_refs = self._get_debate_refs_for_law(primary_mod.law_numero)
                search_results = self.search_service.search_article_modification(
                    article_ref=article_info.reference,
                    article_keywords=article_info.keywords,
                    law_type=primary_mod.law_type,
                    law_year=primary_mod.law_year,
                    date_start=date_start,
                    date_end=date_end,
                    top_k=search_top_k,
                    min_relevant=5,
                    min_similarity_override=min_sim_override,
                    article_in_law=primary_mod.article_in_law,
                    all_modification_dates=all_mod_dates,
                    current_mod_date=primary_mod.date,
                    law_debate_refs=law_debate_refs,
                    law_numero=primary_mod.law_numero,
                    query_embedding=article_query_embedding,
                )
                _t_search = _time.monotonic() - _mod_start
                debate_chunks = search_results.get("debates", [])
                amendment_chunks = search_results.get("amendments", [])

            # Supplement with directly linked amendments
            _t0 = _time.monotonic()
            linked_amends = self._fetch_linked_amendments(primary_mod, article_info.reference)
            if linked_amends:
                existing = {c.content[:100] for c, _ in amendment_chunks}
                for chunk, score in linked_amends:
                    if chunk.content[:100] not in existing:
                        amendment_chunks.append((chunk, score))
                        existing.add(chunk.content[:100])

            # Multi-stage relevance filter (optional)
            if multi_stage and (debate_chunks or amendment_chunks):
                debate_chunks = self._llm_relevance_filter(
                    debate_chunks, article_info.reference, primary_mod.law_type, primary_mod.law_year
                )
                amendment_chunks = self._llm_relevance_filter(
                    amendment_chunks, article_info.reference, primary_mod.law_type, primary_mod.law_year
                )
            _t_linked = _time.monotonic() - _t0

            # Fetch exposé des motifs
            _t0 = _time.monotonic()
            expose_max = 10000 if self.settings.llm_provider == "anthropic" else 5000
            expose_text = self._fetch_expose_motifs(
                article_ref=article_info.reference,
                law_numero=primary_mod.law_numero,
                max_chars=expose_max,
            )

            # Fetch amendment exposés
            try:
                with get_session() as db:
                    loi_obj = db.query(Loi).filter(Loi.numero == primary_mod.law_numero).first()
                    if loi_obj:
                        dossier_objs = db.query(DossierLegislatif).filter(
                            DossierLegislatif.loi_id == loi_obj.id
                        ).all()
                        dossier_id_list = [d.id for d in dossier_objs]
                        if dossier_id_list:
                            amend_exposes = db.query(Amendement).filter(
                                Amendement.dossier_id.in_(dossier_id_list),
                                Amendement.expose_motifs.isnot(None),
                                Amendement.expose_motifs != '',
                                or_(
                                    Amendement.expose_motifs.ilike(f'%{article_info.reference}%'),
                                    Amendement.objet.ilike(f'%{article_info.reference}%'),
                                ),
                            ).limit(5).all()
                            if not amend_exposes and article_info.keywords:
                                kw_filters = [
                                    Amendement.expose_motifs.ilike(f'%{kw}%')
                                    for kw in article_info.keywords[:2] if len(kw) > 5
                                ]
                                if kw_filters:
                                    amend_exposes = db.query(Amendement).filter(
                                        Amendement.dossier_id.in_(dossier_id_list),
                                        Amendement.expose_motifs.isnot(None),
                                        Amendement.expose_motifs != '',
                                        and_(*kw_filters),
                                    ).limit(5).all()
                            if amend_exposes:
                                parts = []
                                for a in amend_exposes:
                                    dos = db.get(DossierLegislatif, a.dossier_id)
                                    ch = dos.chambre if dos else '?'
                                    parts.append(
                                        f"[{ch}] Amendement N°{a.numero} ({(a.auteur or '?')[:30]}) "
                                        f"[{a.sort}]: {(a.expose_motifs or '')[:500]}"
                                    )
                                expose_amend = "\n".join(parts)
                                expose_text = (
                                    f"{expose_text}\n\n## Exposés des motifs des amendements\n{expose_amend}"
                                    if expose_text else
                                    f"## Exposés des motifs des amendements\n{expose_amend}"
                                )
            except Exception:
                pass
            _t_expose = _time.monotonic() - _t0

            # Filter chunks discussing a DIFFERENT CGI article without mentioning ours.
            # Three tiers: strict (current behaviour), family-aware fallback (only
            # fires if strict empties everything), unfiltered last resort.
            _article_cgi_re = re.compile(
                r"article\s+(\d+[\w\s\-]*(?:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies)?)",
                re.IGNORECASE,
            )
            _leading_num_re = re.compile(
                r"\s*(\d+)(?:\s*-?\s*(bis|ter|quater|quinquies|sexies|septies|octies|nonies|"
                r"decies|undecies|duodecies|terdecies|quaterdecies|quindecies|sexdecies))?"
                r"(?:\s+([A-Z])(?!\w))?(?:\s+(bis|ter|quater))?",
                re.IGNORECASE,
            )
            _target_norm = article_info.reference.lower().replace(" ", "").replace("-", "")

            def _norm_ref(s: str) -> str:
                return s.strip().lower().replace(" ", "").replace("-", "")

            def _matches_ref(cited: str, ref: str) -> bool:
                if not ref:
                    return False
                if cited == ref:
                    return True
                if cited.startswith(ref):
                    rest = cited[len(ref):]
                    if rest and rest[0].isalpha():
                        return True
                return False

            def _chunk_cites_other(content, target_ref):
                # Tier 1 — unchanged behaviour (kept verbatim for non-regression)
                found = _article_cgi_re.findall(content)
                target_norm = target_ref.lower().replace(" ", "").replace("-", "")
                for a in found:
                    a_norm = a.strip().lower().replace(" ", "").replace("-", "")
                    if len(a_norm) > 2 and target_norm not in a_norm:
                        return True
                return False

            def _chunk_passes_relaxed(content):
                """Tier 2 — accept chunks that:
                   - mention the target CGI ref, or
                   - cite no other article, or
                   - cite the LF article that modified our CGI (primary_mod.article_in_law), or
                   - cite only articles in our cross-ref family.
                Rejects only chunks citing unrelated articles."""
                if _target_norm and _target_norm in content.lower().replace(" ", "").replace("-", ""):
                    return True
                cited_prefixes = set()
                for raw in _article_cgi_re.findall(content):
                    m = _leading_num_re.match(raw)
                    if not m:
                        continue
                    prefix = _norm_ref("".join(g for g in m.groups() if g))
                    if prefix and len(prefix) >= 2:
                        cited_prefixes.add(prefix)
                if not cited_prefixes:
                    return True
                # LF article (the article-in-law that modified our CGI article)
                lf_norm = ""
                if primary_mod.article_in_law:
                    lf_raw = re.sub(r"^Art\.?\s*", "", primary_mod.article_in_law).strip()
                    lf_norm = _norm_ref(lf_raw)
                if lf_norm and any(_matches_ref(c, lf_norm) for c in cited_prefixes):
                    return True
                # Cross-ref family
                family_norms = {_target_norm} | {
                    _norm_ref(r) for r in (article_info.references_croisees or [])
                }
                if all(any(_matches_ref(c, fam) for fam in family_norms) for c in cited_prefixes):
                    return True
                return False

            before_filter = len(debate_chunks)
            # [DIAG:FILTER] Entry: log target and input count.
            logger.info(
                f"[DIAG:FILTER] enter art={article_info.reference!r} "
                f"mod_short_ref={primary_mod.short_ref!r} input_chunks={before_filter}"
            )
            filtered_strict = []
            # [DIAG:FILTER] Track per-chunk decision for reject diagnosis.
            _diag_rejected_strict = []
            for chunk, score in debate_chunks:
                _matched_via = None
                if article_info.reference.lower() in chunk.content.lower():
                    filtered_strict.append((chunk, score))
                    _matched_via = "substring"
                elif not _chunk_cites_other(chunk.content, article_info.reference):
                    filtered_strict.append((chunk, score))
                    _matched_via = "no_other_cgi"
                else:
                    _diag_rejected_strict.append(chunk)
                # No-op if matched; only collect rejects for sample
                _ = _matched_via

            # [DIAG:FILTER] tier-1 outcome + sample reject reasons.
            if _diag_rejected_strict:
                _sample = [c.content[:120].replace("\n", " ") for c in _diag_rejected_strict[:3]]
                logger.info(
                    f"[DIAG:FILTER] tier1_strict input={before_filter} "
                    f"kept={len(filtered_strict)} rejected={len(_diag_rejected_strict)} "
                    f"sample_rejected={_sample}"
                )
            else:
                logger.info(
                    f"[DIAG:FILTER] tier1_strict input={before_filter} "
                    f"kept={len(filtered_strict)} rejected=0"
                )

            if filtered_strict:
                debate_chunks = filtered_strict
                if before_filter != len(debate_chunks):
                    logger.info(
                        f"  Article filter [strict]: {before_filter} → {len(debate_chunks)} "
                        f"debate chunks (removed {before_filter - len(debate_chunks)})"
                    )
            elif debate_chunks:
                filtered_relaxed = [
                    (c, s) for c, s in debate_chunks if _chunk_passes_relaxed(c.content)
                ]
                # [DIAG:FILTER] tier-2 outcome.
                logger.info(
                    f"[DIAG:FILTER] tier2_family_aware input={before_filter} "
                    f"kept={len(filtered_relaxed)} target={article_info.reference!r}"
                )
                if filtered_relaxed:
                    logger.warning(
                        f"  Article filter [Tier 2 family-aware]: strict killed all "
                        f"{before_filter} chunks; relaxed kept {len(filtered_relaxed)} "
                        f"for {article_info.reference} / {primary_mod.short_ref}"
                    )
                    debate_chunks = filtered_relaxed
                else:
                    # [DIAG:FILTER] tier-3 fired (no chunks pass any tier).
                    logger.warning(
                        f"[DIAG:FILTER] tier3_last_resort input={before_filter} "
                        f"target={article_info.reference!r} reason=both_filters_empty"
                    )
                    logger.warning(
                        f"  Article filter [Tier 3 last-resort]: both filters dropped all "
                        f"{before_filter} chunks for {article_info.reference} / "
                        f"{primary_mod.short_ref}; keeping unfiltered to preserve recall"
                    )
                    # debate_chunks unchanged
            else:
                # [DIAG:FILTER] strict empty AND original was empty (no chunks at all).
                logger.info(
                    f"[DIAG:FILTER] empty_input target={article_info.reference!r} "
                    f"mod={primary_mod.short_ref!r} (no chunks reached the filter)"
                )

            _mod_total = _time.monotonic() - _mod_start
            logger.info(
                f"  [Search done] {primary_mod.short_ref}: {len(debate_chunks)} debates, "
                f"{len(amendment_chunks)} amends | "
                f"search={_t_search:.1f}s linked={_t_linked:.1f}s expose={_t_expose:.1f}s "
                f"total={_mod_total:.1f}s"
            )

            # Fetch relevant jurisprudence
            jurisprudence_text = self._fetch_relevant_jurisprudence(
                article_ref=article_info.reference,
                mod_date=primary_mod.date,
            )
            if jurisprudence_text:
                logger.info(f"  Found relevant jurisprudence ({len(jurisprudence_text)} chars)")

            search_results_by_mod.append((mod_group, debate_chunks, amendment_chunks, expose_text, jurisprudence_text))

        _stage1_total = _time.monotonic() - _stage1_start
        logger.info(f"Pipeline Stage 1 complete: {len(search_results_by_mod)} modifications in {_stage1_total:.1f}s")

        # --- Pipeline Stage 2: LLM extraction for all sections (slow, API bound) ---
        _stage2_start = _time.monotonic()
        for mod_group, debate_chunks, amendment_chunks, expose_text, jurisprudence_text in search_results_by_mod:
            primary_mod = mod_group[0]
            _llm_start = _time.monotonic()

            # When multiple modifications in same law, enrich the objet with all volets
            # so the LLM covers all aspects (not just primary_mod's objet)
            if len(mod_group) > 1:
                all_objets = [m.objet for m in mod_group if m.objet and m.objet.strip()]
                if len(all_objets) > 1:
                    combined_objet = "\n".join(
                        f"Volet {j+1}: {o[:200]}" for j, o in enumerate(all_objets)
                    )
                    primary_mod = dataclasses.replace(primary_mod, objet=combined_objet)

            if debate_chunks or amendment_chunks:
                section = self._extract_section_with_llm(
                    article_info, primary_mod, debate_chunks, amendment_chunks, expose_text,
                    jurisprudence_text=jurisprudence_text,
                )
            else:
                section = self._create_empty_section(primary_mod)

            _llm_time = _time.monotonic() - _llm_start
            logger.info(f"  [LLM done] {primary_mod.short_ref}: {_llm_time:.1f}s")

            section.debates_analyzed = len(debate_chunks)
            section.amendments_analyzed = len(amendment_chunks)

            raw_debate_chunks = debate_chunks
            debates_count = len(debate_chunks)
            interventions_count = len(amendment_chunks)

            # Collect chunks for cross-law reuse
            law_key = f"{primary_mod.law_type}_{primary_mod.law_year}"
            debate_chunks_by_law[law_key] = raw_debate_chunks
            sections_by_law[law_key] = {
                "law_type": primary_mod.law_type,
                "law_year": primary_mod.law_year,
                "law_numero": primary_mod.law_numero,
            }
            # Record all articles in the group
            if len(mod_group) > 1:
                all_articles = ", ".join(dict.fromkeys(m.article_in_law for m in mod_group))
                section.modification = dataclasses.replace(
                    primary_mod, article_in_law=all_articles
                )
            sections.append(section)
            total_debates += debates_count
            total_interventions += interventions_count

        _stage2_total = _time.monotonic() - _stage2_start
        logger.info(f"Pipeline Stage 2 complete: {len(sections)} sections in {_stage2_total:.1f}s")

        # Stage 4.5: Deduplicate citations across modifications by date proximity
        sections = self._dedup_citations_by_date_proximity(sections)

        # Stage 4.6: Recalculate certitude after dedup (dedup may remove all sources)
        for section in sections:
            if not section.sources_an and not section.sources_senat:
                section.niveau_certitude = CertitudeLevel.FAIBLE
                if section.intention and not section.intention.startswith("Modification"):
                    first_line = section.intention.split("\n")[0]
                    section.intention = (
                        f"Modification technique. {first_line}\n"
                        "Aucun débat parlementaire de fond identifié dans les travaux parlementaires."
                    )
            # Certitude adjustment based on actual source count and chambers
            actual_src = len(section.sources_an) + len(section.sources_senat)
            actual_ch = (1 if section.sources_an else 0) + (1 if section.sources_senat else 0)
            # Downgrade ÉLEVÉ if insufficient evidence
            if section.niveau_certitude == CertitudeLevel.ELEVE:
                if actual_src < 2 or actual_ch < 2:
                    section.niveau_certitude = CertitudeLevel.MOYEN
            # Promote to ÉLEVÉ if 3+ sources from 2 chambers
            elif section.niveau_certitude == CertitudeLevel.MOYEN:
                if actual_src >= 3 and actual_ch >= 2:
                    section.niveau_certitude = CertitudeLevel.ELEVE
            # Downgrade MOYEN → FAIBLE if only 1 source from 1 chamber
            # (insufficient evidence for medium confidence)
            if section.niveau_certitude == CertitudeLevel.MOYEN:
                if actual_src <= 1 and actual_ch <= 1:
                    section.niveau_certitude = CertitudeLevel.FAIBLE

        # Stage 4.6b: Clear operational params for minor modifications
        # (LLM extracts params from existing text, not from changes)
        # Only clear if the objet indicates purely cosmetic changes (no substantive verbs)
        _substantive_verbs = {"ajout", "suppression", "remplacement", "création", "modification du"}
        for section in sections:
            objet = (section.modification.objet or "").lower()
            is_minor = "modification mineure" in objet
            is_mise_en_forme = objet == "modification mineure (mise en forme)"
            has_substance = any(v in objet for v in _substantive_verbs)
            if (is_minor or is_mise_en_forme) and not has_substance:
                section.operational_params = None

        # Stage 4.6c: Dedup operational params identical to previous section
        prev_params = None
        for section in sections:
            if section.operational_params and section.operational_params.parameters:
                curr = section.operational_params.parameters
                if prev_params:
                    # Count overlap
                    common = sum(1 for k in curr if k in prev_params and curr[k] == prev_params[k])
                    if len(curr) > 0 and common / len(curr) > 0.8:
                        logger.info(f"[params_dedup] Clearing params for {section.modification.short_ref} "
                                   f"({common}/{len(curr)} identical to previous)")
                        section.operational_params = None
                    else:
                        prev_params = dict(curr)
                else:
                    prev_params = dict(curr)

        # Stage 4.7: Deduplicate amendments across sections by numero
        # Normalize roman prefixes (I-94 == 94) for dedup
        _roman_re = re.compile(r'^[IVX]+-\s*', re.IGNORECASE)
        seen_amendment_numeros: set[str] = set()
        for section in sections:
            unique_amends = []
            for amend in section.amendements_rejetes_detailles:
                numero = amend.get('numero', '')
                numero_norm = _roman_re.sub('', numero).strip() if numero else ''
                if numero_norm and numero_norm not in seen_amendment_numeros:
                    seen_amendment_numeros.add(numero_norm)
                    unique_amends.append(amend)
            section.amendements_rejetes_detailles = unique_amends

            # Also dedup points_vigilance cross-section by amendment number
            unique_pv = []
            for pv in section.points_vigilance:
                m = re.search(r'N°\s*([IVX]*-?\s*\d+)', pv)
                if m:
                    num = _roman_re.sub('', m.group(1)).strip()
                    if num in seen_amendment_numeros:
                        continue  # Already seen in a previous section
                    seen_amendment_numeros.add(num)
                unique_pv.append(pv)
            section.points_vigilance = unique_pv

        # Stage 4.8: Remove points_vigilance that are parameter descriptions, not actual vigilance
        _param_patterns = re.compile(
            r"^(seuil|délai|taux|plafond|quota|limite|montant|durée)\b.*\b(fixé|porté|maintenu|de \d+)\b",
            re.IGNORECASE,
        )
        for section in sections:
            section.points_vigilance = [
                pv for pv in section.points_vigilance
                if not _param_patterns.search(pv) or "amendement" in pv.lower() or "rejet" in pv.lower()
            ]

        # Stage 5: Build synthesis
        synthese = self._build_synthesis(sections)

        # Detect consolidation periods
        consolidation_periods = []
        if include_consolidation_periods:
            consolidation_periods = self._detect_consolidation_periods(
                article_info, modifications
            )

        # Build parliamentary source references
        an_sources, senat_sources = self._build_parliamentary_sources(sections)

        # Cross-law reference detection (Phase 5A)
        # Uses debate_chunks_by_law collected during the main analysis loop (no re-search)
        cross_references = []
        try:
            if sections_by_law and debate_chunks_by_law:
                settings = get_settings()
                cross_analyzer = CrossLawAnalyzer()
                raw_refs = cross_analyzer.detect_all(
                    sections_by_law, debate_chunks_by_law,
                    article_ref=article_info.reference,
                    min_chunk_score=settings.cross_ref_min_score,
                    enable_llm=settings.cross_ref_enable_llm,
                )
                cross_references = [
                    {
                        "source_law": f"{cr.source_law_type} {cr.source_law_year}",
                        "target_law": cr.target_law_ref,
                        "citation": cr.citation,
                        "ref_type": cr.ref_type.value,
                        "confidence": round(cr.confidence, 3),
                    }
                    for cr in raw_refs
                ]
        except Exception as e:
            logger.debug(f"Cross-law detection failed: {e}")

        # Enrich cross-references with Légifrance data
        if article_info.references_croisees:
            legifrance_refs = {
                ref for ref in article_info.references_croisees
            }
            existing_targets = {cr.get("target_law") for cr in cross_references}
            for ref in legifrance_refs:
                if ref not in existing_targets:
                    cross_references.append({
                        "source_law": "",
                        "target_law": ref,
                        "citation": "",
                        "ref_type": "legifrance_reference",
                        "confidence": 1.0,
                    })

        # Build truncation note if max_modifications was applied
        truncation_note = ""
        if max_modifications and len(modifications) < len(article_info.modifications):
            truncation_note = (
                f"Note: Seules {len(modifications)} des "
                f"{len(article_info.modifications)} modifications ont été analysées."
            )

        # Merge skipped sections back and sort chronologically
        all_sections = sections + skipped_sections
        all_sections.sort(key=lambda s: s.modification.date)

        # Build result - historique includes all modifications
        return ArticleAnalysisResult(
            article_ref=article_ref,
            article_titre=article_info.titre,
            article_code=article_info.code,
            question=question if not truncation_note else f"{question}\n\n> *{truncation_note}*",
            historique=modifications,
            sections=all_sections,
            consolidation_periods=consolidation_periods,
            synthese_evolution=synthese.get("evolution", []),
            legifrance_url=article_info.legifrance_url or "",
            etat_actuel=article_info.etat_actuel or "",
            cross_references=cross_references,
            sources_parlementaires_an=an_sources,
            sources_parlementaires_senat=senat_sources,
            total_debates_searched=total_debates,
            total_interventions_found=total_interventions,
            generated_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )

    # Speaker roles considered authoritative for rescue in chunk filtering
    _AUTHORITY_ROLES = {"rapporteur", "rapporteur général", "ministre", "secrétaire d'état",
                        "ministre délégué", "président de la commission des finances"}

    # Dynamic generic keywords - computed once from the article index
    # Three-tier keyword classification (cached):
    # - always_generic: pure noise, excluded from filtering AND ILIKE
    # - downweight: legal mechanism terms, count for unique but NOT specific
    # - keep: full weight (default)
    _ALWAYS_GENERIC: frozenset | None = None
    _DOWNWEIGHT: frozenset | None = None

    @classmethod
    def _compute_keyword_tiers(cls):
        """Compute 3-tier keyword classification from article index (cached)."""
        if cls._ALWAYS_GENERIC is not None:
            return

        from collections import Counter
        always_generic = set()
        downweight = set()

        try:
            from src.analysis.article_index import get_article_index
            index = get_article_index()
            articles = index._data.get("articles", {})
            kw_freq: Counter = Counter()
            total = 0
            for ref, art in articles.items():
                kws = art.get("keywords", [])
                if kws:
                    total += 1
                    for kw in set(kws):
                        kw_freq[kw] += 1

            for kw, count in kw_freq.items():
                pct = count / total * 100 if total else 0
                is_single = " " not in kw and "-" not in kw
                is_number = kw.replace("-", "").replace(" ", "").isdigit()
                is_pct = "%" in kw

                # always_generic: numbers, percentages, or single words ≥0.5%
                if is_number or is_pct:
                    always_generic.add(kw)
                elif is_single and len(kw) < 8 and pct >= 0.5:
                    always_generic.add(kw)
                elif is_single and pct >= 0.5:
                    always_generic.add(kw)
                # downweight: multiword legal mechanism terms ≥2%
                elif not is_single and pct >= 2.0:
                    downweight.add(kw)

            logger.info(
                f"Keyword tiers: {len(always_generic)} always_generic, "
                f"{len(downweight)} downweight, "
                f"{total - len(always_generic) - len(downweight)} keep"
            )
        except Exception:
            always_generic = {
                "titres", "valeurs", "droits", "sociaux", "mobilières",
                "capital", "revenus", "imposables", "bénéfices",
            }
            downweight = {
                "impôt sur les sociétés", "impôt sur le revenu", "exonération",
                "plus-values", "taxe sur la valeur ajoutée", "crédit d'impôt",
            }

        cls._ALWAYS_GENERIC = frozenset(always_generic)
        cls._DOWNWEIGHT = frozenset(downweight)

    @classmethod
    def _get_generic_keywords(cls) -> frozenset:
        """Return always_generic + downweight (backward compat for search.py)."""
        cls._compute_keyword_tiers()
        return cls._ALWAYS_GENERIC | cls._DOWNWEIGHT

    @classmethod
    def _get_always_generic(cls) -> frozenset:
        """Keywords that are pure noise - excluded everywhere."""
        cls._compute_keyword_tiers()
        return cls._ALWAYS_GENERIC

    @classmethod
    def _get_downweight(cls) -> frozenset:
        """Legal mechanism terms - count for unique but not specific."""
        cls._compute_keyword_tiers()
        return cls._DOWNWEIGHT

    # Procedural patterns for chunk-level filtering
    _CHUNK_PROCEDURAL = [
        "la parole est à", "je mets aux voix", "il en est ainsi décidé",
        "la séance est levée", "la séance est suspendue", "la séance est reprise",
        "prochaine séance", "l'ordre du jour appelle", "nous reprenons la discussion",
        "l'amendement est adopté", "l'amendement est rejeté",
        "l'amendement n'est pas adopté", "en conséquence, un article additionnel",
    ]

    @staticmethod
    def _stem_kw(word: str) -> str:
        return word.lower().rstrip('s').rstrip('e').rstrip('é')

    @classmethod
    def _dedup_kw_by_stem(cls, kw_list: list[str]) -> list[str]:
        seen = set()
        unique = []
        for kw in kw_list:
            stem = cls._stem_kw(kw.split()[0]) if kw else kw
            if stem not in seen:
                seen.add(stem)
                unique.append(kw)
        return unique

    @classmethod
    def _is_procedural_chunk(cls, content: str) -> bool:
        content_lower = re.sub(r'\s+', ' ', content.lower())
        sentences = [s.strip() for s in content.split('.') if len(s.strip()) > 10]
        if not sentences:
            return False
        proc = sum(1 for s in sentences if any(p in s.lower() for p in cls._CHUNK_PROCEDURAL))
        return proc / len(sentences) > 0.5

    @staticmethod
    def _chunk_cites_different_article(content: str, target_ref: str) -> bool:
        # Match CGI article references like "150-0 B ter", "39", "1382 H", "199 terdecies-0 A"
        # Captures: digits + optional "-digits" + optional single uppercase letter (not followed by lowercase)
        # + optional latin suffix (bis, ter, etc.)
        _art_re = re.compile(
            r"article\s+((?:\d+(?:[- ]\d+)?(?:\s+[A-Z](?![a-zéèêà]))?)"
            r"(?:\s+(?:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies"
            r"|undecies|duodecies|terdecies|quaterdecies|quindecies|sexdecies))?)",
            re.IGNORECASE,
        )
        found = _art_re.findall(content)
        if not found:
            return False
        target_norm = target_ref.lower().replace(" ", "").replace("-", "")
        has_other = has_target = False
        for a in found:
            a_norm = a.strip().lower().replace(" ", "").replace("-", "")
            if len(a_norm) > 2:
                if target_norm in a_norm or a_norm in target_norm:
                    has_target = True
                else:
                    has_other = True
        return has_other and not has_target

    def _filter_chunks_by_article_relevance(
        self, chunks: list, article_ref: str, article_keywords: list[str]
    ) -> list:
        """Enhanced chunk filter: explicit_ref → same-debate expansion → strict other debates.

        Three-phase filtering:
        1. Identify explicit_ref chunks (text match or articles_mentionnes metadata)
        2. For debates with explicit_ref: relaxed criteria (1 keyword, authority rescue)
        3. For other debates: strict criteria (2 unique keywords, 1 specific, no other CGI article)
        """
        from src.indexing.search import _build_contextualized_patterns, _is_short_article_ref

        ref_lower = article_ref.lower()
        ref_variants = [ref_lower, ref_lower.replace("-", " ")]

        ctx_patterns = None
        if _is_short_article_ref(article_ref):
            ctx_patterns = _build_contextualized_patterns(article_ref)

        def _is_explicit(chunk):
            # Normalize whitespace (handles double-space variants like "150-0 B  ter")
            content_lower = re.sub(r'\s+', ' ', chunk.content.lower())
            if ctx_patterns:
                if any(p.search(chunk.content) for p in ctx_patterns):
                    return True
            else:
                if any(v in content_lower for v in ref_variants):
                    return True
            # Check articles_mentionnes metadata
            mentions = chunk.metadata.get("articles_mentionnes")
            if mentions:
                for m in mentions:
                    m_norm = m.lower().replace("-", " ").strip()
                    if any(v in m_norm or m_norm in v for v in ref_variants):
                        return True
            return False

        def _keyword_quality(chunk):
            content_lower = chunk.content.lower()
            always_generic = self._get_always_generic()
            downweight = self._get_downweight()
            # Exclude always_generic keywords from matching entirely
            kw_matches = [
                kw for kw in article_keywords
                if kw not in always_generic and _keyword_matches(kw, content_lower)
            ]
            unique = self._dedup_kw_by_stem(kw_matches)
            # "specific" = not always_generic AND not downweight
            specific = [kw for kw in unique if kw not in downweight]
            return len(unique), len(specific)

        # Phase 1: Find explicit_ref chunks and their debates
        explicit_debate_ids = set()
        explicit_chunks = []
        for chunk, score in chunks:
            if _is_explicit(chunk):
                explicit_chunks.append((chunk, score))
                explicit_debate_ids.add(chunk.source_id)

        # Phase 2: Same-debate chunks (relaxed, no other-article filter)
        same_debate_chunks = []
        for chunk, score in chunks:
            if chunk.source_id not in explicit_debate_ids:
                continue
            if any(chunk.content[:80] == ec.content[:80] for ec, _ in explicit_chunks):
                continue  # Already in explicit
            if self._is_procedural_chunk(chunk.content):
                continue
            unique_kw, specific_kw = _keyword_quality(chunk)
            if unique_kw >= 1:
                same_debate_chunks.append((chunk, score))
                continue
            # Authority rescue: any non-generic keyword
            fonction = (chunk.metadata.get("fonction") or "").lower()
            if any(r in fonction for r in self._AUTHORITY_ROLES):
                content_lower = chunk.content.lower()
                always_gen = self._get_always_generic()
                if any(kw not in always_gen and _keyword_matches(kw, content_lower)
                       for kw in article_keywords):
                    same_debate_chunks.append((chunk, score * 0.95))

        # Phase 3: Other debates (strict: 2 unique keywords, 1 specific, no other CGI article)
        other_chunks = []
        for chunk, score in chunks:
            if chunk.source_id in explicit_debate_ids:
                continue
            if self._is_procedural_chunk(chunk.content):
                continue
            if self._chunk_cites_different_article(chunk.content, article_ref):
                continue
            unique_kw, specific_kw = _keyword_quality(chunk)
            if unique_kw >= 2 and specific_kw >= 1:
                other_chunks.append((chunk, score))
                continue
            # Authority rescue with 1 specific keyword
            fonction = (chunk.metadata.get("fonction") or "").lower()
            if any(r in fonction for r in self._AUTHORITY_ROLES) and specific_kw >= 1:
                other_chunks.append((chunk, score * 0.9))

        # Dedup and merge
        seen = set()
        result = []
        for chunk, score in explicit_chunks + same_debate_chunks + other_chunks:
            key = chunk.content[:80]
            if key not in seen:
                seen.add(key)
                result.append((chunk, score))

        rejected = len(chunks) - len(result)
        if rejected > 0:
            logger.info(
                f"[enhanced_filter] {len(chunks)} → {len(result)} chunks "
                f"({len(explicit_chunks)} explicit, {len(same_debate_chunks)} same_debate, "
                f"{len(other_chunks)} other | rejected {rejected} for {article_ref})"
            )

        # Fallback: if enhanced filter eliminated ALL chunks, relax criteria
        if not result and chunks:
            logger.warning(f"[enhanced_filter] 0 chunks after strict filter for {article_ref} - applying fallback")
            for chunk, score in chunks:
                if self._is_procedural_chunk(chunk.content):
                    continue
                content_lower = chunk.content.lower()
                kw = [k for k in article_keywords if _keyword_matches(k, content_lower)]
                specific = [k for k in self._dedup_kw_by_stem(kw) if k not in self._get_generic_keywords()]
                if len(specific) >= 1:
                    result.append((chunk, score))

        return result

    # Cached keyword specificity scores (inverse document frequency)
    _KW_SPECIFICITY: dict | None = None

    @classmethod
    def _get_kw_specificity(cls) -> dict:
        """Compute specificity score for each keyword (cached).

        Score = 1/log2(1 + article_count). A keyword in 1 article scores 1.0,
        in 10 articles ~0.30, in 100 articles ~0.15, in 1000 articles ~0.10.
        """
        if cls._KW_SPECIFICITY is not None:
            return cls._KW_SPECIFICITY

        import math
        from collections import Counter
        try:
            from src.analysis.article_index import get_article_index
            idx = get_article_index()
            articles = idx._data.get("articles", {})
            kw_freq: Counter = Counter()
            for art in articles.values():
                for kw in set(art.get("keywords", [])):
                    kw_freq[kw] += 1
            cls._KW_SPECIFICITY = {
                kw: 1.0 / math.log2(1 + count)
                for kw, count in kw_freq.items()
            }
        except Exception:
            cls._KW_SPECIFICITY = {}
        return cls._KW_SPECIFICITY

    def _topical_relevance_cleanup(
        self, chunks: list, article_ref: str, article_keywords: list[str],
    ) -> list:
        """Final noise cleanup before LLM, scale-invariant.

        Replaces the previous fixed thresholds (weighted_score>=0.6 OR
        hit_count>=3) which silently killed every chunk for articles with
        small keyword lists (cf. docs/diagnostic/04_differential_diagnosis.md
        — kept=0 on 8/8 modifications of 973). Decisions are made on the
        SHAPE of the article's own score distribution, not on absolute
        values, so the filter generalises across density profiles.

        Algorithm:
          1. Reference matching: contextualised regex for short refs (avoids
             substring conflation of "39 C" with "39 C bis", "973" with
             "1973"); substring fallback for long refs (unambiguous).
          2. For non-explicit chunks: compute (weighted_score, hit_count)
             with cross-ref parasites stripped from scoreable_kw.
          3. Adaptive median cut on EACH dimension independently; OR them
             (preserves the dual-signal semantics of v1 — high specificity
             vs broad coverage — but rescales each branch to the article's
             own data shape).

        Adaptive threshold (per dimension):
          - max(values) = 0   → no signal, threshold = +∞ (keep none).
          - else              → threshold = median(values). Keep chunks
            with value > 0 AND value >= median.

        Asymptotic behaviour:
          - all scores 0 (sparse article, no hits) → keep nothing from
            scoring; explicit + downstream amendments carry the analysis.
          - uniform non-zero scores (everyone equally relevant) → keep all.
          - smooth descending distribution → keep top half (the "above
            average" chunks).
          - bimodal (signal cluster + noise cluster) → keep the signal
            cluster (its members are above the median, noise is below).

        Falsifiability:
          - H: this is scale-invariant. Test: kept_fraction(150-0 B ter) ≈
            kept_fraction(973) under same distribution shape.
          - H: non-regression on 150-0 B ter. Test: bench v2 must keep
            ≥ v1 final n_sources and avg_confidence.
        """
        if not chunks:
            return chunks

        ref_lower = article_ref.lower()
        ref_nospace = ref_lower.replace("-", " ")
        always_generic = self._get_always_generic()
        downweight = self._get_downweight()
        specificity = self._get_kw_specificity()

        # Contextualised regex for short refs; substring is fine for long ones.
        use_ctx = _is_short_article_ref(article_ref)
        ctx_patterns = _build_contextualized_patterns(article_ref) if use_ctx else None

        # Multiword scoreable keywords. Drop any kw that mentions a CGI
        # article number anywhere (self-refs are redundant with the
        # explicit-ref branch; cross-refs to other articles would falsely
        # promote off-topic chunks — cf. parasite case 973 → "150-0 b ter").
        scoreable_kw = [
            (kw, specificity.get(kw, 0.5))
            for kw in article_keywords
            if (" " in kw or "-" in kw)
            and kw not in always_generic
            and kw not in downweight
            and not _ARTICLE_REF_IN_KW_RE.search(kw)
        ]

        # Phase 1: separate explicit-ref chunks (always kept) from the rest.
        explicit: list = []
        scored: list = []  # (chunk, original_score, weighted_kw_score, hit_count)
        for chunk, score in chunks:
            content_lower = re.sub(r'\s+', ' ', chunk.content.lower())
            if ctx_patterns is not None:
                is_explicit = any(p.search(chunk.content) for p in ctx_patterns)
            else:
                is_explicit = (
                    ref_lower in content_lower or ref_nospace in content_lower
                )
            if is_explicit:
                explicit.append((chunk, score))
                continue
            weighted = 0.0
            hits = 0
            for kw, spec in scoreable_kw:
                if _keyword_matches(kw, content_lower):
                    weighted += spec
                    hits += 1
            scored.append((chunk, score, weighted, hits))

        # Phase 2: adaptive median cut on both score dimensions; OR them.
        def _keep_mask(values: list[float]) -> list[bool]:
            """Scale-invariant keep mask.

            Asymptotic:
              - empty → []
              - max == 0 → all-False (no signal)
              - else → keep values > 0 AND >= median(all values).
                Including zeros in the median makes the threshold
                naturally adaptive: many zeros pull the median down,
                so positive values are kept; few zeros raise it,
                so only the high values are kept.
            """
            if not values:
                return []
            if max(values) == 0:
                return [False] * len(values)
            sorted_vals = sorted(values)
            n = len(sorted_vals)
            if n % 2 == 1:
                med = sorted_vals[n // 2]
            else:
                med = (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2.0
            return [v > 0 and v >= med for v in values]

        kept_from_score: list = []
        diag_max_ws = 0.0
        diag_max_hc = 0
        diag_med_ws = 0.0
        diag_med_hc = 0.0
        if scored:
            ws_vals = [t[2] for t in scored]
            hc_vals = [float(t[3]) for t in scored]
            diag_max_ws = max(ws_vals)
            diag_max_hc = max(hc_vals)
            ws_keep = _keep_mask(ws_vals)
            hc_keep = _keep_mask(hc_vals)
            # Compute median for diagnostics
            if diag_max_ws > 0:
                sv = sorted(ws_vals)
                diag_med_ws = sv[len(sv)//2] if len(sv) % 2 else (sv[len(sv)//2-1] + sv[len(sv)//2]) / 2
            if diag_max_hc > 0:
                sv = sorted(hc_vals)
                diag_med_hc = sv[len(sv)//2] if len(sv) % 2 else (sv[len(sv)//2-1] + sv[len(sv)//2]) / 2
            for (c, s, _w, _h), kw, kh in zip(scored, ws_keep, hc_keep):
                if kw or kh:
                    kept_from_score.append((c, s))

        kept = explicit + kept_from_score
        removed = len(chunks) - len(kept)

        logger.info(
            f"[DIAG:TOPICAL] art={article_ref!r} input={len(chunks)} kept={len(kept)} "
            f"explicit={len(explicit)} from_score={len(kept_from_score)} "
            f"scoreable_kw_count={len(scoreable_kw)} "
            f"ws_max={diag_max_ws:.3f} ws_med={diag_med_ws:.3f} "
            f"hc_max={int(diag_max_hc)} hc_med={diag_med_hc:.1f}"
        )
        if removed:
            logger.info(
                f"[topical_cleanup] Removed {removed}/{len(chunks)} low-relevance "
                f"chunks for {article_ref} (kept {len(kept)})"
            )
        return kept

    def _filter_amendment_chunks(
        self, chunks: list, article_ref: str, article_keywords: list[str],
        mod_date: date | None = None,
    ) -> list:
        """Filter amendment chunks: explicit ref + temporal + no other CGI article + no procedural."""
        ref_lower = article_ref.lower()
        ref_variants = [ref_lower, ref_lower.replace("-", " ")]
        filtered = []

        for chunk, score in chunks:
            content_lower = chunk.content.lower()

            # Must mention article explicitly OR have at least 1 specific keyword
            has_ref = any(v in content_lower for v in ref_variants)
            if not has_ref:
                mentions = chunk.metadata.get("articles_mentionnes")
                if mentions:
                    for m in mentions:
                        if any(v in m.lower().replace("-", " ") for v in ref_variants):
                            has_ref = True
                            break
            if not has_ref:
                kw = [k for k in article_keywords if _keyword_matches(k, content_lower)]
                specific = [k for k in self._dedup_kw_by_stem(kw) if k not in self._get_generic_keywords()]
                if not specific:
                    continue

            # Reject if cites a different CGI article without citing target
            if self._chunk_cites_different_article(chunk.content, article_ref):
                continue

            # Temporal: reject amendments > 300 days before modification
            amend_date_str = chunk.metadata.get("date", "")
            if amend_date_str and mod_date:
                try:
                    amend_date = date.fromisoformat(amend_date_str[:10])
                    gap = (mod_date - amend_date).days
                    if gap > 300:
                        logger.debug(
                            f"[amend_filter] Rejecting amendment {gap}d before mod "
                            f"({amend_date_str[:10]}) for {article_ref}"
                        )
                        continue
                except (ValueError, TypeError):
                    pass

            # Reject procedural
            if self._is_procedural_chunk(chunk.content):
                continue

            filtered.append((chunk, score))

        rejected = len(chunks) - len(filtered)
        if rejected > 0:
            logger.info(
                f"[amend_filter] {len(chunks)} → {len(filtered)} amendments "
                f"(rejected {rejected} for {article_ref})"
            )
        return filtered

    def _extract_rapport_keywords(
        self, law_numero: str, article_ref: str, existing_keywords: list[str],
    ) -> list[str]:
        """Extract frequent fiscal terms from parliamentary reports for this law.

        Queries SourceExterne type=rapport, extracts bigrams containing fiscal roots,
        and returns terms not already in existing_keywords.
        """
        try:
            with get_session() as db:
                rows = db.query(SourceExterne.contenu).filter(
                    SourceExterne.type == "rapport",
                    SourceExterne.lois_liees.cast(String).ilike(f"%{law_numero}%"),
                ).all()

                if not rows:
                    # Try by article
                    rows = db.query(SourceExterne.contenu).filter(
                        SourceExterne.type == "rapport",
                        SourceExterne.articles_cgi_lies.cast(String).ilike(f"%{article_ref}%"),
                    ).limit(2).all()

            if not rows:
                # Fallback: BOFiP doctrine (available for most articles)
                rows = db.query(SourceExterne.contenu).filter(
                    SourceExterne.type == "bofip",
                    ~SourceExterne.reference.like("ACTU-%"),
                    SourceExterne.titre.ilike(f"%{article_ref}%"),
                ).limit(2).all()

            if not rows:
                # Last resort: CE jurisprudence (contentious terms)
                rows = db.query(SourceExterne.contenu).filter(
                    SourceExterne.type == "jurisprudence",
                    SourceExterne.chambre == "CE",
                    SourceExterne.articles_cgi_lies.cast(String).ilike(f"%{article_ref}%"),
                ).order_by(SourceExterne.date.desc()).limit(3).all()

            if not rows:
                return []

            # Combine all source text
            combined = " ".join((r[0] or "") for r in rows).lower()
            if len(combined) < 100:
                return []

            # Extract bigrams
            words = re.findall(r"[a-zéèêëàâôîïùûüç'-]+", combined)
            from collections import Counter
            bigrams = Counter()
            for i in range(len(words) - 1):
                bg = f"{words[i]} {words[i+1]}"
                bigrams[bg] += 1

            # Fiscal roots to look for
            _fiscal_roots = {
                "imposition", "impôt", "plus-value", "cession", "apport", "titres",
                "société", "report", "sursis", "soulte", "réinvestissement",
                "capital", "fonds", "abus", "optimisation", "exonération",
            }
            # Stop words - bigrams like "de titres" or "la société" are too generic
            _stop_words = {
                "de", "du", "des", "le", "la", "les", "un", "une",
                "à", "au", "aux", "en", "et", "ou", "ce", "ces",
                "sur", "par", "pour", "que", "qui", "son", "ses",
            }

            # Filter: frequent, fiscal, both words meaningful, not already in keywords
            existing_lower = {kw.lower() for kw in existing_keywords}
            new_terms = []
            for bg, count in bigrams.most_common(100):
                if count < 3:
                    break
                w1, w2 = bg.split(" ", 1)
                # Strip leading contractions (l', d', n', s', qu')
                w1_clean = re.sub(r"^[ldnsq]u?'", "", w1) or w1
                w2_clean = re.sub(r"^[ldnsq]u?'", "", w2) or w2
                # Skip if either cleaned word is a stop word or too short
                if w1_clean in _stop_words or w2_clean in _stop_words:
                    continue
                if len(w1_clean) < 3 or len(w2_clean) < 3:
                    continue
                # Skip non-specific bigrams (rapport-wide noise)
                if any(noise in bg for noise in ("impôts ", "article ", "rapporteur", "alinéa")):
                    continue
                if any(r in bg for r in _fiscal_roots):
                    # Check not already covered by existing keywords (either direction)
                    if not any(bg in ekw or ekw in bg for ekw in existing_lower):
                        new_terms.append(bg)
                        if len(new_terms) >= 8:
                            break

            if new_terms:
                logger.info(f"[source_keywords] Enriched with {len(new_terms)} terms: {new_terms[:5]}")
            return new_terms

        except Exception as e:
            logger.debug(f"[source_keywords] Failed: {e}")
            return []

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
                self.token_tracker.set_phase("relevance_filter")
                response = self._call_llm(prompt, temperature=0.0, system_override=SYSTEM_PROMPT_LEGAL_SHORT)
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
        all_modification_dates: list[tuple[date, str]] | None = None,
        query_embedding: list[float] | None = None,
    ) -> tuple[SectionAnalysis, int, int, list]:
        """
        Analyze a single law modification.

        Args:
            all_modification_dates: List of (date, law_type) for all modifications of this article.
                Used to prevent cross-contamination by assigning each debate to its closest modification.

        Returns:
            Tuple of (SectionAnalysis, debates_searched, interventions_found, raw_debate_chunks)
            raw_debate_chunks are pre-filter chunks for reuse by cross-law detection.
        """
        # Define date range for search based on law type:
        # LF/LFSS: PLF filed ~Sep N-1, debates Oct-Dec N-1, promulgation Dec N-1 to Jan N.
        # Must look back 15 months from promulgation to capture PLF examination debates.
        # Use date_debut_vigueur when available for more precise anchoring.
        anchor_date = mod.date
        if mod.date_debut_vigueur:
            try:
                anchor_date = date.fromisoformat(mod.date_debut_vigueur)
            except ValueError:
                pass
        date_start = anchor_date - timedelta(days=450)
        date_end = anchor_date + timedelta(days=30)

        # Stage 1: Search - broader recall when multi-stage retrieval is enabled
        multi_stage = self.settings.multi_stage_retrieval
        search_top_k = 25 if multi_stage else 40
        min_sim_override = self.settings.multi_stage_min_similarity if multi_stage else None

        law_debate_refs = self._get_debate_refs_for_law(mod.law_numero)

        # Enrich keywords with terms from parliamentary reports (if available)
        enriched_keywords = list(article_info.keywords)
        rapport_kw = self._extract_rapport_keywords(
            mod.law_numero, article_info.reference, article_info.keywords
        )
        if rapport_kw:
            enriched_keywords.extend(rapport_kw)

        search_results = self.search_service.search_article_modification(
            article_ref=article_info.reference,
            article_keywords=enriched_keywords,
            law_type=mod.law_type,
            law_year=mod.law_year,
            date_start=date_start,
            date_end=date_end,
            top_k=search_top_k,
            min_relevant=5,
            min_similarity_override=min_sim_override,
            article_in_law=mod.article_in_law,
            all_modification_dates=all_modification_dates,
            current_mod_date=mod.date,
            law_debate_refs=law_debate_refs,
            law_numero=mod.law_numero,
            query_embedding=query_embedding,
            modification_objet=mod.objet or "",
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

        # Save raw debate chunks before filtering (for cross-law reuse)
        raw_debate_chunks = list(debate_chunks)

        # Pre-filter chunks by article relevance before LLM
        debate_chunks = self._filter_chunks_by_article_relevance(
            debate_chunks, article_info.reference, enriched_keywords
        )
        amendment_chunks = self._filter_amendment_chunks(
            amendment_chunks, article_info.reference, article_info.keywords, mod.date
        )

        # Final noise cleanup: remove chunks with low topical relevance
        # Chunks that passed the keyword filter but don't discuss the article's
        # actual mechanism are noise (e.g. "cession de titres" in a PFU debate)
        debate_chunks = self._topical_relevance_cleanup(
            debate_chunks, article_info.reference, enriched_keywords
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
        # Use larger max_chars for Anthropic (can handle more context)
        expose_max = 10000 if self.settings.llm_provider == "anthropic" else 5000
        expose_text = self._fetch_expose_motifs(
            article_ref=article_info.reference,
            law_numero=mod.law_numero,
            max_chars=expose_max,
        )
        if expose_text:
            logger.info(f"  Found exposé des motifs ({len(expose_text)} chars)")

        # Fetch exposés des motifs des amendements pertinents
        try:
            with get_session() as db:
                loi_obj = db.query(Loi).filter(Loi.numero == mod.law_numero).first()
                if loi_obj:
                    dossier_objs = db.query(DossierLegislatif).filter(
                        DossierLegislatif.loi_id == loi_obj.id
                    ).all()
                    dossier_id_list = [d.id for d in dossier_objs]
                    if dossier_id_list:
                        amend_exposes = db.query(Amendement).filter(
                            Amendement.dossier_id.in_(dossier_id_list),
                            Amendement.expose_motifs.isnot(None),
                            Amendement.expose_motifs != '',
                            or_(
                                Amendement.expose_motifs.ilike(f'%{article_info.reference}%'),
                                *[Amendement.expose_motifs.ilike(f'%{kw}%')
                                  for kw in article_info.keywords[:2] if len(kw) > 5],
                            ),
                        ).limit(5).all()
                        if amend_exposes:
                            parts = []
                            for a in amend_exposes:
                                dos = db.get(DossierLegislatif, a.dossier_id)
                                ch = dos.chambre if dos else '?'
                                expose_clean = (a.expose_motifs or '')[:500]
                                parts.append(
                                    f"[{ch}] Amendement N°{a.numero} ({(a.auteur or '?')[:30]}) "
                                    f"[{a.sort}]: {expose_clean}"
                                )
                            expose_amend_text = "\n".join(parts)
                            expose_text = (
                                f"{expose_text}\n\n## Exposés des motifs des amendements\n{expose_amend_text}"
                                if expose_text else
                                f"## Exposés des motifs des amendements\n{expose_amend_text}"
                            )
                            logger.info(f"  Found {len(amend_exposes)} amendment exposés for {mod.law_ref}")
        except Exception as e:
            logger.debug(f"Could not fetch amendment exposés: {e}")

        logger.info(
            f"  Found {len(debate_chunks)} debate chunks, "
            f"{len(amendment_chunks)} amendment chunks (after relevance filter)"
        )

        # Fetch relevant jurisprudence for this modification
        jurisprudence_text = self._fetch_relevant_jurisprudence(
            article_ref=article_info.reference,
            mod_date=mod.date,
        )
        if jurisprudence_text:
            logger.info(f"  Found relevant jurisprudence ({len(jurisprudence_text)} chars)")

        # Extract intent using LLM
        if debate_chunks or amendment_chunks:
            section = self._extract_section_with_llm(
                article_info, mod, debate_chunks, amendment_chunks, expose_text,
                jurisprudence_text=jurisprudence_text,
            )
        else:
            section = self._create_empty_section(mod)

        section.debates_analyzed = len(debate_chunks)
        section.amendments_analyzed = len(amendment_chunks)

        # Note: chamber/fonction correction is now handled inside _validate_quotes()
        # using the matched chunk's metadata (more reliable than fuzzy name matching).

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

        return section, len(debate_chunks), len(amendment_chunks), raw_debate_chunks

    def _extract_section_with_langextract(
        self,
        article_info: ArticleInfo,
        mod: LawModification,
        debate_chunks: list,
        amendment_chunks: list,
        expose_motifs: str = "",
    ) -> SectionAnalysis:
        """Extract section using LangExtract for source-grounded citations.

        No _validate_quotes() needed - LangExtract guarantees source grounding,
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

    @staticmethod
    def _filter_offtopic_vigilance(vigilance: list[str], article_ref: str) -> list[str]:
        """Remove vigilance points that mention a different CGI article."""
        _cgi_article_re = re.compile(
            r'article\s+(\d{1,3}(?:\s*[-\u2013]\d+)?(?:\s+[A-Z])?'
            r'(?:\s+(?:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies'
            r'|undecies|duodecies|terdecies|quaterdecies|quatervicies)))',
            re.I,
        )
        ref_lower = article_ref.lower()
        filtered = []
        for v in vigilance:
            refs = _cgi_article_re.findall(v)
            if refs and all(r.lower().strip() != ref_lower for r in refs):
                logger.info(f"Filtered off-topic vigilance: {v[:80]}...")
                continue
            filtered.append(v)
        return filtered

    @staticmethod
    def _extract_amendments_from_vigilance(points_vigilance: list[str]) -> list[dict]:
        """Extract structured amendment data from points_vigilance strings.

        Haiku often puts amendment info in points_vigilance instead of
        amendements_rejetes_detailles. This post-processes those strings
        to extract structured data.
        """
        amendments = []
        seen_numeros = set()

        sort_words = r"rejetée?s?|retirée?s?|tombée?s?|non[_ ]soutenus?"
        # Normalize amendment numbers: strip roman prefix (I-, II-, III-) for dedup
        roman_prefix_re = re.compile(r'^[IVX]+-\s*', re.IGNORECASE)

        # Pattern 1: "Amendement(s) [N°] XXX (Auteur) sort: objet..."
        pat_with_author = re.compile(
            rf"Amendements?\s+(?:N°\s*)?(.+?)\s*\(([^)]+)\)\s*({sort_words})\s*[:-]\s*(.+)",
            re.IGNORECASE,
        )
        # Pattern 2: "Amendement(s) [N°] XXX sort : objet..." (no author)
        pat_no_author = re.compile(
            rf"Amendements?\s+(?:N°\s*)?(.+?)\s+({sort_words})\s*[:-]\s*(.+)",
            re.IGNORECASE,
        )

        for text in points_vigilance:
            m = pat_with_author.match(text)
            if m:
                numero = m.group(1).strip().rstrip(".")
                auteur = m.group(2).strip()
                sort_reel = m.group(3).strip().lower().replace("_", " ")
                objet = m.group(4).strip()
            else:
                m = pat_no_author.match(text)
                if not m:
                    continue
                numero = m.group(1).strip().rstrip(".")
                auteur = ""
                sort_reel = m.group(2).strip().lower().replace("_", " ")
                objet = m.group(3).strip()

            # Skip adopted amendments and duplicates
            if "adopté" in sort_reel:
                continue
            # Normalize for dedup: "I-94" and "94" are the same amendment
            numero_normalized = roman_prefix_re.sub('', numero).strip()
            if numero_normalized in seen_numeros:
                continue
            seen_numeros.add(numero_normalized)

            amendments.append({
                "numero": numero,
                "auteur": auteur,
                "sort_reel": sort_reel,
                "objet": objet[:300],
            })

        return amendments

    def _extract_section_with_llm(
        self,
        article_info: ArticleInfo,
        mod: LawModification,
        debate_chunks: list,
        amendment_chunks: list,
        expose_motifs: str = "",
        jurisprudence_text: str = "",
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

        return self._extract_section_single_step(
            article_info, mod, debate_chunks, amendment_chunks, expose_motifs,
            jurisprudence_text=jurisprudence_text,
        )

    def _filter_chunks_heuristic(
        self,
        article_info: ArticleInfo,
        debate_chunks: list,
        max_chunks: int = 10,
    ) -> list:
        """Filter chunks by keyword relevance score (no LLM call)."""
        scored = []
        ref_lower = article_info.reference.lower()
        kw_lower = [kw.lower() for kw in article_info.keywords[:7]]

        for item in debate_chunks:
            chunk = item[0] if isinstance(item, tuple) else item
            sim_score = item[1] if isinstance(item, tuple) else 0.5
            content_lower = (chunk.content if hasattr(chunk, 'content') else str(chunk)).lower()

            boost = 0.0
            if ref_lower in content_lower:
                boost += 0.4
            kw_matches = sum(1 for kw in kw_lower if kw in content_lower)
            boost += kw_matches * 0.08

            scored.append((item, sim_score + boost))

        scored.sort(key=lambda x: x[1], reverse=True)
        filtered = [item for item, _ in scored[:max_chunks]]
        logger.info(
            f"Chunk heuristic filtering: {len(debate_chunks)} → {len(filtered)} chunks"
        )
        return filtered

    def _filter_chunks_with_llm(
        self,
        article_info: ArticleInfo,
        debate_chunks: list,
    ) -> list:
        """Step 1: Use LLM to filter out irrelevant chunks before extraction.

        Only called when there are many chunks (high noise risk).
        Returns filtered list of chunks that actually discuss the target article.
        """
        from src.analysis.prompts import CHUNK_FILTERING_SYSTEM, CHUNK_FILTERING_PROMPT

        # Number each chunk for the LLM
        numbered = []
        for i, item in enumerate(debate_chunks):
            chunk = item[0] if isinstance(item, tuple) else item
            content = chunk.content if hasattr(chunk, 'content') else str(chunk)
            # Truncate long chunks for the filtering step
            numbered.append(f"[{i}] {content[:800]}")

        system = CHUNK_FILTERING_SYSTEM.format(
            article_ref=article_info.reference,
            article_code=article_info.code,
            article_titre=article_info.titre,
            article_keywords=", ".join(article_info.keywords[:5]),
        )
        prompt = CHUNK_FILTERING_PROMPT.format(
            article_ref=article_info.reference,
            article_titre=article_info.titre,
            numbered_chunks="\n\n".join(numbered),
        )

        try:
            self.token_tracker.set_phase("chunk_filtering")
            response = self._call_llm(prompt, system_override=system, max_tokens=500)
            # Parse JSON response
            import json as _json
            # Extract JSON from response
            text = response.strip()
            if "```" in text:
                text = text.split("```")[1].strip()
                if text.startswith("json"):
                    text = text[4:].strip()
            result = _json.loads(text)
            relevant_indices = set(result.get("relevant", []))

            if not relevant_indices:
                logger.warning("Chunk filter returned 0 relevant chunks, keeping all")
                return debate_chunks

            filtered = [debate_chunks[i] for i in sorted(relevant_indices) if i < len(debate_chunks)]
            logger.info(
                f"Chunk filtering: {len(debate_chunks)} → {len(filtered)} chunks "
                f"(removed {len(debate_chunks) - len(filtered)})"
            )
            return filtered
        except Exception as e:
            logger.warning(f"Chunk filtering failed ({e}), keeping all chunks")
            return debate_chunks

    def _extract_section_single_step(
        self,
        article_info: ArticleInfo,
        mod: LawModification,
        debate_chunks: list,
        amendment_chunks: list,
        expose_motifs: str = "",
        jurisprudence_text: str = "",
    ) -> SectionAnalysis:
        """Single-step extraction: one LLM call with full system prompt."""
        # Pre-filter chunks when there's too much noise (>10 chunks)
        # Uses heuristic scoring (keyword + similarity) instead of LLM call for speed
        if len(debate_chunks) > 10:
            debate_chunks = self._filter_chunks_heuristic(article_info, debate_chunks, max_chunks=10)

        # Format chunks for prompt - use more context for Anthropic (200K) vs Groq (128K)
        if self.settings.llm_provider == "anthropic":
            debate_result = format_debate_chunks_for_prompt(debate_chunks, max_chars=70000, numbered=True)
            amend_result = format_amendment_chunks_for_prompt(
                amendment_chunks, max_chars=20000, debate_chunks=debate_chunks, numbered=True
            )
        else:
            debate_result = format_debate_chunks_for_prompt(debate_chunks, max_chars=20000, numbered=True)
            amend_result = format_amendment_chunks_for_prompt(
                amendment_chunks, max_chars=6000, debate_chunks=debate_chunks, numbered=True
            )
        debate_text, debate_mapping = debate_result
        amendment_text, amend_mapping = amend_result
        chunk_mapping = {**debate_mapping, **amend_mapping}

        # Categorize exposé sections with structural markers (Phase 2D)
        if expose_motifs:
            expose_section = self._categorize_expose(expose_motifs)
        else:
            expose_section = "[Aucun exposé des motifs trouvé]"

        # Format system prompt once per article (enables Anthropic prompt caching)
        if self._system_prompt_formatted is None:
            # Build texte_actuel section (truncated to 2000 chars)
            texte_actuel_section = ""
            if article_info.texte_actuel:
                texte = article_info.texte_actuel[:2000]
                if len(article_info.texte_actuel) > 2000:
                    texte += "…"
                texte_actuel_section = f"## Texte actuel de l'article\n{texte}"

            # Build hierarchie string
            hierarchie_str = " > ".join(article_info.hierarchie) if article_info.hierarchie else "Non renseignée"

            self._system_prompt_formatted = SYSTEM_PROMPT_LEGAL.format(
                article_ref=article_info.reference,
                article_code=article_info.code,
                article_titre=article_info.titre,
                etat_actuel=article_info.etat_actuel or "En vigueur",
                hierarchie=hierarchie_str,
                texte_actuel_section=texte_actuel_section,
            )

            # Append BOFiP doctrine summary (cached across all sections)
            bofip_summary = self._fetch_bofip_summary(article_info.reference)
            if bofip_summary:
                self._system_prompt_formatted += (
                    f"\n\n## Doctrine administrative (BOFiP)\n"
                    f"Interprétation officielle de l'administration fiscale :\n{bofip_summary}"
                )

        # Format jurisprudence section
        jurisprudence_section = ""
        if jurisprudence_text:
            jurisprudence_section = f"## Jurisprudence pertinente (CE/CC)\n{jurisprudence_text}"
        else:
            jurisprudence_section = "[Aucune jurisprudence pertinente identifiée pour cette période]"

        # Build prompt with only variable content (context + data)
        prompt = SECTION_EXTRACTION_PROMPT.format(
            article_keywords=", ".join(article_info.keywords[:5]),
            law_ref=mod.law_ref,
            law_date=mod.date.isoformat(),
            article_in_law=mod.article_in_law,
            nature=mod.nature.value,
            objet=mod.objet or "Non renseigné",
            law_numero=mod.law_numero,
            debate_chunks=debate_text,
            amendments=amendment_text,
            expose_motifs=expose_section,
            jurisprudence_section=jurisprudence_section,
        )

        # Call LLM with retry on empty/invalid output
        result = {}
        try:
            self.token_tracker.set_phase("section_extraction")
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
                self.token_tracker.set_phase("section_retry")
                response_text = self._call_llm(prompt, temperature=0.1)
                result = self._parse_section_json(response_text)
            except Exception as e:
                logger.error(f"LLM extraction failed (retry): {e}")

        if not result.get("intention") and not result.get("contexte_legislatif"):
            logger.error("LLM extraction failed after retry, creating empty section")
            return self._create_empty_section(mod)

        # Detect new format (sources with extrait_id) vs old format (sources_an/sources_senat)
        _is_new_format = "sources" in result and isinstance(result["sources"], list) and (
            not result["sources"] or (
                isinstance(result["sources"][0], dict) and "extrait_id" in result["sources"][0]
            )
        )

        if _is_new_format:
            # New format: reconstruct from refs
            sources_an, sources_senat = self._reconstruct_sources_from_refs(
                result["sources"], chunk_mapping, mod_date=mod.date,
            )

            # Lightweight validation: remove procedural citations
            sources_an = [s for s in sources_an if not self._is_procedural_quote(s.citation)]
            sources_senat = [s for s in sources_senat if not self._is_procedural_quote(s.citation)]

            # Intra-section dedup: remove identical citations within same chamber
            sources_an = _dedup_sources_list(sources_an)
            sources_senat = _dedup_sources_list(sources_senat)

            # Chamber bias retry: check balance after reconstruction
            has_an_sources = len(sources_an) > 0
            has_senat_sources = len(sources_senat) > 0
            if result.get("intention") and (has_an_sources != has_senat_sources):
                missing_chamber = "assemblée" if not has_an_sources else "sénat"
                chamber_header = "## Assemblée nationale" if missing_chamber == "assemblée" else "## Sénat"

                should_retry = False
                if chamber_header in debate_text:
                    header_idx = debate_text.index(chamber_header)
                    chamber_content = debate_text[header_idx + len(chamber_header):]
                    for nh in ("## Assemblée nationale", "## Sénat"):
                        if nh != chamber_header and nh in chamber_content:
                            chamber_content = chamber_content[:chamber_content.index(nh)]
                            break
                    content_len = len(chamber_content.strip())
                    if content_len > 1500:
                        should_retry = True
                        logger.info(
                            f"Single-chamber bias detected ({missing_chamber} missing), "
                            f"retrying - {missing_chamber} section has {content_len} chars"
                        )
                    else:
                        logger.info(
                            f"Single-chamber bias detected but skipping retry - "
                            f"{missing_chamber} section only {content_len} chars (< 500)"
                        )

                if should_retry:
                    enhanced_prompt = (
                        prompt
                        + "\n\nIMPORTANT: Cite sources from BOTH chambers"
                        " (Assemblée nationale AND Sénat)."
                        " The debate sources contain contributions from both."
                    )
                    try:
                        self.token_tracker.set_phase("chamber_retry")
                        response_text = self._call_llm(enhanced_prompt, temperature=0.1)
                        retry_result = self._parse_section_json(response_text)
                        if "sources" in retry_result and isinstance(retry_result["sources"], list):
                            retry_an, retry_senat = self._reconstruct_sources_from_refs(
                                retry_result["sources"], chunk_mapping, mod_date=mod.date,
                            )
                            if retry_an and retry_senat:
                                result = retry_result
                                sources_an = [s for s in retry_an if not self._is_procedural_quote(s.citation)]
                                sources_senat = [s for s in retry_senat if not self._is_procedural_quote(s.citation)]
                    except Exception as retry_err:
                        logger.debug(f"Chamber bias retry failed: {retry_err}")

            # Skip _validate_quotes - metadata guaranteed correct from chunk
        else:
            # Old format fallback (sources_an/sources_senat with full citations)
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

            # Keep old fallback for "sources" key too
            if not sources_an and not sources_senat:
                for s in result.get("sources", []):
                    if isinstance(s, dict) and "citation" in s:
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

            # Retry if single-chamber bias detected and source material has both chambers
            has_an_sources = len(sources_an) > 0
            has_senat_sources = len(sources_senat) > 0
            if result.get("intention") and (has_an_sources != has_senat_sources):
                missing_chamber = "assemblée" if not has_an_sources else "sénat"
                chamber_header = "## Assemblée nationale" if missing_chamber == "assemblée" else "## Sénat"

                should_retry = False
                if chamber_header in debate_text:
                    header_idx = debate_text.index(chamber_header)
                    chamber_content = debate_text[header_idx + len(chamber_header):]
                    for nh in ("## Assemblée nationale", "## Sénat"):
                        if nh != chamber_header and nh in chamber_content:
                            chamber_content = chamber_content[:chamber_content.index(nh)]
                            break
                    content_len = len(chamber_content.strip())
                    if content_len > 1500:
                        should_retry = True
                        logger.info(
                            f"Single-chamber bias detected ({missing_chamber} missing), "
                            f"retrying - {missing_chamber} section has {content_len} chars"
                        )
                    else:
                        logger.info(
                            f"Single-chamber bias detected but skipping retry - "
                            f"{missing_chamber} section only {content_len} chars (< 500)"
                        )

                if should_retry:
                    enhanced_prompt = (
                        prompt
                        + "\n\nIMPORTANT: Cite sources from BOTH chambers"
                        " (Assemblée nationale AND Sénat)."
                        " The debate sources contain contributions from both."
                    )
                    try:
                        self.token_tracker.set_phase("chamber_retry")
                        response_text = self._call_llm(enhanced_prompt, temperature=0.1)
                        retry_result = self._parse_section_json(response_text)
                        if retry_result.get("sources_an") and retry_result.get("sources_senat"):
                            result = retry_result
                    except Exception as retry_err:
                        logger.debug(f"Chamber bias retry failed: {retry_err}")

        if not _is_new_format:
            # Filter out non-person sources (LLM sometimes cites legislative text as a "source")
            # 99.9% of real speakers in DB start with M./Mme/Mlle
            def _is_real_speaker(orateur: str) -> bool:
                if not orateur:
                    return False
                return orateur.startswith(('M.', 'Mme', 'Mlle', 'M '))

            before_filter = len(sources_an) + len(sources_senat)
            sources_an = [s for s in sources_an if _is_real_speaker(s.orateur)]
            sources_senat = [s for s in sources_senat if _is_real_speaker(s.orateur)]
            after_filter = len(sources_an) + len(sources_senat)
            if before_filter != after_filter:
                logger.info(
                    f"Filtered {before_filter - after_filter} non-person source(s) "
                    f"(legislative text cited as speaker)"
                )

        # Filter out citations that are legislative text, not parliamentary debate
        # Applied to both old format (sources_an/senat) and new format (extrait_id)
        def _is_legislative_text(citation: str) -> bool:
            """Detect citations that are law text rather than debate speech."""
            if not citation:
                return False
            c = citation.lower()
            leg_patterns = (
                "est ainsi modifié", "est ainsi rédigé", "il est inséré",
                "est remplacé par", "est complété par", "est abrogé",
                "est ajouté", "les mots", "la référence", "les références",
                "après le mot", "avant le mot", "au premier alinéa",
            )
            has_leg = sum(1 for p in leg_patterns if p in c)
            if has_leg < 1:
                return False
            # Opinion/debate patterns - if present, it's real debate
            debate_patterns = (
                "souhaite", "estime", "considère", "vise à", "a pour objet",
                "nous proposons", "il convient", "il est nécessaire",
                "l'objectif", "afin de", "dans le but",
            )
            has_debate = any(p in c for p in debate_patterns)
            return not has_debate

        before_leg = len(sources_an) + len(sources_senat)
        sources_an = [s for s in sources_an if not _is_legislative_text(s.citation)]
        sources_senat = [s for s in sources_senat if not _is_legislative_text(s.citation)]
        after_leg = len(sources_an) + len(sources_senat)
        if before_leg != after_leg:
            logger.info(
                f"Filtered {before_leg - after_leg} legislative text source(s) "
                f"(law text cited as debate)"
            )

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
        # Merge amendements_rejetes_detailles into points_vigilance without duplicates
        _roman_prefix = re.compile(r'^[IVX]+-\s*', re.IGNORECASE)
        points_vigilance = list(result.get("points_vigilance", []))

        # Build set of amendment numbers already in points_vigilance (from LLM short summaries)
        existing_nums = set()
        for pv in points_vigilance:
            m = re.search(r'N°\s*([IVX]*-?\s*\d+)', pv)
            if m:
                num = _roman_prefix.sub('', m.group(1)).strip()
                existing_nums.add(num)

        for amend_rej in result.get("amendements_rejetes_detailles", []):
            numero = amend_rej.get("numero", "?")
            numero_clean = numero.lstrip("N°").strip()
            numero_norm = _roman_prefix.sub('', numero_clean).strip()

            # Skip if this amendment number already exists in points_vigilance
            if numero_norm in existing_nums:
                continue

            auteur = amend_rej.get("auteur", "inconnu")
            objet = amend_rej.get("objet", "")
            raison = amend_rej.get("raison_rejet", "")
            implication = amend_rej.get("implication_intention", "")
            sort_reel = amend_rej.get("sort_reel", "rejeté")
            points_vigilance.append(
                f"Amendement N° {numero_clean} ({auteur}) {sort_reel}: {objet}. "
                f"Raison: {raison}. Implication: {implication}"
            )
            existing_nums.add(numero_norm)

        # Post-process: extract structured amendments from vigilance strings
        extracted_amendments = self._extract_amendments_from_vigilance(points_vigilance)

        # Filter off-topic vigilance points that mention a different CGI article
        points_vigilance = self._filter_offtopic_vigilance(points_vigilance, article_info.reference)

        # Fix C4: Correct "Création" hallucination when nature is "Modification"
        contexte = result.get("contexte_legislatif", "")
        if mod.nature.value == "Modification" and contexte:
            contexte = re.sub(
                r"(?i)\bcré(?:é|ation de l'article)\b",
                lambda m: "modifié" if "créé" in m.group().lower() else "modification de l'article",
                contexte,
            )

        section = SectionAnalysis(
            modification=mod,
            contexte=contexte,
            intention=result.get("intention", ""),
            sources_an=sources_an,
            sources_senat=sources_senat,
            evolution_debats=result.get("evolution_debats", ""),
            points_vigilance=points_vigilance,
            amendements_rejetes_detailles=extracted_amendments,
            niveau_certitude=niveau,
            certitude_justification=result.get("justification_certitude", ""),
            operational_params=operational_params,
            doctrine_convergence=result.get("doctrine_convergence", ""),
            doctrine_references=result.get("doctrine_references", []),
        )

        # Validate quotes against source chunks (only for old format - new format has guaranteed metadata)
        if not _is_new_format:
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

        # Hard rule: 0 sources → faible + "Modification technique" prefix
        if not section.sources_an and not section.sources_senat:
            section.niveau_certitude = CertitudeLevel.FAIBLE
            if section.intention and not section.intention.startswith("Modification"):
                first_line = section.intention.split("\n")[0]
                section.intention = (
                    f"Modification technique. {first_line}\n"
                    "Aucun débat parlementaire de fond identifié dans les travaux parlementaires."
                )
        # Élevé requires ≥2 sources from ≥2 chambers
        actual_sources = len(section.sources_an) + len(section.sources_senat)
        actual_chambers = (1 if section.sources_an else 0) + (1 if section.sources_senat else 0)
        if section.niveau_certitude == CertitudeLevel.ELEVE:
            if actual_sources < 2 or actual_chambers < 2:
                section.niveau_certitude = CertitudeLevel.MOYEN

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
                    key = (source.citation[:80].lower().strip(), source.orateur.lower().strip(), source.date or "")
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

    def _create_skipped_section(
        self,
        mod: LawModification,
        reason: str,
    ) -> SectionAnalysis:
        """Create a section for a modification skipped from analysis."""
        return SectionAnalysis(
            modification=mod,
            contexte=reason,
            intention="",
            niveau_certitude=CertitudeLevel.FAIBLE,
            certitude_justification=reason,
        )

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

        # Format chunks - same limits as single-step for maximum coverage
        _debate_max = 70000 if self.settings.llm_provider == "anthropic" else 20000
        _amend_max = 20000 if self.settings.llm_provider == "anthropic" else 6000
        debate_text, debate_mapping = format_debate_chunks_for_prompt(debate_chunks, max_chars=_debate_max, numbered=True)
        amendment_text, amend_mapping = format_amendment_chunks_for_prompt(amendment_chunks, max_chars=_amend_max, numbered=True)
        chunk_mapping = {**debate_mapping, **amend_mapping}
        expose_section = expose_motifs if expose_motifs else "[Aucun exposé des motifs trouvé]"

        # Log chunks sent to step 1 by chamber
        an_debate_count = sum(1 for c, _ in debate_chunks if (c.metadata.get("chambre") or "").upper() == "AN")
        senat_debate_count = sum(1 for c, _ in debate_chunks if (c.metadata.get("chambre") or "").upper() in ("SENAT", "SÉNAT"))
        logger.info(
            f"Two-step input for {mod.law_ref}: {len(debate_chunks)} debate chunks "
            f"(AN={an_debate_count}, Sénat={senat_debate_count}), "
            f"{len(amendment_chunks)} amendment chunks, "
            f"debate_text={len(debate_text)} chars, amendment_text={len(amendment_text)} chars"
        )

        # Step 1: Extract facts (temperature 0.0)
        fact_prompt = FACT_EXTRACTION_PROMPT.format(
            article_ref=article_info.reference,
            article_code=article_info.code,
            article_keywords=", ".join(article_info.keywords[:5]),
            law_ref=mod.law_ref,
            law_numero=mod.law_numero,
            article_in_law=mod.article_in_law,
            debate_chunks=debate_text,
            amendments=amendment_text,
            expose_motifs=expose_section,
        )
        try:
            self.token_tracker.set_phase("two_step_facts")
            facts_text = self._call_llm(fact_prompt, temperature=0.0, system_override=SYSTEM_PROMPT_LEGAL_SHORT, max_tokens=3000)
            facts_result = self._parse_section_json(facts_text)
        except Exception as e:
            logger.warning(f"Two-step fact extraction failed: {e}, falling back to single-step")
            return self._extract_section_single_step(
                article_info, mod, debate_chunks, amendment_chunks, expose_motifs
            )

        if not facts_result.get("facts"):
            logger.warning("No facts extracted, falling back to single-step")
            return self._extract_section_single_step(
                article_info, mod, debate_chunks, amendment_chunks, expose_motifs
            )

        # Log extracted facts
        facts = facts_result.get("facts", [])
        # Detect if facts use new format (extrait_id) or old format (chambre)
        _facts_new_format = facts and isinstance(facts[0], dict) and "extrait_id" in facts[0]
        if _facts_new_format:
            logger.info(f"Two-step facts for {mod.law_ref}: {len(facts)} total (new extrait_id format)")
            for f in facts:
                logger.info(
                    f"  [{f.get('extrait_id', '?')}] "
                    f"\"{(f.get('passage_cle') or '')[:80]}...\""
                )
        else:
            an_facts = [f for f in facts if (f.get("chambre") or "").upper() == "AN"]
            senat_facts = [f for f in facts if (f.get("chambre") or "").upper() in ("SÉNAT", "SENAT")]
            logger.info(
                f"Two-step facts for {mod.law_ref}: {len(facts)} total "
                f"(AN={len(an_facts)}, Sénat={len(senat_facts)})"
            )
            for f in facts:
                logger.info(
                    f"  [{f.get('chambre', '?')}] {f.get('orateur', '?')}: "
                    f"\"{(f.get('citation_exacte') or '')[:80]}...\""
                )
        amends = facts_result.get("amendements_identifies", [])
        if amends:
            logger.info(f"  Amendements: {len(amends)}")
            for a in amends[:5]:
                logger.info(f"    N° {a.get('numero', '?')} ({a.get('auteur', '?')}) [{a.get('sort', '?')}]")

        # Step 2: Synthesize intent from facts (temperature 0.2)
        facts_formatted = json.dumps(facts_result, ensure_ascii=False, indent=2)
        synth_prompt = INTENT_SYNTHESIS_PROMPT.format(
            article_ref=article_info.reference,
            article_code=article_info.code,
            article_titre=article_info.titre,
            article_keywords=", ".join(article_info.keywords[:5]),
            law_ref=mod.law_ref,
            law_date=mod.date.isoformat(),
            nature=mod.nature.value,
            extracted_facts=facts_formatted,
        )
        try:
            self.token_tracker.set_phase("two_step_synthesis")
            synth_text = self._call_llm(synth_prompt, temperature=0.2, system_override=SYSTEM_PROMPT_LEGAL_SHORT, max_tokens=2500)
            result = self._parse_section_json(synth_text)
        except Exception as e:
            logger.warning(f"Two-step synthesis failed: {e}, falling back to single-step")
            return self._extract_section_single_step(
                article_info, mod, debate_chunks, amendment_chunks, expose_motifs
            )

        # Detect new format (sources with extrait_id) vs old format (sources_an/sources_senat)
        _synth_new_format = "sources" in result and isinstance(result["sources"], list) and (
            not result["sources"] or (
                isinstance(result["sources"][0], dict) and "extrait_id" in result["sources"][0]
            )
        )

        # Log synthesis result
        if _synth_new_format:
            s2_src = len(result.get("sources", []))
            s2_cert = result.get("niveau_certitude", "?")
            logger.info(f"Two-step synthesis for {mod.law_ref}: {s2_src} sources (new format), cert={s2_cert}")
        else:
            s2_an = len(result.get("sources_an", []))
            s2_se = len(result.get("sources_senat", []))
            s2_cert = result.get("niveau_certitude", "?")
            logger.info(f"Two-step synthesis for {mod.law_ref}: AN={s2_an}, Sénat={s2_se}, cert={s2_cert}")

        # Build section from synthesis result
        if _synth_new_format:
            # New format: reconstruct from refs using chunk_mapping
            sources_an, sources_senat = self._reconstruct_sources_from_refs(
                result["sources"], chunk_mapping, mod_date=mod.date,
            )
            # Lightweight validation: remove procedural citations
            sources_an = [s for s in sources_an if not self._is_procedural_quote(s.citation)]
            sources_senat = [s for s in sources_senat if not self._is_procedural_quote(s.citation)]
        else:
            # Old format fallback
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

        _roman_prefix2 = re.compile(r'^[IVX]+-\s*', re.IGNORECASE)
        points_vigilance = list(result.get("points_vigilance", []))

        existing_nums2 = set()
        for pv in points_vigilance:
            m = re.search(r'N°\s*([IVX]*-?\s*\d+)', pv)
            if m:
                num = _roman_prefix2.sub('', m.group(1)).strip()
                existing_nums2.add(num)

        for amend_rej in result.get("amendements_rejetes_detailles", []):
            numero = amend_rej.get("numero", "?")
            numero_clean = numero.lstrip("N°").strip()
            numero_norm = _roman_prefix2.sub('', numero_clean).strip()

            if numero_norm in existing_nums2:
                continue

            auteur = amend_rej.get("auteur", "inconnu")
            objet = amend_rej.get("objet", "")
            raison = amend_rej.get("raison_rejet", "")
            implication = amend_rej.get("implication_intention", "")
            sort_reel = amend_rej.get("sort_reel", "rejeté")
            points_vigilance.append(
                f"Amendement N° {numero_clean} ({auteur}) {sort_reel}: {objet}. "
                f"Raison: {raison}. Implication: {implication}"
            )
            existing_nums2.add(numero_norm)

        # Post-process: extract structured amendments from vigilance strings
        extracted_amendments = self._extract_amendments_from_vigilance(points_vigilance)

        # Filter off-topic vigilance points that mention a different CGI article
        points_vigilance = self._filter_offtopic_vigilance(points_vigilance, article_info.reference)

        section = SectionAnalysis(
            modification=mod,
            contexte=result.get("contexte_legislatif", ""),
            intention=result.get("intention", ""),
            sources_an=sources_an,
            sources_senat=sources_senat,
            evolution_debats=result.get("evolution_debats", ""),
            points_vigilance=points_vigilance,
            amendements_rejetes_detailles=extracted_amendments,
            niveau_certitude=niveau,
            certitude_justification=result.get("justification_certitude", ""),
            operational_params=operational_params,
        )

        # Validate quotes against source chunks (only for old format - new format has guaranteed metadata)
        if not _synth_new_format:
            # In two-step old format, step 2 cites from step 1 facts, not raw chunks directly.
            # Add facts as synthetic chunks so _validate_quotes can match them.
            facts_chunks = []
            for fact in facts_result.get("facts", []):
                citation = fact.get("citation_exacte", "")
                if citation:
                    facts_chunks.append((
                        Chunk(content=citation, metadata=fact, chunk_type="fact",
                              source_id="two_step_facts", position=0),
                        1.0,
                    ))
            section = self._validate_quotes(
                section, debate_chunks + amendment_chunks + facts_chunks,
                article_info.keywords, article_ref=article_info.reference,
            )

        # Weighted source confidence
        all_sources = section.sources_an + section.sources_senat
        weighted_niveau = CertitudeLevel.from_weighted_sources(all_sources)
        if weighted_niveau.score < section.niveau_certitude.score:
            section.niveau_certitude = weighted_niveau

        # Hard rule: 0 sources cannot be "élevé", faible clears intention
        if not section.sources_an and not section.sources_senat:
            if section.niveau_certitude == CertitudeLevel.ELEVE:
                section.niveau_certitude = CertitudeLevel.MOYEN
        # Élevé requires ≥2 sources from ≥2 chambers
        actual_sources = len(section.sources_an) + len(section.sources_senat)
        actual_chambers = (1 if section.sources_an else 0) + (1 if section.sources_senat else 0)
        if section.niveau_certitude == CertitudeLevel.ELEVE:
            if actual_sources < 2 or actual_chambers < 2:
                section.niveau_certitude = CertitudeLevel.MOYEN

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

    def _call_llm(self, prompt: str, temperature: float = 0.2, system_override: str | None = None, max_tokens: int = 4500) -> str:
        """
        Call LLM API and return response text.

        For Groq, uses GroqClientPool for automatic key rotation on rate limits.
        Records token usage in self.token_tracker for cost analysis.

        Args:
            system_override: If provided, use this as system prompt instead of the
                default. Used by consolidation/relevance_filter which need the short
                prompt, not the section extraction instructions.
        """
        phase = self.token_tracker._current_phase

        # Use override if provided, else formatted article prompt, else short
        system_prompt = system_override or self._system_prompt_formatted or SYSTEM_PROMPT_LEGAL_SHORT

        # [DIAG:LLM] log prompt size and phase before the call.
        logger.info(
            f"[DIAG:LLM] phase={phase} provider={self.settings.llm_provider} "
            f"prompt_chars={len(prompt)} system_chars={len(system_prompt)} "
            f"max_tokens={max_tokens} temperature={temperature}"
        )

        if self.settings.llm_provider == "groq":
            text, groq_usage = self._get_groq_pool().chat_completion_with_usage(
                model=self.settings.groq_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=90.0,
            )
            self.token_tracker.record(TokenUsage(
                input_tokens=groq_usage.get("prompt_tokens", 0),
                output_tokens=groq_usage.get("completion_tokens", 0),
                phase=phase,
                provider="groq",
            ))
            logger.info(
                f"LLM [{phase}]: in={groq_usage.get('prompt_tokens', 0)} "
                f"out={groq_usage.get('completion_tokens', 0)} (groq)"
            )
            return text
        else:
            # Rate limiting for Anthropic API
            now = time.monotonic()
            elapsed = now - self._last_anthropic_call
            min_delay = self.settings.anthropic_call_delay
            if elapsed < min_delay:
                wait = min_delay - elapsed
                logger.info(f"Rate limit: waiting {wait:.1f}s before Anthropic call")
                time.sleep(wait)
            self._last_anthropic_call = time.monotonic()

            response = self.anthropic_client.messages.create(
                model=self.settings.claude_model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": prompt}],
                timeout=90.0,
            )
            u = response.usage
            cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
            self.token_tracker.record(TokenUsage(
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                cache_creation_input_tokens=cache_write,
                cache_read_input_tokens=cache_read,
                phase=phase,
                provider="anthropic",
            ))
            logger.info(
                f"LLM [{phase}]: in={u.input_tokens} out={u.output_tokens} "
                f"cache_write={cache_write} cache_read={cache_read}"
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
        sections: list[SectionAnalysis],
    ) -> dict:
        """Build synthesis from analyzed sections."""
        # Collect parameter evolution timeline from sections
        param_timeline = self._collect_parameter_timeline(sections)

        # Build evolution table from per-section operational parameters
        # Use date_debut_vigueur for precise periods when available
        evolution = []
        for section in sections:
            mod = section.modification
            if mod.date_debut_vigueur and mod.date_fin_vigueur and mod.date_fin_vigueur != "2999-01-01":
                periode = f"{mod.date_debut_vigueur} → {mod.date_fin_vigueur}"
            elif mod.date_debut_vigueur:
                periode = f"{mod.date_debut_vigueur} → en vigueur"
            else:
                periode = f"{mod.law_year}"
            params = {"nature": mod.nature.value}

            if section.operational_params and section.operational_params.parameters:
                params.update(section.operational_params.parameters)

            evolution.append(
                SynthesisRow(periode=periode, parameters=params)
            )

        return {
            "evolution": evolution,
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
                # Direct DB search instead of search_article_modification (which needs law_type)
                date_start = date(start_year, 1, 1)
                date_end = date(end_year, 12, 31)

                debate_chunks = []
                try:
                    with get_session() as session:
                        # Use contextualized pattern for short article refs
                        if _is_short_article_ref(article_info.reference):
                            pattern = f"%article {article_info.reference}%"
                        else:
                            pattern = f"%{article_info.reference}%"

                        interventions = (
                            session.query(Intervention)
                            .join(Debat, Intervention.debat_id == Debat.id)
                            .filter(
                                Debat.date >= date_start,
                                Debat.date <= date_end,
                                Intervention.contenu.ilike(pattern),
                            )
                            .order_by(Debat.date)
                            .limit(20)
                            .all()
                        )

                        # Format interventions as chunks for the consolidation prompt
                        for inter in interventions:
                            debat = session.query(Debat).filter(Debat.id == inter.debat_id).first()
                            if debat and inter.contenu:
                                chunk = Chunk(
                                    content=inter.contenu[:2000],
                                    metadata={
                                        "orateur": inter.orateur or "",
                                        "chambre": debat.chambre or "",
                                        "etape": debat.etape or "",
                                        "date": debat.date.isoformat() if debat.date else "",
                                    },
                                    chunk_type="intervention",
                                    source_id=str(debat.id),
                                    position=inter.ordre or 0,
                                )
                                debate_chunks.append((chunk, 1.0))

                        logger.info(
                            f"Consolidation search: {len(debate_chunks)} interventions "
                            f"mentioning {article_info.reference} in {start_year}-{end_year}"
                        )
                except Exception as e:
                    logger.warning(f"Consolidation DB search failed: {e}")

                # Filter to substantive chunks (not purely procedural, >100 chars)
                substantive_chunks = [
                    (chunk, score) for chunk, score in debate_chunks
                    if len(chunk.content) > 100
                    and not any(p in chunk.content.lower()[:80] for p in (
                        "la parole est à", "je mets aux voix", "l'amendement est",
                        "la séance est", "il est procédé",
                    ))
                ]

                if len(substantive_chunks) >= 2:
                    consolidation = self._analyze_consolidation_period(
                        article_info, start_year, end_year, substantive_chunks
                    )
                elif substantive_chunks:
                    logger.info(
                        f"Skipping consolidation LLM for {start_year}-{end_year}: "
                        f"only {len(substantive_chunks)} substantive chunk(s) (need ≥2)"
                    )
                    consolidation = None
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
            self.token_tracker.set_phase("consolidation")
            response_text = self._call_llm(prompt, system_override=SYSTEM_PROMPT_LEGAL_SHORT)
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

    # Hard procedural: always reject (purely procedural, no substance)
    HARD_PROCEDURAL_PATTERNS = [
        "la parole est à",
        "je mets aux voix",
        "il en est ainsi décidé",
        "la séance est levée",
        "la séance est suspendue",
        "la séance est reprise",
        "je vous remercie",
        "rappel au règlement",
        "aucune citation pertinente",
        "prochaine séance",
        "l'ordre du jour appelle",
        "nous reprenons la discussion",
        "nous abordons l'examen",
        "nous passons à l'examen",
    ]

    # Soft procedural: reject only if citation is short (<80 chars)
    # In long citations these are just context fragments, not the main content
    SOFT_PROCEDURAL_PATTERNS = [
        "l'article est ainsi modifié",
        "l'article est ainsi rédigé",
        "l'amendement est adopté",
        "l'amendement est rejeté",
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
        "ces amendements sont ainsi libellés",
        "ces deux amendements sont ainsi libellés",
        "cet amendement est ainsi libellé",
        "sanctions, garanties, sûretés",
    ]

    def _is_procedural_quote(self, citation: str) -> bool:
        """Check if a quote is primarily procedural content."""
        # Normalize whitespace (newlines in debate text break pattern matching)
        citation_lower = re.sub(r'\s+', ' ', citation.lower())
        # Hard patterns: always reject
        for pattern in self.HARD_PROCEDURAL_PATTERNS:
            if pattern in citation_lower:
                return True
        # Soft patterns: reject only if citation is short (purely procedural)
        if len(citation) < 80:
            for pattern in self.SOFT_PROCEDURAL_PATTERNS:
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
        # Check for article-specific mechanism keywords (with singular/plural tolerance)
        matches = sum(1 for kw in article_keywords if _keyword_matches(kw, citation_lower))
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

        Validates per-chunk (not against concatenated corpus) to prevent cross-chunk
        false matches. Corrects chamber and fonction from matched chunk metadata.
        Re-sorts sources into sources_an/sources_senat by corrected chamber.
        """
        original_an_count = len(section.sources_an)
        original_senat_count = len(section.sources_senat)
        article_ref_lower = article_ref.lower() if article_ref else ""
        tier_stats = {1: 0, 2: 0, 3: 0, "rejected": 0}
        orateur_mismatches = 0
        chamber_corrections = 0

        def _validate_and_correct(sources):
            nonlocal orateur_mismatches, chamber_corrections
            validated = []
            for source in sources:
                # Skip procedural check if citation explicitly mentions our article
                if not (article_ref_lower and article_ref_lower in source.citation.lower()):
                    if self._is_procedural_quote(source.citation):
                        logger.info(f"[validation] REJECT procedural: {source.citation[:80]}...")
                        tier_stats["rejected"] += 1
                        continue

                if article_ref and not self._quote_is_relevant(
                    source.citation, article_ref, article_keywords,
                ):
                    logger.info(f"[validation] REJECT irrelevant: {source.citation[:80]}...")
                    tier_stats["rejected"] += 1
                    continue

                result = self._quote_matches_sources(
                    source.citation, source_chunks, cited_orateur=source.orateur,
                )

                if result["matched"]:
                    tier = result["tier"]
                    tier_stats[tier] = tier_stats.get(tier, 0) + 1

                    # --- Chamber/fonction correction from matched chunk metadata ---
                    corrected_chambre = source.chambre
                    corrected_fonction = source.fonction
                    chunk_idx = result["chunk_idx"]

                    if 0 <= chunk_idx < len(source_chunks):
                        matched_chunk = source_chunks[chunk_idx][0]
                        real_chambre = (matched_chunk.metadata.get("chambre") or "").strip()
                        real_fonction = (matched_chunk.metadata.get("fonction") or "").strip()

                        # Correct chamber from chunk metadata
                        if real_chambre:
                            chambre_upper = real_chambre.upper().replace("É", "E")
                            if chambre_upper in ("SENAT", "SÉNAT", "SENAT"):
                                corrected_chambre = "Sénat"
                            elif chambre_upper == "AN":
                                corrected_chambre = "AN"

                        # Correct fonction from chunk metadata (prefer DB over LLM)
                        if real_fonction:
                            corrected_fonction = real_fonction

                    # Log correction if chamber changed
                    if corrected_chambre != source.chambre:
                        chamber_corrections += 1
                        logger.info(
                            f"[validation] CHAMBER FIX: {source.orateur} "
                            f"{source.chambre} → {corrected_chambre} "
                            f"(chunk={chunk_idx})"
                        )

                    if corrected_fonction != source.fonction:
                        logger.info(
                            f"[validation] FONCTION FIX: {source.orateur} "
                            f"'{source.fonction}' → '{corrected_fonction}' "
                            f"(chunk={chunk_idx})"
                        )

                    # Log orateur mismatch
                    if not result["orateur_match"] and source.orateur:
                        orateur_mismatches += 1
                        logger.warning(
                            f"[validation] ORATEUR MISMATCH tier={tier}: "
                            f"cited={source.orateur}, chunk_idx={chunk_idx} | "
                            f"{source.citation[:60]}..."
                        )
                    else:
                        logger.debug(
                            f"[validation] MATCH tier={tier} chunk={chunk_idx}: "
                            f"{source.orateur} ({corrected_chambre}) | {source.citation[:60]}..."
                        )

                    # Create corrected source
                    corrected_source = QuotedSource(
                        citation=source.citation,
                        orateur=source.orateur,
                        fonction=corrected_fonction,
                        date=source.date,
                        chambre=corrected_chambre,
                        is_curated=source.is_curated,
                    )
                    validated.append(corrected_source)
                else:
                    logger.warning(
                        f"[validation] REJECT no-match: "
                        f"{source.orateur} | {source.citation[:80]}..."
                    )
                    tier_stats["rejected"] += 1
            return validated

        # Merge all sources and validate together, then re-sort by corrected chamber
        all_sources = list(section.sources_an) + list(section.sources_senat)
        all_validated = _validate_and_correct(all_sources)

        # Re-sort into AN/Sénat by corrected chamber
        section.sources_an = [s for s in all_validated if s.chambre != "Sénat"]
        section.sources_senat = [s for s in all_validated if s.chambre == "Sénat"]

        # Log validation summary
        validated_count = len(section.sources_an) + len(section.sources_senat)
        original_count = original_an_count + original_senat_count
        logger.info(
            f"[validation] Summary: {validated_count}/{original_count} validated | "
            f"tier1={tier_stats.get(1, 0)} tier2={tier_stats.get(2, 0)} "
            f"tier3={tier_stats.get(3, 0)} rejected={tier_stats.get('rejected', 0)} "
            f"orateur_mismatches={orateur_mismatches} chamber_corrections={chamber_corrections}"
        )

        # Downgrade confidence if many quotes were removed
        if original_count > 0 and validated_count == 0:
            section.niveau_certitude = CertitudeLevel.FAIBLE
        elif original_count > 0 and validated_count < original_count / 2:
            if section.niveau_certitude == CertitudeLevel.ELEVE:
                section.niveau_certitude = CertitudeLevel.MOYEN

        return section

    def _quote_matches_sources(
        self,
        citation: str,
        source_chunks: list,
        cited_orateur: str = "",
    ) -> dict:
        """Check if a citation matches source text using tiered per-chunk validation.

        Validates against individual chunks (not concatenated corpus) to prevent
        cross-chunk false matches. Also checks orateur consistency.

        Returns:
            dict with keys:
            - matched: bool
            - tier: int (0=no match, 1=exact, 2=window, 3=overlap)
            - chunk_idx: index of matched chunk or -1
            - orateur_match: bool (True if cited orateur matches chunk's orateur)
        """
        def _normalize_chars(text: str) -> str:
            """Normalize special characters for comparison."""
            return (text.lower().strip()
                    .replace("\u2019", "'").replace("\u2018", "'")  # smart quotes → '
                    .replace("\u00ab", '"').replace("\u00bb", '"')  # « » → "
                    .replace("\u2013", "-").replace("\u2014", "-")  # em/en dash → -
                    .replace("\xa0", " ")  # non-breaking space
                    .replace("  ", " "))

        citation_lower = _normalize_chars(citation)
        if not citation_lower:
            return {"matched": False, "tier": 0, "chunk_idx": -1, "orateur_match": False}

        citation_words = re.findall(r'\w+', citation_lower)
        significant_words = [w for w in citation_words if len(w) > 3]

        best_result = {"matched": False, "tier": 0, "chunk_idx": -1, "orateur_match": False}

        for idx, (chunk, _score) in enumerate(source_chunks):
            chunk_lower = _normalize_chars(chunk.content)

            # Check orateur match for this chunk
            chunk_orateur = (chunk.metadata.get("orateur") or "").lower()
            cited_lower = cited_orateur.lower() if cited_orateur else ""
            orateur_ok = False
            if cited_lower and chunk_orateur:
                # Extract last significant name word for fuzzy match
                cited_words = [w for w in re.sub(r'[,.]', '', cited_lower).split()
                               if len(w) >= 3 and w not in ("mme", "monsieur", "madame")]
                chunk_words = [w for w in re.sub(r'[,.]', '', chunk_orateur).split()
                               if len(w) >= 3 and w not in ("mme", "monsieur", "madame")]
                if cited_words and chunk_words:
                    orateur_ok = cited_words[-1] in chunk_orateur or chunk_words[-1] in cited_lower

            # Tier 1: Full exact substring match
            if citation_lower in chunk_lower:
                return {"matched": True, "tier": 1, "chunk_idx": idx, "orateur_match": orateur_ok}

            # Tier 2: Contiguous 8-word sliding window match
            window_size = 8
            if len(citation_words) >= window_size:
                for i in range(len(citation_words) - window_size + 1):
                    window = " ".join(citation_words[i:i + window_size])
                    if window in chunk_lower:
                        return {"matched": True, "tier": 2, "chunk_idx": idx, "orateur_match": orateur_ok}

            # Tier 3: Word overlap per chunk (not corpus-wide)
            if significant_words:
                matched_count = sum(1 for w in significant_words if w in chunk_lower)
                overlap = matched_count / len(significant_words)
                if overlap >= self.settings.quote_word_overlap_threshold:
                    # Keep as candidate but prefer higher tiers from other chunks
                    if not best_result["matched"] or best_result["tier"] > 3:
                        best_result = {"matched": True, "tier": 3, "chunk_idx": idx, "orateur_match": orateur_ok}

        return best_result

    @staticmethod
    def _extract_sentence_from_passage_cle(
        chunk_content: str, passage_cle: str, max_length: int = 600
    ) -> str:
        """Extract the full sentence containing passage_cle from chunk content.

        1. Strip inline metadata headers (e.g., "Nom (fonction): " prefix)
        2. Case-insensitive substring search for passage_cle (whitespace-normalized)
        3. If found, expand to sentence boundaries (. ! ? \\n or start/end of text)
        4. If not found, try progressively shorter prefixes
        5. Clean up: ensure citation starts at a word boundary (uppercase or after punctuation)
        6. Fallback: first 500 chars of chunk (word-boundary aware)
        """
        if not passage_cle or not chunk_content:
            return _truncate_at_word(chunk_content, 500) if chunk_content else ""

        # Strip inline metadata headers from chunk content AND passage_cle
        # Patterns: "Nom (fonction): text" or "[Nom (fonction): text...]"
        # Chunks may have multiple stacked headers:
        # "[Mme la présidente: La parole est à...]\nM. Dupont (rapporteur): Le texte..."
        _header_re = re.compile(r'^(?:\[)?[A-ZÀ-Ü][^:]{3,60}(?:\([^)]+\))?\s*:\s*')
        _bracket_block_re = re.compile(r'^\[.*?\]\.{0,4}\s*', re.DOTALL)

        clean_content = chunk_content
        # Strip bracketed procedural block first: [Mme la présidente: La parole est à...]
        bracket_match = _bracket_block_re.match(clean_content)
        if bracket_match:
            clean_content = clean_content[bracket_match.end():]
        # Strip remaining header (the actual speaker): M. Dupont (fonction):
        for _ in range(2):  # max 2 iterations for nested headers
            header_match = _header_re.match(clean_content)
            if header_match:
                clean_content = clean_content[header_match.end():]
            else:
                break

        # Also strip header from passage_cle (LLM may include it)
        clean_passage = passage_cle
        for _ in range(2):
            key_header = _header_re.match(clean_passage)
            if key_header:
                clean_passage = clean_passage[key_header.end():]
            else:
                break

        # Normalize whitespace for matching: \n, \t, multiple spaces → single space
        # This fixes the main cause of fallback (80% of truncated citations):
        # LLM strips \n from passage_cle but chunk content contains \n
        content_normalized = re.sub(r'\s+', ' ', clean_content)
        key_normalized = re.sub(r'\s+', ' ', clean_passage).strip()

        content_lower = content_normalized.lower()
        key_lower = key_normalized.lower()

        # Try exact match first (on whitespace-normalized text)
        pos = content_lower.find(key_lower)

        # Fuzzy: try progressively shorter word prefixes
        if pos == -1:
            words = key_lower.split()
            for n in range(len(words) - 1, max(2, len(words) // 2) - 1, -1):
                sub = " ".join(words[:n])
                pos = content_lower.find(sub)
                if pos != -1:
                    break

        if pos == -1:
            # Last resort: return first 500 chars of clean content (word-boundary aware)
            return _truncate_at_word(clean_content, 500)

        # From here on, work with the normalized content (single-space version)
        clean_content = content_normalized
        clean_passage = key_normalized

        # Expand backward to sentence start (. or newline; skip ! ? inside parentheticals)
        start = pos
        for i in range(pos - 1, max(pos - 500, -1), -1):
            if i < 0:
                start = 0
                break
            if clean_content[i] == '\n':
                start = i + 1
                break
            if clean_content[i] == '.' and i + 1 < len(clean_content):
                next_char = clean_content[i + 1]
                if next_char in ' \n\t' and next_char != '\n':
                    # Check next non-space char is uppercase (real sentence start)
                    rest = clean_content[i + 1:i + 4].lstrip()
                    if rest and rest[0].isupper():
                        start = i + 2
                        break
                elif next_char == '\n':
                    start = i + 2
                    break
        else:
            start = max(0, pos - 50)

        # Expand forward to sentence end
        # Only treat '.' as sentence-ender if followed by space+uppercase
        # (avoids cutting on "art.", "n°", "M." etc.)
        end = pos + len(clean_passage)
        for i in range(end, min(end + 500, len(clean_content))):
            if clean_content[i] == '\n':
                end = i
                break
            if clean_content[i] in '!?':
                end = i + 1
                break
            if clean_content[i] == '.':
                if i + 2 < len(clean_content):
                    next_stripped = clean_content[i + 1:i + 3].lstrip()
                    if next_stripped and next_stripped[0].isupper():
                        end = i + 1
                        break
                elif i + 1 >= len(clean_content):
                    end = i + 1  # End of text
                    break
        else:
            end = min(len(clean_content), end + 100)

        sentence = clean_content[start:end].strip()

        # Clean up: strip leading em-dash fragments ("- text" from mid-parenthetical)
        sentence = re.sub(r'^[\s\-–-]+', '', sentence)

        # If sentence starts lowercase (mid-sentence), try to expand back to start of clean_content
        if sentence and sentence[0].islower() and not sentence.startswith(("l'", "d'", "n'", "s'", "j'", "qu'")):
            # If we're close to the start, just use the full clean_content
            if start < 80:
                sentence = clean_content[:end].strip()
                sentence = re.sub(r'^[\s\-–-]+', '', sentence)
            else:
                # Try to find next uppercase letter or sentence start
                for i, ch in enumerate(sentence):
                    if ch.isupper() or (i > 0 and sentence[i - 1] in '.!?\n'):
                        sentence = sentence[i:]
                        break

        # Final header cleanup: strip any remaining "Nom (fonction): " prefix
        final_header = _header_re.match(sentence)
        if final_header:
            sentence = sentence[final_header.end():]

        # Truncate if too long (word-boundary aware, never mid-word)
        if len(sentence) > max_length:
            sentence = _truncate_at_word(sentence, max_length)

        return sentence

    def _reconstruct_sources_from_refs(
        self,
        raw_sources: list[dict],
        chunk_mapping: dict[str, tuple],
        mod_date: date | None = None,
    ) -> tuple[list[QuotedSource], list[QuotedSource]]:
        """Reconstruct QuotedSource objects from LLM extrait_id references.

        The LLM outputs references like {"extrait_id": "EXTRAIT-3", "passage_cle": "premiers mots"}.
        We look up the chunk by ID, extract metadata (orateur, chambre, fonction, date)
        from the chunk, and reconstruct the full citation from the passage_cle.

        Args:
            mod_date: Date of the law modification. Sources >300 days before are skipped
                (likely from a previous law's debates).

        Returns:
            Tuple of (sources_an, sources_senat) sorted by chunk chambre metadata.
        """
        sources_an = []
        sources_senat = []

        for ref in raw_sources:
            extrait_id = ref.get("extrait_id", "")
            passage_cle = ref.get("passage_cle", "")

            if not extrait_id:
                logger.warning("[reconstruct] Skipping source with empty extrait_id")
                continue

            if extrait_id not in chunk_mapping:
                logger.warning(f"[reconstruct] Unknown extrait_id: {extrait_id}, skipping")
                continue

            chunk, score = chunk_mapping[extrait_id]
            metadata = chunk.metadata

            # Extract metadata from chunk (guaranteed correct from DB)
            # For amendment chunks, orateur may be empty - fall back to auteur
            # Reject single-char auteur (e.g. "N" from Sénat parsing artifacts)
            raw_orateur = metadata.get("orateur", "")
            raw_auteur = metadata.get("auteur", "")
            if raw_auteur and len(raw_auteur.strip()) <= 2:
                raw_auteur = ""  # "N", "n" etc. are parsing artifacts
            orateur = raw_orateur or raw_auteur
            fonction = metadata.get("fonction", "")
            date_str = metadata.get("date", "")

            # Distance guard: skip sources >300 days before modification (likely previous law)
            if mod_date and date_str:
                try:
                    source_date = date.fromisoformat(date_str[:10])
                    days_gap = (mod_date - source_date).days
                    if days_gap > 300:
                        logger.info(
                            f"[reconstruct] Skipping {orateur or '?'} ({date_str[:10]}, "
                            f"{days_gap}d > 300) - likely from previous law"
                        )
                        continue
                except (ValueError, TypeError):
                    pass

            chambre_raw = (metadata.get("chambre") or "").upper().replace("\u00c9", "E")

            if chambre_raw in ("SENAT", "S\u00c9NAT"):
                chambre = "S\u00e9nat"
            elif chambre_raw == "AN":
                chambre = "AN"
            else:
                chambre = chambre_raw or "AN"  # default to AN if unknown

            # Normalize and fallback fonction
            # "president" from chunk metadata is often a false positive (the speaker
            # mentions "le président" in their speech but isn't the president themselves)
            # "autre" is uninformative - prefer chambre-based fallback
            _UNRELIABLE_FONCTIONS = {"president", "autre", "other", ""}
            if not fonction or fonction.lower() in _UNRELIABLE_FONCTIONS:
                # Check if the chunk content header reveals the real role
                # e.g., "[M. Dupont (rapporteur général):" at the start of the chunk
                role_match = re.search(
                    r'\((?:rapporteur[^)]*|ministre[^)]*|secrétaire d\'[eéÉ]tat[^)]*)\)',
                    chunk.content[:200], re.IGNORECASE,
                )
                if role_match:
                    fonction = role_match.group(0).strip("()")
                else:
                    fonction = "sénateur" if chambre == "Sénat" else "député"

            # Extract full citation from passage_cle
            citation = self._extract_sentence_from_passage_cle(
                chunk.content, passage_cle
            )

            source = QuotedSource(
                citation=citation,
                orateur=orateur,
                fonction=fonction,
                date=date_str,
                chambre=chambre,
                passage_cle=passage_cle,
                extrait_id=extrait_id,
            )

            logger.info(
                f"[reconstruct] {extrait_id} -> {chambre} {orateur}: {citation[:80]}..."
            )

            if chambre == "S\u00e9nat":
                sources_senat.append(source)
            else:
                sources_an.append(source)

        return sources_an, sources_senat

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
            elif any(
                kw in section_lower
                for kw in ("actu bofip", "bofip", "doctrine administrative")
            ):
                label = "### Doctrine administrative (BOFiP)"
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
                # 1. Parliamentary reports
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

                # 2. ACTU BOFiP (official law change summaries)
                actu_sources = (
                    session.query(SourceExterne)
                    .filter(
                        SourceExterne.type == "bofip",
                        SourceExterne.reference.like("ACTU-%"),
                    )
                    .filter(
                        or_(
                            SourceExterne.contenu.ilike(f"%{law_numero}%"),
                            SourceExterne.titre.ilike(f"%{law_numero}%"),
                        )
                    )
                    .order_by(SourceExterne.date.desc())
                    .limit(3)
                    .all()
                )

                # Pick best ACTU: prefer the one mentioning both law and article
                best_actu = None
                best_score = -1
                for actu in actu_sources:
                    actu_text = actu.contenu or ""
                    score = 0
                    if law_numero in actu_text:
                        score += 2
                    if article_ref.lower() in actu_text.lower():
                        score += 1
                    if score > best_score:
                        best_actu = actu
                        best_score = score

                article_patterns = [
                    article_ref.lower(),
                    article_ref.lower().replace("-", " "),
                    f"article {article_ref}".lower(),
                ]

                parts = []
                current_chars = 0
                for src in sources:
                    text = src.contenu or src.resume
                    if not text:
                        continue

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

                # Append ACTU BOFiP (use remaining budget, max 2000 chars)
                if best_actu and current_chars < max_chars:
                    actu_text = best_actu.contenu or ""
                    actu_paras = actu_text.split("\n\n")
                    relevant_actu = [
                        p for p in actu_paras
                        if any(pat in p.lower() for pat in article_patterns) or law_numero in p
                    ]
                    if relevant_actu:
                        actu_text = "\n\n".join(relevant_actu)
                    actu_budget = min(2000, max_chars - current_chars)
                    actu_text = actu_text[:actu_budget]
                    if actu_text.strip():
                        parts.append(f"[ACTU BOFiP: {best_actu.titre}]\n{actu_text}")
                        logger.info(f"[expose] Added ACTU BOFiP ({len(actu_text)} chars) for {law_numero}")

                return "\n\n---\n\n".join(parts) if parts else ""
        except Exception as e:
            logger.debug(f"Could not fetch exposé des motifs: {e}")
            return ""

    def _fetch_jurisprudence_from_db(
        self,
        article_ref: str,
        max_results: int = 10,
    ) -> list[dict]:
        """Fetch verified CE/CC decisions from DB for a CGI article.

        Replaces LLM-hallucinated jurisprudence with real DB entries.
        For CE decisions, skips procedural preamble.
        """
        try:
            with get_session() as session:
                sources = (
                    session.query(SourceExterne)
                    .filter(
                        SourceExterne.type == "jurisprudence",
                        SourceExterne.articles_cgi_lies.cast(String).ilike(
                            f"%{article_ref}%"
                        ),
                    )
                    .order_by(SourceExterne.date.desc())
                    .limit(max_results)
                    .all()
                )

                results = []
                for src in sources:
                    contenu = src.contenu or ""
                    # CE: skip procedural preamble, start from "Considérant"
                    if src.chambre == "CE" and contenu:
                        match = re.search(r"(?:Considérant|Sur le )", contenu)
                        if match:
                            contenu = contenu[match.start():]
                    contenu = contenu[:1500]

                    results.append({
                        "juridiction": src.chambre or "",
                        "date": src.date.strftime("%Y-%m-%d") if src.date else "",
                        "reference": src.reference or "",
                        "titre": src.titre or "",
                        "contenu_extrait": contenu,
                        "url": src.url or "",
                    })

                return results
        except Exception as e:
            logger.debug(f"Could not fetch jurisprudence from DB: {e}")
            return []

    def _fetch_relevant_jurisprudence(
        self,
        article_ref: str,
        mod_date: "date",
        max_decisions: int = 2,
        max_chars_per_decision: int = 1000,
    ) -> str:
        """Fetch CE/CC decisions relevant to a specific modification.

        Temporal filter: decisions rendered within 2 years BEFORE the modification.
        Returns formatted text for inclusion in the LLM prompt.

        Note: the v2.4 prompt change (mandatory doctrine_references extraction
        in SYSTEM_PROMPT_LEGAL section 4) handles the recall gap on its own;
        the earlier attempt to also widen the temporal window AND raise the
        cap to 4 regressed avg_confidence on the reference (post-mod
        decisions sometimes contradict the original intent, lowering the
        LLM's certitude judgment). Kept narrow here.
        """
        try:
            from datetime import timedelta

            date_start = mod_date - timedelta(days=730)
            date_end = mod_date

            with get_session() as session:
                sources = (
                    session.query(SourceExterne)
                    .filter(
                        SourceExterne.type == "jurisprudence",
                        SourceExterne.articles_cgi_lies.cast(String).ilike(
                            f"%{article_ref}%"
                        ),
                        SourceExterne.date >= date_start,
                        SourceExterne.date <= date_end,
                    )
                    .order_by(SourceExterne.date.desc())
                    .limit(max_decisions)
                    .all()
                )

                if not sources:
                    return ""

                parts = []
                for src in sources:
                    contenu = src.contenu or ""
                    if src.chambre == "CE" and contenu:
                        match = re.search(r"(?:Considérant|Sur le )", contenu)
                        if match:
                            contenu = contenu[match.start():]
                    contenu = contenu[:max_chars_per_decision]

                    date_str = src.date.strftime("%Y-%m-%d") if src.date else "date inconnue"
                    header = f"[{src.chambre} {date_str} - n°{src.reference}]"
                    parts.append(f"{header}\n{contenu}")

                return "\n\n---\n\n".join(parts)
        except Exception as e:
            logger.debug(f"Could not fetch relevant jurisprudence: {e}")
            return ""

    def _fetch_bofip_summary(
        self,
        article_ref: str,
        max_chars: int = 2000,
    ) -> str:
        """Fetch main BOFiP doctrine doc for system prompt (cached by Anthropic).

        Queries BOI (non-ACTU) documents linked to the article.
        Prioritizes: titre match > articles_cgi_lies match.
        """
        try:
            with get_session() as session:
                # Priority 1: titre contains article ref
                by_titre = (
                    session.query(SourceExterne)
                    .filter(
                        SourceExterne.type == "bofip",
                        ~SourceExterne.reference.like("ACTU-%"),
                        SourceExterne.titre.ilike(f"%{article_ref}%"),
                    )
                    .limit(3)
                    .all()
                )

                # Priority 2: articles_cgi_lies contains article ref
                if not by_titre:
                    by_titre = (
                        session.query(SourceExterne)
                        .filter(
                            SourceExterne.type == "bofip",
                            ~SourceExterne.reference.like("ACTU-%"),
                            SourceExterne.articles_cgi_lies.cast(String).ilike(
                                f"%{article_ref}%"
                            ),
                        )
                        .limit(3)
                        .all()
                    )

                if not by_titre:
                    return ""

                article_patterns = [
                    article_ref.lower(),
                    article_ref.lower().replace("-", " "),
                ]

                parts = []
                current_chars = 0
                for src in by_titre:
                    text = src.contenu or src.resume or ""
                    if not text:
                        continue
                    # Extract paragraphs mentioning the article
                    # BOFiP text may use single \n or \n\n as separator
                    paragraphs = text.split("\n\n") if "\n\n" in text else text.split("\n")
                    relevant = [
                        p for p in paragraphs
                        if any(pat in p.lower() for pat in article_patterns)
                    ]
                    if relevant:
                        text = "\n\n".join(relevant[:3])
                    else:
                        text = text[:500]

                    entry = f"[{src.titre}]\n{text}"
                    if current_chars + len(entry) > max_chars:
                        break
                    parts.append(entry)
                    current_chars += len(entry)

                return "\n\n".join(parts) if parts else ""
        except Exception as e:
            logger.debug(f"Could not fetch BOFiP summary: {e}")
            return ""

    def _get_debate_refs_for_law(self, law_numero: str) -> list[str]:
        """Get debate IDs (as strings) linked to a law via dossier.

        Returns str(debat.id) to match chunk_embeddings.source_id format.
        """
        try:
            with get_session() as session:
                refs = (
                    session.query(Debat.id)
                    .join(DossierLegislatif, Debat.dossier_id == DossierLegislatif.id)
                    .join(Loi, DossierLegislatif.loi_id == Loi.id)
                    .filter(Loi.numero == law_numero)
                    .all()
                )
                return [str(r[0]) for r in refs]
        except Exception as e:
            logger.debug(f"Could not fetch debate refs for law {law_numero}: {e}")
            return []

    def _resolve_dossiers_for_modification(self, law_numero: str) -> dict[str, dict]:
        """Resolve per-chamber dossier info with tight date windows from actual debates.

        Returns dict keyed by chamber ("AN", "SENAT") with:
            dossier_id, debate_ids, etapes, date_start, date_end

        For chambers missing a dossier (orphan-law case), returns a stub entry
        with dossier_id=None, debate_ids=[] and a date window derived from the
        law's promulgation date ([prom - 540j, prom + 30j]). The downstream
        chamber search uses this window to fall back to a DB-wide search.
        """
        try:
            with get_session() as db:
                loi = db.query(Loi).filter(Loi.numero == law_numero).first()
                if not loi:
                    return {}
                dossiers = db.query(DossierLegislatif).filter(
                    DossierLegislatif.loi_id == loi.id
                ).all()

                result: dict[str, dict] = {}
                for dos in dossiers:
                    chambre = dos.chambre.upper()
                    # Normalize Sénat variants
                    if chambre in ("SÉNAT", "SENAT"):
                        chambre = "SENAT"

                    date_range = db.query(
                        func.min(Debat.date), func.max(Debat.date)
                    ).filter(Debat.dossier_id == dos.id).first()

                    debate_ids = [
                        str(r[0]) for r in
                        db.query(Debat.id).filter(Debat.dossier_id == dos.id).all()
                    ]

                    if date_range and date_range[0]:
                        ds = date_range[0]
                        de = date_range[1] or date_range[0]
                        # Convert datetime to date if needed
                        if hasattr(ds, "date"):
                            ds = ds.date()
                        if hasattr(de, "date"):
                            de = de.date()
                        d_start = ds - timedelta(days=7)
                        d_end = de + timedelta(days=7)
                    else:
                        # Fallback: law promulgation window
                        prom = loi.date_promulgation
                        if hasattr(prom, "date"):
                            prom = prom.date()
                        d_start = prom - timedelta(days=180)
                        d_end = prom + timedelta(days=7)

                    result[chambre] = {
                        "dossier_id": dos.id,
                        "debate_ids": debate_ids,
                        "etapes": dos.etapes or [],
                        "date_start": d_start,
                        "date_end": d_end,
                    }

                # Orphan-law fallback: for any chamber without a dossier, emit
                # a stub with the law-promulgation-derived window so the search
                # can run a DB-wide date-scoped fallback.
                prom = loi.date_promulgation
                if prom is not None and hasattr(prom, "date"):
                    prom = prom.date()
                for chambre in ("AN", "SENAT"):
                    if chambre in result:
                        continue
                    if prom is None:
                        continue
                    result[chambre] = {
                        "dossier_id": None,
                        "debate_ids": [],
                        "etapes": [],
                        "date_start": prom - timedelta(days=540),
                        "date_end": prom + timedelta(days=30),
                        "orphan_fallback": True,
                    }

                logger.info(
                    f"Dossier resolution for {law_numero}: "
                    + ", ".join(
                        f"{ch}={len(v['debate_ids'])} debates "
                        f"[{v['date_start']}→{v['date_end']}]"
                        + (" (orphan-fallback)" if v.get("orphan_fallback") else "")
                        for ch, v in result.items()
                    )
                )
                return result
        except Exception as e:
            logger.warning(f"Dossier resolution failed for {law_numero}: {e}")
            return {}

    @staticmethod
    def _identify_sibling_modifications(
        modifications: list[LawModification],
        target_mod: LawModification,
    ) -> list[LawModification]:
        """Find sibling modifications: same law_numero, different from target.

        Used for sibling competition scoring to avoid cross-modification contamination.
        """
        return [
            m for m in modifications
            if m.law_numero == target_mod.law_numero
            and m is not target_mod
            and m.date == target_mod.date  # Same law = same promulgation date
        ]

    @staticmethod
    def _extract_sibling_discriminants(
        target_mod: LawModification,
        siblings: list[LawModification],
    ) -> tuple[list[str], list[str]]:
        """Extract discriminant terms between target and sibling modifications.

        Returns:
            (positive_terms, negative_terms) where:
            - positive_terms: terms specific to target's objet
            - negative_terms: terms specific to siblings' objets (penalize if found)
        """
        if not siblings:
            return [], []

        # Extract significant words from objet fields
        def _extract_terms(objet: str) -> set[str]:
            if not objet:
                return set()
            # Clean encoding artifacts, keep meaningful words
            clean = re.sub(r'[«»""()\[\]0-9;,.]', ' ', objet)
            words = clean.lower().split()
            stopwords = {
                "de", "du", "des", "le", "la", "les", "un", "une", "et", "ou",
                "en", "au", "aux", "par", "pour", "dans", "sur", "avec", "est",
                "qui", "que", "ce", "cette", "son", "ses", "leur", "ajout",
                "suppression", "modification", "mineure", "mise", "forme",
                "l'article", "article", "alinéa",
            }
            return {w for w in words if len(w) > 4 and w not in stopwords}

        target_terms = _extract_terms(target_mod.objet)
        sibling_terms: set[str] = set()
        for sib in siblings:
            sibling_terms |= _extract_terms(sib.objet)

        # Positive: terms in target but not in siblings
        positive = list(target_terms - sibling_terms)
        # Negative: terms in siblings but not in target
        negative = list(sibling_terms - target_terms)

        return positive, negative

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
                        kw_matches = sum(1 for kw in article_keywords if _keyword_matches(kw, text))
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

