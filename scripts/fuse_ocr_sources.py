#!/usr/bin/env python3
"""Fuse Tesseract reOCR with embedded PDF OCR for improved text quality.

Combines the column-ordered structure of Tesseract output with the higher
character/accent accuracy of the embedded PDF OCR layer using word-level
alignment via difflib.SequenceMatcher.

Pipeline position: after batch_reocr_archives.py, before fix_ocr_claude.py.

Usage:
    python scripts/fuse_ocr_sources.py --legislature 1
    python scripts/fuse_ocr_sources.py --all
    python scripts/fuse_ocr_sources.py --legislature 1 --dry-run
    python scripts/fuse_ocr_sources.py --legislature 1 --test 5
    python scripts/fuse_ocr_sources.py --workers 4
    python scripts/fuse_ocr_sources.py --legislature 1 --force
"""

import argparse
import json
import logging
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path

import fitz

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PDF_DIR = PROJECT_ROOT / "data" / "raw" / "archives_an"
REOCR_DIR = PROJECT_ROOT / "data" / "reocr"
LOG_DIR = PROJECT_ROOT / "data" / "reocr_fix_logs"
REFERENCE_FILE = LOG_DIR / "deputies_reference.json"
FRENCH_WORDS_FILE = LOG_DIR / "french_words.txt"
LEGISLATURES = list(range(1, 12))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalization (shared with fix_ocr_claude.py)
# ---------------------------------------------------------------------------


def normalize_for_match(s: str) -> str:
    """Normalize a word for alignment matching: lowercase, strip accents, strip punctuation."""
    s = s.lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^\w]", "", s)
    return s


# ---------------------------------------------------------------------------
# Reference loading
# ---------------------------------------------------------------------------

_deputy_names: set[str] | None = None
_french_words: set[str] | None = None


def load_deputy_names() -> set[str]:
    """Load normalized deputy names from reference file."""
    global _deputy_names
    if _deputy_names is not None:
        return _deputy_names

    _deputy_names = set()
    if not REFERENCE_FILE.exists():
        logger.warning(f"Deputy reference not found: {REFERENCE_FILE}")
        return _deputy_names

    with open(REFERENCE_FILE, encoding="utf-8") as f:
        data = json.load(f)

    for d in data["deputies"]:
        # Add full name parts
        for part in d["full_name"].split():
            norm = normalize_for_match(part)
            if len(norm) >= 3:
                _deputy_names.add(norm)
        # Add first name and last name separately
        for part in d.get("last_name", "").split():
            norm = normalize_for_match(part)
            if len(norm) >= 3:
                _deputy_names.add(norm)

    logger.info(f"Loaded {len(_deputy_names)} deputy name tokens")
    return _deputy_names


def load_french_words() -> set[str]:
    """Load normalized French word list."""
    global _french_words
    if _french_words is not None:
        return _french_words

    _french_words = set()
    if not FRENCH_WORDS_FILE.exists():
        logger.warning(f"French dictionary not found: {FRENCH_WORDS_FILE}")
        return _french_words

    with open(FRENCH_WORDS_FILE, encoding="utf-8") as f:
        for line in f:
            word = line.strip()
            if word and not word.startswith("#"):
                _french_words.add(normalize_for_match(word))

    logger.info(f"Loaded {len(_french_words)} French words")
    return _french_words


# ---------------------------------------------------------------------------
# Embedded PDF OCR extraction with column reordering
# ---------------------------------------------------------------------------


