#!/usr/bin/env python3

"""CloudSurf - Profile Manager Backend"""

import os, sys, json, time, signal, shutil, subprocess, threading, logging, random

from pathlib import Path
from datetime import datetime, timezone
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

# ─────────────────────────────────────────────────────────────────────────────
# Runtime Budget  (MongoDB Atlas)
#
# Env vars:
#   MONGODB_URI              — Atlas connection string (required for budget)
#   CLOUDSURF_BUDGET_HOURS   — max hours per session (default: 7)
#   CLOUDSURF_INSTANCE_ID    — unique name for this instance (default: "default")
#
# MongoDB document shape (collection: cloudsurf.sessions):
#   {
#     instance_id:    "default",
#     started_at:     <ISODate>,        # set once when a fresh session begins
#     last_heartbeat: <ISODate>,        # updated every 60 s
#     budget_hours:   7,
#     status:         "running" | "stopped" | "budget_exceeded",
#     stopped_at:     <ISODate>         # set on clean shutdown
#   }
# ─────────────────────────────────────────────────────────────────────────────

MONGODB_URI        = os.environ.get("MONGODB_URI", "")
BUDGET_HOURS       = float(os.environ.get("CLOUDSURF_BUDGET_HOURS", "7"))
INSTANCE_ID        = os.environ.get("CLOUDSURF_INSTANCE_ID", "default")
HEARTBEAT_INTERVAL = 60   # seconds between heartbeat writes
_budget_col        = None  # pymongo Collection, set during init
_session_doc_id    = None  # _id of the active session document


def _mongo_connect():
    """Connect to MongoDB and return the sessions collection, or None on failure."""
    if not MONGODB_URI:
        log.info("[budget] MONGODB_URI not set — runtime budget disabled")
        return None
    try:
        from pymongo import MongoClient
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        col = client["cloudsurf"]["sessions"]
        log.info("[budget] MongoDB connected")
        return col
    except Exception as e:
        log.warning(f"[budget] MongoDB connection failed: {e} — budget disabled")
        return None


STALE_AFTER_HOURS = 24  # if last heartbeat is older than this, treat as a new day


def _budget_init():
    """
    Called once at startup.

    Session resume rules:
      1. No existing 'running' doc            → fresh session, full budget.
      2. Existing doc, last heartbeat < 24h ago, within budget → resume (clock continues).
      3. Existing doc, last heartbeat < 24h ago, over budget   → refuse to start.
      4. Existing doc, last heartbeat >= 24h ago (stale)       → reset: fresh session, full budget.
         The instance crashed/died long enough ago that it counts as a new day.
    """
    global _budget_col, _session_doc_id

    _budget_col = _mongo_connect()
    if _budget_col is None:
        return  # budget disabled

    now = datetime.now(timezone.utc)

    # Look for an existing running session for this instance
    existing = _budget_col.find_one({"instance_id": INSTANCE_ID, "status": "running"})

    if existing:
        # ── Stale check: how long since the last heartbeat? ───────────────────
        last_hb = existing.get("last_heartbeat", existing["started_at"])
        if last_hb.tzinfo is None:
            last_hb = last_hb.replace(tzinfo=timezone.utc)
        hours_since_hb = (now - last_hb).total_seconds() / 3600

        if hours_since_hb >= STALE_AFTER_HOURS:
            # Instance was dead for 24h+ — mark the old doc stale and start fresh
            log.info(
                f"[budget] Stale session detected — last heartbeat was "
                f"{hours_since_hb:.1f}h ago (>{STALE_AFTER_HOURS}h threshold). "
                "Resetting to a fresh session."
            )
            _budget_col.update_one(
                {"_id": existing["_id"]},
                {"$set": {"status": "stale", "stopped_at": now}}
            )
            existing = None  # fall through to fresh-session creation below

    if existing:
        # ── Active (non-stale) session found ──────────────────────────────────
        started_at = existing["started_at"]
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        elapsed_h = (now - started_at).total_seconds() / 3600
        budget_h  = existing.get("budget_hours", BUDGET_HOURS)

        if elapsed_h >= budget_h:
            log.warning(
                f"[budget] Session already exceeded budget "
                f"({elapsed_h:.2f}h / {budget_h}h) — refusing to start."
            )
            _budget_col.update_one(
                {"_id": existing["_id"]},
                {"$set": {"status": "budget_exceeded", "stopped_at": now}}
            )
            sys.exit(0)

        # Resume existing session
        _session_doc_id = existing["_id"]
        _budget_col.update_one(
            {"_id": _session_doc_id},
            {"$set": {"last_heartbeat": now}}
        )
        log.info(
            f"[budget] Resuming session — elapsed {elapsed_h:.2f}h "
            f"of {budget_h}h budget (instance={INSTANCE_ID})"
        )
    else:
        # Fresh session (either no doc existed, or the old one was stale)
        result = _budget_col.insert_one({
            "instance_id":    INSTANCE_ID,
            "started_at":     now,
            "last_heartbeat": now,
            "budget_hours":   BUDGET_HOURS,
            "status":         "running",
            "stopped_at":     None,
        })
        _session_doc_id = result.inserted_id
        log.info(
            f"[budget] New session started — budget={BUDGET_HOURS}h "
            f"(instance={INSTANCE_ID})"
        )


