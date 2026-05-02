# CloudSurf

Run multiple isolated Chrome browser profiles in the cloud — accessible from any device, always on, with automatic keep-alive and Puppeteer automation.

[![Banner](https://github.com/rrokutaro/CloudSurf/raw/main/banner.png)](banner.png)

## Use Cases

**Free compute & AI tools**

* Stack free GPU hours across multiple Google Colab accounts — 8 accounts × 5h = 40h of free compute daily
* Run Hugging Face Spaces, Kaggle notebooks, or Lightning.ai sessions simultaneously across accounts
* Keep long-running AI training jobs alive without babysitting them from your local machine
* Use free-tier tools that limit one session per account — run one per profile

**Accounts & identity**

* Manage multiple Gmail, Twitter/X, LinkedIn, Reddit, or TikTok accounts without logging in and out
* Run separate browser identities with isolated cookies, localStorage, and session tokens — no bleedthrough
* Handle client social media accounts from a single dashboard without mixing credentials
* Test referral flows, signup funnels, or onboarding sequences from fresh account states

**Automation & scraping**

* Keep browser-based scrapers running 24/7 without a local machine staying on
* Rotate through logged-in sessions for platforms that block headless browsers
* Monitor dashboards, prices, or feeds across multiple accounts in parallel
* Run multi-step manual flows that are too fragile to automate with scripts

**Development & QA**

* Test multi-user features — collaboration, permissions, messaging — with real simultaneous sessions
* Reproduce logged-in edge cases without polluting your local browser state
* Run cross-account regression tests without needing a local Selenium grid
* Keep staging environments open and authenticated for the whole team

**Remote & mobile access**

* Access a full desktop browser from your phone, tablet, or any device with just a browser
* Use web tools that don't work well on mobile by running them in a cloud desktop browser
* Share a browser session URL with a teammate for collaborative debugging
* Access geo-restricted tools by spinning up a Codespace in a specific region

**Always-on sessions**

* Keep web apps that log you out after inactivity permanently authenticated
* Run long form-filling, upload, or download tasks that would time out on your machine
* Leave a browser parked on a page that needs periodic interaction — keep-alive handles it automatically
* Use as a persistent remote desktop for any browser-based workflow

---

## Quick Start (GitHub Codespaces)

### 1. Create a Codespace

* Fork or clone this repo to your GitHub account
* Click **Code → Codespaces → Create codespace on main**
* Choose a **4-core** machine for best results (free tier: 2-core works for 3–4 profiles)
* Pick a **US East** region for best Colab GPU availability

Everything else is automatic — setup, dependency install, and manager start all happen on boot via `devcontainer.json`.

### 2. Open the UI

* Go to the **Ports** tab in VS Code
* Port **7860** → click the globe icon
* Ports **6080–6089** are forwarded automatically when profiles launch

### 3. Manual start (if needed)

```bash
bash setup.sh   # install dependencies (runs automatically on first boot)
bash start.sh   # start the manager
```

---

## Using CloudSurf

### Creating a profile

1. Tap **+** to open the new profile sheet
2. Enter a name, email, and optional notes
3. Tap **Create Profile**

### Launching a profile

1. Select the profile from the sidebar
2. Click **▸ Launch**
3. Click **↗ Open Browser** to open the VNC session in a new tab
4. Log into whatever service you need — session is saved to disk

### Keep-Alive

Prevents idle timeouts by simulating human activity (right-click + scroll) at a set interval.

1. With a profile running, open the **Keep-Alive** card
2. Set an interval (default: 10 seconds)
3. Click **▸ Start**

The activity log shows each event with timestamp and coordinates.

Keep-alive can also run automatically on startup — see [Automation & Secrets](#automation--secrets) below.

### Downloading a profile

Click the **↓** button next to any profile in the sidebar to download its Chrome data as a `.zip`. This includes cookies, localStorage, session tokens, and login state — everything needed to restore the session elsewhere. Cache directories are excluded to keep the file small.

### Paste text

With a profile running, click **⊕ Paste Text** to inject text directly into the browser as keystrokes — useful for entering credentials without typing manually.

---

## Automation & Secrets

CloudSurf supports fully hands-free operation via Codespace secrets. Set these once and every Codespace you spin up will auto-launch profiles, open notebooks, run scripts, and keep sessions alive — no manual steps.

### Where to set secrets

**GitHub.com → Settings → Codespaces → Codespace secrets**

Make sure to grant each secret access to your CloudSurf repo.

### Finding your profile IDs

Profile IDs are the folder names inside `profiles/` — they look like `alice_12345`. You can also see them in the UI sidebar or in `profiles/<id>/meta.json`.

---

### Secret Reference

#### Auto-launch

| Secret | Default | Description |
|---|---|---|
| `CLOUDSURF_AUTO_LAUNCH` | *(unset = off)* | Comma-separated profile IDs to launch automatically when the Codespace starts. e.g. `alice_12345,bob_67890` |

#### Automation script

| Secret | Default | Description |
|---|---|---|
| `CLOUDSURF_AUTO_SCRIPT` | *(unset = off)* | Script filename (without `.js`) from the `scripts/` folder to run against each auto-launched profile. e.g. `colab_run_all` |
| `CLOUDSURF_NOTEBOOK` | *(unset = skip)* | Notebook name to click in the Colab file picker, e.g. `myproject.ipynb`. The script finds it by text in the picker, handles any "leave page" browser dialog, waits for the notebook to load, then runs it. Leave unset to skip straight to Run all (if notebook is already open). |
| `CLOUDSURF_SCRIPT_DELAY` | `6` | Seconds to wait after Chrome launches before running the script the first time. Gives Chrome time to load Colab before Puppeteer connects. |
| `CLOUDSURF_SCRIPT_REPEAT` | `1` | How many times to run the script per profile. `1` = run once. `0` = run forever until the profile stops. Any other number = run that many times. |
| `CLOUDSURF_SCRIPT_INTERVAL` | `60` | Seconds to wait between repeated script runs. Only relevant when `CLOUDSURF_SCRIPT_REPEAT` is `0` or greater than `1`. |

#### Keep-alive

| Secret | Default | Description |
|---|---|---|
| `CLOUDSURF_KEEPALIVE` | `false` | Set to `true` to automatically start keep-alive for every auto-launched profile. Simulates human activity (right-click + scroll) to prevent idle disconnects. |
| `CLOUDSURF_KEEPALIVE_INTERVAL` | `90` | Seconds between keep-alive ticks. `10`–`30` is aggressive. `60`–`90` is lighter. |

---

### Example Configurations

**Run a Colab notebook once on startup, keep session alive:**
```
CLOUDSURF_AUTO_LAUNCH      = alice_12345
CLOUDSURF_AUTO_SCRIPT      = colab_run_all
CLOUDSURF_NOTEBOOK         = myproject.ipynb
CLOUDSURF_SCRIPT_DELAY     = 8
CLOUDSURF_KEEPALIVE        = true
CLOUDSURF_KEEPALIVE_INTERVAL = 90
```

**Run across 3 accounts, re-run the notebook every 5 minutes indefinitely:**
```
CLOUDSURF_AUTO_LAUNCH      = alice_12345,bob_67890,carol_11111
CLOUDSURF_AUTO_SCRIPT      = colab_run_all
CLOUDSURF_NOTEBOOK         = myproject.ipynb
CLOUDSURF_SCRIPT_DELAY     = 8
CLOUDSURF_SCRIPT_REPEAT    = 0
CLOUDSURF_SCRIPT_INTERVAL  = 300
CLOUDSURF_KEEPALIVE        = true
CLOUDSURF_KEEPALIVE_INTERVAL = 90
```

**Just auto-launch profiles with keep-alive, no scripting:**
```
CLOUDSURF_AUTO_LAUNCH      = alice_12345,bob_67890
CLOUDSURF_KEEPALIVE        = true
```

---

### Writing Custom Scripts

Drop any `.js` file into the `scripts/` folder. CloudSurf injects these environment variables automatically when running a script:

| Variable | Description |
|---|---|
| `CLOUDSURF_CDP_PORT` | Chrome DevTools Protocol port for this profile (e.g. `9222`) |
| `CLOUDSURF_CDP_URL` | Full WebSocket URL for Puppeteer, e.g. `ws://127.0.0.1:9222` |
| `CLOUDSURF_PROFILE_ID` | The profile ID string |
| `DISPLAY` | The Xvfb display for this profile |
| All Codespace secrets | Including `CLOUDSURF_NOTEBOOK` and any others you've set |

Connect Puppeteer like this:

```js
const puppeteer = require('puppeteer-core');
const browser = await puppeteer.connect({
  browserURL: `http://127.0.0.1:${process.env.CLOUDSURF_CDP_PORT}`,
  defaultViewport: null,
});
// ... your automation ...
await browser.disconnect(); // never .close() — you don't own the browser
```

Run a script manually via the API:

```bash
curl -X POST http://localhost:7860/api/profiles/<profile_id>/run-script \
     -H "Content-Type: application/json" \
     -d '{"script": "colab_run_all"}'

# List available scripts
curl http://localhost:7860/api/scripts
```

---

## Mobile

CloudSurf is designed to work fully on mobile:

* **Fullscreen mode** — tap ⛶ in the top-right corner. On iOS/Android this removes all browser chrome for a native app feel
* **Install as app** — on iOS: Safari → Share → Add to Home Screen. On Android: Chrome menu → Install app
* **Dark / Light mode** — tap ◑ to toggle. Preference is saved across sessions

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│               GitHub Codespace                  │
│                                                 │
│  Flask Manager  :7860  (CloudSurf UI + API)     │
│                                                 │
│  Per profile:                                   │
│    Xvfb :{10+n}          virtual display        │
│    x11vnc :{5900+n}      VNC server             │
│    websockify :{6080+n}  NoVNC in browser       │
│    Chrome  --user-data-dir=profiles/{id}        │
│            --remote-debugging-port={9222+n}     │
│                                                 │
│  Keep-alive thread  →  xdotool                  │
│  Script runner      →  Node.js + puppeteer-core │
│  Port auto-forward  →  GitHub REST API          │
│  Codespace keep-alive → ping every 4 min        │
└─────────────────────────────────────────────────┘
             ↑ access from any browser
```

---

## Resource Guide

| Machine | RAM | Profiles | Notes |
|---|---|---|---|
| 2-core | 8 GB | 3–4 | GitHub free tier |
| 4-core | 16 GB | 6–8 | Recommended |
| 8-core | 32 GB | 12–15 | Maximum throughput |

GitHub free tier: **120 core-hours / month** = 60h on 2-core, 30h on 4-core.

---

## Ports Reference

| Port | Service |
|---|---|
| 7860 | CloudSurf UI + API |
| 6080–6089 | NoVNC — one per profile slot |
| 5900–5909 | Raw VNC — internal only, do not expose |
| 9222–9241 | Chrome CDP — internal only, used by scripts |

---

## File Structure

```
cloudsurf/
├── setup.sh              # install dependencies
├── start.sh              # start manager + keep-alive watchdog
├── stop.sh               # stop everything
├── manager.py            # Flask API + process manager + automation
├── gdrive_sync.py        # Google Drive profile backup/restore
├── index.html            # single-file web UI
├── profiles/             # Chrome user data + metadata per profile
├── logs/                 # manager + keepalive logs
├── scripts/              # Puppeteer automation scripts
│   ├── colab_run_all.js  # open a notebook and click Run all
│   ├── example_navigate.js # template for custom scripts
│   └── package.json      # puppeteer-core dependency
└── .devcontainer/
    └── devcontainer.json # Codespaces config — auto-setup, secrets, port forwarding
```

---

## Tips

* **Profile data persists** between Codespace sessions — logins survive restarts as long as you don't delete the Codespace
* **Don't exceed RAM** — Chrome uses ~1–1.5 GB per profile; going over causes silent crashes
* **Keep-Alive interval** — 10s is aggressive but reliable; raise to 60–90s for lighter activity signals
* **Backup before stopping** — use the **↓** download button per profile, or run `python3 gdrive_sync.py backup` for bulk export
* **VNC performance** — x11vnc is tuned for low latency; NoVNC runs at max quality with no compression
* **Script logs** — check `logs/manager.log` to see script run results, repeat counts, and any errors
* **Updating** — pull new changes in an existing Codespace with `git pull && bash stop.sh && bash start.sh`
