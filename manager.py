#!/usr/bin/env python3
"""
CloudSurf - Profile Manager Backend
Flask API that manages Chrome browser profiles, Xvfb displays, VNC + NoVNC sessions
"""

import os
import sys
import json
import time
import signal
import shutil
import subprocess
import threading
import logging
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
PROFILES_DIR = BASE_DIR / "profiles"
LOGS_DIR     = BASE_DIR / "logs"
STATE_FILE   = BASE_DIR / "state.json"
PROFILES_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

API_PORT      = 7860
NOVNC_BASE    = 6080   # novnc for profile 0 = 6080, profile 1 = 6081, etc.
VNC_BASE      = 5900   # x11vnc base port
DISPLAY_BASE  = 10     # :10, :11, :12 ...

# Detect chrome binary
CHROME_ENV = Path("/tmp/cloudsurf_chrome.env")
CHROME_BIN     = "google-chrome"
NOVNC_PATH     = "/usr/share/novnc"
WEBSOCKIFY_CMD = "websockify"
if CHROME_ENV.exists():
    for line in CHROME_ENV.read_text().splitlines():
        if line.startswith("CHROME_BIN="):
            CHROME_BIN = line.split("=",1)[1]
        if line.startswith("NOVNC_PATH="):
            NOVNC_PATH = line.split("=",1)[1]
        if line.startswith("WEBSOCKIFY_CMD="):
            WEBSOCKIFY_CMD = line.split("=",1)[1]

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "manager.log")
    ]
)
log = logging.getLogger("cloudsurf")

# ── State ──────────────────────────────────────────────────────────────────────
# In-memory session state (processes etc.)
sessions: dict = {}   # profile_id -> {xvfb_proc, vnc_proc, chrome_proc, novnc_proc, display, ports}

def load_profiles():
    """Load profile metadata from disk."""
    profiles = []
    if not PROFILES_DIR.exists():
        return profiles
    for p in sorted(PROFILES_DIR.iterdir()):
        meta_file = p / "meta.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
                meta["active"] = p.name in sessions
                profiles.append(meta)
            except Exception as e:
                log.error(f"Error reading {meta_file}: {e}")
    return profiles

def save_profile_meta(profile_id: str, data: dict):
    profile_dir = PROFILES_DIR / profile_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    meta_file = profile_dir / "meta.json"
    meta_file.write_text(json.dumps(data, indent=2))

def get_free_slot():
    """Find the next free display/port slot."""
    used = {s["slot"] for s in sessions.values() if "slot" in s}
    for i in range(0, 20):
        if i not in used:
            return i
    raise RuntimeError("All slots in use (max 20)")

def kill_proc(proc):
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try: proc.kill()
            except Exception: pass

# ── Profile lifecycle ──────────────────────────────────────────────────────────

