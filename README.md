# Elo54 Pulse — Autonomous Facebook & WhatsApp Channel News Agent

An end-to-end autonomous news scraping, organization, and publishing system built with **Flask**, **Playwright (Chromium)**, and **SQLite**.

The system continuously monitors unlimited **Public Facebook Pages** and **WhatsApp Channels** every **5 minutes**, extracts newly published posts (text, images, link thumbnails, and external URLs), organizes them into structured **Elo54 Pulse** fields (*Category*, *Campus/City/Location*, *Headline*, *Details*, *Image*, and *Source / Registration Link*), and publishes them through the **`+ Post News / Event`** modal (`"Shine a Light on Your Campus or City"`).

---

## 1. Project Folder Structure

```text
elo age/
├── app.py                   # Flask Web Server + 5-Minute Background Auto-Pilot Scheduler
├── agent.py                 # Playwright Autonomous Scraper, Organizer & Modal Publisher
├── elo54_agent.py           # Standalone External Publisher for Live https://elo54.com
├── requirements.txt         # Python package dependencies
├── .env.example             # Environment variable template
├── README.md                # Full architecture & usage documentation
├── sandbox_posts.db         # SQLite database (published posts, watched links, settings)
├── agent_memory.db          # SQLite deduplication database (SHA-256 seen_posts table)
├── fb_profile/              # Persistent Chromium profile (stores WhatsApp Web linked session)
├── static/
│   ├── uploads/             # Uploaded & scraped post images
│   ├── wa_live.png          # Live WhatsApp Web QR code stream image
│   └── wa_status.json       # Live WhatsApp connection status
└── templates/
    ├── index.html           # Elo54 Pulse Frontpage + "+ Post News / Event" Modal + Watchlist
    └── login.html           # Legacy login template (redirects straight to frontpage)
```

---

## 2. How the System Works (Step-by-Step Pipeline)

```text
+-------------------------------------------------------------------------+
| 1. WATCHLIST & 5-MINUTE AUTO-PILOT SCHEDULER (app.py)                   |
|    - Stores unlimited Facebook Page & WhatsApp Channel URLs             |
|    - Background daemon thread triggers `python agent.py --watch-cycle`  |
|      every 5 minutes (pauses automatically if browser is already open)  |
+-------------------------------------------------------------------------+
                                    │
                                    ▼
+-------------------------------------------------------------------------+
| 2. SOURCE ROUTER (agent.py)                                             |
|    Checks each active URL in `watched_pages`:                           |
|                                                                         |
|    ├── IF FACEBOOK PAGE (`facebook.com/...`):                           |
|    │   • No Facebook login required                                     |
|    │   • Navigates directly to the public Facebook Page                 |
|    │   • Closes login/cookie popups & scrolls ~450px                    |
|    │   • Expands "See more" on the top `[role="article"]` post          |
|    │   • Extracts caption from `[data-ad-preview="message"]`            |
|    │   • Strips UI noise ("See more", "See less", "View all X replies") |
|    │   • Unwraps `l.facebook.com/l.php?u=...` external links            |
|    │   • Downloads first content photo (`scontent`, >200x200px) OR      |
|    │     falls back to the external link preview thumbnail (`fbcdn`)    |
|    │                                                                    |
|    └── IF WHATSAPP CHANNEL (`whatsapp.com/channel/...`):                |
|        • Resolves real Channel Name from public OpenGraph `<meta>`      |
|        • Uses persistent `./fb_profile` linked once via QR code         |
|        • You do NOT need to follow the channel:                         |
|          - If not in your Channels list, sends the invite link to your  |
|            own `(You)` self-chat and clicks "View channel" in-app       |
|          - Waits for `#main header` to switch to the channel preview    |
|        • Reads the bottom-most message bubble (leaf text nodes only)    |
|        • Converts WhatsApp `blob:` images to JPEG via HTML5 `<canvas>`  |
+-------------------------------------------------------------------------+
                                    │
                                    ▼
+-------------------------------------------------------------------------+
| 3. SHA-256 DEDUPLICATION (agent_memory.db -> `seen_posts`)              |
|    - Hashes `caption + external_link`                                   |
|    - If hash already exists in `seen_posts`:                            |
|      • Marks link status as "Up to date (no new post)" and skips        |
|    - If hash is brand new:                                              |
|      • Proceeds to Elo54 Post Organizer                                 |
+-------------------------------------------------------------------------+
                                    │
                                    ▼
