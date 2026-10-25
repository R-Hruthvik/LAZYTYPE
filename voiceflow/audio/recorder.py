"""
Audio recorder — captures microphone input as a stream of raw PCM chunks.

Format: 16 kHz, mono, 16-bit signed int (what all STT providers expect).
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
from typing import AsyncGenerator

import numpy as np


# Suppress ALSA/JACK noise on stderr during PyAudio init
# These are harmless warnings about unavailable audio backends
def _suppress_alsa_noise():
    """Redirect C-level stderr to /dev/null during PyAudio import."""
    try:
        _devnull = os.open(os.devnull, os.O_WRONLY)
        _old_stderr = os.dup(2)
        os.dup2(_devnull, 2)
        os.close(_devnull)
        return _old_stderr
    except Exception:
        return None


def _restore_stderr(old_fd):
    if old_fd is not None:
        os.dup2(old_fd, 2)
        os.close(old_fd)


_old = _suppress_alsa_noise()
import pyaudio

_restore_stderr(_old)

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHANNELS = 1
FORMAT = pyaudio.paInt16
CHUNK_DURATION_MS = 30  # 30 ms frames for webrtcvad compatibility
CHUNK_FRAMES = int(SAMPLE_RATE * CHUNK_DURATION_MS / 1000)  # 480 frames


class AudioRecorder:
    """
    Push-based mic recorder.

    Usage (async):
        async with AudioRecorder() as recorder:
            async for chunk in recorder.stream():
                # chunk: bytes (16kHz mono int16 PCM, ~30ms)
                process(chunk)
    """

    def __init__(self, device_index: int | None = None, sample_rate: int = SAMPLE_RATE):
        self._device_index = device_index
        self._sample_rate = sample_rate
        self._chunk_frames = int(sample_rate * CHUNK_DURATION_MS / 1000)
        self._pa: pyaudio.PyAudio | None = None
        self._stream: pyaudio.Stream | None = None
        self._q: queue.Queue[bytes | None] = queue.Queue(maxsize=200)
        self._active = False

    # ------------------------------------------------------------------ #
    # Lifecycle & Context manager
    # ------------------------------------------------------------------ #

    def open(self) -> "AudioRecorder":
        """Initialize PortAudio backend."""
        if self._pa is None:
            old = _suppress_alsa_noise()
            try:
                self._pa = pyaudio.PyAudio()
            finally:
                _restore_stderr(old)
        return self

    def close(self) -> None:
        """Stop capturing and terminate PortAudio backend."""
        self.stop()
        if self._pa:
            self._pa.terminate()
            self._pa = None

    def __enter__(self) -> "AudioRecorder":
        return self.open()

    def __exit__(self, *_) -> None:
        self.close()

    async def __aenter__(self) -> "AudioRecorder":
        return self.open()

    async def __aexit__(self, *args) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Open the PyAudio stream and begin capturing."""
        if self._active:
            return
        assert self._pa is not None, "Use as context manager"
        self._active = True
        self._stream = self._pa.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=self._sample_rate,
            input=True,
            input_device_index=self._device_index,
            frames_per_buffer=self._chunk_frames,
            stream_callback=self._callback,
        )
        self._stream.start_stream()
        log.debug(
            "AudioRecorder: stream started (device=%s, rate=%d)",
            self._device_index,
            self._sample_rate,
        )

    def stop(self) -> None:
        """Stop capturing and signal the async generator to end."""
        if not self._active:
            return
        self._active = False
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        # Send sentinel so stream() yields all captured audio before exiting
        self._q.put(None)
        log.debug("AudioRecorder: stream stopped")

    async def stream(self) -> AsyncGenerator[bytes, None]:
        """
        Async generator that yields raw PCM byte chunks.
        Starts recording automatically; stops when stop() is called.
        """
        self.start()
        loop = asyncio.get_running_loop()
        while True:
            chunk = await loop.run_in_executor(None, self._q.get)
            if chunk is None:
                break
            yield chunk

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _callback(
        self,
        in_data: bytes,
        _frame_count: int,
        _time_info: dict,
        _status_flags: int,
    ) -> tuple[None, int]:
        if self._active:
            try:
                self._q.put_nowait(in_data)
            except queue.Full:
                # Ring buffer: discard oldest frame to make room without spamming warnings
                try:
                    self._q.get_nowait()
                    self._q.put_nowait(in_data)
                except Exception:
                    pass
            return (None, pyaudio.paContinue)
        return (None, pyaudio.paComplete)

    # ------------------------------------------------------------------ #
    # Utility
    # ------------------------------------------------------------------ #

    @staticmethod
    def list_devices() -> list[dict]:
        """Return all available audio input devices."""
        old = _suppress_alsa_noise()
        try:
            pa = pyaudio.PyAudio()
        finally:
            _restore_stderr(old)
        devices = []
        try:
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if info["maxInputChannels"] > 0:
                    devices.append(
                        {"index": i, "name": info["name"], "rate": int(info["defaultSampleRate"])}
                    )
        finally:
            pa.terminate()
        return devices

    @staticmethod
    def pcm_to_float(pcm_bytes: bytes) -> np.ndarray:
        """Convert raw int16 PCM bytes to float32 numpy array in [-1, 1]."""
        return np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
