# LAZYTYPE

> **Ultra-low latency, system-wide speech-to-text dictation engine for Linux**  
> Hold a hotkey anywhere — speech instantly transcribes and injects into any application, terminal, browser, or editor with automatic multi-tier cloud and local fallback.

---

## Overview

**LAZYTYPE** gives you a seamless, always-on dictation experience across your entire Linux desktop without subscription paywalls or vendor lock-in. 

Press or hold your global hotkey (default: **Right Ctrl**), speak naturally, and your transcription is automatically filtered with WebRTC Voice Activity Detection (VAD) and typed directly into the active window at lightning speed.

### Highlights

- **Universal Hotkey Integration:** Works natively across X11 and Wayland (supporting GNOME Terminal, Alacritty, Kitty, VS Code, Chrome, etc.).
- **Unstoppable Multi-Tier Fallback Chain:** If your primary cloud provider hits rate limits or network issues, LAZYTYPE auto-cascades down the pipeline in milliseconds without interrupting your workflow.
- **Local Fallback Mode:** When completely offline or off-grid, falls back to a fast, local `faster-whisper` CPU engine.
- **Smart Voice Activity Detection (VAD):** Trims silence before sending audio, saving API quota and reducing latency.
- **Multiple UI Modes:**
  - **OSD Floating Bar & Tray:** Desktop floating status indicator + tray icon (Idle [Red] -> Recording [Yellow] -> Transcribing [Green]).
  - **Terminal TUI Dashboard:** Rich, real-time terminal UI (`voiceflow --tui`) for headless or minimalist setups.

---

## Provider Fallback Pipeline

When audio is captured, LAZYTYPE tries providers in prioritized order. If a provider returns a rate limit (HTTP 429) or network error, it is placed on a temporary cooldown, and LAZYTYPE immediately routes the request to the next provider down the list. If all cloud providers are unreachable, the local offline model steps in so dictation never fails.

```
+-------------------------------------------------------------------+
|                     Global Hotkey Triggered                       |
+-------------------------------------------------------------------+
                                  |
                                  v
+-------------------------------------------------------------------+
|               Capture Audio & Apply WebRTC VAD                    |
+-------------------------------------------------------------------+
                                  |
                                  v
+-------------------------------------------------------------------+
| Priority 1: Groq (whisper-large-v3-turbo)                         |
|   ~300ms latency | High accuracy                                  |
+-------------------------------------------------------------------+
          | (On 429 Rate Limit / Network Error)
          v
+-------------------------------------------------------------------+
| Priority 2: NVIDIA NIM (whisper-large-v3)                         |
|   ~400ms latency | OpenAI-compatible endpoint                     |
+-------------------------------------------------------------------+
          | (On 429 Rate Limit / Network Error)
          v
+-------------------------------------------------------------------+
| Priority 3: Deepgram (nova-3)                                     |
|   ~250ms latency | Ultra-fast cloud STT                           |
+-------------------------------------------------------------------+
          | (On 429 Rate Limit / Network Error)
          v
+-------------------------------------------------------------------+
| Priority 4: AssemblyAI (universal-2)                              |
|   ~600ms latency | Robust cloud STT                               |
+-------------------------------------------------------------------+
          | (On 429 Rate Limit / Network Error)
          v
+-------------------------------------------------------------------+
| Priority 5: Local faster-whisper (base.en)                        |
|   Always available | Runs on local CPU | 100% Offline             |
+-------------------------------------------------------------------+
                                  |
                                  v
+-------------------------------------------------------------------+
|       Inject Transcribed Text into Active Window                  |
|       (xdotool / ydotool / clipboard fallback)                    |
+-------------------------------------------------------------------+
```

### Provider Details

| Priority | Provider | Engine / Model | Typ. Latency | Availability |
|---|---|---|---|---|
| **1 (Default)** | **Groq** | `whisper-large-v3-turbo` | ~300ms | Free tier available |
| **2** | **NVIDIA NIM** | `whisper-large-v3` | ~400ms | 1,000 free credits |
| **3** | **Deepgram** | `nova-3` | ~250ms | Free credits on signup |
| **4** | **AssemblyAI** | `universal-2` | ~600ms | Free monthly hours |
| **5 (Offline)** | **Local Whisper** | `faster-whisper (base.en)` | ~800ms | Completely offline & free |

---

## Configuring Providers & API Keys

LAZYTYPE follows the security practice of **never storing API keys in configuration files**. Keys are loaded strictly from environment variables at runtime.

### Step 1: Set Environment Variables

