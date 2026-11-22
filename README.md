# Elo54 Pulse — Autonomous Facebook & WhatsApp Channel News Agent

> Connect your WhatsApp at [`/admin/whatsapp-qr`](/admin/whatsapp-qr)

An end-to-end autonomous news scraping, organization, and publishing platform built with **Flask**, **Playwright (Chromium)**, and **SQLite**.

The system monitors unlimited **Public Facebook Pages** and **WhatsApp Channels** on a **5-minute Auto-Pilot cycle**, extracts newly published posts (clean text, photos, link preview thumbnails, and external URLs), organizes them into structured **Elo54 Pulse** fields (*Category*, *Campus/City/Location*, *Headline*, *Details*, *Image*, and *Source / Registration Link*), and publishes them through the **`+ Post News / Event`** modal (`"Shine a Light on Your Campus or City"`).

---

## 1. Architecture & File Map

```text
elo age/
├── app.py                # Flask web server, watchlist & feed routes, JSON API, 5-min Auto-Pilot thread
├── agent.py              # Playwright scraper (Facebook + WhatsApp), organizer, deduplicator & modal publisher
├── elo54_agent.py        # Standalone helper script to organize & prefill posts on https://elo54.com
├── requirements.txt      # Python package dependencies
├── .env.example          # Environment variable template
├── Dockerfile            # Cloud Linux container image (Playwright + Xvfb virtual display)
├── render.yaml           # Render Web Service deployment blueprint
├── .dockerignore         # Docker build context exclusions
├── .gitignore            # Git exclusions (.env, fb_profile/, *.db, runtime uploads)
├── README.md             # Complete system documentation
├── templates/
│   ├── index.html        # Elo54 Pulse dashboard, "+ Post News / Event" modal, hidden admin QR modal, live feed
│   └── login.html        # Legacy login template (routes redirect directly to frontpage)
├── sandbox_posts.db      # SQLite DB: posts, watched_pages (FB & WA URLs), and settings (created at runtime)
├── agent_memory.db       # SQLite DB: seen_posts (SHA-256 hashes to block duplicate reposts, created at runtime)
├── fb_profile/           # Persistent Chromium browser profile (stores your linked WhatsApp Web session)
└── static/
    ├── uploads/          # Saved images downloaded from Facebook/WhatsApp or uploaded manually
    ├── wa_live.png       # Latest screenshot of WhatsApp Web streamed for /admin/whatsapp-qr
    └── wa_status.json    # Live status JSON ("starting" / "qr_ready" / "connected")
```

---

## 2. Database Schemas (`sandbox_posts.db` & `agent_memory.db`)

### A. `sandbox_posts.db` (Managed by `init_db()` in `app.py`)
1. **`posts` Table:**
   * `id` (`INTEGER PRIMARY KEY AUTOINCREMENT`)
   * `author` (`TEXT NOT NULL`)
   * `category` (`TEXT DEFAULT 'News / Gist'` — `'News / Gist'`, `'Event'`, `'Opportunity'`)
   * `campus_city` (`TEXT` — detected campus, Nigerian city/state, or foreign location; `NULL` when uncertain)
   * `headline` (`TEXT` — factual headline, max 140 characters, no ALL CAPS)
   * `content` (`TEXT NOT NULL` — cleaned post details)
   * `image_filename` (`TEXT` — saved inside `static/uploads/`)
   * `youtube_link` (`TEXT`)
   * `source_url` (`TEXT` — extracted external article or registration link)
   * `created_at` (`TEXT NOT NULL`)
2. **`watched_pages` Table:**
   * `id` (`INTEGER PRIMARY KEY AUTOINCREMENT`)
   * `fb_url` (`TEXT UNIQUE NOT NULL` — stores both Facebook Page and WhatsApp Channel URLs)
   * `label` (`TEXT NOT NULL` — resolved page/channel display name)
   * `enabled` (`INTEGER DEFAULT 1` — `1` active, `0` paused)
   * `last_checked_at` (`TEXT`)
   * `last_status` (`TEXT DEFAULT 'Waiting for scan'`)
   * `created_at` (`TEXT NOT NULL`)
3. **`settings` Table:**
   * `fb_target_url` (default target URL)
   * `autopilot_enabled` (`"1"` ON by default)
   * `poll_interval_minutes` (`"5"` minutes by default)
   * `headless_mode` (`"0"` visible browser / Xvfb headed mode, `"1"` headless mode)

