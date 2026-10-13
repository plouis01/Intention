"""Search service for finding relevant debates and extracting intent."""

import html as _html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from sqlalchemy import String, or_ as sa_or

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


def _keyword_matches(keyword: str, content: str) -> bool:
    """Test si un keyword matche dans le contenu, avec tolérance singulier/pluriel.

    En français, les groupes nominaux changent de nombre dans le discours :
    "condition de réinvestissement" doit matcher "conditions de réinvestissement".

    Stratégie : on génère une variante en ajoutant/retirant un 's' au premier
    mot du groupe nominal. Le reste est matché par substring naturellement
    ("sociétés bénéficiaire" est contenu dans "sociétés bénéficiaires").
    """
    kw = _normalize_text(keyword)
    text = content if content == content.lower() else _normalize_text(content)

    # Match exact (substring)
    if kw in text:
        return True

    # Variante singulier/pluriel sur le premier mot du groupe
    words = kw.split()
    if not words:
        return False

    first = words[0]
    rest = " ".join(words[1:])

    # Tester avec 's' ajouté au premier mot
    if not first.endswith("s"):
        variant = first + "s"
        if rest:
            variant += " " + rest
        if variant in text:
            return True

    # Tester avec 's' retiré du premier mot
    if first.endswith("s") and len(first) > 3:
        variant = first[:-1]
        if rest:
            variant += " " + rest
        if variant in text:
            return True

    return False


@dataclass
class ScoreExplanation:
    """Breakdown of how a chunk's final score was computed."""

    semantic_similarity: float = 0.0
    keyword_boost: float = 0.0
    speaker_boost: float = 0.0
    bm25_score: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)
    speaker_role: str = ""


@dataclass
class RelevanceSignals:
    """Multi-signal relevance breakdown for the bicameral pipeline."""

    semantic_similarity: float = 0.0
    article_ref_explicit: bool = False
    articles_mentionnes_match: bool = False
    keyword_match_count: int = 0
    keyword_specificity: float = 0.0
    speaker_authority: float = 0.0
    etape_weight: float = 0.5
    temporal_proximity: float = 0.5
    amendment_linked: bool = False
    sibling_penalty: float = 0.0


def _compute_relevance_score(signals: RelevanceSignals, settings=None) -> float:
    """Compute unified relevance score from multi-signal breakdown."""
    if settings is None:
        settings = get_settings()

    score = signals.semantic_similarity * settings.signal_weight_semantic
    if signals.article_ref_explicit or signals.articles_mentionnes_match:
        score += settings.signal_weight_article_ref
    score += min(signals.keyword_specificity, 1.0) * settings.signal_weight_keywords
    score += signals.speaker_authority * settings.signal_weight_speaker
    score += signals.etape_weight * settings.signal_weight_etape
    score += signals.temporal_proximity * settings.signal_weight_temporal
    if signals.amendment_linked:
        score += settings.signal_weight_amendment
    # Sibling penalty (reduces score when chunk matches sibling terms but not target)
    score -= signals.sibling_penalty
    return max(min(score, 1.0), 0.0)


# Default expansions file path
DEFAULT_EXPANSIONS_PATH = Path(__file__).parent.parent.parent / "data" / "legal_term_expansions.json"
DEFAULT_INDEX_PATH = Path(__file__).parent.parent.parent / "data" / "modifications_cgi_enriched.json"

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
    Auto-generate term expansions from modifications_cgi_enriched.json keywords.

    Extracts keywords per article, filters out generic single words (< 4 chars,
    French stopwords), and returns a dict mapping article refs to keyword lists.

    Args:
        index_path: Path to modifications_cgi_enriched.json (uses default if None)

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


_OBJET_STOPWORDS = frozenset({
    "de", "du", "des", "le", "la", "les", "un", "une", "et", "ou", "en", "au", "aux",
    "par", "pour", "dans", "sur", "avec", "est", "sont", "qui", "que", "ce", "cette",
    "son", "ses", "leur", "leurs", "même", "article", "alinéa", "paragraphe",
    "ajout", "suppression", "modification", "mineure", "mise", "forme", "caractères",
    "abrogé", "abrogation", "création", "l'article", "d'un", "d'une",
})


def _select_best_ilike_keywords(article_keywords: list[str], top_k: int = 7) -> list[str]:
    """Select the best keywords for ILIKE expansion queries.

    Strategy: curated multiword non-generic terms first (highest precision),
    then fill with remaining multiword non-generic terms sorted by length.
    """
    # Load curated expansions (cached by load_legal_term_expansions)
    from src.analysis.keyword_generator import load_legal_term_expansions
    expansions = load_legal_term_expansions()

    # Build curated set for quick lookup
    curated_flat = set()
    for terms in expansions.values():
        curated_flat.update(t.lower() for t in terms)

    # Filter candidates: >5 chars, no commas, <=40 chars, not article refs, not pure digits
    # Also exclude article reference variants (e.g. "150 0 B ter", "787 B")
    _ref_variant_re = re.compile(r'^[\d\s\-]+(?:[a-zA-Z]{1,5}\s*)*$')
    candidates = [
        kw for kw in article_keywords
        if len(kw) > 5 and "," not in kw and len(kw) <= 40
        and not kw.lower().startswith("article ")
        and not kw.lower().startswith("l'article ")
        and not kw.lower().replace("-", "").replace(" ", "").isdigit()
        and not _ref_variant_re.match(kw.strip())
    ]

    # Prefer multiword non-generic terms
    from src.analysis.article_analyzer import ArticleAnalyzer
    generic = ArticleAnalyzer._get_generic_keywords()

    multiword_non_generic = [
        kw for kw in candidates
        if (" " in kw or "-" in kw) and kw not in generic
    ]

    # Split into curated and non-curated
    curated_kw = [kw for kw in multiword_non_generic if kw.lower() in curated_flat]
    non_curated_kw = [kw for kw in multiword_non_generic if kw.lower() not in curated_flat]

    # Curated first (highest precision), then fill by length
    non_curated_kw.sort(key=len, reverse=True)
    result = curated_kw[:top_k]
    for kw in non_curated_kw:
        if len(result) >= top_k:
            break
        if kw not in result:
            result.append(kw)

    return result


def _extract_objet_keywords(objet: str, max_terms: int = 3) -> list[str]:
    """Extract significant terms from a modification's objet field."""
    if not objet:
        return []
    # Remove quotes, parentheses, special chars
    clean = re.sub(r'[«»""()\[\]0-9]', ' ', objet)
    words = clean.lower().split()
    # Keep words > 5 chars not in stopwords
    candidates = [
        w for w in words
        if len(w) > 5 and w not in _OBJET_STOPWORDS
        and not w.startswith("l'") and not w.startswith("d'")
    ]
    # Deduplicate preserving order, take longest first
    seen = set()
    unique = []
    for w in sorted(candidates, key=len, reverse=True):
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return unique[:max_terms]


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
        # Short refs are highly ambiguous in ILIKE - require CGI context
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
        # Skip single-digit numbers - too many false positives
        # "article 3" matches 24K+ chunks vs "article 24" matches ~2K
        if len(num) == 1 and num.isdigit():
            continue
        variants.append(f"article {num}")
        variants.append(f"art. {num}")

    return variants


def _directional_date_filter(
    anchor_source_ids: list[str],
    anchor_dates: dict[str, date],
    all_modification_dates: list[tuple[date, str]] | None,
    current_mod_date: date | None,
    date_start: date | None,
    date_end: date | None,
) -> list[str]:
    """Assign debate to closest FUTURE modification only.

    A debate can only inform a law that hasn't been enacted yet.
    Falls back to keeping the debate if it's within the explicit search window.
    """
    if not all_modification_dates or not current_mod_date:
        return list(anchor_source_ids)

    filtered_ids = []
    for sid in anchor_source_ids:
        if sid not in anchor_dates:
            filtered_ids.append(sid)
            continue
        debate_dt = anchor_dates[sid]

        best_date = None
        best_dist = float("inf")
        for mod_dt, mod_ltype in all_modification_dates:
            if mod_dt < debate_dt:
                continue  # Modification already enacted
            dist = (mod_dt - debate_dt).days
            if dist < best_dist or (dist == best_dist and mod_ltype.startswith("LFR")):
                best_dist = dist
                best_date = mod_dt

        if best_date is None:
            # Debate after all modifications - keep if within search window
            if date_start and date_end and date_start <= debate_dt <= date_end:
                filtered_ids.append(sid)
        elif best_date == current_mod_date:
            # Reject debates too far from the modification (likely from an unrelated LF)
            # LF debates happen ~3-15 months before promulgation (60-450 days)
            # A debate >400 days before promulgation is almost certainly from a prior PLF
            if best_dist <= 400:
                filtered_ids.append(sid)
            else:
                logger.debug(
                    f"Directional filter: rejecting debate {sid} "
                    f"(distance={best_dist} days > 400 to {current_mod_date})"
                )

    if len(filtered_ids) < len(anchor_source_ids):
        logger.info(
            f"Directional date filter: {len(anchor_source_ids)} → {len(filtered_ids)} debates"
        )
    return filtered_ids


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
    # Check remainder after numeric prefix - if it contains additional
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
        # "39 du CGI" - "du" already anchors, no lookahead needed
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


