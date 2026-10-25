"""Unit tests for Groq multi-key pooling and round-robin failover."""

import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from voiceflow.providers.groq_provider import GroqProvider, GroqKeyEntry, _discover_groq_keys
from voiceflow.providers.base import ProviderRateLimitError


def test_discover_multiple_keys(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEYS", "gsk_key1, gsk_key2")
    monkeypatch.setenv("GROQ_API_KEY_1", "gsk_key3")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_key4")

    keys = _discover_groq_keys()
    assert keys == ["gsk_key1", "gsk_key2", "gsk_key3", "gsk_key4"]


@pytest.mark.asyncio
async def test_groq_round_robin_rotation(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEYS", "gsk_alpha,gsk_beta")

    provider = GroqProvider()
    assert provider.key_count == 2

    # Mock both clients
    for entry in provider._entries:
        setattr(
            entry.client.audio.transcriptions, "create", AsyncMock(return_value="transcribed text")
        )

    # Call 1 -> uses first key
    res1 = await provider.transcribe(b"\x00" * 3200)
    assert res1 == "transcribed text"
    assert provider._entries[0].total_requests == 1
    assert provider._entries[1].total_requests == 0

    # Call 2 -> round-robins to second key!
    res2 = await provider.transcribe(b"\x00" * 3200)
    assert res2 == "transcribed text"
    assert provider._entries[0].total_requests == 1
    assert provider._entries[1].total_requests == 1


@pytest.mark.asyncio
async def test_groq_failover_on_rate_limit(monkeypatch):
    from groq import RateLimitError

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEYS", "gsk_key_ratelimited,gsk_key_backup")
    provider = GroqProvider()

    # Make first client fail with RateLimitError
    fake_response = MagicMock()
    fake_response.status_code = 429
    fake_response.headers = {}
    err = RateLimitError(message="rate limit exceeded", response=fake_response, body=None)

    setattr(provider._entries[0].client.audio.transcriptions, "create", AsyncMock(side_effect=err))
    # Second client succeeds
    setattr(
        provider._entries[1].client.audio.transcriptions,
        "create",
        AsyncMock(return_value="backup success"),
    )

    res = await provider.transcribe(b"\x00" * 3200)
    assert res == "backup success"
    # First key should be in cooldown
    assert provider._entries[0].cooldown_until > time.monotonic()
    assert provider._entries[1].total_requests == 1
