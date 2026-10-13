"""Law-level analyzer for generating comprehensive law analysis documents.

This module provides analysis from a law-centric perspective, showing all CGI
modifications made by a single finance law with their legislative intent.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from src.analysis.cgi_extractor import CGIExtractor, ExtractedModification
from src.analysis.keyword_generator import generate_all_keywords
from src.analysis.models import (
    CertitudeLevel,
    CGIModificationSummary,
    LawAnalysisResult,
    ModificationNature,
)
from src.analysis.prompts import SECTION_EXTRACTION_PROMPT
from src.db.models import AnalysisCache, Article, Loi, get_session
from src.indexing.search import SearchService
from src.utils.config import get_settings
from src.utils.groq_pool import GroqClientPool, get_groq_pool

logger = logging.getLogger(__name__)


class LawAnalyzer:
    """
    Generate comprehensive law analysis documents.

    This analyzer works at the law level, extracting all CGI modifications
    from a finance law and analyzing the legislative intent for each.

    Features:
    - Uses KeywordGenerator for comprehensive search keywords
    - Caches LLM results in AnalysisCache to avoid redundant API calls
    - Uses GroqClientPool for automatic API key rotation on rate limits
    """

    def __init__(
        self,
        search_service: SearchService | None = None,
        use_cache: bool = True,
        cache_ttl_days: int = 30,
        groq_pool: GroqClientPool | None = None,
    ):
        self.settings = get_settings()
        self.search_service = search_service
        self._anthropic_client = None
        self._groq_pool = groq_pool
        self._extractor = CGIExtractor()
        self._lois_reference = None
        self.use_cache = use_cache
        self.cache_ttl_days = cache_ttl_days

        if self.settings.llm_provider == "groq":
            pool = self._get_groq_pool()
            logger.info(f"LawAnalyzer using GroqClientPool with {pool.total_keys_count} API keys")

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

    def _load_lois_reference(self) -> dict:
        """Load the laws reference files (CGI + finance laws overlay)."""
        if self._lois_reference is None:
            laws_by_numero = {}

            # Load CGI laws first (lower priority)
            cgi_path = Path("data/lois_cgi_1958_2026.json")
            if cgi_path.exists():
                with open(cgi_path, encoding="utf-8") as f:
                    data = json.load(f)
                for loi in data.get("lois", []):
                    if loi.get("numero"):
                        laws_by_numero[loi["numero"]] = loi

            # Overlay finance laws (higher priority, curated fields)
            fin_path = Path("data/lois_finances_2012_2025.json")
            if fin_path.exists():
                with open(fin_path, encoding="utf-8") as f:
                    data = json.load(f)
                for loi in data.get("lois", []):
                    if loi.get("numero"):
                        laws_by_numero[loi["numero"]] = loi

            self._lois_reference = laws_by_numero
        return self._lois_reference

    def find_law(
        self,
        law_type: str,
        law_year: int,
    ) -> dict | None:
        """
        Find a law by type and year.

        Args:
            law_type: "LF", "LFR", or "LFSS"
            law_year: Budget year (e.g., 2024)

        Returns:
            Law info dict or None
        """
        ref = self._load_lois_reference()

        for numero, loi in ref.items():
            if loi.get("type") == law_type and loi.get("annee_budgetaire") == law_year:
                return loi

        return None

    def find_law_by_numero(self, numero: str) -> dict | None:
        """Find a law by its numero."""
        ref = self._load_lois_reference()
        return ref.get(numero)

    def analyze_law(
        self,
        law_type: str,
        law_year: int,
        analyze_intent: bool = True,
        max_modifications: int | None = None,
    ) -> LawAnalysisResult:
        """
        Analyze all CGI modifications in a finance law.

        Args:
            law_type: "LF", "LFR", or "LFSS"
            law_year: Budget year
            analyze_intent: Whether to analyze legislative intent with LLM
            max_modifications: Limit number of modifications to analyze

        Returns:
            Complete LawAnalysisResult
        """
        # Find the law
        law_info = self.find_law(law_type, law_year)
        if not law_info:
            raise ValueError(f"Law {law_type} {law_year} not found in reference")

        return self.analyze_law_by_numero(
            law_info["numero"],
            analyze_intent=analyze_intent,
            max_modifications=max_modifications,
        )

    def analyze_law_by_numero(
        self,
        law_numero: str,
        analyze_intent: bool = True,
        max_modifications: int | None = None,
    ) -> LawAnalysisResult:
        """
        Analyze a law by its numero.

        Args:
            law_numero: Law number (e.g., "2023-1322")
            analyze_intent: Whether to analyze legislative intent
            max_modifications: Limit modifications to analyze

        Returns:
            Complete LawAnalysisResult
        """
        if analyze_intent:
            self._ensure_search_service()

        # Get law from database
        with get_session() as session:
            db_law = session.query(Loi).filter(Loi.numero == law_numero).first()
            if not db_law:
                raise ValueError(f"Law {law_numero} not found in database")

            # Get law info from reference
            law_info = self.find_law_by_numero(law_numero) or {}

            # Get all articles for this law
            articles = session.query(Article).filter(Article.loi_id == db_law.id).all()

            logger.info(f"Analyzing {db_law.type} {db_law.annee} with {len(articles)} articles")

            # Extract CGI modifications
            article_dicts = [
                {"numero_article": a.numero_article, "contenu_texte": a.contenu_texte}
                for a in articles
            ]
            law_dict = {
                "numero": db_law.numero,
                "type": db_law.type,
                "annee_budgetaire": db_law.annee,
                "date_promulgation": db_law.date_promulgation.strftime("%Y-%m-%d"),
            }

            extracted_mods = self._extractor.extract_modifications_from_law(
                article_dicts, law_dict
            )

            logger.info(f"Found {len(extracted_mods)} CGI modifications")

            # Limit if requested
            if max_modifications:
                extracted_mods = extracted_mods[:max_modifications]

            # Analyze each modification
            cgi_modifications = []
            for mod in extracted_mods:
                summary = self._analyze_modification(mod, analyze_intent)
                cgi_modifications.append(summary)

            # Detect key themes
            key_themes = self._detect_themes(cgi_modifications)

            return LawAnalysisResult(
                law_numero=db_law.numero,
                law_type=db_law.type,
                law_year=db_law.annee,
                law_title=db_law.titre,
                date_promulgation=db_law.date_promulgation.strftime("%Y-%m-%d"),
                cgi_modifications=cgi_modifications,
                key_themes=key_themes,
                fiscal_context="",  # Could be enriched from debates
                total_articles=len(articles),
                total_cgi_modifications=len(cgi_modifications),
                articles_analyzed=len(extracted_mods),
                generated_date=datetime.now().strftime("%Y-%m-%d"),
                dossier_an_url=law_info.get("dossier_an_url", ""),
                dossier_senat_url=law_info.get("dossier_senat_url", ""),
            )

    def _analyze_modification(
        self,
        mod: ExtractedModification,
        analyze_intent: bool,
    ) -> CGIModificationSummary:
        """Analyze a single CGI modification."""
        # Map nature string to enum
        nature_map = {
            "Création": ModificationNature.CREATION,
            "Modification": ModificationNature.MODIFICATION,
            "Abrogation": ModificationNature.ABROGATION,
        }
        nature = nature_map.get(mod.nature, ModificationNature.MODIFICATION)

        intention_summary = ""
        key_changes = []
        sources_count = 0
        confidence = CertitudeLevel.FAIBLE

        if analyze_intent and self.search_service:
            # Search for debates about this modification
            intention_summary, key_changes, sources_count, confidence_level = self._extract_intent(mod)
            # Map string confidence to enum
            if "élevé" in confidence_level or "eleve" in confidence_level:
                confidence = CertitudeLevel.ELEVE
            elif "moyen" in confidence_level:
                confidence = CertitudeLevel.MOYEN
            else:
                confidence = CertitudeLevel.FAIBLE

        return CGIModificationSummary(
            cgi_article_ref=mod.cgi_article,
            cgi_article_titre=self._get_article_titre(mod.cgi_article),
            article_in_law=mod.article_in_law,
            nature=nature,
            intention_summary=intention_summary,
            key_changes=key_changes,
            sources_count=sources_count,
            confidence=confidence,
        )

    def _check_cache(
        self,
        cgi_article: str,
        law_numero: str,
    ) -> dict | None:
        """Check if we have a cached analysis result."""
        if not self.use_cache:
            return None

        with get_session() as session:
            cached = session.query(AnalysisCache).filter(
                AnalysisCache.cgi_article == cgi_article,
                AnalysisCache.law_numero == law_numero,
            ).first()

            if cached:
                # Check expiration
                if cached.expires_at and cached.expires_at < datetime.now():
                    logger.debug(f"Cache expired for {cgi_article} in {law_numero}")
                    session.delete(cached)
                    session.commit()
                    return None

                logger.info(f"Cache hit for {cgi_article} in {law_numero}")
                return cached.analysis_json

            return None

    def _save_to_cache(
        self,
        cgi_article: str,
        law_numero: str,
        article_in_law: str,
        analysis_result: dict,
        sources_count: int,
        confidence_level: str,
    ) -> None:
        """Save analysis result to cache."""
        if not self.use_cache:
            return

        with get_session() as session:
            try:
                # Check if already exists
                existing = session.query(AnalysisCache).filter(
                    AnalysisCache.cgi_article == cgi_article,
                    AnalysisCache.law_numero == law_numero,
                ).first()

                expires_at = datetime.now() + timedelta(days=self.cache_ttl_days)

                if existing:
                    existing.analysis_json = analysis_result
                    existing.sources_count = sources_count
                    existing.confidence_level = confidence_level
                    existing.expires_at = expires_at
                    existing.llm_model = self.settings.groq_model if self.settings.llm_provider == "groq" else self.settings.claude_model
                else:
                    cache_entry = AnalysisCache(
                        cgi_article=cgi_article,
                        law_numero=law_numero,
                        article_in_law=article_in_law,
                        analysis_json=analysis_result,
                        sources_count=sources_count,
                        confidence_level=confidence_level,
                        created_at=datetime.now(),
                        expires_at=expires_at,
                        embedding_model=self.settings.embedding_model if self.settings.embedding_provider == "openai" else self.settings.local_embedding_model,
                        llm_model=self.settings.groq_model if self.settings.llm_provider == "groq" else self.settings.claude_model,
                    )
                    session.add(cache_entry)

                session.commit()
                logger.debug(f"Cached analysis for {cgi_article} in {law_numero}")
            except Exception as e:
                logger.warning(f"Failed to cache analysis: {e}")
                session.rollback()

    def _extract_intent(
        self,
        mod: ExtractedModification,
    ) -> tuple[str, list[str], int, str]:
        """
        Extract legislative intent for a modification using hybrid search + LLM.

        Uses KeywordGenerator for comprehensive keywords and caches results.

        Returns:
            Tuple of (intention_summary, key_changes, sources_count, confidence_level)
        """
        from src.analysis.prompts import format_debate_chunks_for_prompt

        # Check cache first
        cached = self._check_cache(mod.cgi_article, mod.law_numero)
        if cached:
            return (
                cached.get("intention", ""),
                cached.get("key_changes", []),
                cached.get("sources_count", 0),
                cached.get("confidence_level", "faible"),
            )

        # Parse date
        try:
            date_obj = datetime.strptime(mod.date_promulgation, "%Y-%m-%d").date()
        except Exception:
            return "", [], 0, "faible"

        # Search for debates — wider window for LF/LFSS (full budget calendar)
        if mod.law_type in ("LF", "LFSS"):
            date_start = date_obj - timedelta(days=270)
        else:
            date_start = date_obj - timedelta(days=120)
        date_end = date_obj + timedelta(days=30)

        # Use KeywordGenerator for comprehensive keywords
        article_titre = self._get_article_titre(mod.cgi_article)
        keywords = generate_all_keywords(
            article_ref=mod.cgi_article,
            titre=article_titre,
            law_text=mod.context,  # Use the context from extraction
            include_curated=True,
        )
        logger.debug(f"Generated {len(keywords)} keywords for {mod.cgi_article}")

        try:
            search_results = self.search_service.search_article_modification(
                article_ref=mod.cgi_article,
                article_keywords=keywords,
                law_type=mod.law_type,
                law_year=mod.law_year,
                date_start=date_start,
                date_end=date_end,
                top_k=10,
                min_relevant=3,
            )
        except Exception as e:
            logger.warning(f"Search failed for {mod.cgi_article}: {e}")
            return "", [], 0, "faible"

        debate_chunks = search_results.get("debates", [])
        if not debate_chunks:
            return "", [], 0, "faible"

        # Format for LLM
        max_chars = 20000 if self.settings.llm_provider == "anthropic" else 12000
        debate_text = format_debate_chunks_for_prompt(debate_chunks, max_chars=max_chars)

        # Simple extraction prompt
        prompt = f"""Analyse les débats parlementaires suivants concernant l'article {mod.cgi_article} du CGI,
