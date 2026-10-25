#!/usr/bin/env bash
# VoiceFlow Installer — Ubuntu/Debian
# One-command: bash scripts/install.sh
set -euo pipefail

BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()    { echo -e "${BLUE}[VoiceFlow]${NC} $*"; }
success() { echo -e "${GREEN}[VoiceFlow] ✓${NC} $*"; }
warn()    { echo -e "${YELLOW}[VoiceFlow] ⚠${NC} $*"; }
error()   { echo -e "${RED}[VoiceFlow] ✗${NC} $*" >&2; exit 1; }

VENV_DIR="$HOME/.local/share/voiceflow/venv"
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="$HOME/.config/voiceflow"
SYSTEMD_DIR="$HOME/.config/systemd/user"
AUTOSTART_DIR="$HOME/.config/autostart"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo ""
echo "  ╔═══════════════════════════════════╗"
echo "  ║    VoiceFlow Installer  🎤        ║"
echo "  ╚═══════════════════════════════════╝"
echo ""

# ── 1. System dependencies ──────────────────────────────────────────────────
info "Installing system dependencies…"
if command -v apt-get &>/dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq \
        portaudio19-dev \
        python3-pip \
        python3-venv \
        xdotool \
        ydotool \
        wl-clipboard \
        libnotify-bin \
        || warn "Some apt packages may have failed — continuing"
else
    warn "apt-get not found — please install portaudio19-dev, xdotool, wl-clipboard manually"
fi
success "System dependencies done"

# ── 2. uinput group for evdev (Wayland hotkey support) ──────────────────────
info "Setting up input group for global hotkey (evdev)…"
if ! groups | grep -q "input"; then
    sudo usermod -aG input "$USER"
    warn "Added $USER to 'input' group — you need to LOG OUT and back in for hotkeys to work on Wayland"
    warn "After re-login, hotkeys will work without sudo"
else
    success "Already in 'input' group"
fi

# ── 3. Python virtual environment ────────────────────────────────────────────
info "Creating Python virtual environment at $VENV_DIR…"
mkdir -p "$(dirname "$VENV_DIR")"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q
success "venv created"

# ── 4. Install VoiceFlow package ─────────────────────────────────────────────
info "Installing VoiceFlow and dependencies…"
pip install -e "$REPO_DIR" -q
success "VoiceFlow installed"

# ── 5. Wrapper script in ~/.local/bin ────────────────────────────────────────
info "Creating voiceflow launcher script…"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/voiceflow" << EOF
#!/usr/bin/env bash
source "\$HOME/.bashrc" 2>/dev/null || true
source "$VENV_DIR/bin/activate"
exec python -m voiceflow "\$@"
EOF
chmod +x "$BIN_DIR/voiceflow"

# Ensure ~/.local/bin is in PATH
if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    warn "Added ~/.local/bin to PATH in ~/.bashrc — run: source ~/.bashrc"
fi
success "Launcher created at $BIN_DIR/voiceflow"

# ── 6. Config file ──────────────────────────────────────────────────────────
info "Setting up config file…"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
    cp "$REPO_DIR/config/voiceflow.toml.example" "$CONFIG_DIR/config.toml"
    success "Config created at $CONFIG_DIR/config.toml"
else
    success "Config already exists — not overwriting"
fi

# ── 7. systemd user service ──────────────────────────────────────────────────
info "Installing systemd user service…"
mkdir -p "$SYSTEMD_DIR"
cat > "$SYSTEMD_DIR/voiceflow.service" << EOF
[Unit]
Description=VoiceFlow — System-wide STT daemon
After=graphical-session.target

[Service]
Type=simple
ExecStart=$BIN_DIR/voiceflow
Restart=on-failure
RestartSec=3
KillMode=mixed
TimeoutStopSec=5
SuccessExitStatus=0 143 15
Environment=DISPLAY=:0
Environment=WAYLAND_DISPLAY=wayland-0

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable voiceflow.service 2>/dev/null || true
success "systemd service installed (voiceflow.service)"

# ── 8. Desktop autostart entry ───────────────────────────────────────────────
info "Creating autostart entry…"
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/voiceflow.desktop" << EOF
[Desktop Entry]
Type=Application
Name=VoiceFlow
Comment=Free WhisperFlow alternative — system-wide STT
Exec=$BIN_DIR/voiceflow
Icon=audio-input-microphone
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
X-systemd-skip=true
EOF
success "Autostart entry created"

# ── 9. API key reminder ──────────────────────────────────────────────────────
echo ""
echo "  ┌─────────────────────────────────────────────────────────┐"
echo "  │  🔑  API Key Setup                                      │"
echo "  │                                                         │"
echo "  │  Add to ~/.bashrc (at minimum Groq for free tier):      │"
echo "  │                                                         │"
echo "  │    export GROQ_API_KEY=\"gsk_...\"      # Required       │"
echo "  │    export NVIDIA_API_KEY=\"nvapi-...\"   # Optional      │"
echo "  │    export DEEPGRAM_API_KEY=\"...\"       # Optional      │"
echo "  │    export ASSEMBLYAI_API_KEY=\"...\"     # Optional      │"
echo "  │                                                         │"
echo "  │  Get free Groq key at: https://console.groq.com        │"
echo "  └─────────────────────────────────────────────────────────┘"
echo ""
success "Installation complete!"
echo ""
echo "  Quick test:  voiceflow --test"
echo "  Start TUI:   voiceflow --tui"
echo "  Start daemon: voiceflow"
echo "  Start service: systemctl --user start voiceflow"
echo ""
