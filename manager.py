#!/usr/bin/env python3

"""CloudSurf - Profile Manager Backend"""

import os, sys, json, time, signal, shutil, subprocess, threading, logging, random

from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

BASE_DIR = Path(__file__).resolve().parent
PROFILES_DIR = BASE_DIR / "profiles"
LOGS_DIR = BASE_DIR / "logs"
PROFILES_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

API_PORT = 7860
NOVNC_BASE = 6080
VNC_BASE = 5900
DISPLAY_BASE = 10
CHROME_BIN = "google-chrome"
NOVNC_PATH = "/usr/share/novnc"
WEBSOCKIFY_CMD = "websockify"

cfg = Path("/tmp/cloudsurf_chrome.env")
if cfg.exists():
    for line in cfg.read_text().splitlines():
        k, _, v = line.partition("=")
        if k == "CHROME_BIN": CHROME_BIN = v
        if k == "NOVNC_PATH": NOVNC_PATH = v
        if k == "WEBSOCKIFY_CMD": WEBSOCKIFY_CMD = v

# ── Auto-launch / automation env vars ────────────────────────────────────────
#
# CLOUDSURF_AUTO_LAUNCH      comma-separated profile IDs to launch on startup
#                             e.g. "alice_12345,bob_67890"
#
# CLOUDSURF_AUTO_SCRIPT       script name from scripts/ to run on each profile
#                             e.g. "colab_run_all"
#
# CLOUDSURF_SCRIPT_DELAY      seconds to wait after Chrome launches before
#                             running the script the first time (default: 6)
#
# CLOUDSURF_SCRIPT_REPEAT     how many times to run the script per profile
#                             1 = run once, 0 = run forever, default: 1
#
# CLOUDSURF_SCRIPT_INTERVAL   seconds between repeated script runs (default: 60)
#
# CLOUDSURF_KEEPALIVE         "true" to auto-start keep-alive for every
#                             auto-launched profile (default: false)
#
# CLOUDSURF_KEEPALIVE_INTERVAL seconds between keep-alive ticks (default: 90)
#
AUTO_LAUNCH_IDS         = [x.strip() for x in os.environ.get("CLOUDSURF_AUTO_LAUNCH", "").split(",") if x.strip()]
AUTO_SCRIPT             = os.environ.get("CLOUDSURF_AUTO_SCRIPT", "").strip()
AUTO_SCRIPT_DELAY       = int(os.environ.get("CLOUDSURF_SCRIPT_DELAY", "6"))
AUTO_SCRIPT_REPEAT      = int(os.environ.get("CLOUDSURF_SCRIPT_REPEAT", "1"))   # 0 = infinite
AUTO_SCRIPT_INTERVAL    = int(os.environ.get("CLOUDSURF_SCRIPT_INTERVAL", "60"))
AUTO_KEEPALIVE          = os.environ.get("CLOUDSURF_KEEPALIVE", "").lower() == "true"
AUTO_KEEPALIVE_INTERVAL = int(os.environ.get("CLOUDSURF_KEEPALIVE_INTERVAL", "90"))
# CLOUDSURF_NOTEBOOK is passed straight through to the JS script via os.environ
AUTO_NOTEBOOK           = os.environ.get("CLOUDSURF_NOTEBOOK", "").strip()
SCRIPTS_DIR             = BASE_DIR / "scripts"
SCRIPTS_DIR.mkdir(exist_ok=True)

