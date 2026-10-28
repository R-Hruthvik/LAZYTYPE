"""
Tumbling-window streaming engine — inspired by dimastatz/whisper-flow.

Architecture:
  AudioRecorder → VAD filter → RingBuffer → ProviderChain.transcribe()
                                            ↓
                                        on_transcript(text)

Features:
  - VAD Silence Auto-Stop: automatically stops recording after 1.5s of silence
    following speech, avoiding long idle recordings.
  - Sub-second transcription dispatch via Groq/fallback providers.
  - Millisecond precision timing metrics (Speech time, STT latency, Total time).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

log = logging.getLogger(__name__)

# Type aliases
AudioChunk = bytes
TranscriptCallback = Callable[[str, bool, dict], None]  # (text, is_final, metrics)


class TumblingWindow:
    """
    Accumulates raw PCM chunks during a recording session.

    - add_chunk(): called for every 30ms audio frame
    - flush():     returns all accumulated bytes and clears buffer
    - duration_s:  total audio collected so far (seconds)
    """

    def __init__(self, sample_rate: int = 16_000) -> None:
        self._chunks: list[bytes] = []
        self._sample_rate = sample_rate
        self._bytes_per_second = sample_rate * 2  # int16 = 2 bytes/sample

    def add_chunk(self, chunk: bytes) -> None:
        self._chunks.append(chunk)

    def flush(self) -> bytes:
        """Return all accumulated audio and clear the buffer."""
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data

    def snapshot(self) -> bytes:
        """Return accumulated audio WITHOUT clearing (for partial results)."""
        return b"".join(self._chunks)

    @property
    def duration_s(self) -> float:
        total_bytes = sum(len(c) for c in self._chunks)
        return total_bytes / self._bytes_per_second

    @property
    def is_empty(self) -> bool:
        return len(self._chunks) == 0

    def clear(self) -> None:
        self._chunks.clear()


class StreamingSession:
    """
    Manages a single push-to-talk or auto-stop recording session.

    Accepts audio chunks via add_chunk(chunk, is_speech).
    Automatically triggers stop() when silence exceeds `silence_timeout_s`
    after speech has occurred, or when hotkey is released.
    """

    def __init__(
        self,
        chain,  # ProviderChain
        on_transcript: TranscriptCallback,
        language: str = "en",
        sample_rate: int = 16_000,
        max_duration_s: float = 30.0,
        silence_timeout_s: float = 1.5,  # Auto-stop after 1.5s of silence after speech
        partial_interval_s: float | None = None,  # None = no partial results
        min_audio_bytes: int = 3200,  # ~100ms — skip tiny accidental taps
    ) -> None:
        self._chain = chain
        self._on_transcript = on_transcript
        self._language = language
        self._window = TumblingWindow(sample_rate)
        self._max_duration = max_duration_s
        self._silence_timeout_s = silence_timeout_s
        self._partial_interval = partial_interval_s
        self._min_bytes = min_audio_bytes

        self._running = False
        self._has_detected_speech = False
        self._start_time: float = 0.0
        self._last_speech_time: float = 0.0
        self._partial_task: asyncio.Task | None = None
        self._stop_lock = asyncio.Lock()

    async def start(self) -> None:
        """Begin the recording session."""
        self._running = True
        self._has_detected_speech = False
        self._start_time = time.monotonic()
        self._last_speech_time = self._start_time
        self._window.clear()
        log.debug("StreamingSession: started")

        if self._partial_interval:
            self._partial_task = asyncio.create_task(self._partial_loop())

    def add_chunk(self, chunk: bytes, is_speech: bool = True) -> None:
        """Feed a raw PCM chunk into the window with speech activity flag."""
        if not self._running:
            return

        now = time.monotonic()
        if self.elapsed_s >= self._max_duration:
            log.info(
                "StreamingSession: max duration (%.1fs) reached — auto-stopping", self._max_duration
            )
            asyncio.create_task(self.stop())
            return

        if is_speech:
            self._has_detected_speech = True
            self._last_speech_time = now

        # Only check silence cutoff if user has started speaking
        elif self._has_detected_speech and self._silence_timeout_s > 0:
            silence_duration = now - self._last_speech_time
            if silence_duration >= self._silence_timeout_s:
                log.info(
                    "StreamingSession: silence detected (%.1fs after speech) — auto-stopping immediately",
                    silence_duration,
                )
                asyncio.create_task(self.stop())
                return

        self._window.add_chunk(chunk)

    async def stop(self) -> str:
        """
        Stop the session, flush all audio to STT, and return transcript.
        Thread-safe and idempotent via lock.
        """
        async with self._stop_lock:
            if not self._running:
                return ""
            self._running = False

            if self._partial_task:
                self._partial_task.cancel()
                try:
                    await self._partial_task
                except asyncio.CancelledError:
                    pass

            audio = self._window.flush()
            audio_duration_s = len(audio) / 32000.0  # 16kHz * 2 bytes/sample

            if len(audio) < self._min_bytes:
                log.debug("StreamingSession: audio too short (%d bytes) — skipping", len(audio))
                self.last_metrics = {}
                self._call_callback("", True, {})
                return ""

            provider_name = self._chain.active_provider()
            log.info(
                "StreamingSession: transcribing %.2fs audio via %s…",
                audio_duration_s,
                provider_name,
            )

            t_stt_start = time.monotonic()
            error_msg: str | None = None
            try:
                # Allow ample time for network requests and fallback providers (up to 45s)
                text = await asyncio.wait_for(
                    self._chain.transcribe(audio, self._language),
                    timeout=45.0,
                )
            except asyncio.TimeoutError:
                log.error("StreamingSession: transcription timed out after 45s")
                text = ""
                error_msg = "Transcription timed out after 45s"
            except Exception as exc:  # pylint: disable=broad-except
                log.error("StreamingSession: transcription failed: %s", exc)
                text = ""
                error_msg = str(exc)
            stt_latency_s = time.monotonic() - t_stt_start
            total_duration_s = time.monotonic() - self._start_time

            metrics = {
                "audio_duration_s": round(audio_duration_s, 2),
                "stt_latency_ms": round(stt_latency_s * 1000, 1),
                "total_duration_s": round(total_duration_s, 2),
                "provider": provider_name,
                "error": error_msg,
            }

            log.info(
                "StreamingSession: ⚡ Transcribed in %dms (audio: %.1fs, total: %.1fs) via %s: %r",
                metrics["stt_latency_ms"],
                metrics["audio_duration_s"],
                metrics["total_duration_s"],
                provider_name,
                text,
            )

            self.last_metrics = metrics
            self._call_callback(text, True, metrics)
            return text

    def _call_callback(self, text: str, is_final: bool, metrics: dict) -> None:
        try:
            self._on_transcript(text, is_final, metrics)
        except TypeError:
            # Backwards-compatible for callbacks expecting (text, is_final)
            self._on_transcript(text, is_final)  # type: ignore[call-arg]

    async def _partial_loop(self) -> None:
        """Periodically emit partial results while recording."""
        while self._running:
            await asyncio.sleep(self._partial_interval)  # type: ignore[arg-type]
            if not self._running or self._window.is_empty:
                continue
            snapshot = self._window.snapshot()
            if len(snapshot) < self._min_bytes:
                continue
            try:
                text = await self._chain.transcribe(snapshot, self._language)
                if text:
                    self._call_callback(text, False, {})
            except Exception as exc:  # pylint: disable=broad-except
                log.debug("StreamingSession: partial transcription error: %s", exc)

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self._start_time if self._start_time else 0.0

    @property
    def is_running(self) -> bool:
        return self._running
