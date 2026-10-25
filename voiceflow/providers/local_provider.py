"""
Local STT Provider — faster-whisper (CTranslate2 backend, CPU-optimized).

No API key required. Always available, even offline.
This is the LAST fallback in the chain — never fails.

Default model: base.en (~150MB, good quality, ~1s on modern CPU)
Configurable: tiny.en (fast, lower quality) → medium.en (slow, best quality)
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from voiceflow.providers.base import STTProvider, pcm_to_wav

log = logging.getLogger(__name__)

# Models ordered by speed (fastest first)
LOCAL_MODELS = ["tiny.en", "base.en", "small.en", "medium.en", "large-v3"]


class LocalProvider(STTProvider):
    """
    CPU-based Whisper via faster-whisper.

    Uses CTranslate2 backend with int8 quantization for fast CPU inference.
    Model is cached in ~/.cache/huggingface/hub after first download.
    """

    name = "local"

    def __init__(
        self,
        model_size: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model: Any = None  # lazy-loaded on first use
        log.info("LocalProvider: will use model=%s on %s (%s)", model_size, device, compute_type)

    async def _ensure_model(self) -> None:
        """Lazy-load the model in a thread pool (avoids blocking the event loop)."""
        if self._model is not None:
            return
        log.info(
            "LocalProvider: loading faster-whisper model %r (first time may download ~150MB)...",
            self._model_size,
        )
        loop = asyncio.get_running_loop()
        self._model = await loop.run_in_executor(None, self._load_model)
        log.info("LocalProvider: model loaded")

    def _load_model(self) -> object:
        from faster_whisper import WhisperModel  # imported lazily to avoid startup delay

        return WhisperModel(
            self._model_size,
            device=self._device,
            compute_type=self._compute_type,
        )

    async def transcribe(self, audio_bytes: bytes, language: str = "en") -> str:
        """Run faster-whisper inference in a thread pool."""
        await self._ensure_model()
        loop = asyncio.get_running_loop()
        wav_bytes = pcm_to_wav(audio_bytes)
        text = await loop.run_in_executor(None, self._run_inference, wav_bytes, language)
        log.debug("LocalProvider: → %r", text[:80])
        return text

    def _run_inference(self, wav_bytes: bytes, language: str) -> str:
        buf = io.BytesIO(wav_bytes)
        segments, info = self._model.transcribe(  # type: ignore[attr-defined]
            buf,
            language=language if not language.endswith(".en") else "en",
            beam_size=5,
            vad_filter=True,  # built-in VAD removes silence
            vad_parameters={"min_silence_duration_ms": 500},
        )
        log.debug(
            "LocalProvider: detected language=%s (prob=%.2f)",
            info.language,
            info.language_probability,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def health_check(self) -> bool:
        """Local provider is always healthy (just checks the model can be imported)."""
        try:
            import faster_whisper  # noqa: F401

            return True
        except ImportError:
            log.error(
                "LocalProvider: faster-whisper is not installed — run: pip install faster-whisper"
            )
            return False
