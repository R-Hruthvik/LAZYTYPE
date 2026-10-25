"""Tests for ProviderChain fallback logic."""

from __future__ import annotations

import asyncio
import pytest

from voiceflow.providers.base import (
    STTProvider,
    ProviderRateLimitError,
    ProviderUnavailableError,
    ProviderAuthError,
)
from voiceflow.chain import ProviderChain, ProviderStatus


class _FakeProvider(STTProvider):
    def __init__(self, name, response="ok", raises=None):
        self.name = name
        self._response = response
        self._raises = raises
        self.call_count = 0

    async def transcribe(self, audio_bytes, language="en"):
        self.call_count += 1
        if self._raises:
            raise self._raises
        return self._response

    async def health_check(self):
        return self._raises is None


class TestProviderChain:
    @pytest.mark.asyncio
    async def test_uses_first_provider(self):
        p1 = _FakeProvider("p1", "hello")
        p2 = _FakeProvider("p2", "world")
        chain = ProviderChain([p1, p2])
        result = await chain.transcribe(b"\x00" * 100)
        assert result == "hello"
        assert p1.call_count == 1
        assert p2.call_count == 0

    @pytest.mark.asyncio
    async def test_falls_back_on_rate_limit(self):
        p1 = _FakeProvider("p1", raises=ProviderRateLimitError("p1", retry_after=60))
        p2 = _FakeProvider("p2", "fallback text")
        chain = ProviderChain([p1, p2])
        result = await chain.transcribe(b"\x00" * 100)
        assert result == "fallback text"
        assert p1.call_count == 1
        assert p2.call_count == 1

    @pytest.mark.asyncio
    async def test_disables_on_auth_error(self):
        p1 = _FakeProvider("p1", raises=ProviderAuthError("p1", "bad key"))
        p2 = _FakeProvider("p2", "ok")
        chain = ProviderChain([p1, p2])
        result = await chain.transcribe(b"\x00" * 100)
        assert result == "ok"
        # p1 should be permanently disabled now
        state = next(s for s in chain._states if s.name == "p1")
        assert state.status == ProviderStatus.DISABLED

    @pytest.mark.asyncio
    async def test_raises_when_all_fail(self):
        p1 = _FakeProvider("p1", raises=ProviderUnavailableError("p1", "down"))
        p2 = _FakeProvider("p2", raises=ProviderUnavailableError("p2", "down"))
        chain = ProviderChain([p1, p2], cooldown_after_errors=1, default_cooldown_seconds=60)
        with pytest.raises(RuntimeError, match="All providers failed"):
            await chain.transcribe(b"\x00" * 100)

    @pytest.mark.asyncio
    async def test_status_table(self):
        p1 = _FakeProvider("groq", "ok")
        p2 = _FakeProvider("local", "fallback")
        chain = ProviderChain([p1, p2])
        await chain.transcribe(b"\x00" * 100)
        table = chain.status_table()
        assert len(table) == 2
        assert table[0]["provider"] == "groq"
        assert table[0]["requests"] == 1

    @pytest.mark.asyncio
    async def test_unexpected_exception_triggers_cooldown(self):
        p1 = _FakeProvider("p1", raises=KeyError("unexpected crash"))
        p2 = _FakeProvider("p2", "fallback")
        chain = ProviderChain([p1, p2], cooldown_after_errors=2, default_cooldown_seconds=60)
        res1 = await chain.transcribe(b"\x00" * 100)
        assert res1 == "fallback"
        state1 = next(s for s in chain._states if s.name == "p1")
        assert state1.error_count == 1

        res2 = await chain.transcribe(b"\x00" * 100)
        assert res2 == "fallback"
        assert state1.status == ProviderStatus.COOLING_DOWN
