"""
Abstract base class for all STT providers.

Every provider must implement `transcribe()` and `health_check()`.
The ProviderChain in chain.py selects and rotates among them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import io
import wave


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 16_000) -> bytes:
    """Wrap raw 16-bit mono PCM bytes in a RIFF/WAV header."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)  # pylint: disable=no-member
        wf.setsampwidth(2)  # pylint: disable=no-member
        wf.setframerate(sample_rate)  # pylint: disable=no-member
        wf.writeframes(pcm_bytes)  # pylint: disable=no-member
    return buf.getvalue()


_pcm_to_wav = pcm_to_wav  # Backward compatibility


class STTProvider(ABC):
    """Abstract speech-to-text provider."""

    name: str = "unknown"

    @abstractmethod
    async def transcribe(self, audio_bytes: bytes, language: str = "en") -> str:
        """
        Transcribe raw PCM audio bytes (16kHz, mono, int16) to text.

        Args:
            audio_bytes: Raw PCM audio data.
            language: BCP-47 language code (e.g. "en", "es", "fr").

        Returns:
            Transcribed text string (may be empty if silence).

        Raises:
            ProviderRateLimitError: When the provider is rate-limited.
            ProviderUnavailableError: When the provider is down/unreachable.
            ProviderAuthError: When the API key is invalid/missing.
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the provider is reachable and the key is valid."""

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r}>"


# ------------------------------------------------------------------ #
# Provider-specific exceptions
# ------------------------------------------------------------------ #


class ProviderError(Exception):
    """Base class for all provider errors."""

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"[{provider}] {message}")


class ProviderRateLimitError(ProviderError):
    """Provider returned 429 / rate limit exceeded."""

    def __init__(self, provider: str, retry_after: int | None = None) -> None:
        self.retry_after = retry_after  # seconds until retry is safe
        suffix = f"; retry after {retry_after}s" if retry_after else ""
        msg = f"Rate limited{suffix}"
        super().__init__(provider, msg)


class ProviderUnavailableError(ProviderError):
    """Network error, 5xx, or provider is down."""


class ProviderAuthError(ProviderError):
    """API key missing, invalid, or expired."""
