"""
Deepgram STT Provider — Nova-3 model.

Free tier: $200 credit for new accounts.
API key read from env var DEEPGRAM_API_KEY (set in ~/.bashrc).
"""

from __future__ import annotations

import logging
import os

from deepgram import AsyncDeepgramClient
from deepgram.core.api_error import ApiError

from voiceflow.providers.base import (
    STTProvider,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    _pcm_to_wav,
)

log = logging.getLogger(__name__)


class DeepgramProvider(STTProvider):
    """Deepgram Nova-3 STT provider."""

    name = "deepgram"

    def __init__(self, model: str = "nova-3", sample_rate: int = 16_000) -> None:
        api_key = os.environ.get("DEEPGRAM_API_KEY")
        if not api_key:
            raise ProviderAuthError("deepgram", "DEEPGRAM_API_KEY not set in environment")
        self._client = AsyncDeepgramClient(api_key=api_key)
        self._model = model
        self._sample_rate = sample_rate
        log.info("DeepgramProvider: initialized with model=%s", model)

    async def transcribe(self, audio_bytes: bytes, language: str = "en") -> str:
        try:
            wav_bytes = _pcm_to_wav(audio_bytes, self._sample_rate)
            response = await self._client.listen.v1.media.transcribe_file(
                request=wav_bytes,
                model=self._model,
                language=language,
                smart_format=True,
                punctuate=True,
            )
            channels = getattr(response.results, "channels", None)
            if not channels or not channels[0].alternatives:
                return ""
            text = channels[0].alternatives[0].transcript.strip()
            log.debug("DeepgramProvider: → %r", text[:80])
            return text
        except ApiError as exc:
            if exc.status_code in (401, 403):
                raise ProviderAuthError("deepgram", str(exc)) from exc
            if exc.status_code == 429:
                raise ProviderRateLimitError("deepgram") from exc
            raise ProviderUnavailableError("deepgram", str(exc)) from exc
        except Exception as exc:  # pylint: disable=broad-except
            raise ProviderUnavailableError("deepgram", str(exc)) from exc

    async def health_check(self) -> bool:
        try:
            await self._client.manage.v1.projects.list()
            return True
        except Exception:  # pylint: disable=broad-except
            return False
