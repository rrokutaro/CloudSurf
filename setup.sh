#!/bin/bash
# ============================================================
# CloudSurf - Setup Script
# Installs all dependencies for running browser profiles in cloud
# ============================================================

set -e
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

echo -e "${CYAN}"
echo "  ██████╗██╗      ██████╗ ██╗   ██╗██████╗ ███████╗██╗   ██╗██████╗ ███████╗"
echo " ██╔════╝██║     ██╔═══██╗██║   ██║██╔══██╗██╔════╝██║   ██║██╔══██╗██╔════╝"
echo " ██║     ██║     ██║   ██║██║   ██║██║  ██║███████╗██║   ██║██████╔╝█████╗  "
echo " ██║     ██║     ██║   ██║██║   ██║██║  ██║╚════██║██║   ██║██╔══██╗██╔══╝  "
echo " ╚██████╗███████╗╚██████╔╝╚██████╔╝██████╔╝███████║╚██████╔╝██║  ██║██║     "
echo "  ╚═════╝╚══════╝ ╚═════╝  ╚═════╝ ╚═════╝ ╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚═╝     "
echo -e "${NC}"
echo -e "${GREEN}Free Cloud Browser Farm — Setup${NC}"
echo ""

# ---- Detect OS ----
if [ -f /etc/debian_version ]; then
    PKG="apt-get"
elif [ -f /etc/redhat-release ]; then
    PKG="yum"
else
    echo -e "${RED}Unsupported OS${NC}"; exit 1
fi

echo -e "${YELLOW}[1/6] Updating package lists...${NC}"
sudo $PKG update -y -q

echo -e "${YELLOW}[2/6] Installing Xvfb + VNC + NoVNC...${NC}"
sudo $PKG install -y -q \
    xvfb \
    x11vnc \
    novnc \
    websockify \
    x11-utils \
    xdotool \
    wmctrl \
    openbox \
    xterm \
    net-tools \
    curl \
    wget \
    unzip \
    python3 \
    python3-pip \
    jq \
    procps

echo -e "${YELLOW}[3/6] Installing Google Chrome...${NC}"
if ! command -v google-chrome &> /dev/null && ! command -v chromium-browser &> /dev/null; then
    wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -O /tmp/chrome.deb
    sudo dpkg -i /tmp/chrome.deb || sudo apt-get -f install -y -q
    rm /tmp/chrome.deb
    echo -e "${GREEN}Chrome installed.${NC}"
else
    echo -e "${GREEN}Chrome/Chromium already present.${NC}"
fi

# Detect chrome binary
if command -v google-chrome &> /dev/null; then
    CHROME_BIN="google-chrome"
elif command -v chromium-browser &> /dev/null; then
    CHROME_BIN="chromium-browser"
elif command -v chromium &> /dev/null; then
    CHROME_BIN="chromium"
else
    echo -e "${RED}No Chrome binary found after install.${NC}"; exit 1
fi
echo "CHROME_BIN=$CHROME_BIN" > /tmp/cloudsurf_chrome.env

echo -e "${YELLOW}[4/6] Installing Python deps...${NC}"
pip3 install flask flask-cors watchdog --quiet

echo -e "${YELLOW}[5/6] Checking NoVNC...${NC}"
# Find novnc path
NOVNC_PATH=""
for p in /usr/share/novnc /usr/local/share/novnc /opt/novnc; do
    if [ -d "$p" ]; then NOVNC_PATH="$p"; break; fi
done
if [ -z "$NOVNC_PATH" ]; then
    echo "NoVNC not found in standard paths, cloning..."
    git clone --depth 1 https://github.com/novnc/noVNC.git /opt/novnc
    git clone --depth 1 https://github.com/novnc/websockify.git /opt/websockify
    NOVNC_PATH="/opt/novnc"
fi
echo "NOVNC_PATH=$NOVNC_PATH" >> /tmp/cloudsurf_chrome.env
echo -e "${GREEN}NoVNC at: $NOVNC_PATH${NC}"

echo -e "${YELLOW}[6/6] Creating profile & log directories...${NC}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$SCRIPT_DIR/profiles"
mkdir -p "$SCRIPT_DIR/logs"
mkdir -p "$SCRIPT_DIR/backups"

echo ""
echo -e "${GREEN}✓ Setup complete!${NC}"
echo ""
echo -e "  Run: ${CYAN}./start.sh${NC}  to launch CloudSurf"
echo ""