# ── Daily budget / Atlas session tracking ─────────────────────────────────────
#
# CLOUDSURF_MONGO_URI         MongoDB Atlas connection string (required for budget)
#                              e.g. "mongodb+srv://user:pass@cluster.mongodb.net/cloudsurf"
#
# CLOUDSURF_DAILY_BUDGET_HOURS  How many hours per 24-hour window this instance
#                              may run (default: 0 = unlimited / feature disabled)
#                              e.g. "7" → 7 hours per day
#
# CLOUDSURF_INSTANCE_NAME     Unique name for this instance so multiple Codespaces
#                              can share the same Atlas DB without colliding
#                              (default: hostname)
#                              e.g. "colab-worker-1"
#
MONGO_URI            = os.environ.get("CLOUDSURF_MONGO_URI", "").strip()
DAILY_BUDGET_HOURS   = float(os.environ.get("CLOUDSURF_DAILY_BUDGET_HOURS", "0"))
DAILY_BUDGET_SECONDS = DAILY_BUDGET_HOURS * 3600
INSTANCE_NAME        = os.environ.get("CLOUDSURF_INSTANCE_NAME", "").strip() or \
                       os.environ.get("CODESPACE_NAME", "").strip() or \
                       __import__("socket").gethostname()
BUDGET_ENABLED       = bool(MONGO_URI and DAILY_BUDGET_HOURS > 0)
HEARTBEAT_INTERVAL   = 60   # seconds between Atlas heartbeat writes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOGS_DIR / "manager.log")]
)
log = logging.getLogger("cloudsurf")

# ── Budget tracker ─────────────────────────────────────────────────────────────

