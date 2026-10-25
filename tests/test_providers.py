"""Tests for Groq provider (mocked — no real API calls)."""

from __future__ import annotations

import io
import wave
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_pcm(duration_s: float = 0.5, sample_rate: int = 16_000) -> bytes:
    """Generate silent PCM audio for testing."""
    return b"\x00" * int(sample_rate * 2 * duration_s)


class TestGroqProvider:
    @pytest.fixture(autouse=True)
    def set_env(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk_test_fake_key_for_unit_tests")

    def test_init_reads_env_key(self):
        from voiceflow.providers.groq_provider import GroqProvider

        provider = GroqProvider()
        assert provider.name == "groq"

    def test_init_fails_without_key(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        from voiceflow.providers.groq_provider import GroqProvider
        from voiceflow.providers.base import ProviderAuthError

        with pytest.raises(ProviderAuthError):
            GroqProvider()

    @pytest.mark.asyncio
    async def test_transcribe_calls_groq_api(self, monkeypatch):
        from voiceflow.providers.groq_provider import GroqProvider

        mock_response = "hello world"
        mock_client = AsyncMock()
        mock_client.audio.transcriptions.create = AsyncMock(return_value=mock_response)

        with patch("voiceflow.providers.groq_provider.AsyncGroq", return_value=mock_client):
            provider = GroqProvider()
            result = await provider.transcribe(_make_pcm(), language="en")

        assert result == "hello world"
        mock_client.audio.transcriptions.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limit_raises_provider_error(self):
        from voiceflow.providers.groq_provider import GroqProvider
        from voiceflow.providers.base import ProviderRateLimitError
        from groq import RateLimitError

        mock_client = AsyncMock()
        mock_client.audio.transcriptions.create.side_effect = RateLimitError(
            message="rate limited", response=MagicMock(headers={}), body={}
        )

        with patch("voiceflow.providers.groq_provider.AsyncGroq", return_value=mock_client):
            provider = GroqProvider()
            with pytest.raises(ProviderRateLimitError):
                await provider.transcribe(_make_pcm())

    def test_parse_retry_after_formats(self):
        from voiceflow.providers.groq_provider import _parse_retry_after
        from groq import RateLimitError

        # 1. Compound duration string with trailing punctuation
        exc1 = RateLimitError(
            message="Rate limit reached. Please try again in 1m23.456s.",
            response=MagicMock(headers={}),
            body={},
        )
        assert _parse_retry_after(exc1) == 84  # 1*60 + 23.456 = 83.456 -> 84

        # 2. Simple seconds with decimal
        exc2 = RateLimitError(
            message="Rate limit reached. Please try again in 5.2s.",
            response=MagicMock(headers={}),
            body={},
        )
        assert _parse_retry_after(exc2) == 6  # 5.2 -> 6

        # 3. HTTP Header
        resp = MagicMock()
        resp.headers = {"retry-after": "45"}
        exc3 = RateLimitError(
            message="Rate limit reached",
            response=resp,
            body={},
        )
        assert _parse_retry_after(exc3) == 45


class TestDeepgramProvider:
    @pytest.mark.asyncio
    async def test_transcribe_wraps_wav_and_parses(self, monkeypatch):
        monkeypatch.setenv("DEEPGRAM_API_KEY", "fake_deepgram_key")
        from voiceflow.providers.deepgram_provider import DeepgramProvider

        mock_response = MagicMock()
        mock_alt = MagicMock()
        mock_alt.transcript = "testing deepgram"
        mock_channel = MagicMock()
        mock_channel.alternatives = [mock_alt]
        mock_response.results.channels = [mock_channel]

        mock_client = MagicMock()
        mock_client.listen.v1.media.transcribe_file = AsyncMock(return_value=mock_response)

        with patch(
            "voiceflow.providers.deepgram_provider.AsyncDeepgramClient", return_value=mock_client
        ):
            provider = DeepgramProvider()
            res = await provider.transcribe(_make_pcm())
            assert res == "testing deepgram"

            call_kwargs = mock_client.listen.v1.media.transcribe_file.call_args[1]
            assert call_kwargs["request"][:4] == b"RIFF"


class TestAssemblyAIProvider:
    @pytest.mark.asyncio
    async def test_transcribe_passes_bytesio(self, monkeypatch):
        monkeypatch.setenv("ASSEMBLYAI_API_KEY", "fake_aai_key")
        import assemblyai as aai
        from voiceflow.providers.assemblyai_provider import AssemblyAIProvider

        mock_transcript = MagicMock()
        mock_transcript.status = aai.TranscriptStatus.completed
        mock_transcript.text = "testing assembly"

        passed_buffer = None

        def fake_transcribe(buf, config=None):
            nonlocal passed_buffer
            passed_buffer = buf
            return mock_transcript

        mock_transcriber = MagicMock()
        mock_transcriber.transcribe = fake_transcribe

        with patch(
            "voiceflow.providers.assemblyai_provider.aai.Transcriber", return_value=mock_transcriber
        ):
            provider = AssemblyAIProvider()
            res = await provider.transcribe(_make_pcm())
            assert res == "testing assembly"
            assert isinstance(passed_buffer, io.BytesIO)
            assert passed_buffer.getvalue()[:4] == b"RIFF"


class TestPcmToWav:
    def test_pcm_to_wav_generates_valid_riff_header(self):
        from voiceflow.providers.base import pcm_to_wav

        pcm_data = b"\x01\x02\x03\x04" * 100
        wav_bytes = pcm_to_wav(pcm_data, sample_rate=16_000)

        assert wav_bytes[:4] == b"RIFF"
        assert wav_bytes[8:12] == b"WAVE"

        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 16_000
            read_pcm = wf.readframes(wf.getnframes())
            assert read_pcm == pcm_data


class TestNvidiaProvider:
    def test_init_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        from voiceflow.providers.nvidia_provider import NvidiaProvider
        from voiceflow.providers.base import ProviderAuthError

        with pytest.raises(ProviderAuthError):
            NvidiaProvider()

    @pytest.mark.asyncio
    async def test_transcribe_success(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nv_fake_key")
        from voiceflow.providers.nvidia_provider import NvidiaProvider

        mock_client = AsyncMock()
        mock_client.audio.transcriptions.create.return_value = "nvidia transcription result"

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            provider = NvidiaProvider()
            text = await provider.transcribe(_make_pcm())

            assert text == "nvidia transcription result"
            mock_client.audio.transcriptions.create.assert_called_once()
            call_kwargs = mock_client.audio.transcriptions.create.call_args[1]
            assert call_kwargs["file"][0] == "audio.wav"
            assert call_kwargs["file"][1][:4] == b"RIFF"

    @pytest.mark.asyncio
    async def test_transcribe_error_mappings(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nv_fake_key")
        import openai
        from voiceflow.providers.nvidia_provider import NvidiaProvider
        from voiceflow.providers.base import (
            ProviderRateLimitError,
            ProviderAuthError,
            ProviderUnavailableError,
        )

        mock_client = AsyncMock()
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            provider = NvidiaProvider()

            # Rate limit
            mock_client.audio.transcriptions.create.side_effect = openai.RateLimitError(
                message="rate limit", response=MagicMock(), body={}
            )
            with pytest.raises(ProviderRateLimitError):
                await provider.transcribe(_make_pcm())

            # Auth error
            mock_client.audio.transcriptions.create.side_effect = openai.AuthenticationError(
                message="invalid key", response=MagicMock(), body={}
            )
            with pytest.raises(ProviderAuthError):
                await provider.transcribe(_make_pcm())

            # Connection error
            mock_client.audio.transcriptions.create.side_effect = openai.APIConnectionError(
                request=MagicMock()
            )
            with pytest.raises(ProviderUnavailableError):
                await provider.transcribe(_make_pcm())


class TestLocalProvider:
    def test_init_defaults(self):
        from voiceflow.providers.local_provider import LocalProvider

        provider = LocalProvider(model_size="tiny.en", device="cpu", compute_type="int8")
        assert provider.name == "local"
        assert provider._model is None

    @pytest.mark.asyncio
    async def test_health_check(self):
        from voiceflow.providers.local_provider import LocalProvider

        provider = LocalProvider()
        assert await provider.health_check() is True

    @pytest.mark.asyncio
    async def test_transcribe_invokes_model(self):
        from voiceflow.providers.local_provider import LocalProvider

        mock_model = MagicMock()
        mock_seg1 = MagicMock()
        mock_seg1.text = "Hello"
        mock_seg2 = MagicMock()
        mock_seg2.text = " world"
        mock_info = MagicMock()
        mock_info.language = "en"
        mock_info.language_probability = 0.99
        mock_model.transcribe.return_value = ([mock_seg1, mock_seg2], mock_info)

        provider = LocalProvider()
        provider._model = mock_model  # bypass _load_model

        text = await provider.transcribe(_make_pcm())
        assert text == "Hello world"