def extract_embedded_page(doc: fitz.Document, page_idx: int) -> str:
    """Extract text from PDF embedded OCR layer with column reordering.

    Classifies text blocks as LEFT, RIGHT, or HEADER based on bounding boxes,
    then reads HEADER -> LEFT -> RIGHT (same order as Tesseract output).
    """
    page = doc[page_idx]
    blocks = page.get_text("dict")["blocks"]
    page_width = page.rect.width
    col_threshold = page_width * 0.52

    left: list[tuple[float, str]] = []
    right: list[tuple[float, str]] = []
    header: list[tuple[float, str]] = []

    for b in blocks:
        if b["type"] != 0:  # skip image blocks
            continue
        x0 = b["bbox"][0]
        y0 = b["bbox"][1]
        x1 = b["bbox"][2]

        # Extract text from block lines/spans
        lines = []
        for line in b.get("lines", []):
            spans_text = "".join(s["text"] for s in line.get("spans", []))
            if spans_text.strip():
                lines.append(spans_text)
        text = "\n".join(lines)
        if not text.strip():
            continue

        # Classify block position
        if x0 < col_threshold and x1 < col_threshold + 10:
            left.append((y0, text))
        elif x0 >= col_threshold - 10:
            right.append((y0, text))
        else:
            # Spans both columns -> header/full-width
            header.append((y0, text))

    # Read order: header -> left column -> right column (matches Tesseract)
    ordered = sorted(header) + sorted(left) + sorted(right)
    return "\n".join(t for _, t in ordered)


def extract_all_embedded_pages(pdf_path: Path) -> list[str]:
    """Extract embedded OCR text from all pages of a PDF."""
    doc = fitz.open(str(pdf_path))
    try:
        pages = []
        for i in range(len(doc)):
            pages.append(extract_embedded_page(doc, i))
        return pages
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Word-level alignment
# ---------------------------------------------------------------------------


def tokenize_with_positions(text: str) -> list[tuple[str, int, int]]:
    """Tokenize text into words with their start/end positions.

    Returns list of (word, start_pos, end_pos) tuples.
    """
    tokens = []
    for m in re.finditer(r"\S+", text):
        tokens.append((m.group(), m.start(), m.end()))
    return tokens


def align_words(
    tess_words: list[str], embed_words: list[str]
) -> list[tuple[str | None, str | None]]:
    """Align two word sequences using SequenceMatcher.

    Returns list of (tess_word, embed_word) pairs.
    None means the word exists in only one source (insert/delete).

    Large misaligned regions (replace blocks > 3 words with very different sizes)
    are treated as unaligned - Tesseract words are kept, embedded words dropped.
    This prevents garbage substitutions from column ordering differences.
    """
    # Normalize for matching
    tess_norm = [normalize_for_match(w) for w in tess_words]
    embed_norm = [normalize_for_match(w) for w in embed_words]

    sm = SequenceMatcher(None, tess_norm, embed_norm, autojunk=False)
    pairs: list[tuple[str | None, str | None]] = []

    for op, i1, i2, j1, j2 in sm.get_opcodes():
        tess_len = i2 - i1
        embed_len = j2 - j1

        if op == "equal":
            for ti, ej in zip(range(i1, i2), range(j1, j2)):
                pairs.append((tess_words[ti], embed_words[ej]))
        elif op == "replace":
            # If the replace block is large and sizes differ a lot,
            # it's a misaligned region - keep Tesseract, drop embedded
            size_ratio = max(tess_len, embed_len) / max(min(tess_len, embed_len), 1)
            if (tess_len > 3 or embed_len > 3) and size_ratio > 2.0:
                # Misaligned region - keep Tesseract words as-is
                for ti in range(i1, i2):
                    pairs.append((tess_words[ti], None))
            else:
                # Small replace - zip 1:1 for word-level comparison
                tess_slice = tess_words[i1:i2]
                embed_slice = embed_words[j1:j2]
                for k in range(max(len(tess_slice), len(embed_slice))):
                    tw = tess_slice[k] if k < len(tess_slice) else None
                    ew = embed_slice[k] if k < len(embed_slice) else None
                    pairs.append((tw, ew))
        elif op == "delete":
            for ti in range(i1, i2):
                pairs.append((tess_words[ti], None))
        elif op == "insert":
            # Embed-only insertions are dropped (column artifacts)
            for ej in range(j1, j2):
                pairs.append((None, embed_words[ej]))

    return pairs


# ---------------------------------------------------------------------------
# Word scoring and selection
# ---------------------------------------------------------------------------


