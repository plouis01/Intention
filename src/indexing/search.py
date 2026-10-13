"""Search service for finding relevant debates and extracting intent."""

import html as _html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np

from src.indexing.chunking import Chunk
from src.indexing.embeddings import EmbeddingService, VectorStore
from src.indexing.intent_extraction import IntentExtractor, IntentResult
from src.indexing.reranker import get_reranker
from src.utils.config import get_settings

logger = logging.getLogger(__name__)


def _normalize_text(text: str) -> str:
    """Normalize text for keyword matching: lowercase, apostrophes, HTML entities."""
    text = _html.unescape(text)
    return text.lower().replace("\u2019", "'").replace("\u2018", "'")


@dataclass
class ScoreExplanation:
    """Breakdown of how a chunk's final score was computed."""

    semantic_similarity: float = 0.0
    keyword_boost: float = 0.0
    speaker_boost: float = 0.0
    bm25_score: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)
    speaker_role: str = ""


# Default expansions file path
DEFAULT_EXPANSIONS_PATH = Path(__file__).parent.parent.parent / "data" / "legal_term_expansions.json"
DEFAULT_INDEX_PATH = Path(__file__).parent.parent.parent / "data" / "article_modifications.json"

# Stopwords for filtering index keywords (superset of common French words)
_INDEX_STOPWORDS = frozenset({
    "le", "la", "les", "de", "du", "des", "un", "une",
    "et", "en", "au", "aux", "ce", "ces", "se", "sa", "son", "ses",
    "il", "elle", "ils", "elles", "on", "nous", "vous",
    "je", "tu", "me", "te", "ne", "pas", "que", "qui", "est",
    "sur", "par", "pour", "dans", "avec", "plus", "ou", "où",
    "mais", "si", "ni", "car", "donc", "été", "sont", "ont",
    "cette", "tout", "tous", "très", "aussi", "bien",
    "lors", "values", "report", "150", "plus", "d'imposition",
    "l'apport", "régime", "contrôlée", "l'apporteur", "société",
    "titres", "cession", "holding",
})


def generate_expansions_from_index(
    index_path: Path | None = None,
) -> dict[str, list[str]]:
    """
    Auto-generate term expansions from article_modifications.json keywords.

    Extracts keywords per article, filters out generic single words (< 4 chars,
    French stopwords), and returns a dict mapping article refs to keyword lists.

    Args:
        index_path: Path to article_modifications.json (uses default if None)

    Returns:
        Dict mapping article references to filtered keyword lists
    """
    path = index_path or DEFAULT_INDEX_PATH
    if not path.exists():
        logger.debug(f"Article modifications index not found: {path}")
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Failed to load article modifications index: {e}")
        return {}

    articles = data.get("articles", {})
    expansions: dict[str, list[str]] = {}

    for article_ref, article_data in articles.items():
        raw_keywords = article_data.get("keywords", [])
        if not raw_keywords:
            continue

        filtered = []
        seen = set()
        for kw in raw_keywords:
            kw_lower = kw.lower().strip()
            # Skip short single words (< 4 chars)
            if " " not in kw_lower and len(kw_lower) < 4:
                continue
            # Skip stopwords
            if kw_lower in _INDEX_STOPWORDS:
                continue
            # Skip duplicates (case-insensitive)
            if kw_lower in seen:
                continue
            seen.add(kw_lower)
            filtered.append(kw)

        if filtered:
            expansions[article_ref] = filtered

    logger.info(f"Generated expansions for {len(expansions)} articles from index")
    return expansions

# Cache for loaded expansions
_loaded_expansions: dict[str, list[str]] | None = None


def load_term_expansions(path: Path | None = None) -> dict[str, list[str]]:
    """
    Load legal term expansions from JSON file, supplemented by index keywords.

    Curated expansions from the JSON file take priority. Index-generated
    keywords are merged in for articles not covered by the curated file,
    and appended (without duplicates) for articles that are covered.

    Args:
        path: Path to expansions JSON file (uses default if None)

    Returns:
        Dict mapping article references to related search terms
    """
    global _loaded_expansions

    if _loaded_expansions is not None and path is None:
        return _loaded_expansions

    expansions_path = path or DEFAULT_EXPANSIONS_PATH

    if not expansions_path.exists():
        logger.warning(f"Term expansions file not found: {expansions_path}")
        logger.info("Run 'python scripts/generate_term_expansions.py' to generate")
        curated = _get_fallback_expansions()
    else:
        try:
            with open(expansions_path, encoding="utf-8") as f:
                data = json.load(f)
            curated = data.get("expansions", {})
            logger.info(f"Loaded {len(curated)} curated term expansions")
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Failed to load term expansions: {e}")
            curated = _get_fallback_expansions()

    # Supplement with auto-generated expansions from the index
    index_expansions = generate_expansions_from_index()
    expansions = _merge_expansions(curated, index_expansions)

    if path is None:
        _loaded_expansions = expansions

    return expansions


def _merge_expansions(
    curated: dict[str, list[str]],
    generated: dict[str, list[str]],
) -> dict[str, list[str]]:
    """
    Merge curated and generated expansions. Curated entries take priority.

    For articles in both dicts, generated keywords are appended (deduped).
    For articles only in generated, they are added as-is.
    """
    merged = dict(curated)  # Start with curated

    for article_ref, gen_terms in generated.items():
        if article_ref in merged:
            # Append new terms not already in curated list
            existing_lower = {t.lower() for t in merged[article_ref]}
            for term in gen_terms:
                if term.lower() not in existing_lower:
                    merged[article_ref].append(term)
                    existing_lower.add(term.lower())
        else:
            merged[article_ref] = list(gen_terms)

    return merged


def _get_fallback_expansions() -> dict[str, list[str]]:
    """Return minimal fallback expansions if JSON file not available."""
    return {
        "150-0 B ter": [
            "apport-cession",
            "report d'imposition",
            "plus-value en report",
            "plus-value d'apport",
            "societe controlee",
            "holding",
            "reinvestissement",
            "cession de titres",
            "apport de titres",
            "sursis d'imposition",
        ],
        "150-0 B": [
            "apport-cession",
            "report d'imposition",
            "sursis d'imposition",
            "plus-value d'apport",
        ],
        "150-0 A": [
            "plus-value mobiliere",
            "cession de valeurs mobilieres",
            "imposition des plus-values",
        ],
    }


# Legacy alias for backward compatibility
LEGAL_TERM_EXPANSIONS = _get_fallback_expansions()


def expand_legal_terms(query: str, expansions: dict[str, list[str]] | None = None) -> list[str]:
    """
    Expand a legal query with related terms.

    Args:
        query: Original search query
        expansions: Optional custom expansions dict (loads from file if None)

    Returns:
        List of additional search terms to include
    """
    if expansions is None:
        expansions = load_term_expansions()

    expanded_terms = []
    query_lower = query.lower()

    for key, related_terms in expansions.items():
        if key.lower() in query_lower:
            expanded_terms.extend(related_terms)

    # Remove duplicates while preserving order
    seen = set()
    unique_terms = []
    for term in expanded_terms:
        if term.lower() not in seen and term.lower() not in query_lower:
            seen.add(term.lower())
            unique_terms.append(term)

    return unique_terms