# Stopwords for French legal text - too common to be meaningful alone
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

    # Multi-word phrase or hyphenated compound with 2+ meaningful words → specific
    words = re.split(r'[\s\-]+', kw_lower)
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

        # Count keyword matches (with singular/plural tolerance)
        matches = sum(1 for kw in required_keywords if _keyword_matches(kw, content_lower))

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
                if _keyword_matches(kw, content_lower)
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
    """Count how many keywords appear in content (with singular/plural tolerance)."""
    content_lower = _normalize_text(content)
    return sum(1 for kw in keywords if _keyword_matches(kw, content_lower))


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

        # Optimization: when debate_store and amendment_store are the same external store
        # (pgvector), use a combined UNION ALL query to halve SQL round-trips (4 → 2).
        same_external_store = (
            search_debates and search_amendments
            and self.debate_store is self.amendment_store
            and hasattr(self.debate_store, "search_with_text_filter_combined")
            and article_text_filters
        )

        if same_external_store:
            store = self.debate_store
            combined = store.search_with_text_filter_combined(
                query_embedding,
                text_filters=article_text_filters,
                chunk_types=["intervention", "amendement"],
                top_k=top_k * 3,
                min_similarity=min_similarity,
                date_start=date_start.isoformat() if date_start else None,
                date_end=date_end.isoformat() if date_end else None,
            )
            # Apply keyword boosting + speaker importance (same as _hybrid_search_external_store)
            keywords_lower = [_normalize_text(kw) for kw in expanded_keywords]
            for key, chunk_type_key in [("intervention", "debates"), ("amendement", "amendments")]:
                raw = combined.get(key, [])
                scored = []
                for chunk, similarity in raw:
                    if not self._passes_date_filter(chunk, date_start, date_end):
                        continue
                    score = similarity
                    content_lower = _normalize_text(chunk.content)
                    matched = [kw for kw in keywords_lower if kw in content_lower]
                    if matched:
                        score += keyword_boost
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
                results[chunk_type_key] = scored[:top_k]
        else:
            # Fallback: separate searches (in-memory stores or non-pgvector)
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
            return False  # No date metadata - exclude from date-filtered searches

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
            return False  # Unparseable date - exclude rather than silently pass

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

    # =========================================================================
    # Bicameral search pipeline (Phase 1-3)
    # =========================================================================

    def search_chamber(
        self,
        debate_ids: list[str],
        article_ref: str,
        article_keywords: list[str],
        article_in_law: str,
        chambre: str,
        law_type: str = "",
        law_year: int = 0,
        date_start: date | None = None,
        date_end: date | None = None,
        top_k: int = 15,
        query_embedding: list[float] | None = None,
        modification_objet: str = "",
        sibling_negative_terms: list[str] | None = None,
        mod_date: date | None = None,
    ) -> dict[str, list[tuple[Chunk, float]]]:
        """Search for relevant debates within a specific chamber's dossier.

        Implements the triple-anchor strategy:
          Anchor 1: article_in_law in articles_mentionnes (scoped to dossier)
          Anchor 2: article_ref CGI in articles_mentionnes or content
          Anchor 3: ILIKE keywords (fallback)

        Then performs vector search, multi-signal scoring, and reranking.

        Returns:
            Dict with 'debates' and 'amendments' keys containing scored results.
        """
        settings = get_settings()
        store = self.debate_store

        if not debate_ids:
            # Orphan-law fallback: when the law has no dossier scope, search the
            # whole DB within the date window passed by the caller, limited to
            # the requested chamber. Requires both date_start and date_end.
            if date_start and date_end:
                debate_ids = self._discover_orphan_chamber_debates(
                    chambre=chambre,
                    date_start=date_start,
                    date_end=date_end,
                    article_ref=article_ref,
                    article_keywords=article_keywords,
                    article_in_law=article_in_law,
                    modification_objet=modification_objet,
                )
                if not debate_ids:
                    logger.info(
                        f"  [{chambre}] Orphan-law fallback found no matching debates "
                        f"in [{date_start}→{date_end}]"
                    )
                    return {"debates": [], "amendments": []}
                logger.info(
                    f"  [{chambre}] Orphan-law fallback: {len(debate_ids)} debates "
                    f"in [{date_start}→{date_end}]"
                )
            else:
                logger.info(f"No debate_ids for {chambre}, skipping search")
                return {"debates": [], "amendments": []}

        # --- Phase 1: Find relevant debates via triple anchor ---
        relevant_debate_ids = self._discover_chamber_debates(
            debate_ids=debate_ids,
            article_ref=article_ref,
            article_keywords=article_keywords,
            article_in_law=article_in_law,
            chambre=chambre,
            modification_objet=modification_objet,
        )

        if not relevant_debate_ids:
            logger.info(f"  [{chambre}] No relevant debates found")
            return {"debates": [], "amendments": []}

        # Limit to 20 debates max per chamber
        if len(relevant_debate_ids) > 20:
            relevant_debate_ids = relevant_debate_ids[:20]

        logger.info(f"  [{chambre}] {len(relevant_debate_ids)} relevant debates found")

        # --- Phase 2: Vector search within discovered debates ---
        fetch_k = top_k * 2

        if query_embedding is None:
            # Build chamber-adapted semantic query
            kw_str = ", ".join(article_keywords[:5]) if article_keywords else ""
            if chambre == "AN":
                semantic_query = (
                    f"Pourquoi modifier l'article {article_ref} du CGI ({kw_str}). "
                    f"Débat Assemblée nationale PLF {law_year}. "
                    f"Objectifs du gouvernement et justifications."
                )
            else:
                semantic_query = (
                    f"Dispositif de l'article {article_ref} du CGI ({kw_str}). "
                    f"Analyse du rapporteur, commission des finances, Sénat, {law_type} {law_year}."
                )
            query_embedding = self.embedding_service.generate_embedding(semantic_query)

        if not hasattr(store, "search_in_source_ids"):
            logger.warning(f"  [{chambre}] Store lacks search_in_source_ids, returning empty")
            return {"debates": [], "amendments": []}

        raw_results = store.search_in_source_ids(
            query_embedding=query_embedding,
            source_ids=relevant_debate_ids,
            chunk_types=["intervention", "amendement"],
            top_k=fetch_k,
            min_similarity=settings.search_min_similarity,
            date_start=date_start.isoformat() if date_start else None,
            date_end=date_end.isoformat() if date_end else None,
            article_ref=article_ref,
        )

        # Inject ILIKE-guaranteed chunks (explicit article ref mentions)
        if hasattr(store, "search_by_text_in_source_ids"):
            existing_contents = set()
            for ct in raw_results:
                for chunk, _ in raw_results[ct]:
                    existing_contents.add(chunk.content[:100])
            ilike_added = 0
            ilike_results = store.search_by_text_in_source_ids(
                text_filter=article_ref,
                source_ids=relevant_debate_ids,
                chunk_types=["intervention", "amendement"],
                top_k=10,
            )
            for ct in ilike_results:
                for chunk, score in ilike_results[ct]:
                    if chunk.content[:100] not in existing_contents:
                        raw_results.setdefault(ct, []).append((chunk, max(score, 0.50)))
                        existing_contents.add(chunk.content[:100])
                        ilike_added += 1
            if ilike_added:
                logger.info(f"  [{chambre}] ILIKE guarantee: +{ilike_added} chunks")

        # --- Multi-signal scoring ---
        ref_lower = article_ref.lower()
        ref_nospace = ref_lower.replace("-", " ")
        keywords_lower = [_normalize_text(kw) for kw in article_keywords]

        scored_results: dict[str, list[tuple[Chunk, float]]] = {}
        for ct, results_key in [("intervention", "debates"), ("amendement", "amendments")]:
            scored = []
            for chunk, similarity in raw_results.get(ct, []):
                signals = self._compute_signals(
                    chunk, similarity, ref_lower, ref_nospace,
                    keywords_lower, article_keywords, settings,
                    date_start, date_end,
                    sibling_negative_terms=sibling_negative_terms,
                )
                score = _compute_relevance_score(signals, settings)
                scored.append((chunk, score))
            scored.sort(key=lambda x: x[1], reverse=True)
            scored_results[results_key] = scored[:fetch_k]

            # Diagnostic: log top chunks with signal breakdown
            if scored:
                explicit_count = sum(
                    1 for c, _ in scored if ref_lower in c.content.lower()
                    or ref_nospace in c.content.lower()
                )
                avg_score = sum(s for _, s in scored[:10]) / min(10, len(scored))
                logger.info(
                    f"  [{chambre}] {results_key}: {len(scored)} scored, "
                    f"{explicit_count} explicit ref, avg_top10={avg_score:.2f}"
                )

        # --- Strict temporal filter ---
        # Dossier date windows can span 7+ years (multiple PLF in same dossier).
        # Enforce a hard window: mod_date - 18 months → mod_date + 1 month.
        if mod_date:
            hard_start = mod_date - timedelta(days=540)  # 18 months before
            hard_end = mod_date + timedelta(days=30)     # 1 month after
            for key in ["debates", "amendments"]:
                before = len(scored_results.get(key, []))
                filtered = []
                for chunk, score in scored_results.get(key, []):
                    chunk_date_str = chunk.metadata.get("date", "")
                    if not chunk_date_str:
                        filtered.append((chunk, score))  # Keep undated chunks
                        continue
                    try:
                        chunk_dt = date.fromisoformat(chunk_date_str[:10])
                        if hard_start <= chunk_dt <= hard_end:
                            filtered.append((chunk, score))
                    except (ValueError, TypeError):
                        filtered.append((chunk, score))
                scored_results[key] = filtered
                removed = before - len(filtered)
                if removed:
                    logger.info(
                        f"  [{chambre}] Temporal filter ({hard_start}→{hard_end}): "
                        f"removed {removed}/{before} {key} outside window"
                    )

        # --- Cross-encoder rerank per chamber ---
        kw_str = ", ".join(article_keywords[:5]) if article_keywords else ""
        rerank_query = (
            f"Intention du législateur pour l'article {article_ref} du CGI "
            f"({kw_str}). {law_type} {law_year}."
        )
        # Compute LF-article identifier (the article number IN the modifying law) so
        # we can rescue chunks that discuss it even if they never name the CGI ref.
        # The cross-encoder otherwise cuts those chunks because the rerank_query is
        # phrased around the CGI ref, not the LF article — yet they are the very
        # chunks that explain the reform's intent (e.g. LF 2017-1837 art. 31 created
        # the IFI suite including CGI 973).
        lf_art_num = ""
        if article_in_law:
            _m = re.match(r"^Art\.?\s*(\S+)", article_in_law)
            if _m:
                lf_art_num = _m.group(1).strip().lower()

        def _mentions_lf_article(metadata: dict) -> bool:
            if not lf_art_num:
                return False
            mentions = metadata.get("articles_mentionnes") or []
            for raw in mentions:
                m_norm = str(raw).strip().lower()
                if m_norm == lf_art_num:
                    return True
                # Suffixed siblings of the same article: "31 bis", "31 ter", "31a"
                if m_norm.startswith(lf_art_num) and len(m_norm) > len(lf_art_num):
                    nxt = m_norm[len(lf_art_num)]
                    if not nxt.isdigit():
                        return True
            return False

        for key in ["debates", "amendments"]:
            chunks = scored_results.get(key, [])
            if self.reranker and chunks:
                # Save chunks with explicit article ref OR mentioning the LF article
                # — they should survive reranking
                explicit_chunks = []
                lf_rescue_count = 0
                for c, s in chunks:
                    has_cgi_ref = (
                        ref_lower in c.content.lower()
                        or ref_nospace in c.content.lower()
                        or (c.metadata.get("articles_mentionnes") and
                            any(ref_lower in m.lower() for m in c.metadata["articles_mentionnes"]))
                    )
                    if has_cgi_ref:
                        explicit_chunks.append((c, s))
                    elif _mentions_lf_article(c.metadata):
                        explicit_chunks.append((c, s))
                        lf_rescue_count += 1
                if lf_rescue_count:
                    logger.info(
                        f"  [{chambre}] LF-article rescue pool: +{lf_rescue_count} chunks "
                        f"mentioning Art. {lf_art_num} (modifying article for CGI {article_ref})"
                    )

                # [DIAG:RERANK] pre-rerank state
                logger.info(
                    f"[DIAG:RERANK] chambre={chambre} key={key} art={article_ref!r} "
                    f"input_chunks={len(chunks)} explicit_pool={len(explicit_chunks)} "
                    f"lf_rescue_pool={lf_rescue_count} top_k={top_k} "
                    f"min_score={settings.reranker_min_score}"
                )

                reranked = self.reranker.rerank(
                    rerank_query, chunks, top_k=top_k,
                    min_score=settings.reranker_min_score,
                )

                # Re-inject explicit chunks that were cut by the reranker
                reranked_ids = {c.content[:80] for c, _ in reranked}
                rescued = 0
                _rescue_capped = 0
                for c, s in explicit_chunks:
                    if c.content[:80] not in reranked_ids:
                        if len(reranked) < top_k:
                            reranked.append((c, max(s, 0.5)))
                            rescued += 1
                        else:
                            _rescue_capped += 1
                if rescued:
                    reranked.sort(key=lambda x: x[1], reverse=True)
                    logger.info(
                        f"  [{chambre}] Rescued {rescued} explicit/LF-article chunks "
                        f"cut by reranker"
                    )

                # [DIAG:RERANK] post-rerank with rescue accounting
                logger.info(
                    f"[DIAG:RERANK] chambre={chambre} key={key} reranker_kept="
                    f"{len(reranked) - rescued} rescued={rescued} "
                    f"rescue_capped_by_topk={_rescue_capped} final={len(reranked)}"
                )

                scored_results[key] = reranked
                logger.info(
                    f"  [{chambre}] Reranked {len(chunks)} → {len(reranked)} {key}"
                )
            else:
                scored_results[key] = chunks[:top_k]

        return scored_results

    def _discover_chamber_debates(
        self,
        debate_ids: list[str],
        article_ref: str,
        article_keywords: list[str],
        article_in_law: str,
        chambre: str,
        modification_objet: str = "",
    ) -> list[str]:
        """Find relevant debates within a chamber via triple-anchor discovery.

        Anchor 1: article_in_law in articles_mentionnes (scoped to dossier debates)
        Anchor 2: article_ref CGI in articles_mentionnes or contenu
        Anchor 3: ILIKE keywords on contenu (fallback)
        """
        from src.db.models import (
            Debat,
            Intervention,
            get_session as _get_session,
        )

        settings = get_settings()
        debate_id_ints = [int(d) for d in debate_ids if d.isdigit()]
        if not debate_id_ints:
            return []

        try:
            with _get_session() as db:
                found_ids: dict[str, str] = {}  # debate_id → anchor_type

                # --- Anchor 1: article_in_law in articles_mentionnes ---
                if settings.articles_mentionnes_filter and article_in_law:
                    art_num = re.sub(r"^Art\.?\s*", "", article_in_law).strip()
                    # Handle multi-article: "Art. 32, 33" → ["32", "33"]
                    art_nums = [n.strip() for n in art_num.split(",") if n.strip()]
                    if art_nums:
                        mention_conds = []
                        for num in art_nums:
                            if len(num) >= 2 or not num.isdigit():
                                mention_conds.append(
                                    Intervention.articles_mentionnes.cast(String).ilike(f'%"{num}"%')
                                )
                                # Also try without quotes (JSON format varies)
                                mention_conds.append(
                                    Intervention.articles_mentionnes.cast(String).ilike(f'%{num}%')
                                )
                        if mention_conds:
                            p1_results = (
                                db.query(Debat.id)
                                .join(Intervention, Intervention.debat_id == Debat.id)
                                .filter(
                                    Debat.id.in_(debate_id_ints),
                                    sa_or(*mention_conds),
                                )
                                .distinct().all()
                            )
                            for r in p1_results:
                                found_ids[str(r[0])] = "article_in_law"
                            logger.info(
                                f"  [{chambre}] Anchor 1 (article_in_law '{article_in_law}'): "
                                f"{len(p1_results)} debates"
                            )

                # --- Anchor 2: article_ref CGI in articles_mentionnes or contenu ---
                ref_lower = article_ref.lower()
                ref_variants = [ref_lower, ref_lower.replace("-", " "), ref_lower.replace(" ", "")]

                # 2a: articles_mentionnes
                if settings.articles_mentionnes_filter:
                    cgi_mention_conds = [
                        Intervention.articles_mentionnes.cast(String).ilike(f'%{v}%')
                        for v in ref_variants
                    ]
                    p2a_results = (
                        db.query(Debat.id)
                        .join(Intervention, Intervention.debat_id == Debat.id)
                        .filter(
                            Debat.id.in_(debate_id_ints),
                            sa_or(*cgi_mention_conds),
                        )
                        .distinct().all()
                    )
                    for r in p2a_results:
                        if str(r[0]) not in found_ids:
                            found_ids[str(r[0])] = "cgi_ref_mentions"

                # 2b: ILIKE on contenu for CGI ref
                cgi_ilike_conds = [
                    Intervention.contenu.ilike(f'%{article_ref}%')
                ]
                if " " in article_ref:
                    cgi_ilike_conds.append(
                        Intervention.contenu.ilike(f'%{article_ref.replace(" ", "%")}%')
                    )
                p2b_results = (
                    db.query(Debat.id)
                    .join(Intervention, Intervention.debat_id == Debat.id)
                    .filter(
                        Debat.id.in_(debate_id_ints),
                        sa_or(*cgi_ilike_conds),
                    )
                    .distinct().all()
                )
                for r in p2b_results:
                    if str(r[0]) not in found_ids:
                        found_ids[str(r[0])] = "cgi_ref_ilike"

                logger.info(
                    f"  [{chambre}] Anchor 2 (CGI ref '{article_ref}'): "
                    f"{len(p2a_results) if settings.articles_mentionnes_filter else 0} mentions + "
                    f"{len(p2b_results)} ILIKE"
                )

                # --- Anchor 3: ILIKE keywords (fallback if < 3 debates found) ---
                if len(found_ids) < 3:
                    _ilike_kw = _select_best_ilike_keywords(article_keywords, top_k=5)
                    kw_conditions = [
                        Intervention.contenu.ilike(f'%{kw}%') for kw in _ilike_kw
                    ]
                    if modification_objet:
                        objet_kw = _extract_objet_keywords(modification_objet)
                        for okw in objet_kw:
                            kw_conditions.append(Intervention.contenu.ilike(f'%{okw}%'))

                    if kw_conditions:
                        # Try AND first (2+ keywords) for higher precision
                        # This avoids flooding with debates matching only "réinvestissement"
                        p3_and_results = []
                        if len(_ilike_kw) >= 2:
                            and_pairs = [
                                (_ilike_kw[j], _ilike_kw[k])
                                for j in range(min(3, len(_ilike_kw)))
                                for k in range(j + 1, min(4, len(_ilike_kw)))
                            ]
                            for kw1, kw2 in and_pairs:
                                pair_results = (
                                    db.query(Debat.id)
                                    .join(Intervention, Intervention.debat_id == Debat.id)
                                    .filter(
                                        Debat.id.in_(debate_id_ints),
                                        Intervention.contenu.ilike(f'%{kw1}%'),
                                        Intervention.contenu.ilike(f'%{kw2}%'),
                                    )
                                    .distinct().all()
                                )
                                for r in pair_results:
                                    if str(r[0]) not in found_ids:
                                        p3_and_results.append(r)

                        new_from_and = 0
                        for r in p3_and_results:
                            if str(r[0]) not in found_ids:
                                found_ids[str(r[0])] = "keyword_and"
                                new_from_and += 1
                        if new_from_and:
                            logger.info(
                                f"  [{chambre}] Anchor 3a (AND keywords): +{new_from_and} debates"
                            )

                        # If still < 3, fall back to OR (original behavior)
                        if len(found_ids) < 3:
                            p3_or_results = (
                                db.query(Debat.id)
                                .join(Intervention, Intervention.debat_id == Debat.id)
                                .filter(
                                    Debat.id.in_(debate_id_ints),
                                    sa_or(*kw_conditions),
                                )
                                .distinct().all()
                            )
                            new_from_or = 0
                            for r in p3_or_results:
                                if str(r[0]) not in found_ids:
                                    found_ids[str(r[0])] = "keyword_fallback"
                                    new_from_or += 1
                            if new_from_or:
                                logger.info(
                                    f"  [{chambre}] Anchor 3b (OR keywords): +{new_from_or} debates"
                                )

                # Return ordered: anchor 1 first, then 2, then 3
                order = {"article_in_law": 0, "cgi_ref_mentions": 1, "cgi_ref_ilike": 2, "keyword_fallback": 3}
                sorted_ids = sorted(found_ids.keys(), key=lambda d: order.get(found_ids[d], 9))
                return sorted_ids

        except Exception as e:
            logger.warning(f"  [{chambre}] Debate discovery failed: {e}")
            return []

    def _discover_orphan_chamber_debates(
        self,
        chambre: str,
        date_start: date,
        date_end: date,
        article_ref: str,
        article_keywords: list[str],
        article_in_law: str,
        modification_objet: str = "",
        max_debates: int = 50,
    ) -> list[str]:
        """Find debates relevant to an article modification when the law has
        no dossier scope (orphan-law fallback).

        Scopes to chambre + date window, then applies the same triple-anchor
        recall logic as _discover_chamber_debates without the dossier filter.
        """
        from src.db.models import (
            Debat,
            Intervention,
            get_session as _get_session,
        )

        settings = get_settings()
        try:
            with _get_session() as db:
                base = (
                    db.query(Debat.id)
                    .join(Intervention, Intervention.debat_id == Debat.id)
                    .filter(
                        Debat.chambre == chambre,
                        Debat.date >= date_start,
                        Debat.date <= date_end,
                    )
                )

                found_ids: dict[str, str] = {}

                # --- Anchor 1: article_in_law in articles_mentionnes ---
                # In orphan mode we only use the *precise quoted* form to avoid
                # massive false positives (e.g. "21" matching "210", "211", "21 bis"
                # over a 540-day window with no dossier scope).
                if settings.articles_mentionnes_filter and article_in_law:
                    art_num = re.sub(r"^Art\.?\s*", "", article_in_law).strip()
                    art_nums = [n.strip() for n in art_num.split(",") if n.strip()]
                    mention_conds = []
                    for num in art_nums:
                        if len(num) >= 2 or not num.isdigit():
                            mention_conds.append(
                                Intervention.articles_mentionnes.cast(String).ilike(f'%"{num}"%')
                            )
                    if mention_conds:
                        a1_results = (
                            base.filter(sa_or(*mention_conds)).distinct()
                            .limit(max_debates).all()
                        )
                        for r in a1_results:
                            found_ids[str(r[0])] = "article_in_law"
                        if a1_results:
                            logger.info(
                                f"  [{chambre}] Orphan Anchor 1 (article_in_law '{article_in_law}'): "
                                f"{len(a1_results)} debates"
                            )

                # --- Anchor 2: CGI ref in articles_mentionnes or contenu ---
                ref_lower = article_ref.lower()
                ref_variants = [ref_lower, ref_lower.replace("-", " "), ref_lower.replace(" ", "")]

                if settings.articles_mentionnes_filter:
                    cgi_mention_conds = [
                        Intervention.articles_mentionnes.cast(String).ilike(f'%{v}%')
                        for v in ref_variants
                    ]
                    a2a = (
                        base.filter(sa_or(*cgi_mention_conds)).distinct()
                        .limit(max_debates).all()
                    )
                    for r in a2a:
                        if str(r[0]) not in found_ids:
                            found_ids[str(r[0])] = "cgi_ref_mentions"

                cgi_ilike_conds = [Intervention.contenu.ilike(f'%{article_ref}%')]
                if " " in article_ref:
                    cgi_ilike_conds.append(
                        Intervention.contenu.ilike(f'%{article_ref.replace(" ", "%")}%')
                    )
                a2b = (
                    base.filter(sa_or(*cgi_ilike_conds)).distinct()
                    .limit(max_debates).all()
                )
                for r in a2b:
                    if str(r[0]) not in found_ids:
                        found_ids[str(r[0])] = "cgi_ref_ilike"

                logger.info(
                    f"  [{chambre}] Orphan Anchor 2 (CGI ref '{article_ref}'): "
                    f"{len(a2a) if settings.articles_mentionnes_filter else 0} mentions + "
                    f"{len(a2b)} ILIKE"
                )

                # --- Anchor 3: keyword discovery (always runs in orphan mode) ---
                # In orphan mode, semantic keywords are usually the only reliable
                # signal (e.g. pre-codification articles where the modern CGI
                # number was not yet in use). We always run keyword discovery to
                # seed the pool with high-precision matches.
                _ilike_kw = _select_best_ilike_keywords(article_keywords, top_k=5)
                objet_kw: list[str] = []
                if modification_objet:
                    objet_kw = _extract_objet_keywords(modification_objet)

                # AND of pairs first (precision)
                if len(_ilike_kw) >= 2:
                    and_pairs = [
                        (_ilike_kw[j], _ilike_kw[k])
                        for j in range(min(3, len(_ilike_kw)))
                        for k in range(j + 1, min(4, len(_ilike_kw)))
                    ]
                    for kw1, kw2 in and_pairs:
                        pair_results = (
                            base.filter(
                                Intervention.contenu.ilike(f'%{kw1}%'),
                                Intervention.contenu.ilike(f'%{kw2}%'),
                            ).distinct()
                            .limit(max_debates).all()
                        )
                        for r in pair_results:
                            if str(r[0]) not in found_ids:
                                found_ids[str(r[0])] = "keyword_and"

                # OR fallback for broader recall
                kw_conditions = [
                    Intervention.contenu.ilike(f'%{kw}%') for kw in _ilike_kw
                ]
                for okw in objet_kw:
                    kw_conditions.append(Intervention.contenu.ilike(f'%{okw}%'))
                if kw_conditions:
                    p3 = (
                        base.filter(sa_or(*kw_conditions)).distinct()
                        .limit(max_debates).all()
                    )
                    for r in p3:
                        if str(r[0]) not in found_ids:
                            found_ids[str(r[0])] = "keyword_fallback"

                order = {"article_in_law": 0, "cgi_ref_mentions": 1, "cgi_ref_ilike": 2,
                         "keyword_and": 3, "keyword_fallback": 4}
                sorted_ids = sorted(found_ids.keys(), key=lambda d: order.get(found_ids[d], 9))
                return sorted_ids[:max_debates]

        except Exception as e:
            logger.warning(f"  [{chambre}] Orphan-fallback discovery failed: {e}")
            return []

    @staticmethod
    def _compute_signals(
        chunk: Chunk,
        similarity: float,
        ref_lower: str,
        ref_nospace: str,
        keywords_lower: list[str],
        article_keywords: list[str],
        settings,
        date_start: date | None = None,
        date_end: date | None = None,
        sibling_negative_terms: list[str] | None = None,
    ) -> "RelevanceSignals":
        """Compute all relevance signals for a single chunk."""
        content_lower = _normalize_text(chunk.content)

        # Article ref explicit
        article_ref_explicit = ref_lower in content_lower or ref_nospace in content_lower

        # articles_mentionnes match
        mentions = chunk.metadata.get("articles_mentionnes")
        articles_mentionnes_match = False
        if mentions:
            for m in mentions:
                m_norm = m.lower().replace("-", " ").strip()
                if ref_lower in m_norm or ref_nospace in m_norm or m_norm in ref_lower:
                    articles_mentionnes_match = True
                    break

        # Keyword matching
        matched_kw = [kw for kw in keywords_lower if kw in content_lower]
        keyword_match_count = len(matched_kw)

        # Keyword specificity (IDF-weighted)
        from src.analysis.article_analyzer import ArticleAnalyzer
        specificity = ArticleAnalyzer._get_kw_specificity()
        always_generic = ArticleAnalyzer._get_always_generic()
        keyword_specificity = sum(
            specificity.get(kw, 0.3) for kw in article_keywords
            if kw not in always_generic and _keyword_matches(kw, content_lower)
        )

        # Speaker authority
        fonction = _normalize_text(chunk.metadata.get("fonction") or "")
        speaker_authority = 0.0
        if "rapporteur" in fonction and ("général" in fonction or "genera" in fonction):
            speaker_authority = 1.0
        elif "rapporteur" in fonction:
            speaker_authority = 0.85
        elif any(r in fonction for r in ("ministre", "secrétaire d'état", "secretaire d")):
            speaker_authority = 0.80
        elif "président" in fonction and "commission" in fonction:
            speaker_authority = 0.50
        elif "senateur" in fonction or "sénateur" in fonction:
            speaker_authority = 0.30
        elif "depute" in fonction or "député" in fonction:
            speaker_authority = 0.30

        # Etape weight
        etape = (chunk.metadata.get("etape") or "").lower()
        if "seance" in etape or "séance" in etape:
            etape_weight = 1.0
        elif "commission" in etape:
            etape_weight = 0.7
        else:
            etape_weight = 0.5

        # Temporal proximity
        temporal_proximity = 0.5  # default if no date
        chunk_date_str = chunk.metadata.get("date", "")
        if chunk_date_str and date_start and date_end:
            try:
                chunk_date = date.fromisoformat(chunk_date_str[:10])
                # Normalize to 0-1 within the search window
                window_days = max((date_end - date_start).days, 1)
                days_from_end = max(0, (date_end - chunk_date).days)
                temporal_proximity = max(0, 1.0 - (days_from_end / window_days))
            except (ValueError, TypeError):
                pass

        # Sibling penalty
        sibling_penalty = 0.0
        if sibling_negative_terms:
            neg_matches = sum(1 for t in sibling_negative_terms if t in content_lower)
            pos_from_kw = keyword_match_count
            if neg_matches > 0 and pos_from_kw == 0 and not article_ref_explicit:
                sibling_penalty = min(neg_matches * 0.1, 0.3)

        return RelevanceSignals(
            semantic_similarity=similarity,
            article_ref_explicit=article_ref_explicit,
            articles_mentionnes_match=articles_mentionnes_match,
            keyword_match_count=keyword_match_count,
            keyword_specificity=keyword_specificity,
            speaker_authority=speaker_authority,
            etape_weight=etape_weight,
            temporal_proximity=temporal_proximity,
            amendment_linked=False,  # Set by caller if applicable
            sibling_penalty=sibling_penalty,
        )

    @staticmethod
    def merge_bicameral_results(
        an_results: dict[str, list[tuple[Chunk, float]]],
        senat_results: dict[str, list[tuple[Chunk, float]]],
        top_k: int = 25,
        quality_threshold: int = 1,
    ) -> tuple[dict[str, list[tuple[Chunk, float]]], bool]:
        """Merge AN and Sénat results with quality-based bicameral inclusion.

        A chamber is only included if it has ≥quality_threshold chunks with
        explicit article reference. Specificity over forced bicamerality.

        Returns:
            (merged_results, is_bicameral)
        """
        def _count_explicit(results: dict, ref_lower: str = "") -> int:
            count = 0
            for key in results:
                for chunk, _ in results[key]:
                    content = chunk.content.lower()
                    if ref_lower and (ref_lower in content or ref_lower.replace("-", " ") in content):
                        count += 1
                    elif chunk.metadata.get("articles_mentionnes"):
                        count += 1
            return count

        an_total = sum(len(v) for v in an_results.values())
        senat_total = sum(len(v) for v in senat_results.values())
        an_qualifies = an_total > 0
        senat_qualifies = senat_total > 0

        merged = {}
        is_bicameral = an_qualifies and senat_qualifies

        for key in ["debates", "amendments"]:
            an_chunks = an_results.get(key, [])
            senat_chunks = senat_results.get(key, [])

            if is_bicameral:
                # Both chambers: interleave by score
                all_chunks = an_chunks + senat_chunks
                # Dedup by content prefix
                seen = set()
                deduped = []
                for chunk, score in all_chunks:
                    content_key = chunk.content[:80]
                    if content_key not in seen:
                        seen.add(content_key)
                        deduped.append((chunk, score))
                deduped.sort(key=lambda x: x[1], reverse=True)
                merged[key] = deduped[:top_k]
            elif an_qualifies:
                merged[key] = an_chunks[:top_k]
            elif senat_qualifies:
                merged[key] = senat_chunks[:top_k]
            else:
                merged[key] = []

        if is_bicameral:
            an_in_merged = sum(
                1 for key in merged for c, _ in merged[key]
                if (c.metadata.get("chambre") or "").upper() == "AN"
            )
            senat_in_merged = sum(
                1 for key in merged for c, _ in merged[key]
                if (c.metadata.get("chambre") or "").upper() in ("SENAT", "SÉNAT")
            )
            logger.info(f"Bicameral merge: AN={an_in_merged}, Sénat={senat_in_merged}")
        else:
            active = "AN" if an_qualifies else ("SENAT" if senat_qualifies else "none")
            logger.info(f"Mono-chamber result: {active}")

        return merged, is_bicameral

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
        all_modification_dates: list[tuple[date, str]] | None = None,
        current_mod_date: date | None = None,
        law_debate_refs: list[str] | None = None,
        law_numero: str = "",
        query_embedding: list[float] | None = None,
        modification_objet: str = "",
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
        # Build article-specific semantic query with most discriminant keywords
        # Use _select_best_ilike_keywords to get substance terms (no ref variants)
        core_keywords = _select_best_ilike_keywords(article_keywords, top_k=8)
        if not core_keywords:
            core_keywords = article_keywords[:5]
        keywords_str = ", ".join(core_keywords) if core_keywords else ""
        objet_clause = ""
        objet_keywords: list[str] = []
        if modification_objet:
            objet_keywords = _extract_objet_keywords(modification_objet)
            if objet_keywords:
                objet_clause = f"Modification: {', '.join(objet_keywords)}. "
        semantic_query = (
            f"Intention du législateur pour l'article {article_ref} du CGI "
            f"({keywords_str}). {objet_clause}"
            f"Pourquoi cette modification dans la {law_type} {law_year} ? "
            f"Objectifs, justifications et débats parlementaires."
        )

        logger.info(f"Searching for {article_ref} modification {law_type} {law_year}")
        logger.debug(f"Semantic query: {semantic_query}")

        # Fetch more chunks initially (2x top_k for reranking headroom)
        fetch_k = top_k * 2

        # Primary text filters: CGI article ref variants only (for fallback)
        article_text_filters = _generate_article_variants(article_ref)
        if not _is_short_article_ref(article_ref):
            article_text_filters = [article_ref] + article_text_filters

        settings = get_settings()
        effective_min_sim = min_similarity_override if min_similarity_override is not None else settings.search_min_similarity

        # --- Dossier-first debate discovery + tiered filtering ---
        debate_scoped = False
        store = self.debate_store

        # Regex to detect CGI article references in text (for filtering false positives)
        _article_cgi_re = re.compile(
            r"article\s+((?:\d+(?:[- ]\d+)?(?:\s+[A-Z](?![a-zéèêà]))?)"
            r"(?:\s+(?:bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies"
            r"|undecies|duodecies|terdecies|quaterdecies|quindecies|sexdecies))?)",
            re.IGNORECASE,
        )

        def _cites_other_cgi_article(text: str, target_ref: str) -> bool:
            """Return True if text cites a CGI article different from target_ref."""
            found = _article_cgi_re.findall(text)
            target_norm = target_ref.lower().replace(" ", "").replace("-", "")
            for a in found:
                a_norm = a.strip().lower().replace(" ", "").replace("-", "")
                if len(a_norm) > 2 and target_norm not in a_norm:
                    return True
            return False

        if hasattr(store, "find_source_ids_by_text") and hasattr(store, "search_in_source_ids"):

            # ===== ÉTAPE 1 : Trouver les débats pertinents via table interventions =====
            anchor_source_ids = []
            if law_debate_refs:
                # Fast path: query interventions table with dossier join (1-3s vs 130s on chunk_embeddings)
                from src.db.models import get_session as _get_db_session, Debat, Intervention, DossierLegislatif, Loi
                try:
                    with _get_db_session() as db:
                        # Build ILIKE conditions: article_ref + best keywords + objet terms
                        kw_conditions = [Intervention.contenu.ilike(f'%{article_ref}%')]
                        # Also match double-space variants (e.g. "150-0 B  ter")
                        if " " in article_ref:
                            kw_conditions.append(Intervention.contenu.ilike(
                                f'%{article_ref.replace(" ", "%")}%'
                            ))
                        # Select best keywords: curated-first, then multiword non-generic
                        _ilike_kw = _select_best_ilike_keywords(article_keywords, top_k=7)
                        for kw in _ilike_kw:
                            kw_conditions.append(Intervention.contenu.ilike(f'%{kw}%'))
                        for okw in objet_keywords:
                            kw_conditions.append(Intervention.contenu.ilike(f'%{okw}%'))

                        # JOIN direct: 10× faster than subquery IN
                        # PostgreSQL filters by dossier first (238 debates → 114K interventions)
                        # then applies ILIKE on the subset
                        # Filter by law + keywords + date window to exclude
                        # mislinked debates (e.g., 2025 debates linked to LF 2020 dossier)
                        date_filters = []
                        if date_start:
                            date_filters.append(Debat.date >= date_start)
                        if date_end:
                            date_filters.append(Debat.date <= date_end)

                        relevant_ids = (
                            db.query(Debat.id)
                            .join(Intervention, Intervention.debat_id == Debat.id)
                            .join(DossierLegislatif, Debat.dossier_id == DossierLegislatif.id)
                            .join(Loi, DossierLegislatif.loi_id == Loi.id)
                            .filter(
                                Loi.numero == law_numero,
                                sa_or(*kw_conditions),
                                *date_filters,
                            )
                            .distinct()
                            .all()
                        )
                        anchor_source_ids = [str(r[0]) for r in relevant_ids]
                except Exception as e:
                    logger.warning(f"Interventions query failed: {e}, falling back to text search")
                    anchor_source_ids = []

                logger.info(
                    f"Dossier-first: {len(law_debate_refs)} total → {len(anchor_source_ids)} "
                    f"debates mentioning article/keywords"
                )

            # Fallback: si 0 débats liés → text-search (ancien comportement)
            if not anchor_source_ids:
                anchors = store.find_source_ids_by_text(
                    text_filters=article_text_filters,
                    chunk_type="intervention",
                    date_start=date_start.isoformat() if date_start else None,
                    date_end=date_end.isoformat() if date_end else None,
                )
                anchor_source_ids = list({a["source_id"] for a in anchors if a["source_id"]})

                # Directional date filter
                if anchor_source_ids:
                    anchor_dates: dict[str, date] = {}
                    for a in anchors:
                        if a["source_id"] and a.get("date"):
                            try:
                                anchor_dates[a["source_id"]] = date.fromisoformat(a["date"][:10])
                            except (ValueError, TypeError):
                                pass
                    if all_modification_dates and current_mod_date:
                        anchor_source_ids = _directional_date_filter(
                            anchor_source_ids, anchor_dates,
                            all_modification_dates, current_mod_date,
                            date_start, date_end,
                        )
                logger.info(f"Text-search fallback: {len(anchor_source_ids)} debates")

            # ===== ÉTAPE 2 : Recherche vectorielle dans les débats trouvés =====
            # Limit to 30 debates max to keep search fast (the dossier-first
            # query already returns them ordered by keyword relevance)
            if len(anchor_source_ids) > 30:
                anchor_source_ids = anchor_source_ids[:30]

            if anchor_source_ids:
                logger.info(
                    f"Debate-scoped search for {article_ref}: {len(anchor_source_ids)} debates"
                )
                if query_embedding is None:
                    query_embedding = self.embedding_service.generate_embedding(semantic_query)
                debate_results = store.search_in_source_ids(
                    query_embedding=query_embedding,
                    source_ids=anchor_source_ids,
                    chunk_types=["intervention", "amendement"],
                    top_k=fetch_k,
                    min_similarity=effective_min_sim,
                    date_start=date_start.isoformat() if date_start else None,
                    date_end=date_end.isoformat() if date_end else None,
                    article_ref=article_ref,
                )

                # ILIKE guarantee: inject chunks mentioning article_ref OR top keywords
                existing_contents = set()
                for ct in debate_results:
                    for chunk, _ in debate_results[ct]:
                        existing_contents.add(chunk.content[:100])
                ilike_added = 0

                # 1) Inject chunks mentioning article_ref explicitly
                ilike_results = store.search_by_text_in_source_ids(
                    text_filter=article_ref,
                    source_ids=anchor_source_ids,
                    chunk_types=["intervention", "amendement"],
                    top_k=10,
                )
                for ct in ilike_results:
                    for chunk, score in ilike_results[ct]:
                        if chunk.content[:100] not in existing_contents:
                            debate_results.setdefault(ct, []).append((chunk, max(score, 0.50)))
                            existing_contents.add(chunk.content[:100])
                            ilike_added += 1

                # 2) Inject chunks mentioning top 5 specific keywords (catches
                #    Marini "report d'imposition" or Eckert "apport de titres")
                _top_kw = _select_best_ilike_keywords(article_keywords, top_k=5)
                for kw in _top_kw:
                    kw_ilike = store.search_by_text_in_source_ids(
                        text_filter=kw,
                        source_ids=anchor_source_ids,
                        chunk_types=["intervention"],
                        top_k=8,
                    )
                    for ct in kw_ilike:
                        for chunk, score in kw_ilike[ct]:
                            if chunk.content[:100] not in existing_contents:
                                debate_results.setdefault(ct, []).append((chunk, max(score, 0.40)))
                                existing_contents.add(chunk.content[:100])
                                ilike_added += 1

                if ilike_added:
                    logger.info(
                        f"ILIKE guarantee: injected {ilike_added} chunks mentioning "
                        f"{article_ref} or keywords"
                    )

                # ===== ÉTAPE 3 : Keyword expansion pour AN manquants =====
                # Variant E: check AN quality (ref OR 2+ unique stem-deduped keywords)
                # instead of just counting AN chunks (which includes noise)
                an_quality_count = 0
                ref_lower = article_ref.lower()
                ref_nospace = ref_lower.replace("-", " ")
                for ct in debate_results:
                    for c, _ in debate_results[ct]:
                        if (c.metadata.get("chambre") or "").upper() != "AN":
                            continue
                        content_lower = c.content.lower()
                        # Explicit article reference → relevant
                        if ref_lower in content_lower or ref_nospace in content_lower:
                            an_quality_count += 1
                            continue
                        # 2+ unique stem-deduped non-generic keywords → relevant
                        from src.analysis.article_analyzer import ArticleAnalyzer
                        always_generic = ArticleAnalyzer._get_always_generic()
                        kw_matched = [
                            kw for kw in article_keywords
                            if kw not in always_generic
                            and len(kw) > 5
                            and not kw.lower().startswith("article ")
                            and _keyword_matches(kw, content_lower)
                        ]
                        unique_kw = ArticleAnalyzer._dedup_kw_by_stem(kw_matched)
                        if len(unique_kw) >= 2:
                            an_quality_count += 1

                if an_quality_count == 0 and article_keywords:
                    # Fast path: query interventions table for AN debates with keywords
                    from src.db.models import get_session as _get_db_session2, Debat as Debat2, Intervention as Inter2
                    try:
                        with _get_db_session2() as db:
                            kw_conds = [Inter2.contenu.ilike(f'%{article_ref}%')]
                            if " " in article_ref:
                                kw_conds.append(Inter2.contenu.ilike(
                                    f'%{article_ref.replace(" ", "%")}%'
                                ))
                            _ilike_kw_an = _select_best_ilike_keywords(article_keywords, top_k=7)
                            for kw in _ilike_kw_an:
                                kw_conds.append(Inter2.contenu.ilike(f'%{kw}%'))
                            for okw in objet_keywords:
                                kw_conds.append(Inter2.contenu.ilike(f'%{okw}%'))

                            an_debate_ids = (
                                db.query(Debat2.id)
                                .join(Inter2, Inter2.debat_id == Debat2.id)
                                .filter(
                                    Debat2.chambre == 'AN',
                                    Debat2.date >= date_start if date_start else True,
                                    Debat2.date <= date_end if date_end else True,
                                    sa_or(*kw_conds),
                                )
                                .distinct()
                                .limit(10)
                                .all()
                            )
                            an_keyword_ids = [
                                str(r[0]) for r in an_debate_ids
                                if str(r[0]) not in set(anchor_source_ids)
                            ]
                    except Exception as e:
                        logger.warning(f"AN keyword expansion query failed: {e}")
                        an_keyword_ids = []

                    if an_keyword_ids:
                        # Generate keyword-focused embedding for AN expansion
                        # (AN debates often use mechanism names like "apport-cession"
                        # instead of article numbers like "150-0 B ter")
                        kw_query = (
                            f"{', '.join(article_keywords[:5])}. "
                            f"Débat parlementaire {law_type} {law_year} sur ce mécanisme fiscal."
                        )
                        try:
                            kw_embedding = self.embedding_service.generate_embedding(kw_query)
                        except Exception:
                            kw_embedding = query_embedding

                        an_extra = store.search_in_source_ids(
                            query_embedding=kw_embedding,
                            source_ids=an_keyword_ids[:5],
                            chunk_types=["intervention"],
                            top_k=fetch_k,
                            min_similarity=effective_min_sim,
                            date_start=date_start.isoformat() if date_start else None,
                            date_end=date_end.isoformat() if date_end else None,
                            article_ref=article_ref,
                        )
                        an_added = 0
                        for ct in an_extra:
                            for chunk, score in an_extra[ct]:
                                if chunk.content[:100] not in existing_contents:
                                    if not _cites_other_cgi_article(chunk.content, article_ref):
                                        # Guarantee minimum score for keyword-matched chunks
                                        # These were found by ILIKE on article keywords, so they're
                                        # topically relevant even if semantic score is low
                                        boosted_score = max(score, 0.40)
                                        debate_results.setdefault(ct, []).append((chunk, boosted_score))
                                        existing_contents.add(chunk.content[:100])
                                        an_added += 1
                        if an_added:
                            logger.info(
                                f"Keyword AN expansion: +{an_added} AN chunks from {len(an_keyword_ids)} AN debates"
                            )

                # ===== ÉTAPE 3b : Keyword expansion pour Sénat manquants =====
                senat_count = sum(
                    1 for ct in debate_results for c, _ in debate_results[ct]
                    if (c.metadata.get("chambre") or "").upper() in ("SENAT", "SÉNAT")
                )
                if senat_count == 0 and article_keywords:
                    # Fast path: query interventions table for Sénat debates with keywords
                    from src.db.models import get_session as _get_db_session3, Debat as Debat3, Intervention as Inter3
                    try:
                        with _get_db_session3() as db:
                            kw_conds_senat = [Inter3.contenu.ilike(f'%{article_ref}%')]
                            if " " in article_ref:
                                kw_conds_senat.append(Inter3.contenu.ilike(
                                    f'%{article_ref.replace(" ", "%")}%'
                                ))
                            _ilike_kw_senat = _select_best_ilike_keywords(article_keywords, top_k=7)
                            for kw in _ilike_kw_senat:
                                kw_conds_senat.append(Inter3.contenu.ilike(f'%{kw}%'))
                            for okw in objet_keywords:
                                kw_conds_senat.append(Inter3.contenu.ilike(f'%{okw}%'))

                            senat_debate_ids = (
                                db.query(Debat3.id)
                                .join(Inter3, Inter3.debat_id == Debat3.id)
                                .filter(
                                    Debat3.chambre.in_(['Sénat', 'SENAT', 'senat']),
                                    Debat3.date >= date_start if date_start else True,
                                    Debat3.date <= date_end if date_end else True,
                                    sa_or(*kw_conds_senat),
                                )
                                .distinct()
                                .limit(10)
                                .all()
                            )
                            senat_keyword_ids = [
                                str(r[0]) for r in senat_debate_ids
                                if str(r[0]) not in set(anchor_source_ids)
                            ]
                    except Exception as e:
                        logger.warning(f"Sénat keyword expansion query failed: {e}")
                        senat_keyword_ids = []

                    if senat_keyword_ids:
                        # Use keyword-focused embedding for Sénat expansion
                        kw_query_senat = (
                            f"{', '.join(article_keywords[:5])}. "
                            f"Débat parlementaire {law_type} {law_year} sur ce mécanisme fiscal."
                        )
                        try:
                            kw_embedding_senat = self.embedding_service.generate_embedding(kw_query_senat)
                        except Exception:
                            kw_embedding_senat = query_embedding

                        senat_extra = store.search_in_source_ids(
                            query_embedding=kw_embedding_senat,
                            source_ids=senat_keyword_ids[:5],
                            chunk_types=["intervention"],
                            top_k=fetch_k,
                            min_similarity=effective_min_sim,
                            date_start=date_start.isoformat() if date_start else None,
                            date_end=date_end.isoformat() if date_end else None,
                            article_ref=article_ref,
                        )
                        senat_added = 0
                        for ct in senat_extra:
                            for chunk, score in senat_extra[ct]:
                                if chunk.content[:100] not in existing_contents:
                                    if not _cites_other_cgi_article(chunk.content, article_ref):
                                        boosted_score = max(score, 0.40)
                                        debate_results.setdefault(ct, []).append((chunk, boosted_score))
                                        existing_contents.add(chunk.content[:100])
                                        senat_added += 1
                        if senat_added:
                            logger.info(
                                f"Keyword Sénat expansion: +{senat_added} Sénat chunks from {len(senat_keyword_ids)} Sénat debates"
                            )

                # ===== ÉTAPE 4 : Scoring enrichi (keyword + rôle + temporel) =====
                keywords_lower = [_normalize_text(kw) for kw in article_keywords]
                raw_results = {}
                for ct, results_key in [("intervention", "debates"), ("amendement", "amendments")]:
                    scored = []
                    for chunk, similarity in debate_results.get(ct, []):
                        score = similarity
                        content_lower = _normalize_text(chunk.content)
                        matched = [kw for kw in keywords_lower if kw in content_lower]
                        if matched:
                            score += settings.search_keyword_boost
                        fonction = _normalize_text(chunk.metadata.get("fonction") or "")
                        if fonction:
                            if "rapporteur" in fonction and ("général" in fonction or "genera" in fonction):
                                score += 0.15
                            elif "rapporteur" in fonction:
                                score += 0.12
                            elif any(r in fonction for r in ("ministre", "secrétaire d'état", "secretaire d")):
                                score += 0.10
                            elif "président" in fonction and "commission" in fonction:
                                score += 0.05
                        # Temporal proximity boost + distance guard
                        chunk_date_str = chunk.metadata.get("date", "")
                        if chunk_date_str and current_mod_date:
                            try:
                                chunk_date = date.fromisoformat(chunk_date_str[:10])
                                days_before = (current_mod_date - chunk_date).days
                                # Skip chunks too far from modification (likely from previous law)
                                if days_before > 300:
                                    orateur = chunk.metadata.get("orateur", "?")
                                    logger.info(
                                        f"Distance guard: skipping {orateur} "
                                        f"({chunk_date_str[:10]}, {days_before}d > 300) "
                                        f"for {article_ref} {law_type} {law_year}"
                                    )
                                    continue
                                if 0 <= days_before <= 300:
                                    score += max(0, 1.0 - (days_before / 300)) * 0.1
                            except (ValueError, TypeError):
                                pass
                        scored.append((chunk, score))
                    scored.sort(key=lambda x: x[1], reverse=True)
                    raw_results[results_key] = scored[:fetch_k]
                debate_scoped = True

        # Fallback: original global ILIKE + vector search (no keyword enrichment)
        if not debate_scoped:
            logger.info(f"Fallback to global search for {article_ref} (no debate anchors found)")
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

        # --- Filtering and reranking ---
        tier1_keywords = [article_ref] + _generate_article_variants(article_ref)
        filtered_results = {}

        if debate_scoped:
            # Debate-scoped: results come from known-relevant debates.
            # Only rerank by cross-encoder, no keyword filter needed.
            for key in ["debates", "amendments"]:
                chunks = raw_results.get(key, [])
                if self.reranker and chunks:
                    # Pre-sort by score, but guarantee AN chunks reach the reranker
                    # (AN expansion chunks have score=0.5, would be cut by top-60)
                    chunks.sort(key=lambda x: x[1], reverse=True)
                    an_pool = [(c, s) for c, s in chunks
                               if (c.metadata.get("chambre") or "").upper() == "AN"]
                    non_an = [(c, s) for c, s in chunks
                              if (c.metadata.get("chambre") or "").upper() != "AN"]
                    # Guarantee up to 6 AN in the reranker input
                    an_guaranteed = an_pool[:6]
                    an_used = {c.content[:80] for c, _ in an_guaranteed}
                    rest = [(c, s) for c, s in chunks if c.content[:80] not in an_used]
                    rest.sort(key=lambda x: x[1], reverse=True)
                    rerank_input = an_guaranteed + rest[:max(54, 60 - len(an_guaranteed))]
                    reranked = self.reranker.rerank(
                        semantic_query, rerank_input, top_k=top_k * 2,
                        min_score=settings.reranker_min_score,
                    )
                    logger.info(
                        f"Reranking {len(rerank_input)} → {len(reranked)} {key} chunks for {article_ref} "
                        f"({law_type} {law_year}) [debate-scoped]"
                    )
                else:
                    reranked = chunks[:top_k * 2]

                # Ensure chamber balance: guarantee minimum per chamber,
                # fill remaining slots by score.
                if key == "debates" and reranked:
                    an_chunks = [(c, s) for c, s in reranked
                                 if (c.metadata.get("chambre") or "").upper() == "AN"]
                    senat_chunks = [(c, s) for c, s in reranked
                                   if (c.metadata.get("chambre") or "").upper() in ("SENAT", "SÉNAT")]
                    min_per_chamber = settings.chamber_balance_min_per_chamber
                    guaranteed_an = an_chunks[:min_per_chamber]
                    guaranteed_senat = senat_chunks[:min_per_chamber]
                    used = {id(c) for c, _ in guaranteed_an + guaranteed_senat}
                    remaining_pool = [(c, s) for c, s in reranked if id(c) not in used]
                    remaining_pool.sort(key=lambda x: x[1], reverse=True)
                    remaining_slots = top_k - len(guaranteed_an) - len(guaranteed_senat)
                    balanced = guaranteed_an + guaranteed_senat + remaining_pool[:max(0, remaining_slots)]
                    balanced.sort(key=lambda x: x[1], reverse=True)
                    chunks = balanced[:top_k]
                    logger.info(
                        f"Chamber balance for {article_ref} ({law_type} {law_year}): "
                        f"AN={len(an_chunks)}→{len([c for c, _ in chunks if (c.metadata.get('chambre') or '').upper() == 'AN'])} "
                        f"Sénat={len(senat_chunks)}→{len([c for c, _ in chunks if (c.metadata.get('chambre') or '').upper() in ('SENAT', 'SÉNAT')])}"
                    )
                else:
                    chunks = reranked[:top_k]

                # ILIKE guarantee: promote chunks explicitly mentioning the article
                article_ref_lower = article_ref.lower()
                explicit = [(c, max(s, 0.95)) for c, s in chunks
                            if article_ref_lower in c.content.lower()]
                non_explicit = [(c, s) for c, s in chunks
                                if article_ref_lower not in c.content.lower()]
                if explicit:
                    chunks = (explicit + non_explicit)[:top_k]
                    logger.info(
                        f"ILIKE guarantee: {len(explicit)} {key} chunks explicitly mention "
                        f"{article_ref} - promoted to top"
                    )

                filtered_results[key] = chunks

                if len(chunks) < min_relevant:
                    logger.warning(
                        f"Only {len(chunks)} {key} chunks in debate-scoped search for {article_ref} "
                        f"(expected >= {min_relevant})."
                    )
        else:
            # Fallback: original tiered keyword filtering
            tier2_keywords = list(article_keywords)
            store_has_text_filter = hasattr(self.debate_store, "search_with_text_filter")

            for key in ["debates", "amendments"]:
                if key in raw_results:
                    if store_has_text_filter:
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
                        strict_filtered = filter_chunks_by_keywords(
                            raw_results[key], tier1_keywords,
                            min_required=1, article_ref=article_ref, require_context=False,
                        )
                        if len(strict_filtered) < 3:
                            relaxed_filtered = filter_chunks_by_keywords(
                                raw_results[key], tier2_keywords,
                                min_required=settings.search_tier2_min_required,
                                article_ref=article_ref, require_context=True,
                            )
                            seen_ids = {id(c) for c, _ in strict_filtered}
                            for chunk, score in relaxed_filtered:
                                if id(chunk) not in seen_ids:
                                    strict_filtered.append((chunk, score))
                                    seen_ids.add(id(chunk))

                    if self.reranker and strict_filtered:
                        logger.info(f"Reranking {len(strict_filtered)} {key} chunks for {article_ref} ({law_type} {law_year})")
                        strict_filtered = self.reranker.rerank(
                            semantic_query, strict_filtered, top_k=top_k, min_score=settings.reranker_min_score,
                        )
                    else:
                        strict_filtered = strict_filtered[:top_k]
                    filtered_results[key] = strict_filtered

                    if len(filtered_results[key]) < min_relevant:
                        logger.warning(
                            f"Only {len(filtered_results[key])} {key} chunks mention article {article_ref} "
                            f"or keywords (expected >= {min_relevant}). Search may have low relevance."
                        )
                else:
                    filtered_results[key] = []

        # --- Secondary search (only for fallback path) ---
        law_article_variants = _generate_law_article_variants(article_in_law)
        if not debate_scoped and law_article_variants and hasattr(self.debate_store, "search_with_text_filter"):
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
                    # Primary is full - replace weakest with stronger secondary
                    for sec_chunk, sec_score in unique_secondary:
                        min_idx = min(range(len(primary_list)), key=lambda i: primary_list[i][1])
                        if sec_score > primary_list[min_idx][1]:
                            primary_list[min_idx] = (sec_chunk, sec_score)
                        else:
                            break  # secondary sorted desc; no more replacements
                    primary_list.sort(key=lambda x: x[1], reverse=True)
                    filtered_results[key] = primary_list[:top_k]
                else:
                    # Primary has room - append
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
                meta_parts.append(f"- {metadata['chambre']}")
            if metadata.get("date"):
                meta_parts.append(f"- {metadata['date']}")
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
                lines.append(f"- *{metadata['sort']}*")
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
