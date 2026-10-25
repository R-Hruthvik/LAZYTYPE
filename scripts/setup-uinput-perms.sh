#!/usr/bin/env bash
# VoiceFlow Permission & Ubuntu Tray Setup
# Run once with: bash scripts/setup-uinput-perms.sh

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}=== VoiceFlow Ubuntu Wayland & Tray Setup ===${NC}"
echo "This sets up:"
echo "  1. Direct text injection into any app via /dev/uinput virtual keyboard"
echo "  2. Global push-to-talk hotkey (Right Ctrl) via evdev"
echo "  3. Top-right Ubuntu system tray indicator"
echo ""

# 1. Add user to input group
echo -e "${BLUE}[1/4] Adding $USER to 'input' group...${NC}"
sudo usermod -aG input "$USER"

# 2. Setup udev rule for uinput
echo -e "${BLUE}[2/4] Setting up udev rules for /dev/uinput...${NC}"
sudo tee /etc/udev/rules.d/99-voiceflow.rules > /dev/null << 'EOF'
KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", GROUP="input", MODE="0660"
KERNEL=="event*", SUBSYSTEM=="input", ENV{ID_INPUT_KEYBOARD}=="1", TAG+="uaccess", GROUP="input", MODE="0660"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger

# 3. Grant immediate ACL access without needing to logout/reboot
echo -e "${BLUE}[3/4] Granting immediate permissions for current session...${NC}"
sudo setfacl -m "u:$USER:rw" /dev/input/event* 2>/dev/null || true
if [ -e /dev/uinput ]; then
    sudo setfacl -m "u:$USER:rw" /dev/uinput 2>/dev/null || true
fi

# 4. Install Ayatana AppIndicator for top-right Ubuntu tray icon
echo -e "${BLUE}[4/4] Installing top-right AppIndicator package...${NC}"
sudo apt-get update -qq
sudo apt-get install -y gir1.2-ayatanaappindicator3-0.1 wl-clipboard

echo ""
echo -e "${GREEN}✅ Setup complete!${NC}"
echo -e "You can now run:"
echo -e "  ${YELLOW}python -m voiceflow${NC}"
echo ""
