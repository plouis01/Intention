"""Chunking strategies for parliamentary debates and documents."""

import html
import re
from dataclasses import dataclass
from typing import Iterator

from src.parsers.debates import ParsedDebat


def strip_html(text: str) -> str:
    """Strip HTML tags and decode HTML entities from text."""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


# Patterns for procedural preambles to strip before embedding.
# Matched case-insensitively at the start of intervention text.
_PREAMBLE_RE = re.compile(
    r"^(?:"
    # President introducing next speaker: "La parole est à M. Dupont, pour soutenir..."
    # Handle M./Mme/Mlle abbreviation periods before matching sentence end
    r"La parole est à (?:M\.|Mme|Mlle)?\s*[^.]+\.\s*"
    # Address to the chair/minister (strip up to first comma)
    r"|(?:Monsieur|Madame) (?:le |la )?(?:président|présidente|ministre|secrétaire d'[ÉEée]tat|rapporteur|rapporteure)[^,]*,\s*"
    # Address to colleagues
    r"|(?:Mes chers|Chers) collègues,?\s*"
    r")",
    re.IGNORECASE,
)


def strip_procedural_preamble(text: str) -> str:
    """Strip procedural preamble from the start of an intervention."""
    stripped = _PREAMBLE_RE.sub("", text, count=1)
    # Keep original if stripping left too little substance
    return stripped if len(stripped.strip()) > 30 else text


@dataclass
class Chunk:
    """A chunk of text for embedding."""

    content: str
    metadata: dict
    chunk_type: str  # intervention, article_discussion, paragraph
    source_id: str  # Reference to source (debate ID, article ID, etc.)
    position: int  # Position within source


def chunk_by_intervention(debat: ParsedDebat) -> Iterator[Chunk]:
    """
    Chunk a debate by individual speaker interventions.

    This is the recommended strategy for legislative debates as it:
    - Preserves speaker attribution
    - Maintains context of who said what
    - Allows filtering by speaker role (ministre, rapporteur, etc.)
    - Includes previous speaker context to preserve conversational flow

    Args:
        debat: Parsed debate data

    Yields:
        Chunk for each intervention
    """
    prev_speaker = None
    prev_content_preview = None

    for interv in debat.interventions:
        # Skip very short interventions (e.g., "Oui", "D'accord")
        if len(interv.contenu) < 50:
            continue

        metadata = {
            "orateur": interv.orateur,
            "fonction": interv.fonction,
            "chambre": debat.chambre,
            "date": debat.date.isoformat() if debat.date else None,
            "etape": debat.etape,
            "articles_mentionnes": interv.articles_mentionnes,
        }

        # Add previous speaker context for conversational flow
        context_prefix = ""
        if prev_speaker:
            context_prefix = f"[{prev_speaker}: {prev_content_preview}...]\n\n"

        # Strip procedural preamble to focus embedding on substantive content
        cleaned_contenu = strip_procedural_preamble(interv.contenu)
        content = f"{context_prefix}{interv.orateur} ({interv.fonction or 'député'}): {cleaned_contenu}"

        yield Chunk(
            content=content,
            metadata=metadata,
            chunk_type="intervention",
            source_id=debat.reference or str(debat.date),
            position=interv.ordre,
        )

        prev_speaker = f"{interv.orateur} ({interv.fonction or 'député'})"
        prev_content_preview = interv.contenu[:150]


def chunk_by_article_discussion(
    debat: ParsedDebat, target_article: str
) -> Iterator[Chunk]:
    """
    Chunk a debate by grouping interventions that discuss a specific article.

    This creates larger, context-rich chunks focused on a single article.

    Args:
        debat: Parsed debate data
        target_article: Article number to filter by (e.g., "12", "15 bis")

    Yields:
        Chunk containing all interventions mentioning the article
    """
    relevant_interventions = []

    for interv in debat.interventions:
        # Check if this intervention mentions the target article
        if any(target_article.lower() in art.lower() for art in interv.articles_mentionnes):
            relevant_interventions.append(interv)

    if not relevant_interventions:
        return

    # Combine interventions into a single chunk
    content_parts = []
    for interv in relevant_interventions:
        content_parts.append(f"{interv.orateur}: {interv.contenu}")

    combined_content = "\n\n".join(content_parts)

    # Split if too long (>8000 chars ~2000 tokens)
    if len(combined_content) > 8000:
        yield from _split_long_text(
            combined_content,
            metadata={
                "article": target_article,
                "chambre": debat.chambre,
                "date": debat.date.isoformat() if debat.date else None,
                "etape": debat.etape,
                "num_speakers": len(set(i.orateur for i in relevant_interventions)),
            },
            chunk_type="article_discussion",
            source_id=debat.reference or str(debat.date),
        )
    else:
        yield Chunk(
            content=combined_content,
            metadata={
                "article": target_article,
                "chambre": debat.chambre,
                "date": debat.date.isoformat() if debat.date else None,
                "etape": debat.etape,
                "num_speakers": len(set(i.orateur for i in relevant_interventions)),
            },
            chunk_type="article_discussion",
            source_id=debat.reference or str(debat.date),
            position=0,
        )


