"""
AssemblyAI STT Provider — Universal-2 model.

Free tier: Free API credits for new accounts.
API key read from env var ASSEMBLYAI_API_KEY (set in ~/.bashrc).
"""

from __future__ import annotations

import asyncio
import io
import logging
import os

import assemblyai as aai

from voiceflow.providers.base import (
    STTProvider,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    pcm_to_wav,
)

log = logging.getLogger(__name__)


class AssemblyAIProvider(STTProvider):
    """AssemblyAI Universal-2 STT provider."""

    name = "assemblyai"

    def __init__(self, sample_rate: int = 16_000) -> None:
        api_key = os.environ.get("ASSEMBLYAI_API_KEY")
        if not api_key:
            raise ProviderAuthError("assemblyai", "ASSEMBLYAI_API_KEY not set in environment")
        aai.settings.api_key = api_key
        self._sample_rate = sample_rate
        self._transcriber = aai.Transcriber()
        log.info("AssemblyAIProvider: initialized")

    async def transcribe(self, audio_bytes: bytes, language: str = "en") -> str:
        wav_bytes = pcm_to_wav(audio_bytes, self._sample_rate)
        config = aai.TranscriptionConfig(language_code=language, punctuate=True, format_text=True)
        try:
            # AssemblyAI SDK is sync — run in thread pool
            loop = asyncio.get_running_loop()
            transcript = await loop.run_in_executor(
                None,
                lambda: self._transcriber.transcribe(io.BytesIO(wav_bytes), config=config),
            )
            if transcript.status == aai.TranscriptStatus.error:
                raise ProviderUnavailableError("assemblyai", transcript.error or "unknown error")
            text = (transcript.text or "").strip()
            log.debug("AssemblyAIProvider: → %r", text[:80])
            return text
        except ProviderUnavailableError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            msg = str(exc)
            if "401" in msg or "403" in msg or "auth" in msg.lower():
                raise ProviderAuthError("assemblyai", msg) from exc
            if "429" in msg:
                raise ProviderRateLimitError("assemblyai") from exc
            raise ProviderUnavailableError("assemblyai", msg) from exc

    async def health_check(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
            # Attempt a dummy list call to validate key
            await loop.run_in_executor(None, lambda: aai.Transcriber().list_transcripts())
            return True
        except Exception:  # pylint: disable=broad-except
            return False
