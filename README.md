# ☁️ CloudSurf — Free Cloud Browser Farm

Run multiple isolated Chrome browser profiles in the cloud with a web UI you can access from **anywhere** — including your phone. Built for Codespaces, works on any Ubuntu server.

---

## The Idea

Colab gives ~5h free GPU per account per day. With multiple Gmail accounts, you stack that:

```
8 accounts × 5h = 40h free GPU compute per day
```

CloudSurf lets you spin up one Chrome profile per account, log into Colab, run your notebooks — all from a browser, no local PC needed. An anti-disconnect keep-alive prevents idle timeouts.

---

## Quick Start (GitHub Codespaces)

### 1. Create a Codespace
- Fork/clone this repo to your GitHub
- Click **Code → Codespaces → Create codespace on main**
- Choose **4-core** machine for best results (free tier: 2-core)
- Region: pick **US East** for best Colab GPU availability

### 2. Setup (runs automatically, or manually)
```bash
bash setup.sh
```

### 3. Start CloudSurf
```bash
bash start.sh
```

### 4. Open the UI
- Codespaces will show a **ports** tab
- Forward port **7860** → click the globe icon to open in browser
- Also forward ports **6080–6090** for the VNC panels

---

## Usage

### Creating profiles
1. In the UI, enter a name (e.g. "Gmail #3") and email
2. Click **Create Profile**
3. Click **▸ Launch** to start the browser
4. The VNC panel embeds directly — you see the Chrome window
5. Log into Google / Colab in that browser
6. Enable **Keep-Alive** (recommended: 90s interval)

### Running Colab on each profile
1. Launch all profiles (up to ~8–10 before RAM gets tight on 4-core)
2. In each VNC window: go to `colab.research.google.com`
3. Upload your notebook or open from Drive
4. Run all cells
5. Enable keep-alive per profile — it randomly moves the mouse + scrolls to simulate activity

### Saving profiles to Google Drive
```bash
# Set up once:
export GDRIVE_CREDS_PATH=/path/to/your/oauth_creds.json
python3 gdrive_sync.py backup    # saves all profiles
python3 gdrive_sync.py restore   # restores from Drive
```

---

## Architecture

```
┌─────────────────────────────────────────────┐
│              GitHub Codespace               │
│                                             │
│  Flask Manager (:7860)                      │
│       ↓                                     │
│  Per profile:                               │
│    Xvfb :10 → x11vnc :5900                 │
│    websockify :6080 → NoVNC (in browser)    │
│    Chrome --user-data-dir=profiles/X/chrome │
│                                             │
│  Keep-alive thread → xdotool mousemove      │
└─────────────────────────────────────────────┘
         ↑ access from phone/PC via browser
```

---

## Resource Guide (Codespaces)

| Machine | RAM  | Profiles | Notes |
|---------|------|----------|-------|
| 2-core  | 8GB  | ~3–4     | Free tier |
| 4-core  | 16GB | ~6–8     | Recommended |
| 8-core  | 32GB | ~12–15   | Best |

GitHub free tier: **120 core-hours/month** = 60h on 2-core or 30h on 4-core.

---

## Ports Reference

| Port | Service |
|------|---------|
| 7860 | CloudSurf UI + API |
| 6080 | NoVNC profile slot 0 |
| 6081 | NoVNC profile slot 1 |
| ... | ... |
| 5900+ | Raw VNC (internal only) |

---

## Tips

- **Region matters**: US servers get better Colab GPUs (T4 vs K80)
- **Don't run >10 profiles** on 16GB RAM — Chrome is hungry
- **Persist profiles**: Chrome user data is saved in `profiles/`. The Codespace persists between sessions unless you delete it
- **Google Drive backup**: Run `gdrive_sync.py backup` to save profiles before stopping the codespace
- **Phone access**: The NoVNC UI works great on mobile — pinch to zoom, tap to click

---

## Files

```
cloudsurf/
├── setup.sh          # install dependencies
├── start.sh          # start manager
├── stop.sh           # stop everything
├── manager.py        # Flask API + process manager
├── gdrive_sync.py    # Google Drive backup/restore
├── ui/
│   └── index.html    # Single-file web UI
├── profiles/         # Chrome user data dirs + metadata
├── logs/             # Manager logs
└── .devcontainer/
    └── devcontainer.json  # Codespaces auto-config
```