def _budget_heartbeat_worker():
    """
    Background thread: updates last_heartbeat every HEARTBEAT_INTERVAL seconds
    and triggers a graceful shutdown when the budget is exhausted.
    """
    if _budget_col is None or _session_doc_id is None:
        return

    while True:
        time.sleep(HEARTBEAT_INTERVAL)

        now = datetime.now(timezone.utc)

        try:
            doc = _budget_col.find_one({"_id": _session_doc_id})
            if doc is None:
                log.warning("[budget] Session doc disappeared from DB — shutting down")
                _graceful_shutdown("session_doc_missing")
                return

            started_at = doc["started_at"]
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)

            elapsed_h  = (now - started_at).total_seconds() / 3600
            budget_h   = doc.get("budget_hours", BUDGET_HOURS)
            remaining_h = budget_h - elapsed_h

            # Write heartbeat
            _budget_col.update_one(
                {"_id": _session_doc_id},
                {"$set": {"last_heartbeat": now}}
            )

            log.info(
                f"[budget] heartbeat — elapsed={elapsed_h:.2f}h "
                f"remaining={remaining_h:.2f}h budget={budget_h}h"
            )

            if elapsed_h >= budget_h:
                log.warning(
                    f"[budget] Budget exhausted ({elapsed_h:.2f}h >= {budget_h}h) "
                    "— initiating graceful shutdown"
                )
                _graceful_shutdown("budget_exceeded")
                return

        except Exception as e:
            log.warning(f"[budget] Heartbeat error: {e}")


def _budget_mark_stopped(reason: str = "stopped"):
    """Mark the session as stopped in MongoDB."""
    if _budget_col is None or _session_doc_id is None:
        return
    try:
        _budget_col.update_one(
            {"_id": _session_doc_id},
            {"$set": {"status": reason, "stopped_at": datetime.now(timezone.utc)}}
        )
        log.info(f"[budget] Session marked as '{reason}' in MongoDB")
    except Exception as e:
        log.warning(f"[budget] Failed to mark session stopped: {e}")