def has_accents(word: str) -> bool:
    """Check if a word contains accented characters."""
    for c in word:
        if unicodedata.category(c) == "Mn" or c in "àâäéèêëïîôùûüÿçœæÀÂÄÉÈÊËÏÎÔÙÛÜŸÇŒÆ":
            return True
    nfd = unicodedata.normalize("NFD", word)
    return any(unicodedata.category(c) == "Mn" for c in nfd)


def strip_punct(word: str) -> str:
    """Strip leading/trailing punctuation from a word, keep the alpha core."""
    return re.sub(r"^[^\w]+|[^\w]+$", "", word)


def score_word(word: str, deputy_names: set[str], french_words: set[str]) -> float:
    """Score a word's quality. Higher = better."""
    if not word:
        return 0.0

    norm = normalize_for_match(word)
    if not norm:
        return 0.1

    score = 0.0

    # Deputy name match (highest priority)
    if norm in deputy_names:
        score += 3.0

    # French dictionary match
    if norm in french_words:
        score += 2.0

    # Has accents (embedded OCR is better at accents)
    if has_accents(word):
        score += 0.5

    # Penalize garbage characters (common embedded OCR artifacts)
    garbage_chars = sum(1 for c in word if c in "§¢£¥°±²³µ¶·¹º»¼½¾¿×÷|\\{}[]<>~`@#$%^&*_=+•!")
    if garbage_chars > 0:
        score -= garbage_chars * 1.0

    # Penalize very short words that look like OCR fragments
    if len(norm) == 1 and norm not in {"a", "y", "i", "o", "n", "l"}:
        score -= 0.5

    return score


def choose_best_word(
    tess_word: str | None,
    embed_word: str | None,
    deputy_names: set[str],
    french_words: set[str],
) -> str:
    """Choose the best word from Tesseract and embedded OCR sources.

    Rules:
    1. If only one exists, use Tesseract (drop embed-only insertions to avoid column artifacts)
    2. If identical (normalized), prefer embedded (better accents)
    3. Score both and choose the better one, with bias toward Tesseract (structure)
    4. Never adopt a word that only differs in punctuation - keep Tesseract
    """
    if tess_word is None and embed_word is None:
        return ""
    if tess_word is None:
        # Embed-only insertions are usually column interleaving artifacts - drop them
        return ""
    if embed_word is None:
        return tess_word

    tess_norm = normalize_for_match(tess_word)
    embed_norm = normalize_for_match(embed_word)

    # Identical normalized form -> prefer embedded (better accents/chars)
    if tess_norm == embed_norm:
        # If embedded has accents that tesseract doesn't, prefer embedded
        if has_accents(embed_word) and not has_accents(tess_word):
            # But only if embedded doesn't add spurious punctuation
            tess_core = strip_punct(tess_word)
            embed_core = strip_punct(embed_word)
            if normalize_for_match(tess_core) == normalize_for_match(embed_core):
                # Preserve Tesseract's punctuation, take embedded's word core
                # Rebuild: tess prefix + embed core + tess suffix
                tess_prefix = tess_word[:tess_word.find(tess_core)] if tess_core else ""
                tess_suffix = tess_word[tess_word.find(tess_core) + len(tess_core):] if tess_core else ""
                return tess_prefix + embed_core + tess_suffix
            return embed_word
        return tess_word

    # If the alpha cores are identical, only punctuation differs - keep Tesseract
    tess_core = strip_punct(tess_word)
    embed_core = strip_punct(embed_word)
    if tess_core and normalize_for_match(tess_core) == normalize_for_match(embed_core):
        # Same word, different punctuation - keep Tesseract (preserves structure)
        # But still upgrade accents on the core if applicable
        if has_accents(embed_core) and not has_accents(tess_core):
            tess_prefix = tess_word[:tess_word.find(tess_core)]
            tess_suffix = tess_word[tess_word.find(tess_core) + len(tess_core):]
            return tess_prefix + embed_core + tess_suffix
        return tess_word

    # Score both
    score_tess = score_word(tess_word, deputy_names, french_words)
    score_embed = score_word(embed_word, deputy_names, french_words)

    # Embedded must have a clear advantage to override Tesseract structure
    if score_embed > score_tess + 0.5:
        # Take the embed word but preserve Tesseract punctuation if possible
        return embed_word

    return tess_word