def chunk_by_sliding_window(
    text: str,
    window_size: int = 1000,
    overlap: int = 200,
    metadata: dict | None = None,
    source_id: str = "",
) -> Iterator[Chunk]:
    """
    Chunk text using a sliding window approach.

    Useful for long, unstructured text where speaker boundaries are unclear.

    Args:
        text: Text to chunk
        window_size: Number of characters per chunk
        overlap: Number of characters to overlap between chunks
        metadata: Metadata to attach to all chunks
        source_id: Source identifier

    Yields:
        Chunks with overlapping windows
    """
    if not text:
        return

    metadata = metadata or {}
    position = 0
    start = 0

    while start < len(text):
        end = start + window_size

        # Try to end at a sentence boundary
        if end < len(text):
            # Look for sentence end within the last 100 chars
            search_start = max(end - 100, start)
            sentence_end = max(
                text.rfind(". ", search_start, end),
                text.rfind("? ", search_start, end),
                text.rfind("! ", search_start, end),
            )
            if sentence_end > search_start:
                end = sentence_end + 1

        chunk_text = text[start:end].strip()

        if chunk_text:
            yield Chunk(
                content=chunk_text,
                metadata=metadata,
                chunk_type="paragraph",
                source_id=source_id,
                position=position,
            )
            position += 1

        start = end - overlap
        if start >= len(text) - overlap:
            break


def _split_long_text(
    text: str,
    metadata: dict,
    chunk_type: str,
    source_id: str,
    max_size: int = 6000,
) -> Iterator[Chunk]:
    """Split long text into smaller chunks at paragraph boundaries."""
    paragraphs = text.split("\n\n")
    current_chunk = []
    current_size = 0
    position = 0

    for para in paragraphs:
        para_size = len(para)

        if current_size + para_size > max_size and current_chunk:
            # Yield current chunk
            yield Chunk(
                content="\n\n".join(current_chunk),
                metadata=metadata,
                chunk_type=chunk_type,
                source_id=source_id,
                position=position,
            )
            position += 1
            current_chunk = []
            current_size = 0

        current_chunk.append(para)
        current_size += para_size

    # Yield remaining
    if current_chunk:
        yield Chunk(
            content="\n\n".join(current_chunk),
            metadata=metadata,
            chunk_type=chunk_type,
            source_id=source_id,
            position=position,
        )


def chunk_amendment(
    numero: str,
    auteur: str | None,
    objet: str | None,
    contenu: str | None,
    expose_motifs: str | None,
    sort: str | None,
    article_ref: str | None,
    chambre: str,
    dossier_ref: str | None,
    date: str | None = None,
) -> Chunk:
    """
    Create a chunk from an amendment.

    Combines all amendment fields into a searchable chunk.

    Args:
        Amendment fields

    Returns:
        Single chunk representing the amendment
    """
    # Strip HTML tags and decode entities from text fields
    if objet:
        objet = strip_html(objet)
    if contenu:
        contenu = strip_html(contenu)
    if expose_motifs:
        expose_motifs = strip_html(expose_motifs)

    parts = []

    if article_ref:
        parts.append(f"Amendement à l'article {article_ref}")
    if auteur:
        parts.append(f"Auteur: {auteur}")
    if objet:
        parts.append(f"Objet: {objet}")
    if contenu:
        parts.append(f"Dispositif: {contenu}")
    if expose_motifs and expose_motifs != objet:
        parts.append(f"Exposé des motifs: {expose_motifs}")
    if sort:
        parts.append(f"Sort: {sort}")

    content = "\n".join(parts)

    return Chunk(
        content=content,
        metadata={
            "numero": numero,
            "auteur": auteur,
            "article_ref": article_ref,
            "sort": sort,
            "chambre": chambre,
            "dossier_ref": dossier_ref,
            "date": date,
        },
        chunk_type="amendement",
        source_id=f"{dossier_ref or 'unknown'}-{numero}",
        position=0,
    )


def estimate_tokens(text: str) -> int:
    """
    Estimate the number of tokens in text.

    Uses a simple heuristic: ~4 characters per token for French text.

    Args:
        text: Text to estimate

    Returns:
        Estimated token count
    """
    return len(text) // 4
