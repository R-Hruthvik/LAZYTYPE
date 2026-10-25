"""
ProviderChain — rotating fallback engine.

Priority order: Groq → NVIDIA → Deepgram → AssemblyAI → Local (never fails).

Behavior:
- Tries providers in priority order.
- On rate-limit (429): cools down that provider, moves to next.
- On auth error: permanently disables that provider for this session.
- On network error: marks provider as temporarily unavailable (cooldown).
- After cooldown expires, retries the provider again.
- Distributes load by rotating the primary provider if all healthy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable

from voiceflow.providers.base import (
    STTProvider,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)

log = logging.getLogger(__name__)


class ProviderStatus(Enum):
    OK = auto()
    COOLING_DOWN = auto()  # Temporarily unavailable (rate limit / network)
    DISABLED = auto()  # Auth error — permanently disabled for session


@dataclass
class ProviderState:
    provider: STTProvider
    status: ProviderStatus = ProviderStatus.OK
    error_count: int = 0
    cooldown_until: float = 0.0
    total_requests: int = 0
    total_errors: int = 0

    @property
    def name(self) -> str:
        return self.provider.name

    def is_available(self) -> bool:
        if self.status == ProviderStatus.DISABLED:
            return False
        if self.status == ProviderStatus.COOLING_DOWN:
            if time.monotonic() >= self.cooldown_until:
                log.info("ProviderChain: %s cooldown expired — re-enabling", self.name)
                self.status = ProviderStatus.OK
                self.error_count = 0
                return True
            return False
        return True


# Callback type: called whenever active provider changes
ProviderChangeCallback = Callable[[str], None]


class ProviderChain:
    """
    Manages a prioritised list of STT providers with automatic
    fallback, cooldown, and load rotation.
    """

    def __init__(
        self,
        providers: list[STTProvider],
        cooldown_after_errors: int = 3,
        default_cooldown_seconds: int = 60,
        on_provider_change: ProviderChangeCallback | None = None,
    ) -> None:
        self._states: list[ProviderState] = [ProviderState(p) for p in providers]
        self._cooldown_threshold = cooldown_after_errors
        self._default_cooldown = default_cooldown_seconds
        self._on_change = on_provider_change
        self._lock = asyncio.Lock()

        log.info(
            "ProviderChain: initialized with %d providers: %s",
            len(providers),
            [p.name for p in providers],
        )

    async def transcribe(self, audio_bytes: bytes, language: str = "en") -> str:
        """
        Transcribe audio using the best available provider.
        Automatically falls back on error.
        """
        async with self._lock:
            available = [s for s in self._states if s.is_available()]

        if not available:
            raise RuntimeError("No STT providers available!")

        last_err: Exception | None = None
        for state in available:
            try:
                async with self._lock:
                    state.total_requests += 1
                current = state.name
                log.debug("ProviderChain: trying %s", current)

                text = await state.provider.transcribe(audio_bytes, language)

                # Reset error count on success
                async with self._lock:
                    state.error_count = 0
                    if state.status == ProviderStatus.COOLING_DOWN:
                        state.status = ProviderStatus.OK

                return text

            except ProviderAuthError as exc:
                log.error(
                    "ProviderChain: %s auth error — disabling for session: %s", state.name, exc
                )
                async with self._lock:
                    state.status = ProviderStatus.DISABLED
                    state.total_errors += 1
                last_err = exc

            except ProviderRateLimitError as exc:
                cooldown = exc.retry_after or self._default_cooldown
                log.warning(
                    "ProviderChain: %s rate limited — cooling down %ds", state.name, cooldown
                )
                async with self._lock:
                    state.status = ProviderStatus.COOLING_DOWN
                    state.cooldown_until = time.monotonic() + cooldown
                    state.total_errors += 1
                last_err = exc
                if self._on_change:
                    self._on_change(self._active_name())

            except ProviderUnavailableError as exc:
                async with self._lock:
                    state.error_count += 1
                    state.total_errors += 1
                    if state.error_count >= self._cooldown_threshold:
                        log.warning(
                            "ProviderChain: %s had %d consecutive errors — cooling down %ds",
                            state.name,
                            state.error_count,
                            self._default_cooldown,
                        )
                        state.status = ProviderStatus.COOLING_DOWN
                        state.cooldown_until = time.monotonic() + self._default_cooldown
                        if self._on_change:
                            self._on_change(self._active_name())
                last_err = exc

            except Exception as exc:  # pylint: disable=broad-except
                log.exception("ProviderChain: unexpected error from %s: %s", state.name, exc)
                async with self._lock:
                    state.error_count += 1
                    state.total_errors += 1
                    if state.error_count >= self._cooldown_threshold:
                        log.warning(
                            "ProviderChain: %s had %d consecutive errors — cooling down %ds",
                            state.name,
                            state.error_count,
                            self._default_cooldown,
                        )
                        state.status = ProviderStatus.COOLING_DOWN
                        state.cooldown_until = time.monotonic() + self._default_cooldown
                        if self._on_change:
                            self._on_change(self._active_name())
                last_err = exc

        raise RuntimeError(f"All providers failed. Last error: {last_err}") from last_err

    def status_table(self) -> list[dict]:
        """Return current status of all providers (for TUI/tray display)."""
        rows = []
        for s in self._states:
            status_str = s.status.name
            if s.status == ProviderStatus.COOLING_DOWN:
                remaining = max(0, s.cooldown_until - time.monotonic())
                status_str = f"COOLING ({remaining:.0f}s)"
            rows.append(
                {
                    "provider": s.name,
                    "status": status_str,
                    "requests": s.total_requests,
                    "errors": s.total_errors,
                }
            )
        return rows

    def _active_name(self) -> str:
        """Return the name of the first available provider."""
        for s in self._states:
            if s.is_available():
                return s.name
        return "none"

    def active_provider(self) -> str:
        return self._active_name()
