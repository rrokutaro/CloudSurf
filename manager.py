#!/usr/bin/env python3
"""CloudSurf - Profile Manager Backend"""

import os, sys, json, time, signal, shutil, subprocess, threading, logging, random
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

BASE_DIR     = Path(__file__).resolve().parent
PROFILES_DIR = BASE_DIR / "profiles"
LOGS_DIR     = BASE_DIR / "logs"
PROFILES_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

API_PORT     = 7860
NOVNC_BASE   = 6080
VNC_BASE     = 5900
DISPLAY_BASE = 10

CHROME_BIN     = "google-chrome"
NOVNC_PATH     = "/usr/share/novnc"
WEBSOCKIFY_CMD = "websockify"

cfg = Path("/tmp/cloudsurf_chrome.env")
if cfg.exists():
    for line in cfg.read_text().splitlines():
        k, _, v = line.partition("=")
        if k == "CHROME_BIN":     CHROME_BIN     = v
        if k == "NOVNC_PATH":     NOVNC_PATH     = v
        if k == "WEBSOCKIFY_CMD": WEBSOCKIFY_CMD = v

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOGS_DIR / "manager.log")]
)
log = logging.getLogger("cloudsurf")

sessions: dict = {}
_ka_active: dict = {}

CHROME_FLAGS = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-gpu", "--disable-software-rasterizer",
    "--disable-gpu-sandbox", "--disable-dev-shm-usage",
    "--disable-accelerated-2d-canvas",
    "--no-first-run", "--no-default-browser-check",
    "--disable-infobars", "--disable-session-crashed-bubble",
    "--disable-features=TranslateUI,VizDisplayCompositor",
    "--disable-background-networking",
    "--start-maximized", "--window-size=1280,900",
]

def kill_proc(proc):
    if proc and proc.poll() is None:
        try: proc.terminate(); proc.wait(timeout=3)
        except:
            try: proc.kill()
            except: pass

def load_profiles():
    out = []
    for p in sorted(PROFILES_DIR.iterdir()):
        mf = p / "meta.json"
        if mf.exists():
            try:
                m = json.loads(mf.read_text())
                m["active"] = p.name in sessions
                if p.name in sessions:
                    m["session"] = sessions[p.name]["info"]
                out.append(m)
            except Exception as e:
                log.error(f"meta read error {mf}: {e}")
    return out

def save_meta(pid, data):
    d = PROFILES_DIR / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(data, indent=2))

def free_slot():
    used = {s["slot"] for s in sessions.values()}
    for i in range(20):
        if i not in used: return i
    raise RuntimeError("All 20 slots in use")


def _autoforward_port(port):
    """Make the NoVNC port publicly accessible in Codespaces via gh CLI."""
    try:
        cs_name = os.environ.get("CODESPACE_NAME")
        if not cs_name:
            return  # Not in Codespaces, nothing to do
        time.sleep(2)  # Let websockify bind first
        # Make port visible (public = accessible via forwarded URL without login)
        subprocess.run(
            ["gh", "codespace", "ports", "visibility", f"{port}:public", "--codespace", cs_name],
            capture_output=True, timeout=10
        )
        log.info(f"Port {port} forwarded as public in Codespace {cs_name}")
    except Exception as e:
        log.warning(f"Auto-forward port {port} failed: {e}")

