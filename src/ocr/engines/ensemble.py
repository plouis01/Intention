"""Ensemble OCR engine combining multiple models via word-level voting.

Uses the Ocromore-inspired approach: run multiple OCR engines on the same
image, align outputs word-by-word, and pick the best word via voting +
dictionary + accent scoring.
"""

import logging
from difflib import SequenceMatcher
from pathlib import Path

from src.ocr.engines.base import OCREngine

logger = logging.getLogger(__name__)

# Common French parliamentary vocabulary for scoring
PARLIAMENTARY_TERMS = {
    "président", "présidente", "ministre", "rapporteur", "rapporteure",
    "assemblée", "nationale", "sénat", "république", "française",
    "amendement", "article", "commission", "projet", "proposition",
    "budget", "finances", "discussion", "scrutin", "vote",
    "adopté", "rejeté", "retiré", "orateur", "séance", "débat",
    "monsieur", "madame", "collègue", "gouvernement", "législature",
}


def has_french_accents(word: str) -> bool:
    """Check if a word contains French accented characters."""
    return any(c in word for c in "éèêëàâùûüôîïçÉÈÊËÀÂÙÛÜÔÎÏÇ")


def is_garbage(word: str) -> bool:
    """Check if a word looks like OCR garbage."""
    if len(word) <= 1:
        return False
    # Too many non-letter chars
    non_letter = sum(1 for c in word if not c.isalpha() and c not in "-'")
    return non_letter / len(word) > 0.5


def score_word(word: str) -> float:
    """Score a word for quality. Higher = better."""
    score = 0.0

    # Dictionary match
    if word.lower().rstrip(".,;:!?") in PARLIAMENTARY_TERMS:
        score += 3.0

    # Has accents (French text should have them)
    if has_french_accents(word):
        score += 1.0

    # Garbage penalty
    if is_garbage(word):
        score -= 2.0

    # Length bonus (very short fragments are usually artifacts)
    if len(word) >= 3:
        score += 0.5

    return score


def align_and_vote(texts: list[str]) -> str:
    """Align multiple OCR outputs and vote on best word.

    Args:
        texts: List of OCR outputs from different engines.

    Returns:
        Best-quality merged text.
    """
    if not texts:
        return ""
    if len(texts) == 1:
        return texts[0]

    # Split into words (preserving whitespace structure)
    word_lists = [t.split() for t in texts]

    # Use the longest output as reference
    ref_idx = max(range(len(word_lists)), key=lambda i: len(word_lists[i]))
    ref_words = word_lists[ref_idx]

    # Align each other output against reference
    alignments = []
    for i, words in enumerate(word_lists):
        if i == ref_idx:
            alignments.append(list(range(len(ref_words))))
            continue

        sm = SequenceMatcher(None, ref_words, words)
        alignment = [None] * len(ref_words)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    alignment[i1 + k] = j1 + k
            elif tag == "replace":
                for k in range(min(i2 - i1, j2 - j1)):
                    alignment[i1 + k] = j1 + k
        alignments.append(alignment)

    # Vote on each position
    result_words = []
    for pos in range(len(ref_words)):
        candidates = []
        for eng_idx in range(len(word_lists)):
            if eng_idx == ref_idx:
                candidates.append(ref_words[pos])
            else:
                aligned_pos = alignments[eng_idx][pos]
                if aligned_pos is not None and aligned_pos < len(word_lists[eng_idx]):
                    candidates.append(word_lists[eng_idx][aligned_pos])

        if not candidates:
            result_words.append(ref_words[pos])
            continue

        # Score each candidate
        best_word = max(candidates, key=score_word)
        result_words.append(best_word)

    return " ".join(result_words)


class EnsembleEngine(OCREngine):
    """Ensemble OCR engine combining multiple models."""

    name = "ensemble"

    def __init__(self, engines: list[OCREngine]):
        """Initialize with a list of OCR engines.

        Args:
            engines: List of OCREngine instances to combine.
        """
        self.engines = engines
        self.name = "ensemble_" + "+".join(e.name for e in engines)

    def ocr_image(self, image_path: Path) -> str:
        """Run all engines on the image and combine results."""
        texts = []
        for engine in self.engines:
            try:
                text = engine.ocr_image(image_path)
                if text.strip():
                    texts.append(text)
            except Exception as e:
                logger.warning(f"Engine {engine.name} failed: {e}")

        if not texts:
            return ""

        return align_and_vote(texts)
