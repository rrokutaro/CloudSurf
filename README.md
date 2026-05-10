# CloudSurf

Run multiple isolated Chrome browser profiles in the cloud — accessible from any device, always on, with automatic keep-alive.

![Banner](https://github.com/rrokutaro/CloudSurf/raw/main/banner.png)

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

### 2. Setup

Runs automatically on first launch via `devcontainer.json`. To run manually:

```
bash setup.sh
```

### 3. Start CloudSurf

```
bash start.sh
```

### 4. Open the UI

* Go to the **Ports** tab in VS Code
* Port **7860** → click the globe icon
* Ports **6080–6089** are forwarded automatically when profiles launch

---

## Deployment (Google Cloud Run)

CloudSurf can be deployed to Cloud Run for reliable, always-available hosting without GitHub Actions timing issues.

Set the following environment variables on your Cloud Run service:

| Variable | Description | Default |
| --- | --- | --- |
| `MONGODB_URI` | MongoDB Atlas connection string (`mongodb+srv://...`) | — |
| `CLOUDSURF_BUDGET_HOURS` | Max hours CloudSurf runs before auto-shutdown | `7` |
| `CLOUDSURF_INSTANCE_ID` | Unique name for this instance in the database | `default` |

`MONGODB_URI` is required to enable the runtime budget. Without it, CloudSurf runs indefinitely with no auto-shutdown.

---

## Runtime Budget

CloudSurf uses MongoDB Atlas to track how long it has been running and automatically shut itself down once the daily budget is reached. This prevents wasted compute when running on always-on infrastructure like Cloud Run.

**How it works:**

On startup, CloudSurf checks the database for an existing session:

* **No session found** → starts fresh, full budget available
* **Session found, last heartbeat < 24h ago, within budget** → resumes from where it left off (e.g. if 2h were used before a restart, 2h are still counted)
* **Session found, last heartbeat < 24h ago, budget exceeded** → refuses to start and exits immediately
* **Session found, last heartbeat ≥ 24h ago** → treats the old session as stale (instance was dead long enough that it counts as a new day), resets and starts fresh with a full budget

Once running, a background thread writes a heartbeat to the database every 60 seconds and checks elapsed time. When `elapsed >= budget_hours`, all profiles are stopped and the process exits cleanly.

**Session document shape** (MongoDB collection: `cloudsurf.sessions`):

```json
{
  "instance_id":    "default",
  "started_at":     "2026-05-10T06:00:00Z",
  "last_heartbeat": "2026-05-10T08:34:00Z",
  "budget_hours":   7,
  "status":         "running",
  "stopped_at":     null
}
```

Possible `status` values: `running`, `stopped`, `budget_exceeded`, `stale`.

**Budget API endpoint:**

```
GET /api/budget
```

```json
{
  "enabled": true,
  "instance_id": "default",
  "started_at": "2026-05-10T06:00:00Z",
  "elapsed_hours": 3.42,
  "budget_hours": 7,
  "remaining_hours": 3.58,
  "status": "running"
}
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

### Downloading a profile

Click the **↓** button next to any profile in the sidebar to download its Chrome data as a `.zip`. This includes cookies, localStorage, session tokens, and login state — everything needed to restore the session elsewhere. Cache directories are excluded to keep the file small.

### Paste text

With a profile running, click **⊕ Paste Text** to inject text directly into the browser as keystrokes — useful for entering credentials without typing manually.

---

## Mobile

CloudSurf is designed to work fully on mobile:

* **Fullscreen mode** — tap ⛶ in the top-right corner. On iOS/Android this removes all browser chrome for a native app feel
* **Install as app** — on iOS: Safari → Share → Add to Home Screen. On Android: Chrome menu → Install app
* **Dark / Light mode** — tap ◑ to toggle. Preference is saved across sessions

---

## Architecture

```
┌──────────────────────────────────────────────┐
│           Cloud Run / Codespace              │
│                                              │
│  Flask Manager  :7860  (CloudSurf UI + API)  │
│                                              │
│  Per profile:                                │
│    Xvfb :{10+n}        virtual display       │
│    x11vnc :{5900+n}    VNC server            │
│    websockify :{6080+n} → NoVNC in browser   │
│    Chrome  --user-data-dir=profiles/{id}     │
│                                              │
│  Keep-alive thread  →  xdotool               │
│  Budget thread      →  MongoDB Atlas         │
│  Port auto-forward  →  GitHub REST API       │
└──────────────────────────────────────────────┘
            ↑ access from any browser
```

---

## Resource Guide

| Machine | RAM | Profiles | Notes |
| --- | --- | --- | --- |
| 2-core | 8 GB | 3–4 | GitHub free tier |
| 4-core | 16 GB | 6–8 | Recommended |
| 8-core | 32 GB | 12–15 | Maximum throughput |

GitHub free tier: **120 core-hours / month** = 60h on 2-core, 30h on 4-core.

---

## Ports Reference

| Port | Service |
| --- | --- |
| 7860 | CloudSurf UI + API |
| 6080–6089 | NoVNC — one per profile slot |
| 5900–5909 | Raw VNC — internal only, do not expose |

---

## File Structure

```
cloudsurf/
├── setup.sh              # install dependencies
├── start.sh              # start manager
├── stop.sh               # stop everything
├── manager.py            # Flask API + process manager + runtime budget
├── gdrive_sync.py        # Google Drive profile backup/restore
├── index.html            # single-file web UI
├── profiles/             # Chrome user data + metadata per profile
├── logs/                 # manager logs
└── .devcontainer/
    └── devcontainer.json # Codespaces config — auto-setup + port forwarding
```

---

## Tips

* **Profile data persists** between sessions — logins survive restarts as long as the profiles directory is not deleted
* **Don't exceed RAM** — Chrome uses ~1–1.5 GB per profile; going over causes silent crashes
* **Keep-Alive interval** — 10s is aggressive but reliable; raise to 30–60s for lighter activity signals
* **Backup before stopping** — use the **↓** download button per profile, or run `python3 gdrive_sync.py backup` for bulk export
* **VNC performance** — x11vnc is tuned for low latency; NoVNC runs at max quality with no compression
* **Budget resets daily** — if the instance has been dead for 24+ hours, the budget resets automatically on next startup