# ---------------------------------------------------------------------------
# Page fusion: reconstruct with Tesseract whitespace
# ---------------------------------------------------------------------------


def fuse_page(tess_text: str, embed_text: str, deputy_names: set[str], french_words: set[str]) -> dict:
    """Fuse a single page's text from two OCR sources.

    Preserves Tesseract whitespace structure while substituting better words.

    Returns dict with:
        text: fused text
        stats: {total_words, changed_words, tess_only, embed_only}
    """
    if not embed_text.strip():
        # Embedded OCR is empty -> keep Tesseract as-is
        return {"text": tess_text, "stats": {"total_words": 0, "changed_words": 0, "tess_only": 0, "embed_only": 0}}

    # Check if embedded text is too short compared to Tesseract (likely bad extraction)
    if len(embed_text.strip()) < len(tess_text.strip()) * 0.3:
        return {"text": tess_text, "stats": {"total_words": 0, "changed_words": 0, "tess_only": 0, "embed_only": 0, "skipped": "embed_too_short"}}

    # Tokenize both
    tess_tokens = tokenize_with_positions(tess_text)
    embed_words_raw = [w for w, _, _ in tokenize_with_positions(embed_text)]

    tess_words = [w for w, _, _ in tess_tokens]

    # Align
    pairs = align_words(tess_words, embed_words_raw)

    # Choose best word for each pair
    fused_words = []
    stats = {"total_words": 0, "changed_words": 0, "tess_only": 0, "embed_only": 0}

    for tw, ew in pairs:
        chosen = choose_best_word(tw, ew, deputy_names, french_words)
        fused_words.append((tw, ew, chosen))
        stats["total_words"] += 1
        if tw is None:
            stats["embed_only"] += 1
        elif ew is None:
            stats["tess_only"] += 1
        elif chosen != tw:
            stats["changed_words"] += 1

    # Reconstruct text preserving Tesseract whitespace
    result = _reconstruct_text(tess_text, tess_tokens, fused_words)

    return {"text": result, "stats": stats}


def _reconstruct_text(
    tess_text: str,
    tess_tokens: list[tuple[str, int, int]],
    fused_words: list[tuple[str | None, str | None, str]],
) -> str:
    """Reconstruct fused text by replacing words in Tesseract text.

    Preserves all whitespace (newlines, spaces, indentation) from the original
    Tesseract output. Only substitutes individual words.
    """
    # Build replacement map: tess_token_index -> replacement word
    replacements: dict[int, str] = {}
    tess_idx = 0
    for tw, ew, chosen in fused_words:
        if tw is not None:
            # This pair includes a Tesseract word
            if tess_idx < len(tess_tokens) and chosen != tw:
                replacements[tess_idx] = chosen
            tess_idx += 1

    if not replacements:
        return tess_text

    # Rebuild text by replacing specific word positions
    parts = []
    prev_end = 0
    for idx, (word, start, end) in enumerate(tess_tokens):
        # Add whitespace before this word
        parts.append(tess_text[prev_end:start])
        # Add the word (original or replaced)
        if idx in replacements:
            parts.append(replacements[idx])
        else:
            parts.append(word)
        prev_end = end

    # Add trailing text after last word
    parts.append(tess_text[prev_end:])

    return "".join(parts)


# ---------------------------------------------------------------------------
# PDF-level fusion
# ---------------------------------------------------------------------------