+-------------------------------------------------------------------------+
| 4. ELO54 POST ORGANIZER & 4-TIER LOCATION DETECTOR                      |
|    - Category : "News / Gist"                                           |
|    - Location : 4-Tier Detection (`detect_actual_location`)             |
|      • Tier 1 : 35+ Nigerian Universities/Polytechnics & Abbreviations  |
|                 (UNILAG, FUOYE, UI, FUTA, OAU, ABU, BUK, UNN, LASU...)  |
|      • Tier 2 : All 36 Nigerian States + FCT + Major Cities             |
|      • Tier 3 : Foreign / Global Cities, US States & Countries          |
|                 (Florida, San Francisco, Seattle, UK, South Africa...)  |
|      • Tier 4 : If uncertain / no location mentioned -> leaves BLANK    |
|    - Headline : First factual sentence (<= 140 chars, no ALL CAPS)      |
|    - Details  : Clean paragraphs (no duplicate lines, no bare URLs,     |
|                 no trailing "Read more:" or empty "LinkedIn:/X:" lines) |
|    - Source   : External article/registration link placed exclusively   |
|                 into `Source / Registration Link (optional)`            |
+-------------------------------------------------------------------------+
                                    │
                                    ▼
+-------------------------------------------------------------------------+
| 5. PUBLISH VIA "+ Post News / Event" MODAL                              |
|    - Opens `http://127.0.0.1:5000/`                                     |
|    - Clicks `#open-post-modal-btn` ("+ Post News / Event")              |
|    - Fills the "Shine a Light on Your Campus or City" modal fields      |
|    - Attaches `temp_post_image.jpg` to `#post-image`                    |
|    - Clicks `#publish-btn` ("Send for Review")                          |
|    - Saves hash in `agent_memory.db` & auto-refreshes the live feed     |
+-------------------------------------------------------------------------+
```

---

## 3. Database Schema

### A. `sandbox_posts.db` (Application Database)
1. **`posts` Table:**
   * `id` (`INTEGER PRIMARY KEY AUTOINCREMENT`)
   * `author` (`TEXT`)
   * `category` (`TEXT` — `'News / Gist'`, `'Event'`, `'Opportunity'`)
   * `campus_city` (`TEXT NULL` — detected campus/city/country, or `NULL` if unknown)
   * `headline` (`TEXT` — max 140 characters)
   * `content` (`TEXT` — cleaned post details)
   * `image_filename` (`TEXT NULL` — stored in `static/uploads/`)
   * `youtube_link` (`TEXT NULL`)
   * `source_url` (`TEXT NULL` — external article or registration link)
   * `created_at` (`TEXT`)
2. **`watched_pages` Table:**
   * `id` (`INTEGER PRIMARY KEY AUTOINCREMENT`)
   * `fb_url` (`TEXT UNIQUE` — Facebook Page or WhatsApp Channel URL)
   * `label` (`TEXT` — auto-resolved page/channel display name)
   * `enabled` (`INTEGER` — `1` active, `0` paused)
   * `last_checked_at` (`TEXT`)
   * `last_status` (`TEXT` — live scan result)
   * `created_at` (`TEXT`)
3. **`settings` Table:**
   * `autopilot_enabled` (`"1"` ON by default)
   * `poll_interval_minutes` (`"5"` minutes by default)
   * `headless_mode` (`"0"` visible browser, `"1"` silent background mode)

### B. `agent_memory.db` (Deduplication Database)
* **`seen_posts` Table:**
  * `post_hash` (`TEXT PRIMARY KEY` — SHA-256 hash of extracted post)
  * `post_preview` (`TEXT` — first 200 characters)
  * `published_at` (`TIMESTAMP`)

---

## 4. Setup & Commands

### Install Dependencies
```powershell
pip install -r requirements.txt
playwright install chromium
```

### Start the Web App & 5-Minute Auto-Pilot
```powershell
python app.py
```
* Open **http://127.0.0.1:5000** in your browser.
* Paste any **Facebook Page link** (`https://www.facebook.com/...`) or **WhatsApp Channel link** (`https://whatsapp.com/channel/...`) into the input box and click **`+ Add Link & Check`**.

### Useful CLI Commands for `agent.py`
```powershell
# 1. Run a full watch cycle across all enabled Facebook & WhatsApp links
python agent.py --watch-cycle

# 2. Check a single Facebook Page or WhatsApp Channel URL immediately
python agent.py https://www.facebook.com/bbcnews
python agent.py https://whatsapp.com/channel/0029VaC48eP6GcG5EiHXyu18

# 3. Connect or re-link WhatsApp Web via live QR code stream
python agent.py --connect-whatsapp
# (Then view the live QR code at http://127.0.0.1:5000/?show_wa_qr=1)

# 4. Run a standalone continuous 5-minute daemon in a separate terminal
python agent.py --daemon
```
