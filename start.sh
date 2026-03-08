#!/bin/bash
# ============================================================
# CloudSurf - Start Script
# Self-healing: runs setup automatically if deps are missing
# ============================================================

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
mkdir -p logs

# ── Auto-setup if dependencies are missing ───────────────────────────────────
NEEDS_SETUP=0
command -v Xvfb       &>/dev/null || NEEDS_SETUP=1
command -v x11vnc     &>/dev/null || NEEDS_SETUP=1
command -v google-chrome &>/dev/null || command -v chromium-browser &>/dev/null || command -v chromium &>/dev/null || NEEDS_SETUP=1
python3 -c "import flask" 2>/dev/null || NEEDS_SETUP=1

if [ "$NEEDS_SETUP" = "1" ]; then
    echo -e "${YELLOW}Dependencies missing — running setup first...${NC}"
    bash "$SCRIPT_DIR/setup.sh" 2>&1 | tee logs/setup.log
    if [ $? -ne 0 ]; then
        echo -e "${RED}Setup failed. Check logs/setup.log${NC}"
        exit 1
    fi
fi

# ── Kill any previous instances ───────────────────────────────────────────────
pkill -f "manager.py"       2>/dev/null || true
pkill -f "cloudsurf_keep"   2>/dev/null || true
sleep 0.5

echo -e "${CYAN}Starting CloudSurf...${NC}"

# ── Launch manager in background ─────────────────────────────────────────────
nohup python3 "$SCRIPT_DIR/manager.py" >> "$SCRIPT_DIR/logs/manager.log" 2>&1 &
MANAGER_PID=$!
echo $MANAGER_PID > /tmp/cloudsurf.pid

# ── Wait for Flask to bind ────────────────────────────────────────────────────
echo -ne "  Waiting for manager"
for i in $(seq 1 20); do
    sleep 0.5
    if curl -sf http://localhost:7860/api/status >/dev/null 2>&1; then
        echo -e " ${GREEN}✓${NC}"
        break
    fi
    echo -n "."
done

# ── Codespaces keep-alive + manager watchdog ─────────────────────────────────
# Pings our own API every 4 min — prevents Codespace inactivity shutdown.
# Also restarts manager.py automatically if it ever crashes.
cat > /tmp/cloudsurf_keep.sh << 'KEEPALIVE'
#!/bin/bash
SCRIPT_DIR="__SCRIPT_DIR__"
while true; do
    curl -sf http://localhost:7860/api/status > /dev/null 2>&1
    if [ -f /tmp/cloudsurf.pid ]; then
        PID=$(cat /tmp/cloudsurf.pid)
        if ! kill -0 "$PID" 2>/dev/null; then
            echo "[keepalive $(date)] Manager died — restarting..." >> "$SCRIPT_DIR/logs/manager.log"
            nohup python3 "$SCRIPT_DIR/manager.py" >> "$SCRIPT_DIR/logs/manager.log" 2>&1 &
            echo $! > /tmp/cloudsurf.pid
        fi
    fi
    sleep 240
done
KEEPALIVE

sed -i "s|__SCRIPT_DIR__|$SCRIPT_DIR|g" /tmp/cloudsurf_keep.sh
chmod +x /tmp/cloudsurf_keep.sh
nohup bash /tmp/cloudsurf_keep.sh >> "$SCRIPT_DIR/logs/keepalive.log" 2>&1 &
echo $! > /tmp/cloudsurf_keep.pid
echo -e "  ${GREEN}✓${NC} Codespace keep-alive active (pings every 4 min)"

# ── Status ────────────────────────────────────────────────────────────────────
if kill -0 $MANAGER_PID 2>/dev/null; then
    echo ""
    echo -e "  ${CYAN}┌─────────────────────────────────────────────┐${NC}"
    echo -e "  ${CYAN}│${NC}  ${GREEN}CloudSurf is running${NC}                      ${CYAN}│${NC}"
    echo -e "  ${CYAN}│${NC}  UI:   http://localhost:7860               ${CYAN}│${NC}"
    echo -e "  ${CYAN}│${NC}  Logs: tail -f logs/manager.log            ${CYAN}│${NC}"
    echo -e "  ${CYAN}│${NC}  Keep: tail -f logs/keepalive.log          ${CYAN}│${NC}"
    echo -e "  ${CYAN}└─────────────────────────────────────────────┘${NC}"
    echo ""
else
    echo -e "${RED}✗ Manager failed to start. Check logs/manager.log${NC}"
    tail -20 logs/manager.log
    exit 1
fi
