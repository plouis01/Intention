"""Batch processing infrastructure for analyzing multiple laws or articles.

Provides resumable batch processing with state management, rate limiting,
and progress tracking.
"""

import json
import logging
import time
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from src.analysis.models import BatchAnalysisState

logger = logging.getLogger(__name__)


class BatchProcessor:
    """
    Batch processor for analyzing laws or articles with resumability.

    Features:
    - State persistence for resume capability
    - Rate limiting to avoid API throttling
    - Progress tracking and reporting
    - Error handling with retry logic
    """

    def __init__(
        self,
        state_dir: Path | None = None,
        rate_limit_delay: float = 1.0,
        max_retries: int = 3,
    ):
        """
        Initialize batch processor.

        Args:
            state_dir: Directory to store state files
            rate_limit_delay: Seconds to wait between items
            max_retries: Max retries for failed items
        """
        self.state_dir = state_dir or Path("data/batch_state")
        self.rate_limit_delay = rate_limit_delay
        self.max_retries = max_retries
        self._current_state: BatchAnalysisState | None = None

    def create_batch(
        self,
        mode: str,
        items: list[str],
    ) -> BatchAnalysisState:
        """
        Create a new batch processing state.

        Args:
            mode: "laws" or "articles"
            items: List of items to process (law refs or article refs)

        Returns:
            New BatchAnalysisState
        """
        batch_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()

        state = BatchAnalysisState(
            batch_id=batch_id,
            started_at=now,
            last_updated=now,
            mode=mode,
            total_items=len(items),
            completed_items=[],
            failed_items=[],
            current_item=None,
        )

        # Save the items list separately
        self._save_items_list(batch_id, items)
        self._save_state(state)

        logger.info(f"Created batch {batch_id} with {len(items)} items")
        return state

    def load_batch(self, batch_id: str) -> BatchAnalysisState | None:
        """Load an existing batch state."""
        state_file = self.state_dir / f"{batch_id}.json"
        if not state_file.exists():
            return None

        with open(state_file, encoding="utf-8") as f:
            data = json.load(f)

        return BatchAnalysisState(**data)

    def get_latest_batch(self) -> BatchAnalysisState | None:
        """Get the most recently created batch."""
        self.state_dir.mkdir(parents=True, exist_ok=True)

        state_files = list(self.state_dir.glob("*.json"))
        if not state_files:
            return None

        # Find most recent by modification time
        latest = max(state_files, key=lambda p: p.stat().st_mtime)

        # Skip items list files
        if latest.stem.endswith("_items"):
            state_files = [f for f in state_files if not f.stem.endswith("_items")]
            if not state_files:
                return None
            latest = max(state_files, key=lambda p: p.stat().st_mtime)

        with open(latest, encoding="utf-8") as f:
            data = json.load(f)

        return BatchAnalysisState(**data)

    def get_pending_items(self, batch_id: str) -> list[str]:
        """Get list of items not yet processed."""
        state = self.load_batch(batch_id)
        if not state:
            return []

        items = self._load_items_list(batch_id)
        completed = set(state.completed_items)
        failed = set(state.failed_items)

        return [item for item in items if item not in completed and item not in failed]

    def process_batch(
        self,
        state: BatchAnalysisState,
        process_func: Callable[[str], bool],
        progress_callback: Callable[[BatchAnalysisState], None] | None = None,
    ) -> BatchAnalysisState:
        """
        Process all pending items in a batch.

        Args:
            state: Batch state to process
            process_func: Function that processes a single item, returns True on success
            progress_callback: Optional callback for progress updates

        Returns:
            Updated BatchAnalysisState
        """
        self._current_state = state
        items = self._load_items_list(state.batch_id)
        pending = self.get_pending_items(state.batch_id)

        logger.info(f"Processing batch {state.batch_id}: {len(pending)} pending of {len(items)} total")

        for item in pending:
            # Update current item
            state.current_item = item
            state.last_updated = datetime.now().isoformat()
            self._save_state(state)

            # Process with retries
            success = False
            for attempt in range(self.max_retries):
                try:
                    success = process_func(item)
                    break
                except Exception as e:
                    logger.warning(f"Attempt {attempt + 1} failed for {item}: {e}")
                    if attempt < self.max_retries - 1:
                        time.sleep(self.rate_limit_delay * (attempt + 1))

            # Update state
            if success:
                state.completed_items.append(item)
                logger.info(f"Completed: {item} ({len(state.completed_items)}/{state.total_items})")
            else:
                state.failed_items.append(item)
                logger.warning(f"Failed: {item}")

            state.current_item = None
            state.last_updated = datetime.now().isoformat()
            self._save_state(state)

            # Progress callback
            if progress_callback:
                progress_callback(state)

            # Rate limiting
            if pending.index(item) < len(pending) - 1:
                time.sleep(self.rate_limit_delay)

        self._current_state = None
        return state

    def get_batch_summary(self, batch_id: str) -> dict:
        """Get a summary of batch processing status."""
        state = self.load_batch(batch_id)
        if not state:
            return {"error": "Batch not found"}

        return {
            "batch_id": batch_id,
            "mode": state.mode,
            "started_at": state.started_at,
            "last_updated": state.last_updated,
            "total": state.total_items,
            "completed": len(state.completed_items),
            "failed": len(state.failed_items),
            "pending": state.total_items - len(state.completed_items) - len(state.failed_items),
            "progress_pct": state.progress_pct,
            "is_complete": state.is_complete,
        }

    def list_batches(self) -> list[dict]:
        """List all batch states."""
        self.state_dir.mkdir(parents=True, exist_ok=True)

        batches = []
        for state_file in self.state_dir.glob("*.json"):
            if state_file.stem.endswith("_items"):
                continue

            try:
                with open(state_file, encoding="utf-8") as f:
                    data = json.load(f)
                state = BatchAnalysisState(**data)
                batches.append(self.get_batch_summary(state.batch_id))
            except Exception as e:
                logger.warning(f"Failed to load batch {state_file}: {e}")

        return sorted(batches, key=lambda x: x.get("started_at", ""), reverse=True)

    def _save_state(self, state: BatchAnalysisState) -> None:
        """Save batch state to file."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        state_file = self.state_dir / f"{state.batch_id}.json"

        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(asdict(state), f, indent=2, ensure_ascii=False)

    def _save_items_list(self, batch_id: str, items: list[str]) -> None:
        """Save the items list for a batch."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        items_file = self.state_dir / f"{batch_id}_items.json"

        with open(items_file, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)

    def _load_items_list(self, batch_id: str) -> list[str]:
        """Load the items list for a batch."""
        items_file = self.state_dir / f"{batch_id}_items.json"
        if not items_file.exists():
            return []

        with open(items_file, encoding="utf-8") as f:
            return json.load(f)