def get_keywords_for_article(
    article_ref: str,
    expansions: dict[str, list[str]] | None = None,
) -> list[str]:
    """
    Get keyword list for a CGI article reference.

    Args:
        article_ref: Article reference (e.g., "150-0 B ter")
        expansions: Optional custom expansions dict (loads from file if None)

    Returns:
        List of keywords to search for
    """
    if expansions is None:
        expansions = load_term_expansions()

    keywords = [article_ref]

    # Normalize the reference
    normalized = article_ref.strip().lower()

    # Generate reference variants (with/without spaces, different formats)
    variants = _generate_article_variants(article_ref)
    keywords.extend(variants)

    # Add expansions if available
    for key, terms in expansions.items():
        key_lower = key.lower()
        if key_lower == normalized or normalized in key_lower or key_lower in normalized:
            keywords.extend(terms)
            break

    return list(set(keywords))


def _generate_article_variants(article_ref: str) -> list[str]:
    """
    Generate common variants of an article reference for ILIKE text filtering.

    For long refs (e.g., "150-0 B ter"), produces broad variants since the ref
    is discriminating enough on its own.

    For short refs (e.g., "39", "200"), produces CGI-contextualized variants
    to avoid matching "article 39 du projet de loi", "amendement n° 39", etc.
    """
    ref = article_ref.strip()

    if _is_short_article_ref(ref):
        # Short refs are highly ambiguous in ILIKE — require CGI context
        return [
            f"article {ref} du c",          # "article 39 du CGI" / "du code"
            f"l'article {ref} du c",         # "l'article 39 du code général"
            f"art. {ref} du c",              # "art. 39 du CGI"
            f"articles {ref}",               # "articles 39, 44 et 49 du CGI"
        ]

    variants = []

    # Lowercase
    variants.append(ref.lower())

    # Without hyphens
    variants.append(ref.replace("-", " ").lower())

    # Without spaces
    variants.append(ref.replace(" ", "").replace("-", "").lower())

    # With "article" prefix
    variants.append(f"article {ref}".lower())

    return list(set(variants))


def _generate_law_article_variants(article_in_law: str) -> list[str]:
    """Generate text filter variants from the law article number.

    E.g., "Art. 24" → ["article 24", "art. 24"]
    E.g., "Art. 32, 33" → ["article 32", "art. 32", "article 33", "art. 33"]
    """
    if not article_in_law:
        return []

    # Parse "Art. 24" or "Art. 32, 33" format
    cleaned = re.sub(r"^Art\.?\s*", "", article_in_law)
    numbers = [n.strip() for n in cleaned.split(",") if n.strip()]

    variants = []
    for num in numbers:
        # Skip single-digit numbers — too many false positives
        # "article 3" matches 24K+ chunks vs "article 24" matches ~2K
        if len(num) == 1 and num.isdigit():
            continue
        variants.append(f"article {num}")
        variants.append(f"art. {num}")

    return variants


def _is_short_article_ref(article_ref: str) -> bool:
    """True for short CGI refs like "39", "200", "787 B" (numeric core ≤ 3 digits, no hyphen).
    False for compound refs like "150-0 B ter", "199 terdecies-0 A"."""
    ref = article_ref.strip()
    core = re.match(r"^(\d+(?:-\d+)*)", ref)
    if not core:
        return False
    numeric = core.group(1)
    if "-" in numeric:
        return False
    # Check remainder after numeric prefix — if it contains additional
    # qualifiers like "terdecies-0 A", the ref is specific enough as-is.
    remainder = ref[core.end():].strip()
    if remainder and not re.match(r"^[A-D]$", remainder, re.IGNORECASE):
        # Has complex qualifier (e.g., "terdecies-0 A", "bis", "ter")
        return False
    return len(numeric) <= 3


def _build_contextualized_patterns(article_ref: str) -> list[re.Pattern]:
    """Regex patterns matching article ref in legal context with word boundaries.
    For "39": matches "article 39" but NOT "2039", "139", "390".
    For short refs, adds negative lookahead to exclude neighboring articles
    (39 bis, 39 ter, 39 A, etc.)."""
    ref_escaped = re.escape(article_ref.strip())

    # Negative lookahead excludes neighboring articles (39 bis, 39 ter, 39 A, etc.)
    not_neighbor = ""
    if _is_short_article_ref(article_ref):
        ORDINALS = (
            r"bis|ter|quater|quinquies|sexies|septies|octies|nonies|"
            r"decies|undecies|duodecies|terdecies|quaterdecies|"
            r"quindecies|sexdecies|septendecies|octodecies|novodecies|vicies"
        )
        # (?-i:[A-Z]) forces case-sensitive match for the letter qualifier,
        # so "article 39 a été" (verb) is not confused with "article 39 A" (CGI neighbor).
        # [A-H] with punctuation catches "39 B." / "39 C," (CGI qualifiers go A-H);
        # avoids false-excluding "39 I." (roman numeral) or "39 M." (Monsieur).
        not_neighbor = (
            rf"(?!\s+(?:{ORDINALS}|"
            rf"(?-i:[A-H])(?:[\s.,;:)]|$)|"
            rf"(?-i:[I-Z])(?:\s|$)))"
        )

    return [
        re.compile(rf"\barticle\s+{ref_escaped}{not_neighbor}\b", re.IGNORECASE),
        re.compile(rf"\bl['\u2019]article\s+{ref_escaped}{not_neighbor}\b", re.IGNORECASE),
        re.compile(rf"\bart\.?\s+{ref_escaped}{not_neighbor}\b", re.IGNORECASE),
        # "39 du CGI" — "du" already anchors, no lookahead needed
        re.compile(rf"\b{ref_escaped}\s+du\s+(cgi|code\s+g)", re.IGNORECASE),
    ]


def reload_term_expansions(path: Path | None = None) -> dict[str, list[str]]:
    """
    Force reload term expansions from disk.

    Useful after generating new expansions.

    Args:
        path: Path to expansions JSON file (uses default if None)

    Returns:
        Dict mapping article references to related search terms
    """
    global _loaded_expansions
    _loaded_expansions = None
    return load_term_expansions(path)


# Procedural patterns to exclude from analysis
PROCEDURAL_PATTERNS = [
    "la parole est à",
    "je mets aux voix",
    "l'amendement est adopté",
    "l'amendement est rejeté",
    "il en est ainsi décidé",
    "adopté.",
    "rejeté.",
    "la séance est levée",
    "la séance est suspendue",
    "je vous remercie",
    "merci, monsieur",
    "merci, madame",
    "rappel au règlement",
]


def is_procedural_content(content: str) -> bool:
    """Check if content is primarily procedural (vote announcements, etc.)."""
    content_lower = content.lower()
    # If the chunk is short and matches procedural patterns, it's procedural
    if len(content) < 200:
        for pattern in PROCEDURAL_PATTERNS:
            if pattern in content_lower:
                return True
    return False


# Stopwords for French legal text — too common to be meaningful alone
_LEGAL_STOPWORDS = {
    "dans", "pour", "avec", "sans", "sous", "lors", "plus", "moins",
    "tout", "tous", "cette", "comme", "dont", "être", "fait", "même",
    "très", "bien", "aussi", "autre", "entre", "après", "avant",
}


