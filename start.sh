#!/bin/bash
# ============================================================
# CloudSurf - Start Script
# ============================================================

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check dependencies
if ! command -v Xvfb &>/dev/null; then
    echo -e "${RED}Dependencies missing. Run: ./setup.sh first${NC}"
    exit 1
fi

# Kill any previous manager instance
pkill -f "manager.py" 2>/dev/null || true
sleep 0.5

echo -e "${CYAN}Starting CloudSurf...${NC}"
echo ""

# Start the manager
nohup python3 manager.py > logs/manager.log 2>&1 &
MANAGER_PID=$!
echo $MANAGER_PID > /tmp/cloudsurf.pid

sleep 1.5

if kill -0 $MANAGER_PID 2>/dev/null; then
    echo -e "${GREEN}✓ CloudSurf Manager running (PID: $MANAGER_PID)${NC}"
    echo ""
    echo -e "  ${CYAN}┌─────────────────────────────────────────────┐${NC}"
    echo -e "  ${CYAN}│${NC}  ${GREEN}UI:${NC}     http://localhost:7860              ${CYAN}│${NC}"
    echo -e "  ${CYAN}│${NC}  ${GREEN}API:${NC}    http://localhost:7860/api           ${CYAN}│${NC}"
    echo -e "  ${CYAN}│${NC}                                             ${CYAN}│${NC}"
    echo -e "  ${CYAN}│${NC}  On Codespaces: Forward port ${YELLOW}7860${NC}            ${CYAN}│${NC}"
    echo -e "  ${CYAN}│${NC}  Also forward ${YELLOW}6080-6099${NC} for NoVNC panels    ${CYAN}│${NC}"
    echo -e "  ${CYAN}└─────────────────────────────────────────────┘${NC}"
    echo ""
    echo -e "  ${YELLOW}Logs:${NC} tail -f logs/manager.log"
    echo ""
else
    echo -e "${RED}✗ Manager failed to start. Check logs/manager.log${NC}"
    exit 1
fi
