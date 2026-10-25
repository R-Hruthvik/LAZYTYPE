"""
NVIDIA NIM STT Provider — OpenAI-compatible Whisper endpoint.

Free tier: ~40 requests/minute via build.nvidia.com.
API key read from env var NVIDIA_API_KEY (set in ~/.bashrc).
"""

from __future__ import annotations

import logging
import os

import openai

from voiceflow.providers.base import (
    STTProvider,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    pcm_to_wav,
)

log = logging.getLogger(__name__)

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "openai/whisper-large-v3"


class NvidiaProvider(STTProvider):
    """
    NVIDIA NIM Whisper STT provider.

    Uses OpenAI SDK with base_url override — OpenAI-compatible endpoint.
    Reads NVIDIA_API_KEY from environment.
    """

    name = "nvidia"

    def __init__(self, model: str = NVIDIA_MODEL, sample_rate: int = 16_000) -> None:
        api_key = os.environ.get("NVIDIA_API_KEY")
        if not api_key:
            raise ProviderAuthError("nvidia", "NVIDIA_API_KEY not set in environment")
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=NVIDIA_BASE_URL,
        )
        self._model = model
        self._sample_rate = sample_rate
        log.info("NvidiaProvider: initialized with model=%s", model)

    async def transcribe(self, audio_bytes: bytes, language: str = "en") -> str:
        wav_bytes = pcm_to_wav(audio_bytes, self._sample_rate)
        try:
            response = await self._client.audio.transcriptions.create(
                file=("audio.wav", wav_bytes, "audio/wav"),
                model=self._model,
                language=language,
                response_format="text",
            )
            text = response.strip() if isinstance(response, str) else response.text.strip()
            log.debug("NvidiaProvider: transcribed → %r", text[:80])
            return text
        except openai.RateLimitError as exc:
            raise ProviderRateLimitError("nvidia") from exc
        except openai.AuthenticationError as exc:
            raise ProviderAuthError("nvidia", str(exc)) from exc
        except (openai.APIConnectionError, openai.APIStatusError) as exc:
            raise ProviderUnavailableError("nvidia", str(exc)) from exc

    async def health_check(self) -> bool:
        try:
            await self._client.models.list()
            return True
        except Exception:  # pylint: disable=broad-except
            return False
