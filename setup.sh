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
# Ignore unsigned repo warnings (e.g. yarn) — not needed for CloudSurf
sudo $PKG update -y -q 2>&1 | grep -v "^W:\|^E: The repository.*yarnpkg\|couldn't be verified" || true

echo -e "${YELLOW}[2/6] Installing Xvfb + VNC + NoVNC...${NC}"
# Install packages one group at a time for clearer error reporting
sudo $PKG install -y -q xvfb x11vnc x11-utils xdotool wmctrl openbox xterm net-tools curl wget unzip python3 python3-pip jq procps git

# novnc package name changed in Ubuntu 22+; try both
sudo $PKG install -y -q novnc websockify 2>/dev/null || \
sudo $PKG install -y -q novnc python3-websockify 2>/dev/null || \
echo -e "${YELLOW}  novnc not in apt — will clone from GitHub in step 5${NC}"

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
# Find novnc path — Ubuntu Noble puts it in different places
NOVNC_PATH=""
for p in /usr/share/novnc /usr/local/share/novnc /opt/novnc /usr/share/noVNC; do
    if [ -d "$p" ] && [ -f "$p/vnc.html" -o -f "$p/index.html" ]; then
        NOVNC_PATH="$p"; break
    fi
done

if [ -z "$NOVNC_PATH" ]; then
    echo "  NoVNC not found in standard paths, cloning from GitHub..."
    sudo git clone --depth 1 https://github.com/novnc/noVNC.git /opt/novnc 2>/dev/null || git clone --depth 1 https://github.com/novnc/noVNC.git /opt/novnc
    NOVNC_PATH="/opt/novnc"
    # Also ensure websockify is available
    if ! command -v websockify &>/dev/null; then
        pip3 install websockify --quiet
    fi
fi

# Create vnc.html symlink if needed (some versions use index.html)
if [ ! -f "$NOVNC_PATH/vnc.html" ] && [ -f "$NOVNC_PATH/index.html" ]; then
    ln -sf "$NOVNC_PATH/index.html" "$NOVNC_PATH/vnc.html"
fi

echo "NOVNC_PATH=$NOVNC_PATH" >> /tmp/cloudsurf_chrome.env
echo -e "${GREEN}  NoVNC at: $NOVNC_PATH${NC}"

# Detect websockify binary (might be python3 -m websockify on Noble)
if command -v websockify &>/dev/null; then
    echo "WEBSOCKIFY_CMD=websockify" >> /tmp/cloudsurf_chrome.env
else
    echo "WEBSOCKIFY_CMD=python3 -m websockify" >> /tmp/cloudsurf_chrome.env
    echo -e "${YELLOW}  websockify will run via python3 -m${NC}"
fi

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