### B. `agent_memory.db` (Managed by `init_memory_db()` in `agent.py`)
* **`seen_posts` Table:**
  * `post_hash` (`TEXT PRIMARY KEY` — SHA-256 hash of extracted post content + link)
  * `post_preview` (`TEXT` — first 200 characters)
  * `published_at` (`TIMESTAMP DEFAULT CURRENT_TIMESTAMP`)

---

## 3. End-to-End 5-Stage Automation Pipeline

```text
+-------------------------------------------------------------------------+
| STAGE 1: WATCHLIST & 5-MINUTE AUTO-PILOT SCHEDULER (app.py)             |
| • Add unlimited Facebook Page or WhatsApp Channel URLs via /watchlist/add|
| • Resolves WhatsApp Channel names from public OpenGraph <meta> tags      |
| • `background_autopilot_loop()` triggers `agent.py --watch-cycle`        |
|   every 5 minutes (skips automatically if a Chromium instance is active) |
+-------------------------------------------------------------------------+
                                    │
                                    ▼
+-------------------------------------------------------------------------+
| STAGE 2: SOURCE ROUTER & SCRAPERS (agent.py)                            |
| `process_single_watched_url()` routes by domain in `fb_profile/`:       |
|                                                                         |
| ├── FACEBOOK PAGE (`facebook.com/...`) — No Login Needed:               |
| │   • Opens public page (`navigate_to_target_page`), dismisses overlays |
| │   • Scrolls ~450px, expands "See more" on `[role="article"]`          |
| │   • Extracts clean caption from `[data-ad-preview="message"]`         |
| │   • Strips UI labels ("See more", "See less", "View all X replies")   |
| │   • Unwraps `l.facebook.com/l.php?u=...` links & strips `fbclid`      |
| │   • Downloads first content photo (`scontent`, >200x200px) OR         |
| │     falls back to external link preview thumbnail (`fbcdn`)           |
| │                                                                       |
| └── WHATSAPP CHANNEL (`whatsapp.com/channel/...`) — No Follow Needed:   |
|     • Single-post links (`/channel/<id>/<msg>`): reads public OG tags   |
|     • Full channels (`/channel/<id>`): opens `web.whatsapp.com` in      |
|       `fb_profile/`, dismisses modals, checks Channels list, or drops   |
|       invite link into `(You)` self-chat and clicks "View channel" to   |
|       open Preview Mode without following                               |
|     • Reads leaf text nodes (`leafNodes`) of bottom message bubble      |
|     • Exports `blob:` images to JPEG via HTML5 `<canvas>`               |
+-------------------------------------------------------------------------+
                                    │
                                    ▼
+-------------------------------------------------------------------------+
| STAGE 3: SHA-256 DEDUPLICATION (agent_memory.db -> `seen_posts`)        |
| • Hashes `caption|external_link` (prefixed `WA|` for WhatsApp)          |
| • If `is_post_seen(hash)` is True: updates `last_status` and skips      |
+-------------------------------------------------------------------------+
                                    │
                                    ▼
+-------------------------------------------------------------------------+
| STAGE 4: ORGANIZER & 4-TIER LOCATION DETECTOR (agent.py)                |
| • Category : `News / Gist`                                              |
| • Location : `detect_actual_location()` checks:                         |
|     Tier 1 — 30+ Nigerian universities & polytechnics (UNILAG, FUOYE…)  |
|     Tier 2 — All 36 Nigerian states, FCT & major cities                 |
|     Tier 3 — International cities, US states & countries                |
|     Tier 4 — Leaves blank (`""`) if no explicit location is detected    |
| • Headline : First factual sentence (no ALL CAPS, max 140 chars)        |
| • Details  : Clean paragraphs (no URLs, no trailing "Read more:",       |
|              no empty "LinkedIn:/Facebook:/X:/TikTok:/YouTube:" lines)  |
| • Source   : External article/registration link (ignores invite links)  |
+-------------------------------------------------------------------------+
                                    │
                                    ▼
+-------------------------------------------------------------------------+
| STAGE 5: MODAL PUBLISHING & LIVE FEED REFRESH                           |
| • Opens `http://127.0.0.1:<PORT>/`, clicks `#open-post-modal-btn`       |
| • Fills `#post-headline`, `#post-text`, `#campus-city`, `#post-image`,  |
|   and `#source-link`, then clicks `#publish-btn` ("Send for Review")    |
| • Frontend polls `/agent-status` every 3s and reloads when new posts    |
|   arrive                                                                |
+-------------------------------------------------------------------------+
```

---

## 4. Private Admin WhatsApp QR Linker (`/admin/whatsapp-qr`)

When deployed on a cloud container or headless server where you cannot see the server's Chromium desktop window directly:

1. **Public Frontpage Stays Clean:** Regular visitors on `/` never see any WhatsApp QR buttons or connection prompts.
2. **One-Time Admin Route (`GET /admin/whatsapp-qr`):**
   * Visiting `https://<your-domain>/admin/whatsapp-qr` launches `python -u agent.py --connect-whatsapp` and redirects to `/?show_wa_qr=1`.
   * `connect_whatsapp_interactive()` opens `https://web.whatsapp.com/` in `fb_profile/`, captures screenshots every **1.5 seconds** to `static/wa_live.png`, and writes status JSON (`starting`, `qr_ready`, `connected`) to `static/wa_status.json`.
   * The hidden modal on `index.html` activates only when `?show_wa_qr=1` is present, polling `/wa-status` and refreshing `/static/wa_live.png?t=<timestamp>` every 1.5 seconds.
   * Once you scan the QR code with your phone (*WhatsApp -> Linked Devices -> Link a Device*), the server waits 8 seconds for IndexedDB to flush to `fb_profile/` and saves the session permanently for all users.

