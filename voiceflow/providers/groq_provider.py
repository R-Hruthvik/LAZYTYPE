"""
Groq STT Provider — whisper-large-v3-turbo via Groq Cloud.

Features:
  - Multi-API Key Pooling: supports multiple Groq API keys with Round-Robin load
    distribution and automatic per-key rate-limit failover.
  - Automatically reads:
      1. GROQ_API_KEYS="gsk_key1,gsk_key2,gsk_key3" (comma-separated)
      2. GROQ_API_KEY_1, GROQ_API_KEY_2, etc.
      3. GROQ_API_KEY (single key)
  - Sub-second latency (~230ms - 850ms) as the PRIMARY provider in the fallback chain.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass

from groq import AsyncGroq, RateLimitError, AuthenticationError, APIConnectionError, APIStatusError

from voiceflow.providers.base import (
    STTProvider,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    pcm_to_wav,
)

log = logging.getLogger(__name__)

GROQ_MODELS = [
    "whisper-large-v3-turbo",
    "whisper-large-v3",
    "distil-whisper-large-v3-en",
]


@dataclass
class GroqKeyEntry:
    key: str
    client: AsyncGroq
    cooldown_until: float = 0.0
    total_requests: int = 0
    errors: int = 0

    def is_available(self) -> bool:
        return time.monotonic() >= self.cooldown_until


def _discover_groq_keys() -> list[str]:
    """Find all configured Groq keys from environment variables."""
    keys: list[str] = []

    # 1. Comma-separated GROQ_API_KEYS
    multi = os.environ.get("GROQ_API_KEYS", "")
    if multi:
        for k in multi.split(","):
            k = k.strip().strip('"').strip("'")
            if k and k not in keys:
                keys.append(k)

    # 2. Numbered keys: GROQ_API_KEY_1, GROQ_API_KEY_2, etc.
    idx = 1
    while True:
        k_val = os.environ.get(f"GROQ_API_KEY_{idx}")
        if not k_val:
            break
        clean_k = k_val.strip().strip('"').strip("'")
        if clean_k and clean_k not in keys:
            keys.append(clean_k)
        idx += 1

    # 3. Single standard GROQ_API_KEY
    single = os.environ.get("GROQ_API_KEY", "").strip().strip('"').strip("'")
    if single and single not in keys:
        keys.append(single)

    return keys


class GroqProvider(STTProvider):
    """
    Groq Cloud Whisper STT provider with multi-key pooling and round-robin.
    """

    name = "groq"

    def __init__(
        self,
        model: str = GROQ_MODELS[0],
        sample_rate: int = 16_000,
    ) -> None:
        raw_keys = _discover_groq_keys()
        if not raw_keys:
            raise ProviderAuthError(
                "groq", "No Groq API key found in GROQ_API_KEY or GROQ_API_KEYS"
            )

        self._model = model
        self._sample_rate = sample_rate
        self._entries: list[GroqKeyEntry] = [
            GroqKeyEntry(key=k, client=AsyncGroq(api_key=k)) for k in raw_keys
        ]
        self._current_index = 0

        log.info(
            "GroqProvider: initialized with %d API key(s) (model=%s)",
            len(self._entries),
            model,
        )

    def _get_candidate_entries(self) -> list[GroqKeyEntry]:
        """Return available keys ordered by round-robin starting position."""
        n = len(self._entries)
        candidates = []
        for i in range(n):
            idx = (self._current_index + i) % n
            entry = self._entries[idx]
            if entry.is_available():
                candidates.append(entry)
        return candidates

    async def transcribe(self, audio_bytes: bytes, language: str = "en") -> str:
        """Send PCM bytes to Groq Whisper with automatic per-key failover."""
        wav_bytes = pcm_to_wav(audio_bytes, self._sample_rate)

        candidates = self._get_candidate_entries()
        if not candidates:
            # All keys in pool are currently cooling down
            min_remaining = min(
                max(1.0, e.cooldown_until - time.monotonic()) for e in self._entries
            )
            log.warning(
                "GroqProvider: all %d keys are rate-limited (min cooldown: %.0fs)",
                len(self._entries),
                min_remaining,
            )
            raise ProviderRateLimitError("groq", int(min_remaining))

        last_error: Exception | None = None

        for entry in candidates:
            # Advance round-robin pointer for next call
            self._current_index = (self._entries.index(entry) + 1) % len(self._entries)

            try:
                entry.total_requests += 1
                response = await entry.client.audio.transcriptions.create(
                    file=("audio.wav", wav_bytes, "audio/wav"),
                    model=self._model,
                    language=language,
                    response_format="text",
                    temperature=0.0,
                )
                text = response.strip() if isinstance(response, str) else response.text.strip()
                log.debug(
                    "GroqProvider: key ending in ...%s transcribed %d bytes",
                    entry.key[-6:],
                    len(audio_bytes),
                )
                return text

            except RateLimitError as exc:
                retry_after = _parse_retry_after(exc) or 60
                log.warning(
                    "GroqProvider: key ...%s rate limited — cooling down %ds",
                    entry.key[-6:],
                    retry_after,
                )
                entry.cooldown_until = time.monotonic() + retry_after
                entry.errors += 1
                last_error = exc
                # Try next key in candidates list!

            except AuthenticationError as exc:
                log.error("GroqProvider: key ...%s auth failed: %s", entry.key[-6:], exc)
                entry.cooldown_until = time.monotonic() + 86400  # Disable for 24h
                entry.errors += 1
                last_error = exc

            except (APIConnectionError, APIStatusError) as exc:
                log.warning("GroqProvider: key ...%s connection error: %s", entry.key[-6:], exc)
                entry.cooldown_until = time.monotonic() + 15
                entry.errors += 1
                last_error = exc

        # If all candidates in this attempt failed:
        if isinstance(last_error, RateLimitError):
            raise ProviderRateLimitError("groq", 60) from last_error
        if isinstance(last_error, AuthenticationError):
            raise ProviderAuthError("groq", str(last_error)) from last_error
        raise ProviderUnavailableError("groq", str(last_error)) from last_error

    async def health_check(self) -> bool:
        """Verify at least one key in the pool is valid."""
        for entry in self._entries:
            try:
                await entry.client.models.list()
                return True
            except Exception:
                continue
        return False

    @property
    def key_count(self) -> int:
        return len(self._entries)

    @property
    def model(self) -> str:
        return self._model


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #


def _parse_retry_after(exc: RateLimitError) -> int | None:
    """Extract retry-after seconds from Groq's error response."""
    try:
        # Check HTTP response headers first
        response = getattr(exc, "response", None)
        if response is not None and hasattr(response, "headers"):
            retry_header = response.headers.get("retry-after")
            if retry_header:
                try:
                    return int(float(retry_header))
                except ValueError:
                    pass

        msg = str(exc)
        # Handle cases like "Please try again in 1m23.456s." or "Please try again in 5.2s."
        match = re.search(
            r"try again in\s+(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?", msg, re.IGNORECASE
        )
        if match and (match.group(1) or match.group(2)):
            minutes = float(match.group(1)) if match.group(1) else 0.0
            seconds = float(match.group(2)) if match.group(2) else 0.0
            total = minutes * 60.0 + seconds
            if total > 0:
                return int(total) + 1
    except Exception:
        pass
    return None
