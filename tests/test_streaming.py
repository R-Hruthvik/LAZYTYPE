"""Tests for the streaming tumbling-window engine."""

from __future__ import annotations

import asyncio
import pytest

from voiceflow.streaming import TumblingWindow, StreamingSession


class TestTumblingWindow:
    def test_empty_on_init(self):
        w = TumblingWindow()
        assert w.is_empty
        assert w.duration_s == 0.0

    def test_add_and_flush(self):
        w = TumblingWindow(sample_rate=16_000)
        chunk = b"\x00\x01" * 480  # 30ms at 16kHz
        w.add_chunk(chunk)
        assert not w.is_empty
        data = w.flush()
        assert data == chunk
        assert w.is_empty  # cleared after flush

    def test_snapshot_does_not_clear(self):
        w = TumblingWindow()
        chunk = b"\x00" * 960
        w.add_chunk(chunk)
        snap = w.snapshot()
        assert snap == chunk
        assert not w.is_empty  # still there

    def test_duration_calculation(self):
        w = TumblingWindow(sample_rate=16_000)
        # 16000 samples/sec * 2 bytes/sample = 32000 bytes/sec
        chunk = b"\x00" * 32_000  # exactly 1 second
        w.add_chunk(chunk)
        assert abs(w.duration_s - 1.0) < 0.01

    def test_clear(self):
        w = TumblingWindow()
        w.add_chunk(b"\xff" * 100)
        w.clear()
        assert w.is_empty


class TestStreamingSession:
    @pytest.mark.asyncio
    async def test_short_audio_skipped(self):
        """Audio shorter than min_audio_bytes should produce empty transcript."""
        transcripts = []

        class FakeChain:
            def active_provider(self):
                return "fake"

            async def transcribe(self, audio, lang):
                return "hello"

        session = StreamingSession(
            chain=FakeChain(),
            on_transcript=lambda t, f, _m: transcripts.append((t, f)),
            min_audio_bytes=10_000,  # 10KB minimum
        )
        await session.start()
        session.add_chunk(b"\x00" * 100)  # way too short
        result = await session.stop()

        assert result == ""
        assert transcripts == [("", True)]

    @pytest.mark.asyncio
    async def test_full_session_calls_chain(self):
        """A properly-sized audio clip should be sent to the chain."""
        transcripts = []

        class FakeChain:
            def active_provider(self):
                return "groq"

            async def transcribe(self, audio, lang):
                assert len(audio) > 0
                return "test transcript"

        session = StreamingSession(
            chain=FakeChain(),
            on_transcript=lambda t, f, _m: transcripts.append((t, f)),
            min_audio_bytes=0,  # no minimum for test
        )
        await session.start()
        session.add_chunk(b"\x00" * 960)  # 30ms of audio
        result = await session.stop()

        assert result == "test transcript"
        assert ("test transcript", True) in transcripts