def start_profile(pid):
    if pid in sessions:
        return {"status": "already_running", **sessions[pid]["info"]}
    slot       = free_slot()
    display    = DISPLAY_BASE + slot
    vnc_port   = VNC_BASE  + slot
    novnc_port = NOVNC_BASE + slot
    pdir       = PROFILES_DIR / pid
    pdir.mkdir(parents=True, exist_ok=True)
    env        = {**os.environ, "DISPLAY": f":{display}"}

    log.info(f"Starting {pid}: :{display} novnc={novnc_port}")
    xvfb = subprocess.Popen(["Xvfb", f":{display}", "-screen", "0", "1280x900x24", "-ac"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)
    wm = subprocess.Popen(["openbox"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    vnc = subprocess.Popen(
        ["x11vnc", "-display", f":{display}", "-rfbport", str(vnc_port),
         "-nopw", "-forever", "-shared", "-quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    ws_cmd = WEBSOCKIFY_CMD.split() + ["--web", NOVNC_PATH, str(novnc_port), f"localhost:{vnc_port}"]
    novnc = subprocess.Popen(ws_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    chrome = subprocess.Popen(
        [CHROME_BIN, f"--user-data-dir={pdir}/chrome"] + CHROME_FLAGS + ["about:blank"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    info = {"profile_id": pid, "display": f":{display}", "vnc_port": vnc_port,
            "novnc_port": novnc_port, "started_at": datetime.now().isoformat(), "last_action": None}
    sessions[pid] = {"slot": slot, "xvfb": xvfb, "wm": wm, "vnc": vnc,
                     "novnc": novnc, "chrome": chrome, "info": info}
    mf = pdir / "meta.json"
    meta = json.loads(mf.read_text()) if mf.exists() else {"id": pid, "name": pid, "created_at": datetime.now().isoformat()}
    meta["last_started"] = datetime.now().isoformat()
    save_meta(pid, meta)
    # Auto-forward port in Codespaces/Gitpod if gh CLI available
    threading.Thread(target=_autoforward_port, args=(novnc_port,), daemon=True).start()
    return {"status": "started", **info}

def stop_profile(pid):
    if pid not in sessions: return {"status": "not_running"}
    _ka_active[pid] = False
    sess = sessions.pop(pid)
    for k in ["chrome", "novnc", "vnc", "wm", "xvfb"]:
        kill_proc(sess.get(k))
    return {"status": "stopped"}

def restart_chrome(pid):
    if pid not in sessions: return {"error": "not running"}
    sess = sessions[pid]; pdir = PROFILES_DIR / pid
    kill_proc(sess.get("chrome")); time.sleep(1.5)
    env = {**os.environ, "DISPLAY": sess["info"]["display"]}
    chrome = subprocess.Popen(
        [CHROME_BIN, f"--user-data-dir={pdir}/chrome"] + CHROME_FLAGS,
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sess["chrome"] = chrome
    return {"status": "restarted", "pid": chrome.pid}

def keepalive_click(pid):
    """Right-click + Escape + scroll — safest keep-alive strategy.
    Right-click opens context menu anywhere without triggering links/buttons.
    Escape closes it immediately. Scroll provides additional activity signal.
    """
    if pid not in sessions: return {"error": "not running"}
    sess = sessions[pid]
    env  = {**os.environ, "DISPLAY": sess["info"]["display"]}

    def run(cmd):
        subprocess.run(cmd, env=env, capture_output=True)

    # Random position — anywhere is safe because right-click+escape harms nothing
    x = random.randint(80, 1200)
    y = random.randint(80, 820)

    # 1. Move mouse
    run(["xdotool", "mousemove", "--sync", str(x), str(y)])
    time.sleep(random.uniform(0.15, 0.35))

    # 2. Right-click → context menu appears
    run(["xdotool", "click", "--clearmodifiers", "3"])
    time.sleep(random.uniform(0.12, 0.25))

    # 3. Escape → dismiss context menu, no item selected
    run(["xdotool", "key", "--clearmodifiers", "Escape"])
    time.sleep(random.uniform(0.1, 0.2))

    # 4. Scroll up N ticks then back down (resets idle timer, looks human)
    ticks = random.randint(2, 5)
    for _ in range(ticks):
        run(["xdotool", "click", "--clearmodifiers", "4"])  # wheel up
        time.sleep(0.05)
    time.sleep(random.uniform(0.1, 0.2))
    for _ in range(ticks):
        run(["xdotool", "click", "--clearmodifiers", "5"])  # wheel down
        time.sleep(0.05)

    # 5. Small random drift
    run(["xdotool", "mousemove", "--sync",
         str(x + random.randint(-6, 6)),
         str(y + random.randint(-6, 6))])

    result = {
        "status": "ok", "x": x, "y": y,
        "action": "rclick+esc+scroll",
        "zone": "free",
        "ts": datetime.now().strftime("%H:%M:%S")
    }
    sess["info"]["last_action"] = result
    return result

def ka_worker(pid, interval):
    log.info(f"KA started: {pid} every {interval}s")
    tick = 0
    # Fire immediately on start, then wait interval between each tick
    while _ka_active.get(pid) and pid in sessions:
        tick += 1
        keepalive_click(pid)
        log.info(f"[ka] {pid} tick={tick} interval={interval}s")
        # Chrome watchdog every 5 ticks
        if tick % 5 == 0:
            sess = sessions.get(pid)
            if sess and sess.get("chrome") and sess["chrome"].poll() is not None:
                log.warning(f"[watchdog] {pid} Chrome died - restarting")
                restart_chrome(pid)
        # Sleep in small chunks so stopping is responsive
        slept = 0
        while slept < interval and _ka_active.get(pid):
            time.sleep(min(1, interval - slept))
            slept += 1

app = Flask(__name__)
CORS(app)

@app.after_request
def add_headers(r):
    r.headers.pop("X-Frame-Options", None)
    r.headers["X-Frame-Options"] = "ALLOWALL"
    r.headers["Access-Control-Allow-Origin"] = "*"
    return r

@app.route("/")
def index():
    f = BASE_DIR / "index.html"
    if not f.exists():
        return f"<pre>index.html not found in {BASE_DIR}\nFiles: {[x.name for x in BASE_DIR.iterdir()]}</pre>", 404
    return send_from_directory(str(BASE_DIR), "index.html")

@app.route("/api/profiles", methods=["GET"])
def api_list(): return jsonify(load_profiles())

@app.route("/api/profiles", methods=["POST"])
def api_create():
    d    = request.json or {}
    name = d.get("name", "").strip()
    if not name: return jsonify({"error": "name required"}), 400
    pid  = name.lower().replace(" ", "_").replace("@", "_at_")[:28] + "_" + str(int(time.time()))[-5:]
    meta = {"id": pid, "name": name, "email": d.get("email",""),
            "notes": d.get("notes",""), "created_at": datetime.now().isoformat()}
    save_meta(pid, meta)
    return jsonify(meta), 201

@app.route("/api/profiles/<pid>", methods=["DELETE"])
def api_delete(pid):
    if pid in sessions: stop_profile(pid)
    d = PROFILES_DIR / pid
    if d.exists(): shutil.rmtree(d)
    return jsonify({"status": "deleted"})

@app.route("/api/profiles/<pid>/start", methods=["POST"])
def api_start(pid):
    if not (PROFILES_DIR / pid).exists(): return jsonify({"error": "profile not found"}), 404
    try: return jsonify(start_profile(pid))
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/profiles/<pid>/stop", methods=["POST"])
def api_stop(pid): return jsonify(stop_profile(pid))

@app.route("/api/profiles/<pid>/restart-chrome", methods=["POST"])
def api_restart_chrome(pid): return jsonify(restart_chrome(pid))

@app.route("/api/profiles/<pid>/keepalive", methods=["POST"])
def api_keepalive(pid):
    d        = request.json or {}
    action   = d.get("action", "start")
    interval = max(5, int(d.get("interval", 90)))  # allow down to 5s for testing

    # Always stop existing worker first (prevents duplicate threads)
    _ka_active[pid] = False
    time.sleep(0.2)  # let old thread notice the flag

    if action == "start":
        _ka_active[pid] = True
        threading.Thread(target=ka_worker, args=(pid, interval), daemon=True).start()
        log.info(f"KA started for {pid} every {interval}s")
        return jsonify({"status": "started", "interval": interval})

    log.info(f"KA stopped for {pid}")
    return jsonify({"status": "stopped"})

@app.route("/api/profiles/<pid>/click", methods=["POST"])
def api_click(pid): return jsonify(keepalive_click(pid))

@app.route("/api/profiles/<pid>/sendtext", methods=["POST"])
def api_sendtext(pid):
    if pid not in sessions: return jsonify({"error": "not running"}), 400
    text = (request.json or {}).get("text", "")
    if not text: return jsonify({"error": "no text"}), 400
    env = {**os.environ, "DISPLAY": sessions[pid]["info"]["display"]}
    r   = subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "30", "--", text],
                         env=env, capture_output=True, text=True)
    if r.returncode != 0: return jsonify({"error": r.stderr.strip()}), 500
    return jsonify({"status": "ok", "chars": len(text)})

@app.route("/api/sessions", methods=["GET"])
def api_sessions():
    return jsonify({pid: {**s["info"], "ka_active": _ka_active.get(pid, False)}
                    for pid, s in sessions.items()})

_server_location = {"city": "—", "region": "—", "country": "—"}

def _fetch_server_location():
    global _server_location
    try:
        import urllib.request as ur
        with ur.urlopen("https://ipapi.co/json/", timeout=6) as r:
            d = json.loads(r.read())
        _server_location = {"city": d.get("city") or "—", "region": d.get("region_code") or "—", "country": d.get("country_name") or "—"}
        log.info(f"Server location: {_server_location}")
    except Exception as e:
        log.warning(f"Location fetch 1 failed: {e}")
        try:
            import urllib.request as ur
            with ur.urlopen("http://ip-api.com/json/?fields=city,regionName,country", timeout=6) as r:
                d = json.loads(r.read())
            _server_location = {"city": d.get("city") or "—", "region": d.get("regionName") or "—", "country": d.get("country") or "—"}
        except Exception as e2:
            log.warning(f"Location fetch 2 failed: {e2}")

threading.Thread(target=_fetch_server_location, daemon=True).start()

@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({"running": len(sessions), "chrome_bin": CHROME_BIN,
                    "novnc_path": NOVNC_PATH, "time": datetime.now().isoformat(),
                    "location": _server_location})

def _shutdown(sig, frame):
    log.info("Shutting down...")
    for pid in list(sessions): stop_profile(pid)
    sys.exit(0)

signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

if __name__ == "__main__":
    log.info(f"CloudSurf :{API_PORT} | chrome={CHROME_BIN} | novnc={NOVNC_PATH}")
    app.run(host="0.0.0.0", port=API_PORT, debug=False, threaded=True)