def fuse_pdf(
    pdf_path: Path,
    body_path: Path,
    deputy_names: set[str],
    french_words: set[str],
    dry_run: bool = False,
) -> dict:
    """Fuse OCR sources for a single PDF.

    Reads the Tesseract _body.txt (or .bak if exists), extracts embedded OCR
    from the PDF, aligns and fuses page by page.

    Returns stats dict.
    """
    # Read Tesseract text (prefer .bak = original pre-Haiku)
    bak_path = Path(str(body_path) + ".bak")
    source_path = bak_path if bak_path.exists() else body_path
    tess_text = source_path.read_text(encoding="utf-8", errors="replace")

    # Split into pages by \n\n
    tess_pages = tess_text.split("\n\n")

    # Extract embedded OCR pages
    embed_pages = extract_all_embedded_pages(pdf_path)

    # Validate page count match
    if len(tess_pages) != len(embed_pages):
        return {
            "status": "skipped",
            "reason": f"page_mismatch: tess={len(tess_pages)} vs pdf={len(embed_pages)}",
            "pages": 0,
            "changed_words": 0,
        }

    # Fuse page by page
    fused_pages = []
    total_stats = {"total_words": 0, "changed_words": 0, "tess_only": 0, "embed_only": 0}

    for i, (tess_page, embed_page) in enumerate(zip(tess_pages, embed_pages)):
        result = fuse_page(tess_page, embed_page, deputy_names, french_words)
        fused_pages.append(result["text"])
        for k in total_stats:
            total_stats[k] += result["stats"].get(k, 0)

    # Reconstruct full text
    fused_text = "\n\n".join(fused_pages)

    if not dry_run:
        # Back up original if no .bak exists
        if not bak_path.exists():
            import shutil
            shutil.copy2(body_path, bak_path)

        # Write fused text
        tmp_path = Path(str(body_path) + ".tmp")
        tmp_path.write_text(fused_text, encoding="utf-8")
        os.replace(str(tmp_path), str(body_path))

        # Write marker for resume
        marker = Path(str(body_path) + ".fused")
        marker.write_text(
            f"fused_at={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"changed_words={total_stats['changed_words']}\n"
            f"total_words={total_stats['total_words']}\n",
            encoding="utf-8",
        )

    return {
        "status": "fused",
        "pages": len(tess_pages),
        **total_stats,
    }


# ---------------------------------------------------------------------------
# Discovery and scanning
# ---------------------------------------------------------------------------


def scan_pdfs(legislatures: list[int]) -> list[dict]:
    """Scan for PDFs that have corresponding _body.txt files."""
    items = []

    for leg in legislatures:
        reocr_leg = REOCR_DIR / f"leg{leg}"
        pdf_leg = PDF_DIR / f"leg{leg}"
        if not reocr_leg.exists():
            continue

        for session_dir in sorted(reocr_leg.iterdir()):
            if not session_dir.is_dir() or session_dir.name.startswith("."):
                continue

            for body_file in sorted(session_dir.glob("*_body.txt")):
                stem = body_file.stem.replace("_body", "")
                pdf_path = pdf_leg / session_dir.name / f"{stem}.pdf"

                if not pdf_path.exists():
                    continue

                items.append({
                    "legislature": leg,
                    "session": session_dir.name,
                    "pdf_path": pdf_path,
                    "body_path": body_file,
                    "stem": stem,
                })

    return items


