"""Legislative intent extraction using LLM APIs.

Supports two providers:
- "anthropic": Uses Claude API (requires ANTHROPIC_API_KEY)
- "groq": Uses Groq API (free tier available, requires GROQ_API_KEY)
"""

import logging
from dataclasses import dataclass
from enum import Enum

from src.indexing.chunking import Chunk
from src.utils.config import get_settings
from src.utils.groq_pool import GroqClientPool, get_groq_pool, reset_groq_pool

logger = logging.getLogger(__name__)


class CertitudeLevel(str, Enum):
    """Level of certainty for intent interpretation."""

    ELEVE = "élevé"
    MOYEN = "moyen"
    FAIBLE = "faible"


@dataclass
class IntentResult:
    """Result of legislative intent extraction."""

    intention_resume: str
    sources_principales: list[dict]  # List of {citation, orateur, date, chambre}
    points_vigilance: list[str]  # Amendments rejetés, objections
    niveau_certitude: CertitudeLevel
    justification_certitude: str
    raw_response: str


INTENT_EXTRACTION_PROMPT = """Tu es un assistant juridique spécialisé dans l'analyse des travaux parlementaires français.

Contexte : L'utilisateur cherche à comprendre l'intention du législateur pour un article de loi de finances.

## Article de loi analysé
{article_contenu}

## Extraits des débats parlementaires pertinents
{chunks_debats}

## Amendements liés
{amendements}

## Ta tâche
Analyse les extraits des débats parlementaires et amendements pour déterminer l'intention du législateur derrière cet article de loi.

1. Résume l'intention du législateur en 3-5 phrases claires et précises
2. Cite les passages clés des débats qui soutiennent cette interprétation (avec source exacte: orateur, date, chambre)
3. Identifie les amendements rejetés ou objections qui pourraient suggérer une interprétation différente ou des limites à l'interprétation
4. Évalue le niveau de certitude de l'interprétation (élevé/moyen/faible) et justifie

## Format de réponse (respecte exactement ce format)

### INTENTION DU LÉGISLATEUR
[Résumé en 3-5 phrases]

### SOURCES PRINCIPALES
- Citation: "[citation exacte]" — Source: [nom de l'orateur], [date], [chambre AN/Sénat]
- Citation: "[citation exacte]" — Source: [nom de l'orateur], [date], [chambre AN/Sénat]
[...]

### POINTS DE VIGILANCE
- [Amendement rejeté ou objection notable]
- [Autre point de vigilance]
[...]

### NIVEAU DE CERTITUDE
[élevé/moyen/faible]

### JUSTIFICATION
[Explication du niveau de certitude en 2-3 phrases]
"""