def get_all_laws(
    years: tuple[int, int] | None = None,
    law_types: list[str] | None = None,
) -> list[str]:
    """
    Get list of all law references to process.

    Args:
        years: Optional (start_year, end_year) filter
        law_types: Optional list of law types to include

    Returns:
        List of law references like "LF_2024"
    """
    from src.analysis.law_analyzer import list_available_laws

    laws = list_available_laws()
    refs = []

    for loi in laws:
        law_type = loi.get("type", "")
        year = loi.get("annee_budgetaire", 0)

        # Apply filters
        if years and not (years[0] <= year <= years[1]):
            continue
        if law_types and law_type not in law_types:
            continue

        refs.append(f"{law_type}_{year}")

    return sorted(refs)


def get_all_cgi_articles() -> list[str]:
    """
    Get list of all CGI articles from reference file.

    Returns:
        List of article references
    """
    ref_path = Path("data/cgi_articles_reference.json")
    if not ref_path.exists():
        return []

    with open(ref_path, encoding="utf-8") as f:
        data = json.load(f)

    return [art["reference"] for art in data.get("articles", [])]


def get_indexed_articles() -> list[str]:
    """
    Get list of articles from the article_modifications index.

    Returns:
        List of article references
    """
    index_path = Path("data/modifications_cgi_enriched.json")
    if not index_path.exists():
        return []

    with open(index_path, encoding="utf-8") as f:
        data = json.load(f)

    return list(data.get("articles", {}).keys())