Add your keys to your shell configuration (`~/.bashrc`, `~/.zshrc`, or `~/.profile`):

```bash
# Primary recommended provider (Free at console.groq.com)
export GROQ_API_KEY="gsk_..."

# Optional fallback provider keys:
export NVIDIA_API_KEY="nvapi-..."
export DEEPGRAM_API_KEY="..."
export ASSEMBLYAI_API_KEY="..."
```

Then reload your environment:
```bash
source ~/.bashrc
```

> **Note:** If only `GROQ_API_KEY` is provided, LAZYTYPE will use Groq and seamlessly fall back to local `faster-whisper` whenever Groq is unreachable. You do not need to register for every provider.

### Step 2: Customizing Provider Priority & Cooldowns

Configuration lives at `~/.config/voiceflow/config.toml` (copy from `config/voiceflow.toml.example` on first run).

You can reorder priorities, change models, or tune fallback cooldowns:

```toml
[providers]
# Reorder or remove providers from the chain as desired:
priority = ["groq", "nvidia", "deepgram", "assemblyai", "local"]

# Change the local offline model
# Options: tiny.en | base.en | small.en | medium.en | large-v3
local_model = "base.en"

# Provider cooldown tuning:
# Number of consecutive errors before a provider is temporarily cooled down
cooldown_after_errors = 3

# Duration (in seconds) to wait before retrying a cooled-down provider
cooldown_seconds = 60

[providers.env_vars]
# Custom environment variable names if desired
groq = "GROQ_API_KEY"
nvidia = "NVIDIA_API_KEY"
deepgram = "DEEPGRAM_API_KEY"
assemblyai = "ASSEMBLYAI_API_KEY"
```

---

## Quick Start

### 1. System Dependencies (Ubuntu / Debian)

```bash
sudo apt update
sudo apt install -y python3-pyaudio portaudio19-dev libevdev-dev xdotool ydotool wl-clipboard
```

> **Hotkey Permissions:** LAZYTYPE reads global hotkeys using `evdev`. Ensure your user belongs to the `input` group:
> ```bash
> sudo usermod -aG input $USER
> # Log out and log back in, or run: newgrp input
> ```

### 2. Installation

Clone and install using `uv` or `pip`:

```bash
git clone https://github.com/R-Hruthvik/LAZYTYPE.git
cd LAZYTYPE

# Using uv (recommended)
uv venv
source .venv/bin/activate
uv pip install -e .

# Or using standard pip
pip install -e .
```

### 3. Verify Hardware & Audio

Run a 3-second test recording to verify microphone capture and transcription:

```bash
voiceflow --test
```

### 4. Run

```bash
# Background tray + floating desktop OSD overlay:
voiceflow

# Or Terminal TUI dashboard:
voiceflow --tui
```

---

## Hotkey Usage & Settings

| Mode | Key | Behavior |
|---|---|---|
| **Push-to-talk (Default)** | Hold `Right Ctrl` | Records while held down; transcribes and injects immediately on release |
| **Toggle** | Press `Right Ctrl` | Press once to start recording; press again to stop and inject |

In `~/.config/voiceflow/config.toml`:

```toml
[general]
hotkey = "KEY_RIGHTCTRL"   # KEY_RIGHTALT, KEY_CAPSLOCK, KEY_F9, etc.
mode = "push_to_talk"      # "push_to_talk" | "toggle"
language = "en"
inject_method = "auto"     # "auto" | "xdotool" | "ydotool" | "clipboard"
osd_enabled = true
tray_enabled = true

[vad]
enabled = true
aggressiveness = 2         # 0 (gentle) to 3 (aggressive silence removal)

[audio]
sample_rate = 16000
chunk_duration_ms = 30
device_index = 0           # 0 = default mic
```

---

## Text Injection Compatibility

| Environment | Protocol | Mechanism | Support |
|---|---|---|---|
| **Terminal emulators** (GNOME Terminal, Kitty, Alacritty) | X11 / Wayland | `xdotool` / `ydotool` | Full keystroke typing |
| **Code editors** (VS Code, Cursor, Zed, Sublime) | X11 / Wayland | `xdotool` / `ydotool` | Full keystroke typing |
| **Browsers & Web Apps** (Chrome, Firefox, Discord, Slack) | X11 / Wayland | Active focus typing | Full keystroke typing |
| **Headless / SSH / Terminal fallback** | CLI | Clipboard (`wl-copy` / `xclip`) | Clipboard sync |

---

## Running Tests

```bash
pytest tests/ -v
```

---

## License

This project is licensed under the [MIT License](LICENSE).