def _is_specific_keyword(kw: str, article_ref: str | None = None) -> bool:
    """Check if a keyword is specific enough to indicate article relevance.

    Specific keywords are:
    - Multi-word phrases with at least 2 meaningful words (>= 4 chars, non-stopword)
    - Keywords containing the article reference number/code
    Single generic words (cession, titres, société, régime, etc.) are NOT specific.
    """
    kw_stripped = kw.strip()
    kw_lower = kw_stripped.lower()

    # Contains article reference → always specific
    if article_ref:
        ref_lower = article_ref.lower()
        ref_compact = ref_lower.replace("-", "").replace(" ", "")
        kw_compact = kw_lower.replace("-", "").replace(" ", "")
        if ref_lower in kw_lower or ref_compact in kw_compact:
            return True

    # Multi-word phrase with 2+ meaningful words → specific
    words = kw_lower.split()
    meaningful = [w for w in words if len(w) >= 4 and w not in _LEGAL_STOPWORDS]
    if len(meaningful) >= 2:
        return True

    return False


def filter_chunks_by_keywords(
    chunks: list[tuple[Chunk, float]],
    required_keywords: list[str],
    min_required: int = 1,
    article_ref: str | None = None,
    require_context: bool = False,
    article_ref_patterns: list[re.Pattern] | None = None,
) -> list[tuple[Chunk, float]]:
    """
    Filter chunks to only include those containing at least min_required keywords.

    Args:
        chunks: List of (chunk, score) tuples
        required_keywords: Keywords that must appear in chunk content
        min_required: Minimum number of keywords that must be present
        article_ref: Optional article reference for boosting explicit mentions
        require_context: If True, require at least one specific (non-generic)
            keyword match when the article reference is absent from the chunk.
        article_ref_patterns: Optional list of compiled regex patterns for
            contextualized article ref matching (overrides default substring check).
            Used for short CGI refs like "39" to avoid matching "2039".

    Returns:
        Filtered list of chunks with adjusted scores
    """
    # Article reference patterns for boosting
    article_patterns = []
    if article_ref:
        article_patterns = [
            article_ref.lower(),
            article_ref.lower().replace("-", " "),
            article_ref.lower().replace(" ", ""),
            f"article {article_ref}".lower(),
        ]

    filtered = []
    for chunk, score in chunks:
        content_lower = _normalize_text(chunk.content)

        # Skip procedural content
        if is_procedural_content(chunk.content):
            continue

        # Count keyword matches
        matches = sum(1 for kw in required_keywords if _normalize_text(kw) in content_lower)

        if matches < min_required:
            continue

        # Check if article is explicitly mentioned
        if article_ref_patterns:
            has_article_ref = any(p.search(chunk.content) for p in article_ref_patterns)
        else:
            has_article_ref = any(pat in content_lower for pat in article_patterns)

        # Context check: require at least one *specific* keyword match
        # when article reference is not found in the chunk content.
        # This prevents generic single-word matches (société, cession, régime, ...)
        # from letting unrelated debates through.
        if require_context and not has_article_ref:
            matched_keywords = [
                kw for kw in required_keywords
                if _normalize_text(kw) in content_lower
            ]
            has_specific = any(
                _is_specific_keyword(kw, article_ref)
                for kw in matched_keywords
            )
            if not has_specific:
                continue

        # Boost score if article is explicitly mentioned
        adjusted_score = score
        if has_article_ref:
            adjusted_score += 0.15  # Significant boost for explicit article mentions

        filtered.append((chunk, adjusted_score))

    # Re-sort by adjusted score
    filtered.sort(key=lambda x: x[1], reverse=True)

    return filtered


def count_keyword_matches(content: str, keywords: list[str]) -> int:
    """Count how many keywords appear in content."""
    content_lower = _normalize_text(content)
    return sum(1 for kw in keywords if _normalize_text(kw) in content_lower)


def reciprocal_rank_fusion(
    dense: list[tuple[Chunk, float]],
    sparse: list[tuple[Chunk, float]],
    k: int = 60,
    top_k: int = 15,
) -> list[tuple[Chunk, float]]:
    """Fuse dense and sparse results using Reciprocal Rank Fusion.

    Args:
        dense: Dense retrieval results (chunk, score) sorted by score desc
        sparse: Sparse BM25 results (chunk, score) sorted by score desc
        k: RRF constant (default 60, standard value from the RRF paper)
        top_k: Number of results to return

    Returns:
        Fused (chunk, rrf_score) list sorted by RRF score descending
    """
    # Map chunk id -> (chunk, rrf_score)
    scores: dict[int, tuple[Chunk, float]] = {}

    for rank, (chunk, _score) in enumerate(dense):
        cid = id(chunk)
        rrf = 1.0 / (k + rank + 1)
        if cid in scores:
            scores[cid] = (chunk, scores[cid][1] + rrf)
        else:
            scores[cid] = (chunk, rrf)

    for rank, (chunk, _score) in enumerate(sparse):
        cid = id(chunk)
        rrf = 1.0 / (k + rank + 1)
        if cid in scores:
            scores[cid] = (chunk, scores[cid][1] + rrf)
        else:
            scores[cid] = (chunk, rrf)

    fused = sorted(scores.values(), key=lambda x: x[1], reverse=True)
    return fused[:top_k]


@dataclass
class SearchResult:
    """Result of a search query."""

    query: str
    relevant_debates: list[tuple[Chunk, float]]  # (chunk, similarity)
    relevant_amendments: list[tuple[Chunk, float]]
    intent: IntentResult | None


