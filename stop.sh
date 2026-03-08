#!/bin/bash
echo "Stopping CloudSurf..."

# Kill keep-alive loop first so it doesn't restart manager
if [ -f /tmp/cloudsurf_keep.pid ]; then
    kill $(cat /tmp/cloudsurf_keep.pid) 2>/dev/null
    rm /tmp/cloudsurf_keep.pid
fi
pkill -f "cloudsurf_keep" 2>/dev/null || true

# Kill manager
if [ -f /tmp/cloudsurf.pid ]; then
    kill $(cat /tmp/cloudsurf.pid) 2>/dev/null
    rm /tmp/cloudsurf.pid
fi
pkill -f "manager.py"   2>/dev/null || true
pkill -f "websockify"   2>/dev/null || true
pkill -f "x11vnc"       2>/dev/null || true

echo "Done."