modifié par {mod.law_type} {mod.law_year} ({mod.article_in_law}).

Extraits des débats:
{debate_text}

Réponds en JSON avec:
{{
    "intention": "Résumé de l'intention du législateur (2-3 phrases)",
    "key_changes": ["Liste des modifications principales"]
}}

JSON:"""

        try:
            response = self._call_llm(prompt)
            result = self._parse_json(response)

            intention = result.get("intention", "")
            key_changes = result.get("key_changes", [])
            sources_count = len(debate_chunks)

            # Determine confidence level weighted by speaker importance
            weighted_sum = 0.0
            for chunk, _score in debate_chunks:
                fonction = (chunk.metadata.get("fonction") or "").lower()
                if "rapporteur" in fonction and ("général" in fonction or "genera" in fonction):
                    weighted_sum += 2.0
                elif "rapporteur" in fonction:
                    weighted_sum += 1.5
                elif "ministre" in fonction or "secrétaire d" in fonction:
                    weighted_sum += 1.5
                elif "président" in fonction and "commission" in fonction:
                    weighted_sum += 1.0
                else:
                    weighted_sum += 0.5

            if weighted_sum >= 2.0:
                confidence_level = "élevé"
            elif weighted_sum >= 0.5:
                confidence_level = "moyen"
            else:
                confidence_level = "faible"

            # Save to cache (include confidence_level for resumption)
            self._save_to_cache(
                cgi_article=mod.cgi_article,
                law_numero=mod.law_numero,
                article_in_law=mod.article_in_law,
                analysis_result={
                    "intention": intention,
                    "key_changes": key_changes,
                    "sources_count": sources_count,
                    "confidence_level": confidence_level,
                },
                sources_count=sources_count,
                confidence_level=confidence_level,
            )

            return intention, key_changes, sources_count, confidence_level
        except Exception as e:
            logger.warning(f"LLM extraction failed: {e}")
            return "", [], len(debate_chunks), "faible"

    def _call_llm(self, prompt: str) -> str:
        """
        Call LLM API.

        For Groq, uses GroqClientPool for automatic key rotation on rate limits.
        """
        if self.settings.llm_provider == "groq":
            return self._get_groq_pool().chat_completion(
                model=self.settings.groq_model,
                messages=[
                    {"role": "system", "content": "Tu es un assistant juridique expert en droit fiscal français. Réponds uniquement en JSON valide."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=2000,
                temperature=0.2,
            )
        else:
            response = self.anthropic_client.messages.create(
                model=self.settings.claude_model,
                max_tokens=2500,
                messages=[
                    {"role": "user", "content": prompt}
                ],
                system="Tu es un assistant juridique expert en droit fiscal français. Réponds uniquement en JSON valide.",
            )
            return response.content[0].text

    def _parse_json(self, text: str) -> dict:
        """Parse JSON from LLM response."""
        import re
        text = text.strip()
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
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
            return {}

    def _get_article_titre(self, article_ref: str) -> str:
        """Get article title from reference data."""
        ref_path = Path("data/cgi_articles_reference.json")
        if not ref_path.exists():
            return ""

        with open(ref_path, encoding="utf-8") as f:
            data = json.load(f)

        for art in data.get("articles", []):
            if art.get("reference") == article_ref:
                return art.get("titre", "")

        return ""

    def _detect_themes(
        self,
        modifications: list[CGIModificationSummary],
    ) -> list[str]:
        """Detect key themes from modifications."""
        themes = set()

        # Map article prefixes to themes
        theme_prefixes = {
            "150": "Plus-values",
            "199": "Réductions d'impôt",
            "200": "Prélèvement forfaitaire",
            "787": "Droits de succession",
            "244": "Crédits d'impôt",
            "209": "Impôt sur les sociétés",
            "210": "Restructurations",
            "223": "Intégration fiscale",
        }

        for mod in modifications:
            for prefix, theme in theme_prefixes.items():
                if mod.cgi_article_ref.startswith(prefix):
                    themes.add(theme)

        return sorted(list(themes))


def list_available_laws() -> list[dict]:
    """List all available laws from reference files (CGI + finance overlay)."""
    laws_by_numero = {}

    # Load CGI laws first (lower priority)
    cgi_path = Path("data/lois_cgi_1958_2026.json")
    if cgi_path.exists():
        with open(cgi_path, encoding="utf-8") as f:
            data = json.load(f)
        for loi in data.get("lois", []):
            if loi.get("numero"):
                laws_by_numero[loi["numero"]] = loi

    # Overlay finance laws (higher priority, curated fields)
    fin_path = Path("data/lois_finances_2012_2025.json")
    if fin_path.exists():
        with open(fin_path, encoding="utf-8") as f:
            data = json.load(f)
        for loi in data.get("lois", []):
            if loi.get("numero"):
                laws_by_numero[loi["numero"]] = loi

    return sorted(
        laws_by_numero.values(),
        key=lambda x: (x.get("date_promulgation", "0000"), x.get("type", "")),
    )