class BudgetTracker:
    """
    Tracks cumulative runtime against a daily budget stored in MongoDB Atlas.

    Schema (collection: cloudsurf_sessions, one doc per instance):
    {
        "_id":          "<instance_name>",
        "window_start": <ISODate — start of the current 24-hour window>,
        "used_seconds": <float — seconds consumed in this window>,
        "last_heartbeat": <ISODate — last time this instance wrote>,
        "last_session_start": <ISODate — when the current run started>,
    }
    """

    def __init__(self):
        self._col      = None   # pymongo Collection, set in connect()
        self._start_ts = None   # float — time.time() when this run started
        self._stop_evt = threading.Event()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _connect(self):
        """Lazy-connect to Atlas. Returns True on success."""
        if self._col is not None:
            return True
        try:
            from pymongo import MongoClient
            client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
            db = client.get_default_database(default="cloudsurf")
            self._col = db["cloudsurf_sessions"]
            return True
        except Exception as e:
            log.error(f"[budget] Atlas connect failed: {e}")
            return False

    def _now_iso(self):
        return datetime.utcnow().isoformat() + "Z"

    def _elapsed_this_run(self):
        """Seconds elapsed since this process started."""
        if self._start_ts is None:
            return 0.0
        return time.time() - self._start_ts

    def _window_used(self, doc):
        """
        How many seconds of the budget have been consumed in this window,
        INCLUDING the time elapsed in the current run (not yet flushed to Atlas).
        """
        base = float(doc.get("used_seconds", 0))
        return base + self._elapsed_this_run()

    # ── Public API ────────────────────────────────────────────────────────────

    def startup_check(self):
        """
        Called once at startup. Returns (ok: bool, message: str).

        ok=False  → budget exhausted; caller should refuse to start.
        ok=True   → cleared to run; heartbeat thread should be started.
        """
        if not BUDGET_ENABLED:
            return True, "Budget tracking disabled (no MONGO_URI / DAILY_BUDGET_HOURS=0)"

        if not self._connect():
            # If Atlas is unreachable, fail open with a warning so a transient
            # network blip doesn't brick the instance permanently.
            log.warning("[budget] Could not reach Atlas — starting without budget enforcement")
            return True, "Atlas unreachable — running without budget enforcement"

        now = time.time()
        window_secs = 24 * 3600
        doc = self._col.find_one({"_id": INSTANCE_NAME})

        if doc is None:
            # First ever run — create the document
            self._col.insert_one({
                "_id":                INSTANCE_NAME,
                "window_start":       self._now_iso(),
                "used_seconds":       0.0,
                "last_heartbeat":     self._now_iso(),
                "last_session_start": self._now_iso(),
            })
            self._start_ts = now
            log.info(f"[budget] New instance '{INSTANCE_NAME}' — full budget of {DAILY_BUDGET_HOURS}h available")
            return True, f"New session — {DAILY_BUDGET_HOURS}h budget available"

        # Check whether the 24-hour window has expired
        try:
            from datetime import timezone
            ws_str = doc["window_start"].replace("Z", "+00:00")
            ws_dt  = datetime.fromisoformat(ws_str)
            window_age = datetime.now(timezone.utc) - ws_dt
            window_age_secs = window_age.total_seconds()
        except Exception as e:
            log.warning(f"[budget] Could not parse window_start: {e} — resetting window")
            window_age_secs = window_secs + 1   # force reset

        if window_age_secs >= window_secs:
            # Window expired → reset
            self._col.update_one({"_id": INSTANCE_NAME}, {"$set": {
                "window_start":       self._now_iso(),
                "used_seconds":       0.0,
                "last_heartbeat":     self._now_iso(),
                "last_session_start": self._now_iso(),
            }})
            self._start_ts = now
            log.info(f"[budget] 24h window expired — resetting. Full {DAILY_BUDGET_HOURS}h available")
            return True, f"24h window reset — {DAILY_BUDGET_HOURS}h budget available"

        # Window is current — check remaining budget
        used = float(doc.get("used_seconds", 0))
        remaining = DAILY_BUDGET_SECONDS - used
        hours_used      = used / 3600
        hours_remaining = remaining / 3600

        if remaining <= 0:
            msg = (f"Daily budget exhausted for '{INSTANCE_NAME}': "
                   f"{hours_used:.2f}h used of {DAILY_BUDGET_HOURS}h. "
                   f"Resets in {(window_secs - window_age_secs)/3600:.1f}h.")
            log.error(f"[budget] REFUSING TO START — {msg}")
            return False, msg

        # Crashed-and-came-back: resume remaining budget
        self._start_ts = now
        self._col.update_one({"_id": INSTANCE_NAME}, {"$set": {
            "last_heartbeat":     self._now_iso(),
            "last_session_start": self._now_iso(),
        }})
        log.info(f"[budget] '{INSTANCE_NAME}' resuming — {hours_used:.2f}h used, "
                 f"{hours_remaining:.2f}h remaining of {DAILY_BUDGET_HOURS}h")
        return True, (f"Resuming session — {hours_used:.2f}h used, "
                      f"{hours_remaining:.2f}h remaining")

    def status(self):
        """Return a JSON-serialisable dict for /api/status."""
        if not BUDGET_ENABLED:
            return {"enabled": False}

        elapsed = self._elapsed_this_run()
        out = {
            "enabled":        True,
            "instance":       INSTANCE_NAME,
            "budget_hours":   DAILY_BUDGET_HOURS,
            "elapsed_this_run_seconds": round(elapsed),
        }

        if not self._connect():
            out["error"] = "Atlas unreachable"
            return out

        doc = self._col.find_one({"_id": INSTANCE_NAME})
        if doc:
            used      = float(doc.get("used_seconds", 0)) + elapsed
            remaining = max(0.0, DAILY_BUDGET_SECONDS - used)
            out["used_seconds"]      = round(used)
            out["remaining_seconds"] = round(remaining)
            out["used_hours"]        = round(used / 3600, 3)
            out["remaining_hours"]   = round(remaining / 3600, 3)
            out["window_start"]      = doc.get("window_start")
            out["last_heartbeat"]    = doc.get("last_heartbeat")
        return out

    def _heartbeat_loop(self):
        """Background thread: write heartbeat + check budget every HEARTBEAT_INTERVAL s."""
        while not self._stop_evt.is_set():
            self._stop_evt.wait(timeout=HEARTBEAT_INTERVAL)
            if self._stop_evt.is_set():
                break
            self._tick()

    def _tick(self):
        """Single heartbeat: flush elapsed time to Atlas and check if budget exceeded."""
        if not BUDGET_ENABLED or self._start_ts is None:
            return
        if not self._connect():
            log.warning("[budget] Heartbeat skipped — Atlas unreachable")
            return

        elapsed = self._elapsed_this_run()

        try:
            # Read current used_seconds from Atlas, then update atomically
            doc = self._col.find_one({"_id": INSTANCE_NAME}, {"used_seconds": 1})
            base_used = float(doc.get("used_seconds", 0)) if doc else 0.0

            # Store only the portion elapsed since last heartbeat flush.
            # We track _start_ts and write the running total each tick.
            new_used = base_used + elapsed

            self._col.update_one({"_id": INSTANCE_NAME}, {"$set": {
                "used_seconds":   new_used,
                "last_heartbeat": self._now_iso(),
            }})

            # Reset _start_ts so next tick doesn't double-count
            self._start_ts = time.time()

            hours_used      = new_used / 3600
            hours_remaining = max(0, (DAILY_BUDGET_SECONDS - new_used)) / 3600
            log.info(f"[budget] Heartbeat — {hours_used:.3f}h used, "
                     f"{hours_remaining:.3f}h remaining")

            if new_used >= DAILY_BUDGET_SECONDS:
                log.warning(f"[budget] Budget exhausted! Initiating clean shutdown…")
                self._shutdown_system()

        except Exception as e:
            log.error(f"[budget] Heartbeat error: {e}")

    def _shutdown_system(self):
        """Stop all profiles then kill the process."""
        self._stop_evt.set()
        log.info("[budget] Stopping all profiles before exit…")
        for pid in list(sessions):
            try:
                stop_profile(pid)
            except Exception as e:
                log.warning(f"[budget] Error stopping {pid}: {e}")
        time.sleep(2)
        log.info("[budget] Clean shutdown complete — daily budget exhausted.")
        os.kill(os.getpid(), signal.SIGTERM)

    def start_heartbeat(self):
        """Start the background heartbeat thread."""
        if BUDGET_ENABLED:
            t = threading.Thread(target=self._heartbeat_loop, daemon=True)
            t.start()
            log.info(f"[budget] Heartbeat thread started (every {HEARTBEAT_INTERVAL}s)")

    def on_shutdown(self):
        """Call this from the SIGTERM/SIGINT handler to flush final elapsed time."""
        self._stop_evt.set()
        if not BUDGET_ENABLED or self._start_ts is None:
            return
        if not self._connect():
            return
        elapsed = self._elapsed_this_run()
        try:
            doc = self._col.find_one({"_id": INSTANCE_NAME}, {"used_seconds": 1})
            base = float(doc.get("used_seconds", 0)) if doc else 0.0
            self._col.update_one({"_id": INSTANCE_NAME}, {"$set": {
                "used_seconds":   base + elapsed,
                "last_heartbeat": self._now_iso(),
            }})
            log.info(f"[budget] Final flush: {(base + elapsed)/3600:.3f}h total used")
        except Exception as e:
            log.error(f"[budget] Final flush error: {e}")