def start_profile(profile_id: str) -> dict:
    if profile_id in sessions:
        return {"status": "already_running", **sessions[profile_id]["info"]}

    slot     = get_free_slot()
    display  = DISPLAY_BASE + slot
    vnc_port = VNC_BASE  + slot
    novnc_port = NOVNC_BASE + slot

    profile_dir = PROFILES_DIR / profile_id
    profile_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"Starting profile {profile_id} | display=:{display} vnc={vnc_port} novnc={novnc_port}")

    # 1. Start Xvfb
    xvfb_proc = subprocess.Popen(
        ["Xvfb", f":{display}", "-screen", "0", "1280x900x24", "-ac", "+extension", "RANDR"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(1.2)

    # 2. Start window manager (openbox)
    env = os.environ.copy()
    env["DISPLAY"] = f":{display}"
    wm_proc = subprocess.Popen(
        ["openbox", "--startup", "openbox --reconfigure"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    # 3. Start x11vnc
    vnc_proc = subprocess.Popen(
        ["x11vnc", "-display", f":{display}", "-rfbport", str(vnc_port),
         "-nopw", "-forever", "-shared", "-quiet", "-bg"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(1)

    # 4. Start NoVNC websockify
    ws_cmd = WEBSOCKIFY_CMD.split()  # handle "python3 -m websockify"
    novnc_cmd = ws_cmd + ["--web", NOVNC_PATH, str(novnc_port), f"localhost:{vnc_port}"]
    novnc_proc = subprocess.Popen(
        novnc_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(0.8)

    # 5. Start Chrome
    chrome_flags = [
        CHROME_BIN,
        f"--user-data-dir={profile_dir}/chrome",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-infobars",
        "--disable-session-crashed-bubble",
        "--disable-features=TranslateUI",
        "--disable-sync-preferences",
        "--start-maximized",
        "--window-size=1280,900",
        "about:blank"
    ]
    chrome_proc = subprocess.Popen(
        chrome_flags, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    info = {
        "profile_id":  profile_id,
        "display":     f":{display}",
        "vnc_port":    vnc_port,
        "novnc_port":  novnc_port,
        "novnc_url":   f"/novnc/{novnc_port}/vnc.html?autoconnect=true&resize=scale&quality=6",
        "started_at":  datetime.now().isoformat(),
    }

    sessions[profile_id] = {
        "slot":       slot,
        "xvfb":       xvfb_proc,
        "wm":         wm_proc,
        "vnc":        vnc_proc,
        "novnc":      novnc_proc,
        "chrome":     chrome_proc,
        "info":       info,
    }

    # Update meta
    meta_file = profile_dir / "meta.json"
    if meta_file.exists():
        meta = json.loads(meta_file.read_text())
    else:
        meta = {"id": profile_id, "name": profile_id, "created_at": datetime.now().isoformat()}
    meta["last_started"] = datetime.now().isoformat()
    save_profile_meta(profile_id, meta)

    log.info(f"Profile {profile_id} running. NoVNC → port {novnc_port}")
    return {"status": "started", **info}


def stop_profile(profile_id: str) -> dict:
    if profile_id not in sessions:
        return {"status": "not_running"}

    sess = sessions.pop(profile_id)
    log.info(f"Stopping profile {profile_id}...")

    for key in ["chrome", "novnc", "vnc", "wm", "xvfb"]:
        kill_proc(sess.get(key))

    return {"status": "stopped", "profile_id": profile_id}


def keep_alive_click(profile_id: str):
    """Send a random mouse wiggle + scroll to keep Colab session alive."""
    if profile_id not in sessions:
        return {"error": "not running"}
    sess = sessions[profile_id]
    display = sess["info"]["display"]
    env = os.environ.copy()
    env["DISPLAY"] = display
    # Random mouse move + scroll
    import random
    x = random.randint(200, 1000)
    y = random.randint(200, 700)
    subprocess.run(["xdotool", "mousemove", str(x), str(y)], env=env, capture_output=True)
    time.sleep(0.3)
    subprocess.run(["xdotool", "click", "1"], env=env, capture_output=True)
    time.sleep(0.2)
    subprocess.run(["xdotool", "key", "End"], env=env, capture_output=True)
    return {"status": "ok", "moved_to": [x, y]}


# ── Anti-disconnect thread ─────────────────────────────────────────────────────
_anti_disconnect_active = {}

def anti_disconnect_worker(profile_id: str, interval_secs: int):
    log.info(f"Anti-disconnect started for {profile_id} every {interval_secs}s")
    while _anti_disconnect_active.get(profile_id):
        time.sleep(interval_secs)
        if profile_id in sessions and _anti_disconnect_active.get(profile_id):
            result = keep_alive_click(profile_id)
            log.info(f"[keep-alive] {profile_id}: {result}")

def start_anti_disconnect(profile_id: str, interval: int = 90):
    _anti_disconnect_active[profile_id] = True
    t = threading.Thread(target=anti_disconnect_worker, args=(profile_id, interval), daemon=True)
    t.start()
    return {"status": "started", "interval_secs": interval}

def stop_anti_disconnect(profile_id: str):
    _anti_disconnect_active[profile_id] = False
    return {"status": "stopped"}


# ── Flask App ──────────────────────────────────────────────────────────────────
UI_DIR = BASE_DIR  # index.html lives next to manager.py
app = Flask(__name__, static_folder=str(UI_DIR), static_url_path="/static")
CORS(app)

@app.route("/")
def index():
    index_file = UI_DIR / "index.html"
    if not index_file.exists():
        return (
            "<pre>404 - index.html not found. "
            "Check that the ui/ folder exists next to manager.py</pre>"
        ), 404
    return send_from_directory(str(UI_DIR), "index.html")

@app.route("/<path:filename>")
def serve_static(filename):
    return send_from_directory(str(UI_DIR), filename)

@app.route("/api/profiles", methods=["GET"])
def list_profiles():
    return jsonify(load_profiles())

@app.route("/api/profiles", methods=["POST"])
def create_profile():
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    # Generate slug ID
    pid = name.lower().replace(" ", "_").replace("@","_at_")[:32] + "_" + str(int(time.time()))[-5:]
    meta = {
        "id":         pid,
        "name":       name,
        "email":      data.get("email", ""),
        "notes":      data.get("notes", ""),
        "created_at": datetime.now().isoformat(),
        "active":     False,
    }
    save_profile_meta(pid, meta)
    log.info(f"Created profile: {pid}")
    return jsonify(meta), 201

@app.route("/api/profiles/<pid>", methods=["DELETE"])
def delete_profile(pid):
    if pid in sessions:
        stop_profile(pid)
    profile_dir = PROFILES_DIR / pid
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
    return jsonify({"status": "deleted"})

@app.route("/api/profiles/<pid>/start", methods=["POST"])
def api_start(pid):
    profile_dir = PROFILES_DIR / pid
    if not profile_dir.exists():
        return jsonify({"error": "profile not found"}), 404
    try:
        result = start_profile(pid)
        return jsonify(result)
    except Exception as e:
        log.error(f"Error starting {pid}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/profiles/<pid>/stop", methods=["POST"])
def api_stop(pid):
    return jsonify(stop_profile(pid))

@app.route("/api/profiles/<pid>/keepalive", methods=["POST"])
def api_keepalive(pid):
    data = request.json or {}
    action = data.get("action", "start")
    interval = int(data.get("interval", 90))
    if action == "start":
        return jsonify(start_anti_disconnect(pid, interval))
    else:
        return jsonify(stop_anti_disconnect(pid))

@app.route("/api/profiles/<pid>/click", methods=["POST"])
def api_click(pid):
    return jsonify(keep_alive_click(pid))

@app.route("/api/sessions", methods=["GET"])
def api_sessions():
    result = {}
    for pid, sess in sessions.items():
        result[pid] = {
            **sess["info"],
            "anti_disconnect": _anti_disconnect_active.get(pid, False)
        }
    return jsonify(result)

@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({
        "running_profiles": len(sessions),
        "chrome_bin": CHROME_BIN,
        "novnc_path": NOVNC_PATH,
        "server_time": datetime.now().isoformat(),
    })

# ── Graceful shutdown ──────────────────────────────────────────────────────────
def shutdown(sig, frame):
    log.info("Shutting down all sessions...")
    for pid in list(sessions.keys()):
        stop_profile(pid)
    sys.exit(0)

signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

if __name__ == "__main__":
    log.info(f"CloudSurf manager starting on port {API_PORT}")
    log.info(f"Chrome: {CHROME_BIN} | NoVNC: {NOVNC_PATH}")
    app.run(host="0.0.0.0", port=API_PORT, debug=False, threaded=True)
