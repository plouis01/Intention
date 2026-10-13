"""Groq API client pool with automatic key rotation on rate limits."""

import logging
import threading
import time
from typing import Any

from src.utils.config import get_settings

logger = logging.getLogger(__name__)


class GroqClientPool:
    """
    Pool of Groq API clients with automatic key rotation.

    Features:
    - Lazy-loads Groq clients for each API key
    - Round-robin rotation on rate limit (429) or access denied (403) errors
    - No cooldown: all keys are always retried on each request
    - Optional Anthropic fallback when all Groq keys are exhausted
    - Configurable delay between calls

    Usage:
        pool = GroqClientPool()
        response = pool.chat_completion(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "Hello"}],
            max_tokens=1000,
        )
    """

    def __init__(
        self,
        api_keys: list[str] | None = None,
        call_delay: float = 0.0,
        anthropic_fallback: bool = False,
    ):
        """
        Initialize the pool.

        Args:
            api_keys: List of Groq API keys. If None, loads from settings.
            call_delay: Seconds to wait between successful calls (rate limiting).
            anthropic_fallback: If True, fall back to Anthropic when all Groq keys exhausted.
        """
        self._lock = threading.RLock()
        self._clients: dict[int, Any] = {}
        self._current_index: int = 0
        self._call_delay = call_delay
        self._anthropic_fallback = anthropic_fallback
        self._anthropic_client = None
        self._last_call_time = 0.0

        if api_keys is not None:
            self._keys = [k for k in api_keys if k]
        else:
            settings = get_settings()
            self._keys = settings.get_groq_api_keys()

        if self._keys:
            logger.info(f"GroqClientPool initialized with {len(self._keys)} API keys")
        else:
            logger.warning("GroqClientPool initialized with no API keys")

    @property
    def available_keys_count(self) -> int:
        """Number of configured keys."""
        return len(self._keys)

    @property
    def total_keys_count(self) -> int:
        """Total number of configured keys."""
        return len(self._keys)

    def _get_client(self, index: int | None = None) -> Any:
        """Get or create a Groq client for the given key index."""
        from groq import Groq

        if not self._keys:
            raise ValueError(
                "No Groq API keys configured. Set GROQ_API_KEYS environment variable. "
                "Get a free API key at https://console.groq.com/"
            )

        if index is None:
            index = self._current_index

        if index >= len(self._keys):
            raise ValueError(f"Invalid key index {index}, only {len(self._keys)} keys available")

        if index not in self._clients:
            self._clients[index] = Groq(api_key=self._keys[index], max_retries=0)

        return self._clients[index]

    def _rotate(self) -> bool:
        """
        Rotate to the next key in round-robin order.

        Returns:
            True if successfully rotated, False if only one key available.
        """
        if len(self._keys) <= 1:
            return False

        self._current_index = (self._current_index + 1) % len(self._keys)
        logger.info(f"Rotated to key {self._current_index + 1}/{len(self._keys)}")
        return True

    def reset(self) -> None:
        """Reset rotation to first key."""
        with self._lock:
            self._current_index = 0
        logger.info("Reset Groq API key rotation to first key")

    def _get_anthropic_client(self):
        """Lazy-load Anthropic client for fallback."""
        if self._anthropic_client is None:
            from anthropic import Anthropic
            settings = get_settings()
            key = settings.anthropic_api_key or ""
            if not key or key.startswith("your_"):
                raise RuntimeError(
                    "Anthropic fallback requested but ANTHROPIC_API_KEY is not set or is a placeholder."
                )
            self._anthropic_client = Anthropic(api_key=key)
        return self._anthropic_client

    def _call_anthropic(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Make a call to Anthropic API as fallback."""
        settings = get_settings()
        client = self._get_anthropic_client()

        # Convert from OpenAI message format to Anthropic format
        system_msg = ""
        user_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_msg = msg["content"]
            else:
                user_messages.append(msg)

        kwargs = {
            "model": settings.claude_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": user_messages,
        }
        if system_msg:
            kwargs["system"] = system_msg

        response = client.messages.create(**kwargs)
        return response.content[0].text

    def _apply_call_delay(self):
        """Apply configured delay between calls."""
        if self._call_delay > 0:
            with self._lock:
                elapsed = time.time() - self._last_call_time
                remaining = self._call_delay - elapsed
            if remaining > 0:
                time.sleep(remaining)

    def chat_completion(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = 2000,
        temperature: float = 0.3,
        **kwargs,
    ) -> str:
        """
        Make a chat completion request with automatic key rotation.

        Features:
        - Round-robin rotation on rate limit or access denied errors
        - Tries all keys before failing (no cooldown between requests)
        - Optional Anthropic fallback
        - Configurable delay between calls

        Args:
            model: Model name (e.g., "llama-3.3-70b-versatile")
            messages: List of message dicts with role and content
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature
            **kwargs: Additional arguments passed to Groq API

        Returns:
            Response content as string

        Raises:
            RuntimeError: If all API keys are rate-limited and no fallback
            Exception: Other API errors
        """
        self._apply_call_delay()

        last_error = None

        # Each call iterates all keys independently using its own starting point.
        # This avoids race conditions with concurrent threads sharing _current_index.
        with self._lock:
            start_index = self._current_index

        for attempt in range(len(self._keys)):
            key_index = (start_index + attempt) % len(self._keys)
            with self._lock:
                client = self._get_client(key_index)

            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    **kwargs,
                )
                with self._lock:
                    self._last_call_time = time.time()
                    # Advance shared index so next call starts from a fresh key
                    self._current_index = (key_index + 1) % len(self._keys)
                return response.choices[0].message.content

            except Exception as e:
                last_error = e
                error_str = str(e).lower()

                is_rate_limit = (
                    "429" in error_str
                    or "rate_limit" in error_str
                    or "rate limit" in error_str
                    or "too many requests" in error_str
                )
                is_timeout = (
                    "timeout" in error_str
                    or "timed out" in error_str
                    or isinstance(e, TimeoutError)
                )
                is_transient = is_rate_limit or is_timeout or "403" in error_str or "access denied" in error_str

                if is_transient:
                    logger.warning(
                        f"{'Rate limit' if is_rate_limit else 'Transient error'} on key "
                        f"{key_index + 1} ({attempt + 1}/{len(self._keys)} tried)"
                    )
                    continue
                else:
                    raise

        # All keys tried — attempt Anthropic fallback
        if self._anthropic_fallback:
            logger.info("All Groq keys failed, falling back to Anthropic")
            try:
                result = self._call_anthropic(messages, max_tokens, temperature)
                with self._lock:
                    self._last_call_time = time.time()
                return result
            except Exception as anthropic_err:
                logger.error(f"Anthropic fallback also failed: {anthropic_err}")

        raise RuntimeError(
            f"All {len(self._keys)} Groq API keys failed. "
            "Add more API keys or use --anthropic-fallback."
        ) from last_error


# Singleton instance for shared use
_default_pool: GroqClientPool | None = None
_default_pool_lock = threading.Lock()


def get_groq_pool(
    call_delay: float = 0.0,
    anthropic_fallback: bool = True,
) -> GroqClientPool:
    """Get the default GroqClientPool instance (singleton).

    Args:
        call_delay: Seconds to wait between calls.
        anthropic_fallback: Fall back to Anthropic when all Groq keys exhausted (default: True).
    """
    global _default_pool
    if _default_pool is None:
        with _default_pool_lock:
            if _default_pool is None:
                # Auto-disable Anthropic fallback if API key is a placeholder
                if anthropic_fallback:
                    settings = get_settings()
                    key = settings.anthropic_api_key or ""
                    if not key or key.startswith("your_"):
                        logger.warning(
                            "ANTHROPIC_API_KEY is unset or a placeholder — disabling Anthropic fallback"
                        )
                        anthropic_fallback = False
                _default_pool = GroqClientPool(
                    call_delay=call_delay,
                    anthropic_fallback=anthropic_fallback,
                )
    return _default_pool


def reset_groq_pool() -> None:
    """Reset the default pool's exhausted keys."""
    if _default_pool is not None:
        _default_pool.reset()


def reset_default_pool() -> None:
    """Reset the singleton so it can be re-created with new parameters."""
    global _default_pool
    with _default_pool_lock:
        _default_pool = None
