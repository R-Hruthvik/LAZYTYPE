"""Tests for audio recording and VAD modules."""

from __future__ import annotations

import asyncio
import queue
from unittest.mock import MagicMock, patch
import pytest

from voiceflow.audio.vad import VAD, SilenceDetector
from voiceflow.audio.recorder import AudioRecorder, SAMPLE_RATE, CHUNK_DURATION_MS


class TestVAD:
    def test_init_validation(self):
        with pytest.raises(AssertionError, match="aggressiveness must be 0-3"):
            VAD(aggressiveness=4)

        with pytest.raises(AssertionError, match="frame_duration_ms must be one of"):
            VAD(frame_duration_ms=45)

    def test_is_speech_invalid_size(self):
        vad = VAD(aggressiveness=2, sample_rate=16_000, frame_duration_ms=30)
        # Expected size is 16000 * 30 / 1000 * 2 = 960 bytes
        assert not vad.is_speech(b"\x00" * 500)
        assert not vad.is_speech(b"\x00" * 1000)

    def test_is_speech_mocked(self):
        vad = VAD(aggressiveness=2, sample_rate=16_000, frame_duration_ms=30)
        frame = b"\x00" * 960

        with patch.object(vad._vad, "is_speech", return_value=True):
            assert vad.is_speech(frame) is True

        with patch.object(vad._vad, "is_speech", return_value=False):
            assert vad.is_speech(frame) is False

    def test_is_speech_fail_open_on_exception(self):
        vad = VAD(aggressiveness=2, sample_rate=16_000, frame_duration_ms=30)
        frame = b"\x00" * 960

        with patch.object(vad._vad, "is_speech", side_effect=RuntimeError("VAD failed")):
            # Should fail-open and return True
            assert vad.is_speech(frame) is True


class TestSilenceDetector:
    def test_hysteresis_state_transitions(self):
        mock_vad = MagicMock(spec=VAD)
        # speech_start=2, silence_end=3
        detector = SilenceDetector(mock_vad, speech_start_frames=2, silence_end_frames=3)

        assert not detector.is_speaking
        frame = b"\x00" * 960

        # Frame 1: speech detected (1 of 2 needed)
        mock_vad.is_speech.return_value = True
        frame_speech, changed = detector.process(frame)
        assert frame_speech is True
        assert changed is False
        assert not detector.is_speaking

        # Frame 2: speech detected (2 of 2) -> speaking starts!
        frame_speech, changed = detector.process(frame)
        assert frame_speech is True
        assert changed is True
        assert detector.is_speaking

        # Frame 3: speech continues
        frame_speech, changed = detector.process(frame)
        assert frame_speech is True
        assert changed is False
        assert detector.is_speaking

        # Frame 4: silence 1 of 3
        mock_vad.is_speech.return_value = False
        frame_speech, changed = detector.process(frame)
        assert frame_speech is False
        assert changed is False
        assert detector.is_speaking  # Still speaking due to hysteresis

        # Frame 5: silence 2 of 3
        frame_speech, changed = detector.process(frame)
        assert frame_speech is False
        assert changed is False
        assert detector.is_speaking

        # Frame 6: silence 3 of 3 -> silence reached!
        frame_speech, changed = detector.process(frame)
        assert frame_speech is False
        assert changed is True
        assert not detector.is_speaking


class TestAudioRecorder:
    def test_open_and_close_lifecycle(self):
        recorder = AudioRecorder()
        assert recorder._pa is None

        with patch("pyaudio.PyAudio") as mock_pa_cls:
            mock_pa = MagicMock()
            mock_pa_cls.return_value = mock_pa

            # open()
            rec = recorder.open()
            assert rec is recorder
            assert recorder._pa is mock_pa

            # Calling open() again is idempotent
            recorder.open()
            assert mock_pa_cls.call_count == 1

            # close()
            recorder.close()
            mock_pa.terminate.assert_called_once()
            assert recorder._pa is None

    def test_context_managers(self):
        with patch("pyaudio.PyAudio") as mock_pa_cls:
            mock_pa = MagicMock()
            mock_pa_cls.return_value = mock_pa

            with AudioRecorder() as rec:
                assert rec._pa is not None
            mock_pa.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_context_manager(self):
        with patch("pyaudio.PyAudio") as mock_pa_cls:
            mock_pa = MagicMock()
            mock_pa_cls.return_value = mock_pa

            async with AudioRecorder() as rec:
                assert rec._pa is not None
            mock_pa.terminate.assert_called_once()

    def test_start_and_stop(self):
        recorder = AudioRecorder()
        recorder._pa = MagicMock()
        mock_stream = MagicMock()
        recorder._pa.open.return_value = mock_stream

        recorder.start()
        assert recorder._active
        mock_stream.start_stream.assert_called_once()

        # Calling start again does nothing
        recorder.start()
        assert mock_stream.start_stream.call_count == 1

        # Stop closes stream and puts sentinel
        recorder.stop()
        assert not recorder._active
        mock_stream.stop_stream.assert_called_once()
        mock_stream.close.assert_called_once()
        assert recorder._stream is None
        # Sentinel placed in queue
        assert recorder._q.get_nowait() is None

    def test_stop_preserves_unconsumed_audio(self):
        recorder = AudioRecorder()
        recorder._pa = MagicMock()
        recorder.start()

        # Add 3 audio chunks
        recorder._q.put_nowait(b"chunk1")
        recorder._q.put_nowait(b"chunk2")
        recorder._q.put_nowait(b"chunk3")

        recorder.stop()

        # All chunks must be preserved in order followed by None sentinel
        assert recorder._q.get_nowait() == b"chunk1"
        assert recorder._q.get_nowait() == b"chunk2"
        assert recorder._q.get_nowait() == b"chunk3"
        assert recorder._q.get_nowait() is None

    def test_callback_enqueues_and_handles_overflow(self):
        recorder = AudioRecorder()
        recorder._active = True
        recorder._q = queue.Queue(maxsize=2)

        recorder._callback(b"frame1", 480, {}, 0)
        recorder._callback(b"frame2", 480, {}, 0)
        assert recorder._q.qsize() == 2

        # Ring buffer drops oldest (frame1) and keeps frame2, frame3
        recorder._callback(b"frame3", 480, {}, 0)
        assert recorder._q.qsize() == 2
        assert recorder._q.get_nowait() == b"frame2"
        assert recorder._q.get_nowait() == b"frame3"

    @pytest.mark.asyncio
    async def test_stream_generator(self):
        recorder = AudioRecorder()
        recorder._pa = MagicMock()
        recorder._pa.open.return_value = MagicMock()

        async def push_frames():
            await asyncio.sleep(0.01)
            recorder._q.put_nowait(b"chunk_a")
            recorder._q.put_nowait(b"chunk_b")
            recorder.stop()

        asyncio.create_task(push_frames())

        collected = []
        async for chunk in recorder.stream():
            collected.append(chunk)

        assert collected == [b"chunk_a", b"chunk_b"]

    def test_list_devices(self):
        mock_pa = MagicMock()
        mock_pa.get_device_count.return_value = 2
        mock_pa.get_device_info_by_index.side_effect = [
            {"index": 0, "name": "Mic", "maxInputChannels": 2, "defaultSampleRate": 44100.0},
            {"index": 1, "name": "Speaker", "maxInputChannels": 0, "defaultSampleRate": 48000.0},
        ]

        with patch("pyaudio.PyAudio", return_value=mock_pa):
            devices = AudioRecorder.list_devices()
            assert len(devices) == 1
            assert devices[0]["name"] == "Mic"
