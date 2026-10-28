"""
VoiceFlow configuration loader.

API keys are NEVER stored in config files — they are read exclusively
from environment variables (set in ~/.bashrc or shell profile).

Config file (~/.config/voiceflow/config.toml) only contains non-secret
settings: hotkey, language, provider priority order, VAD aggressiveness, etc.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore
    except ImportError:
        import tomli as tomllib  # type: ignore

import tomli_w

CONFIG_DIR = Path.home() / ".config" / "voiceflow"
CONFIG_FILE = CONFIG_DIR / "config.toml"

DEFAULT_CONFIG: dict = {
    "general": {
        "hotkey": "KEY_RIGHTCTRL",  # evdev key name — hold to record
        "mode": "push_to_talk",  # push_to_talk | toggle
        "language": "en",
        "inject_method": "auto",  # auto | xdotool | ydotool | clipboard
        "osd_enabled": True,
        "tray_enabled": True,
        "notification_mode": "minimal",  # "errors_only" | "minimal" | "verbose"
    },
    "providers": {
        # Priority order — first available wins; falls back down the list.
        # API keys come from env vars, NOT from this file.
        "priority": ["groq", "nvidia", "deepgram", "assemblyai", "local"],
        # Env var names — VoiceFlow reads os.environ[env_var] at runtime.
        "env_vars": {
            "groq": "GROQ_API_KEY",
            "nvidia": "NVIDIA_API_KEY",
            "deepgram": "DEEPGRAM_API_KEY",
            "assemblyai": "ASSEMBLYAI_API_KEY",
        },
        "local_model": "base.en",  # tiny.en | base.en | small.en | medium.en
        # How many consecutive errors before a provider is cooled down (seconds)
        "cooldown_after_errors": 3,
        "cooldown_seconds": 60,
    },
    "vad": {
        "enabled": True,
        "aggressiveness": 2,  # 0-3: 3 = most aggressive silence removal
    },
    "audio": {
        "sample_rate": 16000,
        "chunk_duration_ms": 30,  # VAD frame size (10 | 20 | 30 ms)
        "device_index": 0,  # 0 = system default mic
    },
    "streaming": {
        "max_window_seconds": 300,  # Max recording length per hotkey press (5 min)
        "silence_timeout_ms": 2000,  # Auto-stop after N ms of silence (in toggle mode)
        "auto_sleep_seconds": 120,  # Auto-sleep after N seconds of inactivity
    },
    "osd": {
        "position": "bottom-center",  # top-left | bottom-center | etc.
        "dismiss_after_ms": 2500,
        "show_errors": False,
    },
}


@dataclass
class Config:
    """Flat accessor over the config dict — avoids dict["key"]["key"] everywhere."""

    # General
    hotkey: str = "KEY_RIGHTCTRL"
    mode: str = "push_to_talk"
    language: str = "en"
    inject_method: str = "auto"
    osd_enabled: bool = True
    tray_enabled: bool = True
    notification_mode: str = "minimal"

    # Providers
    provider_priority: list[str] = field(
        default_factory=lambda: ["groq", "nvidia", "deepgram", "assemblyai", "local"]
    )
    provider_env_vars: dict[str, str] = field(
        default_factory=lambda: {
            "groq": "GROQ_API_KEY",
            "nvidia": "NVIDIA_API_KEY",
            "deepgram": "DEEPGRAM_API_KEY",
            "assemblyai": "ASSEMBLYAI_API_KEY",
        }
    )
    local_model: str = "base.en"
    cooldown_after_errors: int = 3
    cooldown_seconds: int = 60

    # VAD
    vad_enabled: bool = True
    vad_aggressiveness: int = 2

    # Audio
    sample_rate: int = 16000
    chunk_duration_ms: int = 30
    device_index: int | None = 0

    # Streaming
    max_window_seconds: int = 300
    silence_timeout_ms: int = 2000
    auto_sleep_seconds: int = 120

    # OSD
    osd_position: str = "bottom-center"
    osd_dismiss_after_ms: int = 2500
    osd_show_errors: bool = False

    def get_api_key(self, provider: str) -> str | None:
        """Read provider API key from environment (never from disk)."""
        env_var = self.provider_env_vars.get(provider)
        if not env_var:
            return None
        return os.environ.get(env_var)

    def available_providers(self) -> list[str]:
        """Return providers that have an API key set OR are local (no key needed)."""
        available = []
        for p in self.provider_priority:
            if p == "local":
                available.append(p)
            elif self.get_api_key(p):
                available.append(p)
        return available


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (base wins for missing keys)."""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_env_keys() -> None:
    """
    Ensure API keys from ~/.bashrc, ~/.profile, or ~/.config/voiceflow/env
    are loaded into os.environ even when VoiceFlow runs non-interactively
    (e.g., via systemd or GNOME autostart).
    """
    import re

    candidates = [
        Path.home() / ".bashrc",
        Path.home() / ".profile",
        CONFIG_DIR / "env",
    ]

    target_keys = {
        "GROQ_API_KEY",
        "GROQ_API_KEYS",
        "NVIDIA_API_KEY",
        "DEEPGRAM_API_KEY",
        "ASSEMBLYAI_API_KEY",
    }

    pattern = re.compile(r'^\s*export\s+([A-Za-z0-9_]+)\s*=\s*["\']?(.*?)["\']?\s*$')

    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            with open(candidate, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    m = pattern.match(line)
                    if m:
                        k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
                        if (k in target_keys or k.startswith("GROQ_API_KEY_")) and v:
                            if not os.environ.get(k):
                                os.environ[k] = v
        except Exception:
            pass


def load_config() -> Config:
    """Load config from disk, merging with defaults. Create file if missing."""
    load_env_keys()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_FILE.exists():
        _write_default_config()
        raw = DEFAULT_CONFIG
    else:
        with open(CONFIG_FILE, "rb") as f:
            raw = _deep_merge(DEFAULT_CONFIG, tomllib.load(f))

    g = raw["general"]
    p = raw["providers"]
    v = raw["vad"]
    a = raw["audio"]
    s = raw["streaming"]
    o = raw["osd"]

    return Config(
        hotkey=g["hotkey"],
        mode=g["mode"],
        language=g["language"],
        inject_method=g["inject_method"],
        osd_enabled=g["osd_enabled"],
        tray_enabled=g["tray_enabled"],
        notification_mode=g.get("notification_mode", "minimal"),
        provider_priority=p["priority"],
        provider_env_vars=p["env_vars"],
        local_model=p["local_model"],
        cooldown_after_errors=p["cooldown_after_errors"],
        cooldown_seconds=p["cooldown_seconds"],
        vad_enabled=v["enabled"],
        vad_aggressiveness=v["aggressiveness"],
        sample_rate=a["sample_rate"],
        chunk_duration_ms=a["chunk_duration_ms"],
        device_index=a["device_index"] or None,  # 0 in TOML means system default
        max_window_seconds=s["max_window_seconds"],
        silence_timeout_ms=s["silence_timeout_ms"],
        auto_sleep_seconds=int(s.get("auto_sleep_seconds", 120)),
        osd_position=o["position"],
        osd_dismiss_after_ms=o["dismiss_after_ms"],
        osd_show_errors=bool(o.get("show_errors", False)),
    )


def _write_default_config() -> None:
    header = (
        "# VoiceFlow configuration\n"
        "# API keys are NOT stored here — set them in ~/.bashrc:\n"
        '#   export GROQ_API_KEY="gsk_..."\n'
        '#   export NVIDIA_API_KEY="nvapi-..."\n'
        '#   export DEEPGRAM_API_KEY="..."\n'
        '#   export ASSEMBLYAI_API_KEY="..."\n\n'
    )
    with open(CONFIG_FILE, "wb") as f:
        f.write(header.encode())
        f.write(tomli_w.dumps(DEFAULT_CONFIG).encode())
