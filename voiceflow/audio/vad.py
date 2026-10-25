"""
Voice Activity Detection using webrtcvad.

Filters silent 30ms frames before sending to cloud STT, saving API quota
and reducing latency by not transcribing silence.
"""

from __future__ import annotations

import logging
from collections import deque

import webrtcvad

log = logging.getLogger(__name__)

# webrtcvad only supports these exact frame durations
VALID_FRAME_MS = {10, 20, 30}


class VAD:
    """
    Wraps webrtcvad to classify 30ms PCM frames as speech or silence.

    Args:
        aggressiveness: 0-3. Higher = more aggressive silence filtering.
            0 = least aggressive (keeps more frames as "speech")
            3 = most aggressive (only confident speech passes)
        sample_rate: Must be 8000, 16000, 32000, or 48000 Hz.
        frame_duration_ms: Must be 10, 20, or 30 ms.
    """

    def __init__(
        self,
        aggressiveness: int = 2,
        sample_rate: int = 16_000,
        frame_duration_ms: int = 30,
    ) -> None:
        assert aggressiveness in range(4), "aggressiveness must be 0-3"
        assert (
            frame_duration_ms in VALID_FRAME_MS
        ), f"frame_duration_ms must be one of {VALID_FRAME_MS}"
        self._vad = webrtcvad.Vad(aggressiveness)
        self._sample_rate = sample_rate
        self._frame_duration_ms = frame_duration_ms
        self._expected_bytes = (
            int(sample_rate * frame_duration_ms / 1000) * 2
        )  # int16 = 2 bytes/sample

    def is_speech(self, frame: bytes) -> bool:
        """Return True if the frame contains speech."""
        if len(frame) != self._expected_bytes:
            log.debug(
                "VAD: skipping frame with unexpected size %d (expected %d)",
                len(frame),
                self._expected_bytes,
            )
            return False
        try:
            return self._vad.is_speech(frame, self._sample_rate)
        except Exception as exc:  # pylint: disable=broad-except
            log.warning("VAD error: %s", exc)
            return True  # fail-open: pass frame through if VAD errors


class SilenceDetector:
    """
    Tracks a sliding window of speech/silence frames to decide:
      - When speech has started (enough consecutive speech frames)
      - When speech has ended (enough consecutive silent frames)

    Provides hysteresis to avoid choppy start/stop decisions.
    """

    def __init__(
        self,
        vad: VAD,
        speech_start_frames: int = 3,  # N consecutive speech frames → start
        silence_end_frames: int = 50,  # N consecutive silent frames → end (~1.5s at 30ms)
    ) -> None:
        self._vad = vad
        self._speech_start = speech_start_frames
        self._silence_end = silence_end_frames

        self._ring: deque[bool] = deque(maxlen=max(speech_start_frames, silence_end_frames))
        self._is_speaking = False

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking

    def process(self, frame: bytes) -> tuple[bool, bool]:
        """
        Feed one PCM frame. Returns (is_speech_now, state_changed).

        state_changed=True means speaking started or ended this frame.
        """
        speech = self._vad.is_speech(frame)
        self._ring.append(speech)
        prev = self._is_speaking

        if not self._is_speaking:
            # Start speaking if last N frames were all speech
            recent = list(self._ring)[-self._speech_start :]
            if len(recent) == self._speech_start and all(recent):
                self._is_speaking = True
        else:
            # Stop speaking if last N frames were all silent
            recent = list(self._ring)[-self._silence_end :]
            if len(recent) == self._silence_end and not any(recent):
                self._is_speaking = False

        return speech, self._is_speaking != prev

    def reset(self) -> None:
        self._ring.clear()
        self._is_speaking = False