class IntentExtractor:
    """
    Service for extracting legislative intent using LLM.

    Supports two providers:
    - "anthropic": Uses Claude API (requires ANTHROPIC_API_KEY)
    - "groq": Uses Groq API with Llama/Mixtral (free tier, requires GROQ_API_KEY)

    For Groq, uses GroqClientPool for automatic key rotation on rate limits.
    Configure multiple keys using GROQ_API_KEYS=key1,key2,key3 in .env
    """

    def __init__(self, groq_pool: GroqClientPool | None = None):
        self.settings = get_settings()
        self._anthropic_client = None
        self._groq_pool = groq_pool
        self.provider = self.settings.llm_provider

        if self.provider == "groq":
            pool = self._get_groq_pool()
            logger.info(f"IntentExtractor using GroqClientPool with {pool.total_keys_count} API keys")

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

            if not self.settings.anthropic_api_key:
                raise ValueError(
                    "Anthropic API key not configured. Set ANTHROPIC_API_KEY environment variable "
                    "or use LLM_PROVIDER=groq for free Groq API."
                )
            self._anthropic_client = Anthropic(api_key=self.settings.anthropic_api_key)
        return self._anthropic_client

    def reset_groq_keys(self):
        """Reset all Groq keys to available (e.g., after rate limit period ends)."""
        reset_groq_pool()

    def _call_anthropic(self, prompt: str) -> str:
        """Call Anthropic Claude API."""
        response = self.anthropic_client.messages.create(
            model=self.settings.claude_model,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    def _call_groq(self, prompt: str) -> str:
        """
        Call Groq API with Llama/Mixtral.

        Uses GroqClientPool for automatic key rotation on rate limit (429) errors.
        """
        return self._get_groq_pool().chat_completion(
            model=self.settings.groq_model,
            messages=[
                {
                    "role": "system",
                    "content": "Tu es un assistant juridique expert en droit fiscal français et en analyse des travaux parlementaires.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=3000,
            temperature=0.3,
        )

    def extract_intent(
        self,
        article_contenu: str,
        debate_chunks: list[Chunk],
        amendment_chunks: list[Chunk],
        max_debate_chars: int = 25000,
        max_amendment_chars: int = 8000,
    ) -> IntentResult:
        """
        Extract legislative intent for an article.

        Args:
            article_contenu: Full text of the law article
            debate_chunks: Relevant debate chunks (sorted by relevance)
            amendment_chunks: Relevant amendment chunks
            max_debate_chars: Maximum chars to include from debates
            max_amendment_chars: Maximum chars to include from amendments

        Returns:
            IntentResult with extracted intent
        """
        # Format debate chunks
        debate_text = self._format_chunks(debate_chunks, max_debate_chars)
        if not debate_text:
            debate_text = "[Aucun débat parlementaire trouvé pour cet article]"

        # Format amendment chunks
        amendment_text = self._format_chunks(amendment_chunks, max_amendment_chars)
        if not amendment_text:
            amendment_text = "[Aucun amendement trouvé pour cet article]"

        # Build the prompt
        prompt = INTENT_EXTRACTION_PROMPT.format(
            article_contenu=article_contenu,
            chunks_debats=debate_text,
            amendements=amendment_text,
        )

        # Call the appropriate LLM provider
        logger.info(f"Calling {self.provider} API for intent extraction")

        if self.provider == "groq":
            raw_response = self._call_groq(prompt)
        else:
            raw_response = self._call_anthropic(prompt)

        # Parse the response
        return self._parse_response(raw_response)

    def _format_chunks(self, chunks: list[Chunk], max_chars: int) -> str:
        """Format chunks into text, respecting max chars."""
        parts = []
        current_chars = 0

        for chunk in chunks:
            chunk_text = self._format_single_chunk(chunk)
            if current_chars + len(chunk_text) > max_chars:
                break
            parts.append(chunk_text)
            current_chars += len(chunk_text)

        return "\n\n---\n\n".join(parts)

    def _format_single_chunk(self, chunk: Chunk) -> str:
        """Format a single chunk with metadata."""
        metadata = chunk.metadata
        header_parts = []

        if metadata.get("orateur"):
            header_parts.append(f"Orateur: {metadata['orateur']}")
        if metadata.get("fonction"):
            header_parts.append(f"({metadata['fonction']})")
        if metadata.get("chambre"):
            header_parts.append(f"Chambre: {metadata['chambre']}")
        if metadata.get("date"):
            header_parts.append(f"Date: {metadata['date']}")

        header = " | ".join(header_parts) if header_parts else ""
        if header:
            return f"[{header}]\n{chunk.content}"
        return chunk.content

    def _parse_response(self, raw_response: str) -> IntentResult:
        """Parse LLM response into structured result."""
        # Default values
        intention_resume = ""
        sources_principales = []
        points_vigilance = []
        niveau_certitude = CertitudeLevel.MOYEN
        justification_certitude = ""

        # Parse sections
        sections = raw_response.split("###")

        for section in sections:
            section = section.strip()
            if not section:
                continue

            if section.startswith("INTENTION DU LÉGISLATEUR"):
                intention_resume = section.replace("INTENTION DU LÉGISLATEUR", "").strip()

            elif section.startswith("SOURCES PRINCIPALES"):
                lines = section.replace("SOURCES PRINCIPALES", "").strip().split("\n")
                for line in lines:
                    line = line.strip()
                    if line.startswith("-"):
                        source = self._parse_source_line(line[1:].strip())
                        if source:
                            sources_principales.append(source)

            elif section.startswith("POINTS DE VIGILANCE"):
                lines = section.replace("POINTS DE VIGILANCE", "").strip().split("\n")
                for line in lines:
                    line = line.strip()
                    if line.startswith("-"):
                        points_vigilance.append(line[1:].strip())

            elif section.startswith("NIVEAU DE CERTITUDE"):
                niveau_text = section.replace("NIVEAU DE CERTITUDE", "").strip().lower()
                if "élevé" in niveau_text or "eleve" in niveau_text:
                    niveau_certitude = CertitudeLevel.ELEVE
                elif "faible" in niveau_text:
                    niveau_certitude = CertitudeLevel.FAIBLE
                else:
                    niveau_certitude = CertitudeLevel.MOYEN

            elif section.startswith("JUSTIFICATION"):
                justification_certitude = section.replace("JUSTIFICATION", "").strip()

        return IntentResult(
            intention_resume=intention_resume,
            sources_principales=sources_principales,
            points_vigilance=points_vigilance,
            niveau_certitude=niveau_certitude,
            justification_certitude=justification_certitude,
            raw_response=raw_response,
        )

    def _parse_source_line(self, line: str) -> dict | None:
        """Parse a source citation line."""
        # Expected format: Citation: "[quote]" — Source: [name], [date], [chambre]
        try:
            if "Citation:" in line and "Source:" in line:
                citation_part, source_part = line.split("Source:", 1)
                citation = citation_part.replace("Citation:", "").strip().strip('"')

                # Parse source parts
                source_parts = source_part.strip().split(",")
                orateur = source_parts[0].strip() if len(source_parts) > 0 else ""
                date = source_parts[1].strip() if len(source_parts) > 1 else ""
                chambre = source_parts[2].strip() if len(source_parts) > 2 else ""

                return {
                    "citation": citation,
                    "orateur": orateur,
                    "date": date,
                    "chambre": chambre,
                }
        except Exception:
            pass

        # Fallback: return the whole line as citation
        return {"citation": line, "orateur": "", "date": "", "chambre": ""}


def format_intent_result_markdown(result: IntentResult) -> str:
    """Format IntentResult as Markdown for display."""
    lines = []

    lines.append("## Intention du législateur")
    lines.append("")
    lines.append(result.intention_resume)
    lines.append("")

    lines.append("## Sources principales")
    lines.append("")
    for source in result.sources_principales:
        citation = source.get("citation", "")
        orateur = source.get("orateur", "")
        date = source.get("date", "")
        chambre = source.get("chambre", "")

        source_info = ", ".join(filter(None, [orateur, date, chambre]))
        lines.append(f'> "{citation}"')
        if source_info:
            lines.append(f"> — {source_info}")
        lines.append("")

    if result.points_vigilance:
        lines.append("## Points de vigilance")
        lines.append("")
        for point in result.points_vigilance:
            lines.append(f"- {point}")
        lines.append("")

    lines.append(f"## Niveau de certitude: {result.niveau_certitude.value}")
    lines.append("")
    lines.append(result.justification_certitude)

    return "\n".join(lines)