### Minimal Blueprint to Reuse `/admin/whatsapp-qr` in Another Project
* **`agent.py` (`--connect-whatsapp`):** Launch `launch_persistent_context(user_data_dir="./fb_profile", headless=False)`, loop `page.screenshot(path="static/wa_live.png")`, detect `#pane-side` / `[aria-label="Channels"]`, write state JSON to `static/wa_status.json`, and wait 8s before closing.
* **`app.py`:** Expose `GET /admin/whatsapp-qr` (spawns the script and redirects to `/?show_wa_qr=1`) and `GET /wa-status` (returns JSON state + `has_image`).
* **`index.html`:** Keep the QR modal hidden (`display: none`) unless `window.location.search.includes("show_wa_qr=1")`, polling `/wa-status` and reloading `/static/wa_live.png?t=${Date.now()}` every 1.5 seconds.

---

## 5. JSON Feed & Endpoints

* **`GET /`** — Main Elo54 Pulse Dashboard, Watchlist, `+ Post News / Event` modal, and Live Feed.
* **`GET /api/posts.json`** — Returns up to the 100 latest posts as JSON (`{"count": N, "posts": [...]}`) with full `image_url` paths.
* **`GET /agent-status`** — Returns live Auto-Pilot countdown, `running` state, post count, and `watched_pages` statuses.
* **`GET /admin/whatsapp-qr`** — Private admin-only URL to link WhatsApp Web once via live QR stream.

---

## 6. Local Setup & Render Cloud Deployment

### Run Locally
```powershell
pip install -r requirements.txt
playwright install chromium
python -u app.py
```
* Dashboard: `http://127.0.0.1:5000/`
* JSON API: `http://127.0.0.1:5000/api/posts.json`
* Private Admin WhatsApp QR: `http://127.0.0.1:5000/admin/whatsapp-qr`

### Useful CLI Modes (`agent.py`)
```powershell
python -u agent.py --watch-cycle                  # Scan all enabled watchlist URLs once
python -u agent.py https://www.facebook.com/bbcnews
python -u agent.py https://whatsapp.com/channel/0029VaC48eP6GcG5EiHXyu18
python -u agent.py --connect-whatsapp             # Stream WhatsApp Web QR to static/wa_live.png
python -u agent.py --daemon                       # Standalone 5-minute polling loop
```

### Render Docker Deployment (`Dockerfile` & `render.yaml`)
* Base image: `mcr.microsoft.com/playwright/python:v1.48.0-noble` with `xvfb`, `xauth`, and `procps`.
* Starts `Xvfb :99` (`DISPLAY=:99`) in the background and executes `python -u app.py` bound to `0.0.0.0:$PORT` (`PORT=10000` in `render.yaml`).
* On Linux containers, `agent.py` passes `--no-sandbox` and `--disable-dev-shm-usage` and cleans stale Chromium lock files (`SingletonLock`, `SingletonSocket`, `SingletonCookie`) via `release_stale_profile_lock()`.