def filter_pending(items: list[dict], force: bool = False) -> list[dict]:
    """Filter out already-fused PDFs (resume support)."""
    if force:
        return items

    pending = []
    for item in items:
        marker = Path(str(item["body_path"]) + ".fused")
        if not marker.exists():
            pending.append(item)

    return pending


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Fuse Tesseract reOCR with embedded PDF OCR"
    )
    parser.add_argument(
        "--legislature", type=int, choices=range(1, 12), default=None,
        help="Specific legislature to process (1-11)",
    )
    parser.add_argument(
        "--all", action="store_true", dest="all_legislatures",
        help="Process all legislatures (1-11)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report changes without writing files",
    )
    parser.add_argument(
        "--test", type=int, default=None,
        help="Process only the first N PDFs (for testing)",
    )
    parser.add_argument(
        "--workers", type=int, default=1,
        help="Number of parallel workers (default: 1)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-process already-fused files",
    )
    args = parser.parse_args()

    if not args.legislature and not args.all_legislatures:
        parser.error("Either --legislature N or --all is required")

    legislatures = [args.legislature] if args.legislature else LEGISLATURES

    # Load references
    deputy_names = load_deputy_names()
    french_words = load_french_words()

    # Scan for PDFs
    logger.info("Scanning for PDFs...")
    all_items = scan_pdfs(legislatures)
    logger.info(f"Found {len(all_items)} PDFs with body text")

    pending = filter_pending(all_items, force=args.force)
    logger.info(f"Already fused: {len(all_items) - len(pending)}")
    logger.info(f"To process: {len(pending)}")

    if args.test:
        pending = pending[:args.test]
        logger.info(f"Test mode: limited to {len(pending)} PDFs")

    if not pending:
        logger.info("Nothing to process.")
        return

    # Process
    start_time = time.time()
    success = 0
    skipped = 0
    errors = 0
    total_changed = 0
    total_words = 0

    def process_one(item: dict) -> dict:
        try:
            return {
                "item": item,
                "result": fuse_pdf(
                    item["pdf_path"],
                    item["body_path"],
                    deputy_names,
                    french_words,
                    dry_run=args.dry_run,
                ),
            }
        except Exception as e:
            return {
                "item": item,
                "result": {"status": "error", "reason": str(e)},
            }

    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(process_one, item): idx
                for idx, item in enumerate(pending)
            }
            for i, future in enumerate(as_completed(futures), 1):
                data = future.result()
                item = data["item"]
                result = data["result"]

                if result["status"] == "fused":
                    success += 1
                    total_changed += result["changed_words"]
                    total_words += result["total_words"]
                    pct = result["changed_words"] * 100 / result["total_words"] if result["total_words"] else 0
                    logger.info(
                        f"[{i}/{len(pending)}] {item['stem']} "
                        f"({item['session']}): "
                        f"{result['changed_words']}/{result['total_words']} words changed ({pct:.1f}%)"
                    )
                elif result["status"] == "skipped":
                    skipped += 1
                    logger.warning(
                        f"[{i}/{len(pending)}] {item['stem']} SKIPPED: {result['reason']}"
                    )
                else:
                    errors += 1
                    logger.error(
                        f"[{i}/{len(pending)}] {item['stem']} ERROR: {result.get('reason', '?')}"
                    )
    else:
        for i, item in enumerate(pending, 1):
            data = process_one(item)
            result = data["result"]

            if result["status"] == "fused":
                success += 1
                total_changed += result["changed_words"]
                total_words += result["total_words"]
                pct = result["changed_words"] * 100 / result["total_words"] if result["total_words"] else 0
                logger.info(
                    f"[{i}/{len(pending)}] {item['stem']} "
                    f"({item['session']}): "
                    f"{result['changed_words']}/{result['total_words']} words changed ({pct:.1f}%)"
                )
            elif result["status"] == "skipped":
                skipped += 1
                logger.warning(
                    f"[{i}/{len(pending)}] {item['stem']} SKIPPED: {result['reason']}"
                )
            else:
                errors += 1
                logger.error(
                    f"[{i}/{len(pending)}] {item['stem']} ERROR: {result.get('reason', '?')}"
                )

            # ETA after first 3
            if i >= 3:
                elapsed = time.time() - start_time
                avg = elapsed / i
                remaining = len(pending) - i
                eta = avg * remaining
                eta_m = int(eta // 60)
                eta_s = int(eta % 60)
                if i % 50 == 0 or i == len(pending):
                    logger.info(f"  ETA: {eta_m}m{eta_s:02d}s ({remaining} remaining)")

    elapsed = time.time() - start_time
    change_pct = total_changed * 100 / total_words if total_words else 0

    logger.info("=" * 60)
    logger.info("FUSION COMPLETE" if not args.dry_run else "DRY RUN COMPLETE")
    logger.info(f"  Processed:      {success}/{len(pending)}")
    logger.info(f"  Skipped:        {skipped}")
    logger.info(f"  Errors:         {errors}")
    logger.info(f"  Words changed:  {total_changed}/{total_words} ({change_pct:.1f}%)")
    logger.info(f"  Time:           {elapsed:.1f}s")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