budget = BudgetTracker()

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
    """
    Ensure NoVNC port is publicly reachable in Codespaces.

    Strategy 1: REST API with GITHUB_TOKEN (always available, no login needed).
    Strategy 2: gh CLI fallback.
    """
    import urllib.request, urllib.error

    time.sleep(2)  # let websockify bind first

    cs_name  = os.environ.get("CODESPACE_NAME")
    gh_token = os.environ.get("GITHUB_TOKEN")

    if not cs_name:
        return  # not in Codespaces

    # Strategy 1: REST API — GITHUB_TOKEN is auto-injected by Codespaces
    if gh_token:
        try:
            url = f"https://api.github.com/user/codespaces/{cs_name}/ports/{port}/visibility"
            payload = json.dumps({"visibility": "public"}).encode()
            req = urllib.request.Request(
                url, data=payload, method="PATCH",
                headers={
                    "Authorization": f"Bearer {gh_token}",
                    "Accept":        "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Content-Type":  "application/json",
                }
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                log.info(f"Port {port} set public via REST API (HTTP {r.status})")
            return
        except Exception as e:
            log.warning(f"REST API port forward failed for {port}: {e}")

    # Strategy 2: gh CLI
    try:
        r = subprocess.run(
            ["gh", "codespace", "ports", "visibility",
             f"{port}:public", "--codespace", cs_name],
            capture_output=True, timeout=15
        )
        if r.returncode == 0:
            log.info(f"Port {port} set public via gh CLI")
        else:
            log.warning(f"gh CLI failed for {port}: {r.stderr.decode()[:200]}")
    except Exception as e:
        log.warning(f"gh CLI port forward failed for {port}: {e}")

def start_profile(pid):
    if pid in sessions:
        return {"status": "already_running", **sessions[pid]["info"]}

    slot = free_slot()
    display   = DISPLAY_BASE + slot
    vnc_port  = VNC_BASE + slot
    novnc_port = NOVNC_BASE + slot
    cdp_port  = 9222 + slot   # 9222–9241, one per slot

    pdir = PROFILES_DIR / pid
    pdir.mkdir(parents=True, exist_ok=True)

    env = {**os.environ, "DISPLAY": f":{display}"}

    log.info(f"Starting {pid}: :{display} novnc={novnc_port} cdp={cdp_port}")

    # OPTIMIZATION 1: Dropped to 16-bit color (1280x900x16) to halve bandwidth
    # OPTIMIZATION 2: Added "+extension DAMAGE" so x11vnc doesn't have to poll manually
    xvfb = subprocess.Popen(["Xvfb", f":{display}", "-screen", "0", "1280x900x16", "-ac", "+extension", "DAMAGE"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)

    wm = subprocess.Popen(["openbox"], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # OPTIMIZATION 3: CPU-friendly x11vnc settings negotiated for cloud connections
    vnc = subprocess.Popen(["x11vnc", "-display", f":{display}", "-rfbport", str(vnc_port),
                             "-nopw", "-forever", "-shared", "-quiet",
                             "-xdamage",        # Only process pixels that actually change
                             "-wait",  "20",    # Limit to ~50 FPS to stop CPU starvation
                             "-defer", "20",    # Batch updates to save network overhead
                             "-cursor", "arrow", # Hardware/client-side cursor for instant mouse feel
                             "-tightfilexfer",  # Optimize for NoVNC/Tight encoding
                             ],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)

    ws_cmd = WEBSOCKIFY_CMD.split() + ["--web", NOVNC_PATH, str(novnc_port), f"localhost:{vnc_port}"]
    novnc = subprocess.Popen(ws_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)

    chrome = subprocess.Popen(
        [CHROME_BIN,
         f"--user-data-dir={pdir}/chrome",
         f"--remote-debugging-port={cdp_port}",
         f"--remote-debugging-address=127.0.0.1"]
        + CHROME_FLAGS
        + ["https://colab.research.google.com/"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    info = {"profile_id": pid, "display": f":{display}", "vnc_port": vnc_port,
            "novnc_port": novnc_port, "cdp_port": cdp_port,
            "started_at": datetime.now().isoformat(), "last_action": None}

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
    cdp_port = sess["info"].get("cdp_port", 9222)
    chrome = subprocess.Popen(
        [CHROME_BIN,
         f"--user-data-dir={pdir}/chrome",
         f"--remote-debugging-port={cdp_port}",
         f"--remote-debugging-address=127.0.0.1"]
        + CHROME_FLAGS,
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
        "status":  "ok", "x": x, "y": y,
        "action":  "rclick+esc+scroll",
        "zone":    "free",
        "ts":      datetime.now().strftime("%H:%M:%S")
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

# ── Script runner ─────────────────────────────────────────────────────────────

def run_script(pid: str, script_name: str, extra_env: dict | None = None) -> dict:
    """
    Run a Node.js script from the scripts/ directory against a running profile.

    The script receives these env vars:
      CLOUDSURF_CDP_URL     ws://127.0.0.1:<cdp_port>  (Puppeteer connectURL)
      CLOUDSURF_CDP_PORT    raw port number
      CLOUDSURF_PROFILE_ID  profile id string
      DISPLAY               the Xvfb display for this profile

    Returns a dict with keys: status, stdout, stderr, returncode
    """
    if pid not in sessions:
        return {"error": "profile not running"}

    # Accept "colab_run_all" or "colab_run_all.js"
    if not script_name.endswith(".js"):
        script_name += ".js"

    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        return {"error": f"script not found: {script_name}", "scripts_dir": str(SCRIPTS_DIR)}

    sess  = sessions[pid]
    info  = sess["info"]
    cdp_p = info.get("cdp_port", 9222)

    env = {
        **os.environ,
        "DISPLAY":              info["display"],
        "CLOUDSURF_CDP_PORT":   str(cdp_p),
        "CLOUDSURF_CDP_URL":    f"ws://127.0.0.1:{cdp_p}",
        "CLOUDSURF_PROFILE_ID": pid,
    }
    if extra_env:
        env.update(extra_env)

    log.info(f"[script] running {script_name} for {pid} (CDP port {cdp_p})")
    try:
        result = subprocess.run(
            ["node", str(script_path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,   # 5-minute hard cap; scripts can override via their own logic
        )
        log.info(f"[script] {script_name} for {pid} exited rc={result.returncode}")
        return {
            "status":     "ok" if result.returncode == 0 else "error",
            "script":     script_name,
            "returncode": result.returncode,
            "stdout":     result.stdout[-4000:],   # last 4 KB
            "stderr":     result.stderr[-2000:],
        }
    except subprocess.TimeoutExpired:
        return {"error": "script timed out (300s)"}
    except FileNotFoundError:
        return {"error": "node not found — run: apt install nodejs  OR  nvm install node"}
    except Exception as e:
        return {"error": str(e)}


def _script_repeat_worker(pid, script_name, repeat, interval):
    """
    Runs a script against a profile on a loop.
      repeat=1  → run once and stop
      repeat=N  → run N times with `interval` seconds between each
      repeat=0  → run forever until the profile stops
    The initial delay (CLOUDSURF_SCRIPT_DELAY) is handled by the caller
    before this thread is spawned.
    """
    run_count = 0
    while pid in sessions:
        run_count += 1
        log.info(f"[script-repeat] {pid} run #{run_count} of {'∞' if repeat == 0 else repeat}")
        result = run_script(pid, script_name)
        log.info(f"[script-repeat] {pid} run #{run_count} → {result.get('status')} rc={result.get('returncode')}")

        # Stop if we've hit the target (0 means infinite)
        if repeat != 0 and run_count >= repeat:
            log.info(f"[script-repeat] {pid} reached repeat limit ({repeat}) — done")
            break

        # Wait for next run, bailing early if profile stops
        slept = 0
        while slept < interval and pid in sessions:
            time.sleep(min(1, interval - slept))
            slept += 1


def _auto_launch_all():
    """
    Called once in a background thread after Flask binds.
    For each profile in CLOUDSURF_AUTO_LAUNCH:
      1. Launches the profile
      2. Optionally starts keep-alive (CLOUDSURF_KEEPALIVE=true)
      3. Optionally runs the script N times (CLOUDSURF_SCRIPT_REPEAT)
    """
    if not AUTO_LAUNCH_IDS:
        return

    # Wait for Flask to fully bind before we start launching
    for _ in range(20):
        time.sleep(0.5)
        try:
            import urllib.request
            urllib.request.urlopen("http://127.0.0.1:7860/api/status", timeout=1)
            break
        except Exception:
            continue

    log.info(f"[auto-launch] profiles: {AUTO_LAUNCH_IDS}")
    if AUTO_SCRIPT:
        repeat_label = "∞" if AUTO_SCRIPT_REPEAT == 0 else str(AUTO_SCRIPT_REPEAT)
        log.info(f"[auto-launch] script={AUTO_SCRIPT} repeat={repeat_label} interval={AUTO_SCRIPT_INTERVAL}s delay={AUTO_SCRIPT_DELAY}s")
    if AUTO_KEEPALIVE:
        log.info(f"[auto-launch] keep-alive=on interval={AUTO_KEEPALIVE_INTERVAL}s")

    # Stagger launches so ports/displays don't race
    for i, pid in enumerate(AUTO_LAUNCH_IDS):
        if i > 0:
            time.sleep(2)

        pdir = PROFILES_DIR / pid
        if not pdir.exists():
            log.warning(f"[auto-launch] profile '{pid}' not found — skipping")
            continue

        # Launch
        if pid in sessions:
            log.info(f"[auto-launch] {pid} already running — skipping launch")
        else:
            try:
                r = start_profile(pid)
                log.info(f"[auto-launch] {pid} → {r.get('status')}")
            except Exception as e:
                log.error(f"[auto-launch] {pid} failed to launch: {e}")
                continue

        # Auto keep-alive
        if AUTO_KEEPALIVE:
            _ka_active[pid] = True
            threading.Thread(target=ka_worker, args=(pid, AUTO_KEEPALIVE_INTERVAL), daemon=True).start()
            log.info(f"[auto-launch] keep-alive started for {pid} every {AUTO_KEEPALIVE_INTERVAL}s")

        # Auto script
        if AUTO_SCRIPT:
            log.info(f"[auto-launch] waiting {AUTO_SCRIPT_DELAY}s for Chrome to settle ({pid}) …")
            time.sleep(AUTO_SCRIPT_DELAY)
            # Spin up a dedicated thread per profile so profiles run scripts in parallel
            threading.Thread(
                target=_script_repeat_worker,
                args=(pid, AUTO_SCRIPT, AUTO_SCRIPT_REPEAT, AUTO_SCRIPT_INTERVAL),
                daemon=True
            ).start()


threading.Thread(target=_auto_launch_all, daemon=True).start()

# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)

@app.after_request
def add_headers(r):
    r.headers.pop("X-Frame-Options", None)
    r.headers["X-Frame-Options"] = "ALLOWALL"
    r.headers["Access-Control-Allow-Origin"] = "*"
    return r

@app.route("/manifest.json")
def manifest():
    return app.response_class(
        response=json.dumps({
            "name": "CloudSurf",
            "short_name": "CloudSurf",
            "description": "Persistent cloud browser profiles",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0a0a0a",
            "theme_color": "#0a0a0a",
            "orientation": "any",
            "icons": [
                {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}
            ]
        }),
        mimetype="application/manifest+json"
    )

@app.route("/icon.svg")
def icon_svg():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#0a0a0a"/><text x="256" y="340" font-family="-apple-system,Helvetica Neue,sans-serif" font-size="260" font-weight="700" fill="white" text-anchor="middle" letter-spacing="-8">C</text></svg>"""
    return app.response_class(response=svg, mimetype="image/svg+xml")

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
    d = request.json or {}
    name = d.get("name", "").strip()
    if not name: return jsonify({"error": "name required"}), 400
    pid = name.lower().replace(" ", "_").replace("@", "_at_")[:28] + "_" + str(int(time.time()))[-5:]
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
    d = request.json or {}
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

# ── Script routes ─────────────────────────────────────────────────────────────

@app.route("/api/profiles/<pid>/run-script", methods=["POST"])
def api_run_script(pid):
    """
    Run a named script against a running profile.

    Body (JSON):
      { "script": "colab_run_all" }          # .js extension optional

    Returns JSON with status, stdout, stderr, returncode.
    """
    d = request.json or {}
    script_name = d.get("script", "").strip()
    if not script_name:
        return jsonify({"error": "script name required"}), 400
    return jsonify(run_script(pid, script_name))


@app.route("/api/scripts", methods=["GET"])
def api_list_scripts():
    """List available .js scripts in the scripts/ directory."""
    scripts = sorted(p.name for p in SCRIPTS_DIR.glob("*.js"))
    return jsonify({"scripts": scripts, "scripts_dir": str(SCRIPTS_DIR)})

# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/profiles/<pid>/sendtext", methods=["POST"])
def api_sendtext(pid):
    if pid not in sessions: return jsonify({"error": "not running"}), 400
    text = (request.json or {}).get("text", "")
    if not text: return jsonify({"error": "no text"}), 400
    env = {**os.environ, "DISPLAY": sessions[pid]["info"]["display"]}
    r = subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "30", "--", text],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0: return jsonify({"error": r.stderr.strip()}), 500
    return jsonify({"status": "ok", "chars": len(text)})

@app.route("/api/profiles/<pid>/download", methods=["GET"])
def api_download(pid):
    import zipfile, io
    pdir = PROFILES_DIR / pid
    if not pdir.exists():
        return jsonify({"error": "profile not found"}), 404

    # Directories to skip — large caches that serve no purpose in a backup
    SKIP_DIRS = {
        "Cache", "Code Cache", "GPUCache", "DawnCache",
        "ShaderCache", "GrShaderCache", "DawnWebGPUCache",
        "WidevineCdm", "CrashPad", "Crashpad",
    }

    # File extensions to skip
    SKIP_EXTS = {".log", ".lock", ".tmp"}

    buf = io.BytesIO()
    skipped = 0
    added   = 0

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for item in pdir.rglob("*"):
            if not item.is_file():
                continue
            rel   = item.relative_to(pdir)
            parts = rel.parts

            # Skip if any path component is a cache dir
            if any(part in SKIP_DIRS for part in parts):
                skipped += 1
                continue

            # Skip by extension
            if item.suffix.lower() in SKIP_EXTS:
                skipped += 1
                continue

            try:
                zf.write(item, rel)
                added += 1
            except Exception as e:
                log.warning(f"zip skip {rel}: {e}")
                skipped += 1

    log.info(f"Download {pid}: {added} files added, {skipped} skipped")
    buf.seek(0)
    safe_name = pid.replace("/", "_")
    from flask import send_file
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{safe_name}.zip"
    )

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
                    "location": _server_location,
                    "budget": budget.status()})

@app.route("/api/budget", methods=["GET"])
def api_budget():
    return jsonify(budget.status())

def _shutdown(sig, frame):
    log.info("Shutting down...")
    budget.on_shutdown()
    for pid in list(sessions): stop_profile(pid)
    sys.exit(0)

signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

if __name__ == "__main__":
    # ── Budget startup check ──────────────────────────────────────────────────
    ok, msg = budget.startup_check()
    if not ok:
        # Print to both stdout and the log file so it's visible everywhere
        print(f"\n[CloudSurf] STARTUP BLOCKED — {msg}\n", flush=True)
        log.error(f"STARTUP BLOCKED — {msg}")
        sys.exit(1)
    if BUDGET_ENABLED:
        log.info(f"[budget] {msg}")
        budget.start_heartbeat()

    log.info(f"CloudSurf :{API_PORT} | chrome={CHROME_BIN} | novnc={NOVNC_PATH}")
    if AUTO_LAUNCH_IDS:
        log.info(f"Auto-launch enabled for: {AUTO_LAUNCH_IDS}")
    if AUTO_SCRIPT:
        log.info(f"Auto-script: {AUTO_SCRIPT} (delay {AUTO_SCRIPT_DELAY}s)")
    if AUTO_NOTEBOOK:
        log.info(f"Auto-notebook: {AUTO_NOTEBOOK}")
    app.run(host="0.0.0.0", port=API_PORT, debug=False, threaded=True)
