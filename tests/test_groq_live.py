"""Integration test: live Groq API transcription test.

Run with: pytest tests/test_groq_live.py -v -s
Requires GROQ_API_KEY set in environment.
Skipped automatically if key is not present.
"""

from __future__ import annotations

import io
import math
import os
import struct
import wave

import pytest

# Skip entire module if no key
pytestmark = pytest.mark.skipif(
    not os.environ.get("GROQ_API_KEY"),
    reason="GROQ_API_KEY not set — skipping live API test",
)


def _generate_sine_wav(
    frequency: float = 440.0, duration_s: float = 1.0, sample_rate: int = 16_000
) -> bytes:
    """Generate a WAV with a 440Hz sine tone (not real speech, but tests the API pipeline)."""
    n_samples = int(sample_rate * duration_s)
    amplitude = 32767 * 0.3
    samples = [
        int(amplitude * math.sin(2 * math.pi * frequency * i / sample_rate))
        for i in range(n_samples)
    ]
    pcm = struct.pack(f"<{n_samples}h", *samples)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_groq_health_check():
    """Verify Groq API key is valid."""
    from voiceflow.providers.groq_provider import GroqProvider

    provider = GroqProvider()
    healthy = await provider.health_check()
    assert healthy, "Groq health check failed — check GROQ_API_KEY"


@pytest.mark.asyncio
async def test_groq_transcribe_tone():
    """Send a 1-second sine wave to Groq — should return empty or minimal text (no speech)."""
    from voiceflow.providers.groq_provider import GroqProvider
    import struct

    # Generate 1s of silence (not a sine wave — silence produces empty transcript)
    pcm = b"\x00" * 16_000 * 2  # 1s at 16kHz int16

    provider = GroqProvider()
    result = await provider.transcribe(pcm, language="en")
    # Silence → either empty string or minimal hallucination
    assert isinstance(result, str), "Expected string result from Groq"
    print(f"\n  Groq silence transcript: {result!r}")


@pytest.mark.asyncio
async def test_provider_chain_groq_only():
    """ProviderChain with only Groq — successful transcription of silence."""
    from voiceflow.providers.groq_provider import GroqProvider
    from voiceflow.chain import ProviderChain

    provider = GroqProvider()
    chain = ProviderChain([provider])
    pcm = b"\x00" * 16_000 * 2  # 1s silence
    result = await chain.transcribe(pcm)
    assert isinstance(result, str)
    print(f"\n  Chain result: {result!r}")