def _graceful_shutdown(reason: str = "shutdown"):
    """Stop all profiles, update MongoDB, then exit."""
    log.info(f"[budget] Graceful shutdown triggered (reason={reason})")
    for pid in list(sessions):
        try:
            stop_profile(pid)
        except Exception as e:
            log.warning(f"[budget] Error stopping profile {pid}: {e}")
    _budget_mark_stopped(reason)
    # Give Flask a moment to finish any in-flight requests before hard exit
    threading.Timer(2.0, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()


# ─────────────────────────────────────────────────────────────────────────────
# Existing helpers (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

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
    import urllib.request, urllib.error
    time.sleep(2)
    cs_name  = os.environ.get("CODESPACE_NAME")
    gh_token = os.environ.get("GITHUB_TOKEN")
    if not cs_name:
        return
    if gh_token:
        try:
            url     = f"https://api.github.com/user/codespaces/{cs_name}/ports/{port}/visibility"
            payload = json.dumps({"visibility": "public"}).encode()
            req     = urllib.request.Request(
                url, data=payload, method="PATCH",
                headers={
                    "Authorization": f"Bearer {gh_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Content-Type": "application/json",
                }
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                log.info(f"Port {port} set public via REST API (HTTP {r.status})")
            return
        except Exception as e:
            log.warning(f"REST API port forward failed for {port}: {e}")
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

    slot       = free_slot()
    display    = DISPLAY_BASE + slot
    vnc_port   = VNC_BASE + slot
    novnc_port = NOVNC_BASE + slot
    pdir       = PROFILES_DIR / pid
    pdir.mkdir(parents=True, exist_ok=True)

    env = {**os.environ, "DISPLAY": f":{display}"}
    log.info(f"Starting {pid}: :{display} novnc={novnc_port}")

    xvfb = subprocess.Popen(
        ["Xvfb", f":{display}", "-screen", "0", "1280x900x16", "-ac", "+extension", "DAMAGE"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.2)

    wm = subprocess.Popen(["openbox"], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    vnc = subprocess.Popen(
        ["x11vnc", "-display", f":{display}", "-rfbport", str(vnc_port),
         "-nopw", "-forever", "-shared", "-quiet",
         "-xdamage", "-wait", "20", "-defer", "20",
         "-cursor", "arrow", "-tightfilexfer"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)

    ws_cmd = WEBSOCKIFY_CMD.split() + ["--web", NOVNC_PATH, str(novnc_port), f"localhost:{vnc_port}"]
    novnc  = subprocess.Popen(ws_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)

    chrome = subprocess.Popen(
        [CHROME_BIN, f"--user-data-dir={pdir}/chrome"] + CHROME_FLAGS + ["https://colab.research.google.com/"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    info = {
        "profile_id": pid, "display": f":{display}", "vnc_port": vnc_port,
        "novnc_port": novnc_port, "started_at": datetime.now().isoformat(), "last_action": None
    }
    sessions[pid] = {
        "slot": slot, "xvfb": xvfb, "wm": wm, "vnc": vnc,
        "novnc": novnc, "chrome": chrome, "info": info
    }

    mf   = pdir / "meta.json"
    meta = json.loads(mf.read_text()) if mf.exists() else {"id": pid, "name": pid, "created_at": datetime.now().isoformat()}
    meta["last_started"] = datetime.now().isoformat()
    save_meta(pid, meta)

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
    env    = {**os.environ, "DISPLAY": sess["info"]["display"]}
    chrome = subprocess.Popen(
        [CHROME_BIN, f"--user-data-dir={pdir}/chrome"] + CHROME_FLAGS,
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sess["chrome"] = chrome
    return {"status": "restarted", "pid": chrome.pid}


def keepalive_click(pid):
    if pid not in sessions: return {"error": "not running"}
    sess = sessions[pid]
    env  = {**os.environ, "DISPLAY": sess["info"]["display"]}

    def run(cmd):
        subprocess.run(cmd, env=env, capture_output=True)

    x = random.randint(80, 1200)
    y = random.randint(80, 820)
    run(["xdotool", "mousemove", "--sync", str(x), str(y)])
    time.sleep(random.uniform(0.15, 0.35))
    run(["xdotool", "click", "--clearmodifiers", "3"])
    time.sleep(random.uniform(0.12, 0.25))
    run(["xdotool", "key", "--clearmodifiers", "Escape"])
    time.sleep(random.uniform(0.1, 0.2))
    ticks = random.randint(2, 5)
    for _ in range(ticks):
        run(["xdotool", "click", "--clearmodifiers", "4"])
        time.sleep(0.05)
    time.sleep(random.uniform(0.1, 0.2))
    for _ in range(ticks):
        run(["xdotool", "click", "--clearmodifiers", "5"])
        time.sleep(0.05)
    run(["xdotool", "mousemove", "--sync",
         str(x + random.randint(-6, 6)), str(y + random.randint(-6, 6))])

    result = {
        "status": "ok", "x": x, "y": y,
        "action": "rclick+esc+scroll",
        "zone":   "free",
        "ts":     datetime.now().strftime("%H:%M:%S")
    }
    sess["info"]["last_action"] = result
    return result


def ka_worker(pid, interval):
    log.info(f"KA started: {pid} every {interval}s")
    tick = 0
    while _ka_active.get(pid) and pid in sessions:
        tick += 1
        keepalive_click(pid)
        log.info(f"[ka] {pid} tick={tick} interval={interval}s")
        if tick % 5 == 0:
            sess = sessions.get(pid)
            if sess and sess.get("chrome") and sess["chrome"].poll() is not None:
                log.warning(f"[watchdog] {pid} Chrome died - restarting")
                restart_chrome(pid)
        slept = 0
        while slept < interval and _ka_active.get(pid):
            time.sleep(min(1, interval - slept))
            slept += 1


# ─────────────────────────────────────────────────────────────────────────────
# Flask app
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
            "name": "CloudSurf", "short_name": "CloudSurf",
            "description": "Persistent cloud browser profiles",
            "start_url": "/", "display": "standalone",
            "background_color": "#0a0a0a", "theme_color": "#0a0a0a",
            "orientation": "any",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]
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
    d    = request.json or {}
    name = d.get("name", "").strip()
    if not name: return jsonify({"error": "name required"}), 400
    pid  = name.lower().replace(" ", "_").replace("@", "_at_")[:28] + "_" + str(int(time.time()))[-5:]
    meta = {"id": pid, "name": name, "email": d.get("email", ""),
            "notes": d.get("notes", ""), "created_at": datetime.now().isoformat()}
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
    interval = max(5, int(d.get("interval", 90)))
    _ka_active[pid] = False
    time.sleep(0.2)
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


@app.route("/api/profiles/<pid>/download", methods=["GET"])
def api_download(pid):
    import zipfile, io
    pdir = PROFILES_DIR / pid
    if not pdir.exists(): return jsonify({"error": "profile not found"}), 404
    SKIP_DIRS = {
        "Cache", "Code Cache", "GPUCache", "DawnCache",
        "ShaderCache", "GrShaderCache", "DawnWebGPUCache",
        "WidevineCdm", "CrashPad", "Crashpad",
    }
    SKIP_EXTS = {".log", ".lock", ".tmp"}
    buf     = io.BytesIO()
    skipped = 0
    added   = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for item in pdir.rglob("*"):
            if not item.is_file(): continue
            rel   = item.relative_to(pdir)
            parts = rel.parts
            if any(part in SKIP_DIRS for part in parts):
                skipped += 1; continue
            if item.suffix.lower() in SKIP_EXTS:
                skipped += 1; continue
            try:
                zf.write(item, rel); added += 1
            except Exception as e:
                log.warning(f"zip skip {rel}: {e}"); skipped += 1
    log.info(f"Download {pid}: {added} files added, {skipped} skipped")
    buf.seek(0)
    safe_name = pid.replace("/", "_")
    from flask import send_file
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{safe_name}.zip")


@app.route("/api/sessions", methods=["GET"])
def api_sessions():
    return jsonify({pid: {**s["info"], "ka_active": _ka_active.get(pid, False)}
                    for pid, s in sessions.items()})


@app.route("/api/budget", methods=["GET"])
def api_budget():
    """Return current runtime budget status."""
    if _budget_col is None or _session_doc_id is None:
        return jsonify({"enabled": False, "message": "Budget tracking disabled (no MONGODB_URI)"})
    try:
        doc        = _budget_col.find_one({"_id": _session_doc_id})
        if doc is None:
            return jsonify({"enabled": True, "error": "session doc not found"}), 404
        started_at = doc["started_at"]
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        now        = datetime.now(timezone.utc)
        elapsed_h  = (now - started_at).total_seconds() / 3600
        budget_h   = doc.get("budget_hours", BUDGET_HOURS)
        return jsonify({
            "enabled":      True,
            "instance_id":  INSTANCE_ID,
            "started_at":   doc["started_at"].isoformat(),
            "elapsed_hours": round(elapsed_h, 3),
            "budget_hours":  budget_h,
            "remaining_hours": round(max(0, budget_h - elapsed_h), 3),
            "status":        doc.get("status"),
        })
    except Exception as e:
        return jsonify({"enabled": True, "error": str(e)}), 500


_server_location = {"city": "—", "region": "—", "country": "—"}


def _fetch_server_location():
    global _server_location
    try:
        import urllib.request as ur
        with ur.urlopen("https://ipapi.co/json/", timeout=6) as r:
            d = json.loads(r.read())
            _server_location = {
                "city": d.get("city") or "—",
                "region": d.get("region_code") or "—",
                "country": d.get("country_name") or "—"
            }
        log.info(f"Server location: {_server_location}")
    except Exception as e:
        log.warning(f"Location fetch 1 failed: {e}")
        try:
            import urllib.request as ur
            with ur.urlopen("http://ip-api.com/json/?fields=city,regionName,country", timeout=6) as r:
                d = json.loads(r.read())
                _server_location = {
                    "city": d.get("city") or "—",
                    "region": d.get("regionName") or "—",
                    "country": d.get("country") or "—"
                }
        except Exception as e2:
            log.warning(f"Location fetch 2 failed: {e2}")


threading.Thread(target=_fetch_server_location, daemon=True).start()


@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({
        "running":    len(sessions),
        "chrome_bin": CHROME_BIN,
        "novnc_path": NOVNC_PATH,
        "time":       datetime.now().isoformat(),
        "location":   _server_location,
    })


def _shutdown(sig, frame):
    log.info("Shutting down...")
    for pid in list(sessions):
        stop_profile(pid)
    _budget_mark_stopped("stopped")
    sys.exit(0)


signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


if __name__ == "__main__":
    # ── Budget init (exits here if budget already exceeded) ───────────────────
    _budget_init()

    # ── Start heartbeat thread ────────────────────────────────────────────────
    threading.Thread(target=_budget_heartbeat_worker, daemon=True).start()

    log.info(f"CloudSurf :{API_PORT} | chrome={CHROME_BIN} | novnc={NOVNC_PATH} | "
             f"budget={'disabled' if not MONGODB_URI else f'{BUDGET_HOURS}h'}")
    app.run(host="0.0.0.0", port=API_PORT, debug=False, threaded=True)