class SearchService:
    """
    Main search service for legislative intent analysis.

    Combines embedding search with intent extraction.
    """

    def __init__(
        self,
        debate_store: VectorStore | None = None,
        amendment_store: VectorStore | None = None,
    ):
        self.embedding_service = EmbeddingService()
        self.intent_extractor = IntentExtractor()
        self.debate_store = debate_store or VectorStore()
        self.amendment_store = amendment_store or VectorStore()
        self.reranker = get_reranker()
        self.debate_bm25 = None  # BM25Index | None
        self.amendment_bm25 = None  # BM25Index | None

    @classmethod
    def create_configured(cls) -> "SearchService":
        """Create a SearchService using the configured vector store provider.

        Uses VECTOR_STORE_PROVIDER from settings:
        - "pgvector": connects to Railway pgvector (production, 2.97M chunks)
        - "memory": loads local embeddings from data/embeddings/
        """
        settings = get_settings()
        svc = cls()
        provider = settings.vector_store_provider

        if provider == "pgvector":
            from src.indexing.vector_stores import create_vector_store
            pgvector_store = create_vector_store("pgvector")
            svc.debate_store = pgvector_store
            svc.amendment_store = pgvector_store
            logger.info(f"Connected to pgvector ({pgvector_store.count()} chunks)")
        else:
            embeddings_dir = Path(__file__).parent.parent.parent / "data" / "embeddings"
            svc.load_stores(embeddings_dir)
            logger.info(f"Loaded local stores: {len(svc.debate_store)} debate, {len(svc.amendment_store)} amendment chunks")

        return svc

    def search_article(
        self,
        article_numero: str,
        article_contenu: str,
        top_k_debates: int = 10,
        top_k_amendments: int = 5,
        min_similarity: float = 0.5,
        extract_intent: bool = True,
    ) -> SearchResult:
        """
        Search for relevant debates and extract intent for an article.

        Args:
            article_numero: Article number (e.g., "12", "15 bis")
            article_contenu: Full text of the article
            top_k_debates: Number of debate chunks to retrieve
            top_k_amendments: Number of amendment chunks to retrieve
            min_similarity: Minimum similarity threshold
            extract_intent: Whether to run intent extraction

        Returns:
            SearchResult with relevant chunks and intent
        """
        # Create search query
        query = f"Article {article_numero}: {article_contenu[:500]}"

        # Generate query embedding
        query_embedding = self.embedding_service.generate_embedding(query)

        # Search debates
        relevant_debates = self.debate_store.search(
            query_embedding, top_k=top_k_debates, min_similarity=min_similarity
        )

        # Search amendments
        relevant_amendments = self.amendment_store.search(
            query_embedding, top_k=top_k_amendments, min_similarity=min_similarity
        )

        # Extract intent if requested
        intent = None
        if extract_intent and (relevant_debates or relevant_amendments):
            debate_chunks = [chunk for chunk, _ in relevant_debates]
            amendment_chunks = [chunk for chunk, _ in relevant_amendments]

            try:
                intent = self.intent_extractor.extract_intent(
                    article_contenu=article_contenu,
                    debate_chunks=debate_chunks,
                    amendment_chunks=amendment_chunks,
                )
            except Exception as e:
                logger.error(f"Intent extraction failed: {e}")

        return SearchResult(
            query=query,
            relevant_debates=relevant_debates,
            relevant_amendments=relevant_amendments,
            intent=intent,
        )

    def search_text(
        self,
        query_text: str,
        top_k: int = 10,
        min_similarity: float = 0.5,
        search_debates: bool = True,
        search_amendments: bool = True,
    ) -> dict[str, list[tuple[Chunk, float]]]:
        """
        Search for chunks matching a free-text query.

        Args:
            query_text: Search query
            top_k: Number of results per store
            min_similarity: Minimum similarity threshold
            search_debates: Whether to search debates
            search_amendments: Whether to search amendments

        Returns:
            Dict with 'debates' and 'amendments' keys containing results
        """
        query_embedding = self.embedding_service.generate_embedding(query_text)

        results = {}

        if search_debates:
            results["debates"] = self.debate_store.search(
                query_embedding, top_k=top_k, min_similarity=min_similarity
            )

        if search_amendments:
            results["amendments"] = self.amendment_store.search(
                query_embedding, top_k=top_k, min_similarity=min_similarity
            )

        return results

    def hybrid_search(
        self,
        keywords: list[str],
        semantic_query: str,
        date_start: date | None = None,
        date_end: date | None = None,
        top_k: int = 15,
        keyword_boost: float = 0.25,
        min_similarity: float = 0.3,
        search_debates: bool = True,
        search_amendments: bool = True,
        explain: bool = False,
        article_text_filters: list[str] | None = None,
    ) -> dict[str, list[tuple[Chunk, float]]]:
        """
        Combine keyword and semantic search with date filtering.

        Keyword matches get a score boost to ensure relevant content
        is found even if embeddings don't capture legal terminology well.

        Args:
            keywords: List of keywords to search for (exact match boost)
            semantic_query: Query text for semantic search
            date_start: Filter chunks after this date
            date_end: Filter chunks before this date
            top_k: Number of results per store
            keyword_boost: Score boost for keyword matches (0.0-0.5)
            min_similarity: Minimum similarity threshold (before boost)
            search_debates: Whether to search debates
            search_amendments: Whether to search amendments
            explain: If True, store ScoreExplanation in chunk metadata
            article_text_filters: Specific article ref variants for SQL-level
                text filtering in pgvector (e.g., ["150-0 B ter", "150-0 b ter"])

        Returns:
            Dict with 'debates' and 'amendments' keys containing results
        """
        # Expand keywords with related legal terms
        expanded_keywords = list(keywords)  # Copy original
        for kw in keywords:
            expanded_keywords.extend(expand_legal_terms(kw))
        # Remove duplicates
        expanded_keywords = list(set(expanded_keywords))

        logger.debug(f"Expanded keywords: {keywords} -> {expanded_keywords}")

        # Generate query embedding
        query_embedding = self.embedding_service.generate_embedding(semantic_query)

        results = {}

        if search_debates:
            store = self.debate_store
            has_chunks = hasattr(store, "chunks") and store.chunks
            if has_chunks:
                debate_results = self._hybrid_search_store(
                    store,
                    query_embedding,
                    expanded_keywords,
                    date_start,
                    date_end,
                    top_k,
                    keyword_boost,
                    min_similarity,
                    bm25_index=self.debate_bm25,
                    semantic_query=semantic_query,
                    explain=explain,
                )
            elif hasattr(store, "search"):
                # Non-memory store (pgvector, qdrant): use store.search() + keyword filtering
                debate_results = self._hybrid_search_external_store(
                    store, query_embedding, expanded_keywords,
                    date_start, date_end, top_k, keyword_boost, min_similarity,
                    chunk_type="intervention",
                    article_text_filters=article_text_filters,
                )
            else:
                debate_results = []
            results["debates"] = debate_results

        if search_amendments:
            store = self.amendment_store
            has_chunks = hasattr(store, "chunks") and store.chunks
            if has_chunks:
                amendment_results = self._hybrid_search_store(
                    store,
                    query_embedding,
                    expanded_keywords,
                    date_start,
                    date_end,
                    top_k,
                    keyword_boost,
                    min_similarity,
                    bm25_index=self.amendment_bm25,
                    semantic_query=semantic_query,
                    explain=explain,
                )
            elif hasattr(store, "search"):
                amendment_results = self._hybrid_search_external_store(
                    store, query_embedding, expanded_keywords,
                    date_start, date_end, top_k, keyword_boost, min_similarity,
                    chunk_type="amendement",
                    article_text_filters=article_text_filters,
                )
            else:
                amendment_results = []
            results["amendments"] = amendment_results

        return results

    def _hybrid_search_store(
        self,
        store: VectorStore,
        query_embedding: list[float],
        keywords: list[str],
        date_start: date | None,
        date_end: date | None,
        top_k: int,
        keyword_boost: float,
        min_similarity: float,
        bm25_index=None,
        semantic_query: str | None = None,
        explain: bool = False,
    ) -> list[tuple[Chunk, float]]:
        """Perform hybrid search on a single store using vectorized similarity."""
        if len(store.chunks) == 0:
            return []

        # 1. Vectorized cosine similarity (single matrix op)
        if isinstance(store.embeddings, np.ndarray):
            emb_matrix = store.embeddings
        else:
            emb_matrix = np.array(store.embeddings, dtype=np.float32)

        query_arr = np.array(query_embedding, dtype=np.float32)
        norms = np.linalg.norm(emb_matrix, axis=1)
        query_norm = np.linalg.norm(query_arr)
        sim_scores = emb_matrix @ query_arr / (norms * query_norm + 1e-10)
        scores = sim_scores.copy()

        # 2. Similarity threshold mask
        mask = scores >= min_similarity

        # 3. Date filter (only on candidates passing threshold)
        if date_start or date_end:
            for i in np.where(mask)[0]:
                if not self._passes_date_filter(store.chunks[i], date_start, date_end):
                    mask[i] = False

        # Track per-chunk explanations when requested
        explanations: dict[int, ScoreExplanation] = {}

        # 4. Keyword boost (only on remaining candidates)
        keywords_lower = [_normalize_text(kw) for kw in keywords]
        for i in np.where(mask)[0]:
            content_lower = _normalize_text(store.chunks[i].content)
            matched = [kw for kw in keywords_lower if kw in content_lower]
            if matched:
                scores[i] += keyword_boost
            if explain:
                explanations[i] = ScoreExplanation(
                    semantic_similarity=float(sim_scores[i]),
                    keyword_boost=keyword_boost if matched else 0.0,
                    matched_keywords=list(matched),
                )

        # 5. Speaker importance boost for debate chunks
        for i in np.where(mask)[0]:
            fonction = _normalize_text(store.chunks[i].metadata.get("fonction") or "")
            if not fonction:
                continue

            speaker_boost = 0.0
            speaker_role = ""
            settings = get_settings()
            if "rapporteur" in fonction and ("général" in fonction or "genera" in fonction):
                speaker_boost = settings.speaker_boost_rapporteur_general
                speaker_role = "rapporteur général"
            elif "rapporteur" in fonction:
                speaker_boost = settings.speaker_boost_rapporteur
                speaker_role = "rapporteur"
            elif any(r in fonction for r in ("ministre", "secrétaire d'état", "secretaire d")):
                speaker_boost = settings.speaker_boost_ministre
                speaker_role = "ministre"
            elif "président" in fonction and "commission" in fonction:
                speaker_boost = settings.speaker_boost_commission_pres
                speaker_role = "président de commission"

            if speaker_boost > 0:
                scores[i] += speaker_boost
                if explain and i in explanations:
                    explanations[i].speaker_boost = speaker_boost
                    explanations[i].speaker_role = speaker_role

        # 6. Top-k from valid candidates (dense results)
        valid = np.where(mask)[0]
        if len(valid) == 0:
            dense_results = []
        else:
            top = valid[np.argsort(-scores[valid])[:top_k]]
            dense_results = [(store.chunks[i], float(scores[i])) for i in top]
            if explain:
                for i in top:
                    if i in explanations:
                        store.chunks[i].metadata["_score_explanation"] = explanations[i]

        # 7. BM25 sparse retrieval + RRF fusion
        if bm25_index is not None and semantic_query:
            bm25_results = bm25_index.search(semantic_query, top_k=top_k * 2)
            if bm25_results:
                # Apply date filter to BM25 results before fusion
                if date_start or date_end:
                    bm25_results = [
                        (c, s) for c, s in bm25_results
                        if self._passes_date_filter(c, date_start, date_end)
                    ]
                fused = reciprocal_rank_fusion(dense_results, bm25_results, top_k=top_k)
                if explain:
                    # Add BM25 scores to explanations
                    bm25_scores_map = {id(c): s for c, s in bm25_results}
                    for chunk, _ in fused:
                        expl = chunk.metadata.get("_score_explanation")
                        if isinstance(expl, ScoreExplanation):
                            expl.bm25_score = bm25_scores_map.get(id(chunk), 0.0)
                return fused

        return dense_results

    def _hybrid_search_external_store(
        self,
        store,
        query_embedding: list[float],
        keywords: list[str],
        date_start: date | None,
        date_end: date | None,
        top_k: int,
        keyword_boost: float,
        min_similarity: float,
        chunk_type: str | None = None,
        article_text_filters: list[str] | None = None,
    ) -> list[tuple[Chunk, float]]:
        """Hybrid search for non-memory stores (pgvector, qdrant).

        Uses the store's native search() for semantic retrieval, then applies
        keyword boosting and date filtering in Python.

        When the store supports search_with_text_filter (pgvector) and
        article_text_filters are provided, uses SQL-level text filtering to
        find article-specific chunks that pure vector search misses.
        """
        # Use SQL-level text filtering when available (pgvector)
        # article_text_filters contains specific article ref variants that are
        # highly discriminating. Generic keywords are too broad for SQL ILIKE.
        if hasattr(store, "search_with_text_filter") and article_text_filters:
            raw_results = store.search_with_text_filter(
                query_embedding,
                text_filters=article_text_filters,
                top_k=top_k * 3,
                min_similarity=min_similarity,
                chunk_type=chunk_type,
                date_start=date_start.isoformat() if date_start else None,
                date_end=date_end.isoformat() if date_end else None,
            )
        else:
            # Overfetch to allow for filtering
            raw_results = store.search(
                query_embedding,
                top_k=top_k * 3,
                min_similarity=min_similarity,
            )

        if not raw_results:
            return []

        keywords_lower = [_normalize_text(kw) for kw in keywords]
        scored = []

        for chunk, similarity in raw_results:
            if not self._passes_date_filter(chunk, date_start, date_end):
                continue

            score = similarity
            content_lower = _normalize_text(chunk.content)
            matched = [kw for kw in keywords_lower if kw in content_lower]
            if matched:
                score += keyword_boost

            # Speaker importance boost
            fonction = _normalize_text(chunk.metadata.get("fonction") or "")
            if fonction:
                if "rapporteur" in fonction and ("général" in fonction or "genera" in fonction):
                    score += 0.08
                elif "rapporteur" in fonction:
                    score += 0.06
                elif any(r in fonction for r in ("ministre", "secrétaire d'état", "secretaire d")):
                    score += 0.05
                elif "président" in fonction and "commission" in fonction:
                    score += 0.04

            scored.append((chunk, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def keyword_search(
        self,
        keywords: list[str],
        date_start: date | None = None,
        date_end: date | None = None,
        max_results: int = 50,
        search_debates: bool = True,
        search_amendments: bool = True,
    ) -> dict[str, list[tuple[Chunk, float]]]:
        """
        Search for chunks containing any of the keywords.

        Args:
            keywords: List of keywords to search for
            date_start: Filter chunks after this date
            date_end: Filter chunks before this date
            max_results: Maximum results per store
            search_debates: Whether to search debates
            search_amendments: Whether to search amendments

        Returns:
            Dict with 'debates' and 'amendments' keys containing (chunk, score) tuples
        """
        results = {}

        if search_debates:
            store = self.debate_store
            if hasattr(store, "chunks"):
                results["debates"] = self._keyword_search_store(
                    store, keywords, date_start, date_end, max_results,
                    bm25_index=self.debate_bm25,
                )
            elif hasattr(store, "text_only_search"):
                results["debates"] = store.text_only_search(
                    text_filters=keywords, top_k=max_results,
                    chunk_type="intervention",
                    date_start=str(date_start) if date_start else None,
                    date_end=str(date_end) if date_end else None,
                )
            else:
                results["debates"] = []

        if search_amendments:
            store = self.amendment_store
            if hasattr(store, "chunks"):
                results["amendments"] = self._keyword_search_store(
                    store, keywords, date_start, date_end, max_results,
                    bm25_index=self.amendment_bm25,
                )
            elif hasattr(store, "text_only_search"):
                results["amendments"] = store.text_only_search(
                    text_filters=keywords, top_k=max_results,
                    chunk_type="amendement",
                    date_start=str(date_start) if date_start else None,
                    date_end=str(date_end) if date_end else None,
                )
            else:
                results["amendments"] = []

        return results

    @staticmethod
    def _passes_date_filter(
        chunk: Chunk, date_start: date | None, date_end: date | None
    ) -> bool:
        """Check if a chunk passes the date range filter.

        Returns False if a date filter is active but the chunk has no date metadata.
        This prevents undated chunks from bypassing date-scoped searches.
        """
        if not date_start and not date_end:
            return True

        chunk_date_str = chunk.metadata.get("date")
        if not chunk_date_str:
            return False  # No date metadata — exclude from date-filtered searches

        try:
            if isinstance(chunk_date_str, str):
                chunk_date = datetime.fromisoformat(
                    chunk_date_str.replace("Z", "+00:00")
                ).date()
            else:
                chunk_date = chunk_date_str

            if date_start and chunk_date < date_start:
                return False
            if date_end and chunk_date > date_end:
                return False
        except (ValueError, TypeError):
            return False  # Unparseable date — exclude rather than silently pass

        return True

    def _keyword_search_store(
        self,
        store: VectorStore,
        keywords: list[str],
        date_start: date | None,
        date_end: date | None,
        max_results: int,
        bm25_index=None,
    ) -> list[tuple[Chunk, float]]:
        """Perform keyword search on a single store.

        When bm25_index is available, uses BM25 ranking as primary then
        applies keyword presence + date filters. Otherwise, falls back to
        linear scan with score = matched_keywords / total_keywords.

        Returns:
            List of (chunk, score) tuples sorted by score descending.
        """
        keywords_lower = [_normalize_text(kw) for kw in keywords]
        total_kw = len(keywords_lower)

        if bm25_index is not None:
            # BM25-based ranking: search with joined keywords, then filter
            query_str = " ".join(keywords)
            bm25_results = bm25_index.search(query_str, top_k=max_results * 3)

            results: list[tuple[Chunk, float]] = []
            for chunk, bm25_score in bm25_results:
                if not self._passes_date_filter(chunk, date_start, date_end):
                    continue
                # Verify at least one keyword is present
                content_lower = _normalize_text(chunk.content)
                if any(kw in content_lower for kw in keywords_lower):
                    results.append((chunk, bm25_score))
                if len(results) >= max_results:
                    break
            return results

        # Fallback: linear scan with keyword-match scoring
        results = []
        for chunk in store.chunks:
            if not self._passes_date_filter(chunk, date_start, date_end):
                continue

            content_lower = _normalize_text(chunk.content)
            matched = sum(1 for kw in keywords_lower if kw in content_lower)
            if matched > 0:
                score = matched / total_kw if total_kw > 0 else 0.0
                results.append((chunk, score))

            if len(results) >= max_results * 2:
                break

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:max_results]

    def load_stores(self, data_dir: Path) -> None:
        """
        Load vector stores from disk.

        Auto-detects format (binary .npz or legacy .json).
        Also loads BM25 indices if available and enabled.

        Args:
            data_dir: Directory containing store files
        """
        debate_base = data_dir / "debate_store"
        amendment_base = data_dir / "amendment_store"

        if debate_base.with_suffix(".npz").exists() or debate_base.with_suffix(".json").exists():
            self.debate_store.load(debate_base)
            logger.info(f"Loaded {len(self.debate_store)} debate chunks")
        else:
            logger.warning(f"No debate store found at {debate_base}")

        if amendment_base.with_suffix(".npz").exists() or amendment_base.with_suffix(".json").exists():
            self.amendment_store.load(amendment_base)
            logger.info(f"Loaded {len(self.amendment_store)} amendment chunks")
        else:
            logger.warning(f"No amendment store found at {amendment_base}")

        # Load BM25 indices if enabled
        settings = get_settings()
        if settings.bm25_enabled:
            from src.indexing.bm25 import BM25Index

            debate_bm25_path = data_dir / "debate_bm25_tokens.json"
            if debate_bm25_path.exists() and self.debate_store.chunks:
                self.debate_bm25 = BM25Index()
                self.debate_bm25.load(debate_bm25_path, self.debate_store.chunks)
                logger.info(f"Loaded debate BM25 index ({len(self.debate_bm25)} docs)")

            amendment_bm25_path = data_dir / "amendment_bm25_tokens.json"
            if amendment_bm25_path.exists() and self.amendment_store.chunks:
                self.amendment_bm25 = BM25Index()
                self.amendment_bm25.load(amendment_bm25_path, self.amendment_store.chunks)
                logger.info(f"Loaded amendment BM25 index ({len(self.amendment_bm25)} docs)")

    def save_stores(self, data_dir: Path) -> None:
        """
        Save vector stores to disk.

        Args:
            data_dir: Directory to save store files
        """
        data_dir.mkdir(parents=True, exist_ok=True)

        self.debate_store.save(data_dir / "debate_store")
        self.amendment_store.save(data_dir / "amendment_store")

    def search_cgi_article(
        self,
        article_ref: str,
        date_start: date | None = None,
        date_end: date | None = None,
        top_k: int = 20,
        min_similarity: float = 0.3,
        explain: bool = False,
    ) -> dict[str, list[tuple[Chunk, float]]]:
        """
        Search for debates related to a CGI article with automatic term expansion.

        This method automatically expands the article reference to include
        related legal terms (e.g., "150-0 B ter" expands to include
        "apport-cession", "report d'imposition", etc.)

        Args:
            article_ref: CGI article reference (e.g., "150-0 B ter")
            date_start: Filter chunks after this date
            date_end: Filter chunks before this date
            top_k: Number of results
            min_similarity: Minimum similarity threshold
            explain: If True, store ScoreExplanation in chunk metadata

        Returns:
            Dict with 'debates' and 'amendments' keys containing results

        Example:
            >>> results = search_service.search_cgi_article("150-0 B ter")
            >>> for chunk, score in results["debates"][:5]:
            ...     print(f"{score:.2f}: {chunk.content[:100]}")
        """
        # Get expanded keywords for this article
        keywords = get_keywords_for_article(article_ref)

        # Build semantic query as natural language for better embedding retrieval
        semantic_query = (
            f"Quelle est l'intention du législateur concernant l'article {article_ref} "
            f"du code général des impôts ? Objectifs, justifications et conditions du dispositif."
        )

        logger.info(f"Searching for CGI article {article_ref} with keywords: {keywords}")

        # Overfetch to have enough results after filtering
        fetch_k = top_k * 3

        # Article ref variants for SQL-level text filtering in pgvector
        article_text_filters = [article_ref] + _generate_article_variants(article_ref)

        raw_results = self.hybrid_search(
            keywords=keywords,
            semantic_query=semantic_query,
            date_start=date_start,
            date_end=date_end,
            article_text_filters=article_text_filters,
            top_k=fetch_k,
            keyword_boost=0.3,
            min_similarity=min_similarity,
            explain=explain,
        )

        # Tiered post-filtering to remove false positives
        tier1_keywords = [article_ref] + _generate_article_variants(article_ref)

        filtered_results = {}
        for key in ["debates", "amendments"]:
            if key not in raw_results:
                filtered_results[key] = []
                continue

            # Tier 1: chunks explicitly mentioning the article reference
            strict = filter_chunks_by_keywords(
                raw_results[key],
                tier1_keywords,
                min_required=1,
                article_ref=article_ref,
                require_context=False,
            )

            # Tier 2 (fallback): broader keyword match
            # Amendments are shorter, so require only 1 keyword but with context
            # Debates need 2+ keywords to avoid false positives
            if len(strict) < 3:
                is_amendment = key == "amendments"
                relaxed = filter_chunks_by_keywords(
                    raw_results[key],
                    keywords,
                    min_required=1 if is_amendment else 2,
                    article_ref=article_ref,
                    require_context=True,
                )
                seen_ids = {id(c) for c, _ in strict}
                for chunk, score in relaxed:
                    if id(chunk) not in seen_ids:
                        strict.append((chunk, score))
                        seen_ids.add(id(chunk))

            # Rerank with cross-encoder if available
            if self.reranker and strict:
                logger.info(
                    f"Reranking {len(strict)} {key} chunks for article {article_ref}"
                )
                strict = self.reranker.rerank(semantic_query, strict, top_k=top_k)
            else:
                strict = strict[:top_k]

            filtered_results[key] = strict

        return filtered_results

    def search_article_modification(
        self,
        article_ref: str,
        article_keywords: list[str],
        law_type: str,
        law_year: int,
        date_start: date | None = None,
        date_end: date | None = None,
        top_k: int = 15,
        min_relevant: int = 5,
        explain: bool = False,
        min_similarity_override: float | None = None,
        article_in_law: str = "",
    ) -> dict[str, list[tuple[Chunk, float]]]:
        """
        Search for debates related to a specific article modification with strict filtering.

        This method fetches more chunks initially, then filters to only those
        that explicitly mention the article or its core keywords.

        Args:
            article_ref: CGI article reference (e.g., "150-0 B ter")
            article_keywords: Article-specific keywords for filtering
            law_type: Type of law (LF, LFR, LFSS)
            law_year: Year of the law
            date_start: Filter chunks after this date
            date_end: Filter chunks before this date
            top_k: Number of results to return after filtering
            min_relevant: Minimum relevant chunks expected (logs warning if fewer)
            explain: If True, store ScoreExplanation in chunk metadata

        Returns:
            Dict with 'debates' and 'amendments' keys containing filtered results
        """
        # Build article-specific semantic query as natural language
        core_keywords = article_keywords[:3] if len(article_keywords) > 3 else article_keywords
        keywords_str = ", ".join(core_keywords) if core_keywords else ""
        semantic_query = (
            f"Pourquoi le législateur a-t-il modifié l'article {article_ref} du CGI "
            f"dans la {law_type} {law_year} ? "
            f"Intention, objectifs et justifications concernant {keywords_str}."
        )

        logger.info(f"Searching for {article_ref} modification {law_type} {law_year}")
        logger.debug(f"Semantic query: {semantic_query}")

        # Fetch more chunks initially (2x top_k)
        fetch_k = top_k * 2

        # Primary text filters: CGI article ref variants ONLY
        # For short refs (e.g., "39"), _generate_article_variants returns
        # CGI-contextualized variants; don't add the bare ref which would
        # match everything containing "39" via ILIKE.
        article_text_filters = _generate_article_variants(article_ref)
        if not _is_short_article_ref(article_ref):
            article_text_filters = [article_ref] + article_text_filters

        settings = get_settings()
        effective_min_sim = min_similarity_override if min_similarity_override is not None else settings.search_min_similarity
        raw_results = self.hybrid_search(
            keywords=article_keywords,
            semantic_query=semantic_query,
            date_start=date_start,
            date_end=date_end,
            top_k=fetch_k,
            keyword_boost=settings.search_keyword_boost,
            min_similarity=effective_min_sim,
            explain=explain,
            article_text_filters=article_text_filters,
        )

        # Define filter keywords with two tiers:
        # Tier 1 (strict): Article ref and its variants - highest relevance
        tier1_keywords = [article_ref] + _generate_article_variants(article_ref)

        # Tier 2 (relaxed): All article keywords - broader relevance
        tier2_keywords = list(article_keywords)

        # Check if the store already did text-level filtering (pgvector path).
        # If so, text-matched results are already guaranteed relevant —
        # skip aggressive tier filtering to avoid dropping them.
        store_has_text_filter = hasattr(self.debate_store, "search_with_text_filter")

        # Filter results with tiered approach
        filtered_results = {}

        for key in ["debates", "amendments"]:
            if key in raw_results:
                if store_has_text_filter:
                    # pgvector path: text-matched results already prioritized by store.
                    # For short CGI refs ("39", "200"), ILIKE '%39%' matches thousands
                    # of irrelevant chunks. Use word-boundary regex so has_article_ref
                    # doesn't trigger on "2039". require_context=True ensures chunks
                    # without the CGI ref need a specific multi-word keyword.
                    ctx_patterns = None
                    if _is_short_article_ref(article_ref):
                        ctx_patterns = _build_contextualized_patterns(article_ref)

                    strict_filtered = filter_chunks_by_keywords(
                        raw_results[key],
                        tier1_keywords + tier2_keywords,
                        min_required=1,
                        article_ref=article_ref,
                        require_context=True,
                        article_ref_patterns=ctx_patterns,
                    )
                else:
                    # In-memory path: needs aggressive filtering because vector
                    # search returns many false positives without text pre-filtering.
                    strict_filtered = filter_chunks_by_keywords(
                        raw_results[key],
                        tier1_keywords,
                        min_required=1,
                        article_ref=article_ref,
                        require_context=False,
                    )

                    # If not enough results, use relaxed filtering (tier 2)
                    # But require context for generic keywords
                    if len(strict_filtered) < 3:
                        relaxed_filtered = filter_chunks_by_keywords(
                            raw_results[key],
                            tier2_keywords,
                            min_required=settings.search_tier2_min_required,
                            article_ref=article_ref,
                            require_context=True,  # Must include a specific keyword
                        )
                        # Combine: strict first, then relaxed (avoiding duplicates)
                        seen_ids = {id(c) for c, _ in strict_filtered}
                        for chunk, score in relaxed_filtered:
                            if id(chunk) not in seen_ids:
                                strict_filtered.append((chunk, score))
                                seen_ids.add(id(chunk))

                # Rerank with cross-encoder if available
                if self.reranker and strict_filtered:
                    logger.info(
                        f"Reranking {len(strict_filtered)} {key} chunks for {article_ref} "
                        f"({law_type} {law_year})"
                    )
                    # min_score: discard chunks the cross-encoder considers
                    # clearly irrelevant. Threshold is lenient because the
                    # ms-marco model is English-trained; French text scores
                    # systematically lower. Primary filtering is keyword-based.
                    strict_filtered = self.reranker.rerank(
                        semantic_query, strict_filtered, top_k=top_k,
                        min_score=settings.reranker_min_score,
                    )
                else:
                    strict_filtered = strict_filtered[:top_k]

                filtered_results[key] = strict_filtered

                # Log warning if still few relevant chunks
                if len(filtered_results[key]) < min_relevant:
                    logger.warning(
                        f"Only {len(filtered_results[key])} {key} chunks mention article {article_ref} "
                        f"or keywords (expected >= {min_relevant}). Search may have low relevance."
                    )
            else:
                filtered_results[key] = []

        # --- Secondary search: law article number variants ---
        # Law article numbers (e.g., "article 18") are too ambiguous to mix
        # with CGI text filters — they flood primary results with false positives.
        # Instead, run a SEPARATE search and fill remaining slots after primary.
        # Strict post-filtering (require_context=True) ensures only topically
        # relevant chunks pass through.
        law_article_variants = _generate_law_article_variants(article_in_law)
        if law_article_variants and store_has_text_filter:
            for key in ["debates", "amendments"]:
                remaining = top_k - len(filtered_results.get(key, []))
                if remaining <= 0:
                    continue

                logger.info(
                    f"Secondary search for {key}: law article variants "
                    f"{law_article_variants} ({remaining} slots remaining)"
                )
                secondary_raw = self.hybrid_search(
                    keywords=article_keywords,
                    semantic_query=semantic_query,
                    date_start=date_start,
                    date_end=date_end,
                    top_k=fetch_k,
                    keyword_boost=settings.search_keyword_boost,
                    min_similarity=effective_min_sim,
                    explain=explain,
                    article_text_filters=law_article_variants,
                )

                if key not in secondary_raw:
                    continue

                # Strict filtering: require context to prevent false positives.
                # A chunk mentioning "article 18" must also contain a topic-specific
                # keyword (e.g., "apport-cession") to be considered relevant.
                secondary_filtered = filter_chunks_by_keywords(
                    secondary_raw[key],
                    tier2_keywords,
                    min_required=1,
                    article_ref=article_ref,
                    require_context=True,
                )

                if self.reranker and secondary_filtered:
                    secondary_filtered = self.reranker.rerank(
                        semantic_query, secondary_filtered, top_k=remaining,
                        min_score=settings.reranker_min_score,
                    )
                else:
                    secondary_filtered = secondary_filtered[:remaining]

                # Merge: dedup, then either fill remaining slots or replace
                # weakest primary chunks if secondary scores higher.
                existing_contents = {c.content[:200] for c, _ in filtered_results[key]}
                unique_secondary = [
                    (c, s) for c, s in secondary_filtered
                    if c.content[:200] not in existing_contents
                ]

                primary_list = filtered_results[key]

                if len(primary_list) >= top_k and unique_secondary:
                    # Primary is full — replace weakest with stronger secondary
                    for sec_chunk, sec_score in unique_secondary:
                        min_idx = min(range(len(primary_list)), key=lambda i: primary_list[i][1])
                        if sec_score > primary_list[min_idx][1]:
                            primary_list[min_idx] = (sec_chunk, sec_score)
                        else:
                            break  # secondary sorted desc; no more replacements
                    primary_list.sort(key=lambda x: x[1], reverse=True)
                    filtered_results[key] = primary_list[:top_k]
                else:
                    # Primary has room — append
                    for chunk, score in unique_secondary:
                        if remaining <= 0:
                            break
                        filtered_results[key].append((chunk, score))
                        remaining -= 1

        # BM25-only fallback for empty result sets
        for key in ["debates", "amendments"]:
            if not filtered_results.get(key):
                bm25_index = self.debate_bm25 if key == "debates" else self.amendment_bm25
                if bm25_index is not None:
                    logger.info(f"BM25-only fallback for {key}, article {article_ref}")
                    bm25_results = bm25_index.search(f"article {article_ref}", top_k=top_k * 2)
                    # Date-filter the BM25 results
                    if date_start or date_end:
                        bm25_results = [
                            (c, s) for c, s in bm25_results
                            if self._passes_date_filter(c, date_start, date_end)
                        ]
                    # Apply tier 1 keyword filter
                    bm25_filtered = filter_chunks_by_keywords(
                        bm25_results, tier1_keywords, min_required=1,
                        article_ref=article_ref, require_context=False,
                    )
                    filtered_results[key] = bm25_filtered[:top_k]

        # ILIKE-only fallback for external stores (pgvector) when BM25 is unavailable
        for key in ["debates", "amendments"]:
            if not filtered_results.get(key):
                store = self.debate_store if key == "debates" else self.amendment_store
                if hasattr(store, "text_only_search"):
                    logger.info(f"ILIKE-only fallback for {key}, article {article_ref}")
                    ilike_results = store.text_only_search(
                        text_filters=article_text_filters,
                        top_k=top_k * 2,
                        date_start=date_start.isoformat() if date_start else None,
                        date_end=date_end.isoformat() if date_end else None,
                    )
                    # Apply tier 1 keyword filter
                    ilike_filtered = filter_chunks_by_keywords(
                        ilike_results, tier1_keywords, min_required=1,
                        article_ref=article_ref, require_context=False,
                    )
                    filtered_results[key] = ilike_filtered[:top_k]

        return filtered_results


def format_search_result_markdown(result: SearchResult) -> str:
    """Format SearchResult as Markdown for display."""
    from src.indexing.intent_extraction import format_intent_result_markdown

    lines = []

    lines.append("# Analyse de l'article")
    lines.append("")

    if result.intent:
        lines.append(format_intent_result_markdown(result.intent))
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("## Débats pertinents trouvés")
    lines.append("")

    if result.relevant_debates:
        for i, (chunk, similarity) in enumerate(result.relevant_debates[:5], 1):
            lines.append(f"### Extrait {i} (similarité: {similarity:.2%})")
            lines.append("")

            metadata = chunk.metadata
            meta_parts = []
            if metadata.get("orateur"):
                meta_parts.append(f"**{metadata['orateur']}**")
            if metadata.get("fonction"):
                meta_parts.append(f"({metadata['fonction']})")
            if metadata.get("chambre"):
                meta_parts.append(f"— {metadata['chambre']}")
            if metadata.get("date"):
                meta_parts.append(f"— {metadata['date']}")
            if meta_parts:
                lines.append(" ".join(meta_parts))
            lines.append("")

            # Truncate long content
            content = chunk.content
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"> {content}")
            lines.append("")
    else:
        lines.append("*Aucun débat pertinent trouvé*")
        lines.append("")

    lines.append("## Amendements pertinents")
    lines.append("")

    if result.relevant_amendments:
        for i, (chunk, similarity) in enumerate(result.relevant_amendments[:3], 1):
            lines.append(f"### Amendement {i} (similarité: {similarity:.2%})")
            lines.append("")

            metadata = chunk.metadata
            if metadata.get("numero"):
                lines.append(f"**N° {metadata['numero']}** ")
            if metadata.get("auteur"):
                lines.append(f"par {metadata['auteur']} ")
            if metadata.get("sort"):
                lines.append(f"— *{metadata['sort']}*")
            lines.append("")
            lines.append("")

            content = chunk.content
            if len(content) > 300:
                content = content[:300] + "..."
            lines.append(f"> {content}")
            lines.append("")
    else:
        lines.append("*Aucun amendement pertinent trouvé*")

    return "\n".join(lines)
