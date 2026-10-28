#!/usr/bin/env bash
# Build LAZYTYPE standalone binary bundle into ~/.local/lib/lazytype
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

echo "Building LAZYTYPE standalone executable bundle..."
uv run pyinstaller \
    --name lazytype \
    --onedir \
    --noconfirm \
    --clean \
    --collect-all voiceflow \
    --collect-all ctranslate2 \
    --collect-all faster_whisper \
    --collect-all webrtcvad \
    --collect-all pystray \
    --collect-all PyQt6 \
    --collect-all tomli_w \
    --collect-all groq \
    --collect-all openai \
    --collect-all deepgram \
    --collect-all assemblyai \
    --copy-metadata lazytype \
    --copy-metadata faster-whisper \
    --copy-metadata ctranslate2 \
    --copy-metadata tokenizers \
    --copy-metadata huggingface-hub \
    run_lazytype.py

mkdir -p "$HOME/.local/lib"
rm -rf "$HOME/.local/lib/lazytype"
cp -r dist/lazytype "$HOME/.local/lib/lazytype"

echo "✓ Bundle successfully installed to $HOME/.local/lib/lazytype"
