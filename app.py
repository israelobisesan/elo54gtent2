import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse
import requests
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
)
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
DB_PATH = os.path.join(BASE_DIR, "sandbox_posts.db")
AGENT_SCRIPT = os.path.join(BASE_DIR, "agent.py")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
DEFAULT_FB_URL = os.getenv("FB_TARGET_URL", "https://www.facebook.com/bbcnews")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-sandbox-secret-key")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

active_agent_proc = None
proc_lock = threading.Lock()
last_auto_trigger_ts = time.time()
scheduler_started = False

CAMPUS_CITY_OPTIONS = [
    "University of Lagos (UNILAG)",
    "Federal University Oye-Ekiti (FUOYE)",
    "University of Ibadan (UI)",
    "Federal University of Technology, Akure (FUTA)",
    "Obafemi Awolowo University (OAU Ife)",
    "Ahmadu Bello University (ABU Zaria)",
    "Bayero University Kano (BUK)",
    "University of Nigeria, Nsukka (UNN)",
    "University of Benin (UNIBEN)",
    "University of Ilorin (UNILORIN)",
    "Lagos State University (LASU)",
    "University of Port Harcourt (UNIPORT)",
    "Nnamdi Azikiwe University (UNIZIK)",
    "Federal University of Technology, Owerri (FUTO)",
    "Federal University of Agriculture, Abeokuta (FUNAAB)",
    "University of Calabar (UNICAL)",
    "University of Uyo (UNIUYO)",
    "Ekiti State University (EKSU)",
    "Ladoke Akintola University of Technology (LAUTECH)",
    "Olabisi Onabanjo University (OOU)",
    "Delta State University (DELSU)",
    "Ambrose Alli University (AAU Ekpoma)",
    "Kwara State University (KWASU)",
    "University of Jos (UNIJOS)",
    "University of Maiduguri (UNIMAID)",
    "Covenant University, Ota",
    "Babcock University",
    "Afe Babalola University (ABUAD)",
    "Yaba College of Technology (YABATECH)",
    "Lagos",
    "Abuja (FCT)",
    "Kano",
    "Ibadan",
    "Port Harcourt",
    "Enugu",
    "Kaduna",
    "Benin City",
    "Ado-Ekiti, Ekiti",
    "Akure, Ondo",
    "Abeokuta, Ogun",
    "Osogbo, Osun",
    "Ilorin, Kwara",
    "Jos, Plateau",
    "Owerri, Imo",
    "Awka, Anambra",
    "Onitsha, Anambra",
    "Warri, Delta",
    "Asaba, Delta",
    "Umuahia, Abia",
    "Aba, Abia",
    "Calabar, Cross River",
    "Uyo, Akwa Ibom",
    "Yenagoa, Bayelsa",
    "Abakaliki, Ebonyi",
    "Makurdi, Benue",
    "Lokoja, Kogi",
    "Minna, Niger",
    "Lafia, Nasarawa",
    "Maiduguri, Borno",
    "Sokoto",
    "Katsina",
    "Zaria, Kaduna",
    "Bauchi",
    "Gombe",
    "Yola, Adamawa",
    "Jalingo, Taraba",
    "Damaturu, Yobe",
    "Gusau, Zamfara",
    "Birnin Kebbi, Kebbi",
    "Dutse, Jigawa",
]


def is_whatsapp_url(url: str) -> bool:
    try:
        host = (urlparse(url.strip()).netloc or "").lower()
        return "whatsapp.com" in host
    except Exception:
        return False


def extract_page_label(source_url: str) -> str:
    """
    Create a friendly display label from a Facebook Page or WhatsApp Channel URL.
    For WhatsApp Channel links, attempts a quick HTTP metadata lookup of og:title.
    """
    url_clean = source_url.strip()
    if is_whatsapp_url(url_clean):
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                )
            }
            resp = requests.get(url_clean, headers=headers, timeout=6)
            if resp.ok:
                m = re.search(
                    r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
                    resp.text,
                    re.IGNORECASE,
                )
                if m:
                    raw_title = html.unescape(m.group(1)).strip()
                    clean_title = re.sub(
                        r"\s*\|\s*WhatsApp\s+Channel\s*$", "", raw_title, flags=re.IGNORECASE
                    ).strip()
                    if clean_title and clean_title.lower() != "whatsapp":
                        return clean_title
        except Exception:
            pass

        parsed = urlparse(url_clean)
        parts = [p for p in (parsed.path or "").split("/") if p and p.lower() != "channel"]
        if parts:
            return f"WhatsApp Channel ({parts[0][:10]}...)"
        return "WhatsApp Channel"

    try:
        parsed = urlparse(url_clean)
        parts = [p for p in (parsed.path or "").split("/") if p]
        if parts:
            return parts[0]
    except Exception:
        pass
    return url_clean


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author TEXT NOT NULL,
                category TEXT DEFAULT 'News / Gist',
                campus_city TEXT DEFAULT 'Kano',
                headline TEXT,
                content TEXT NOT NULL,
                image_filename TEXT,
                youtube_link TEXT,
                source_url TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(posts)").fetchall()
        }
        migrations = [
            ("category", "TEXT DEFAULT 'News / Gist'"),
            ("campus_city", "TEXT DEFAULT 'Kano'"),
            ("headline", "TEXT"),
            ("youtube_link", "TEXT"),
            ("source_url", "TEXT"),
        ]
        for col_name, col_def in migrations:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE posts ADD COLUMN {col_name} {col_def}")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watched_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fb_url TEXT UNIQUE NOT NULL,
                label TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                last_checked_at TEXT,
                last_status TEXT DEFAULT 'Waiting for scan',
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        default_settings = [
            ("fb_target_url", DEFAULT_FB_URL),
            ("autopilot_enabled", "1"),
            ("poll_interval_minutes", "5"),
            ("headless_mode", "0"),
        ]
        for k, v in default_settings:
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (k, v),
            )

        count = conn.execute("SELECT COUNT(*) FROM watched_pages").fetchone()[0]
        if count == 0:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            seed_urls = [
                "https://www.facebook.com/bbcnews",
                "https://www.facebook.com/cnn",
                "https://www.facebook.com/RealAmericasVoice",
            ]
            for u in seed_urls:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO watched_pages (fb_url, label, enabled, last_status, created_at)
                    VALUES (?, ?, 1, 'Ready to monitor', ?)
                    """,
                    (u, extract_page_label(u), now_str),
                )
        conn.commit()


def get_setting(key: str, default: str = "") -> str:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
        conn.commit()


def is_chromium_profile_active() -> bool:
    """Return True if any chrome/chromium process is currently using fb_profile."""
    try:
        if os.name == "nt":
            ps_cmd = (
                "(Get-CimInstance Win32_Process -Filter \"Name = 'chrome.exe'\" | "
                "Where-Object { $_.CommandLine -like '*fb_profile*' }).Count"
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=5,
            )
            count = int((res.stdout or "0").strip() or "0")
            return count > 0
        else:
            res = subprocess.run(
                ["pgrep", "-f", "fb_profile"],
                capture_output=True,
                timeout=5,
            )
            return res.returncode == 0
    except Exception:
        pass
    return False


def is_agent_running() -> bool:
    global active_agent_proc
    if active_agent_proc is not None and active_agent_proc.poll() is None:
        return True
    return is_chromium_profile_active()


def launch_agent_args(args_list: list) -> bool:
    global active_agent_proc, last_auto_trigger_ts
    with proc_lock:
        if active_agent_proc is not None and active_agent_proc.poll() is None:
            return False
        active_agent_proc = subprocess.Popen(
            [sys.executable, "-u", AGENT_SCRIPT, *args_list],
            cwd=BASE_DIR,
        )
        last_auto_trigger_ts = time.time()
        return True


def background_autopilot_loop():
    global last_auto_trigger_ts
    while True:
        time.sleep(5)
        try:
            autopilot_on = get_setting("autopilot_enabled", "1") == "1"
            if not autopilot_on:
                continue
            interval_mins = max(1, int(get_setting("poll_interval_minutes", "5") or "5"))
            elapsed = time.time() - last_auto_trigger_ts
            if elapsed >= interval_mins * 60 and not is_agent_running():
                with sqlite3.connect(DB_PATH) as conn:
                    enabled_count = conn.execute(
                        "SELECT COUNT(*) FROM watched_pages WHERE enabled = 1"
                    ).fetchone()[0]
                if enabled_count > 0:
                    print(f"[Auto-Pilot] {interval_mins}m interval reached. Launching multi-page watch cycle...")
                    launch_agent_args(["--watch-cycle"])
        except Exception as exc:
            print(f"[Auto-Pilot Error] {exc}")


def ensure_scheduler_started():
    global scheduler_started
    if not scheduler_started:
        scheduler_started = True
        t = threading.Thread(target=background_autopilot_loop, daemon=True)
        t.start()


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/login", methods=["GET", "POST"])
def login():
    return redirect(url_for("index"))


@app.route("/watchlist/connect-whatsapp", methods=["POST"])
def connect_whatsapp_route():
    launch_agent_args(["--connect-whatsapp"])
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"started": True})
    flash(
        "WhatsApp QR Scanner started! Scan the live QR code in the popup on this page.",
        "success",
    )
    return redirect(url_for("index", show_wa_qr="1"))


@app.route("/wa-status", methods=["GET"])
def wa_status_route():
    status_path = os.path.join(BASE_DIR, "static", "wa_status.json")
    img_path = os.path.join(BASE_DIR, "static", "wa_live.png")
    data = {"state": "idle", "message": "Click 'Connect WhatsApp' to load QR code."}
    if os.path.exists(status_path):
        try:
            with open(status_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
    data["has_image"] = os.path.exists(img_path)
    data["agent_running"] = is_agent_running()
    return jsonify(data)


@app.route("/watchlist/add", methods=["POST"])
def add_watched_page():
    raw_input = request.form.get("fb_urls", "").strip()
    scan_immediately = request.form.get("scan_immediately", "1") == "1"

    if not raw_input:
        flash("Please paste at least one Facebook Page or WhatsApp Channel link.", "error")
        return redirect(url_for("index"))

    candidates = [
        u.strip().rstrip("/")
        for u in re.split(r"[\r\n,\s]+", raw_input)
        if u.strip()
    ]
    valid_urls = [
        u for u in candidates if u.startswith("http://") or u.startswith("https://")
    ]

    if not valid_urls:
        flash("Please enter valid Facebook or WhatsApp Channel URLs starting with https://", "error")
        return redirect(url_for("index"))

    init_db()
    added_count = 0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as conn:
        for url in valid_urls:
            label = extract_page_label(url)
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO watched_pages (fb_url, label, enabled, last_status, created_at)
                VALUES (?, ?, 1, 'Added — queued for scan', ?)
                """,
                (url, label, now_str),
            )
            if cur.rowcount > 0:
                added_count += 1
        conn.commit()

    if scan_immediately and valid_urls:
        if len(valid_urls) == 1 and launch_agent_args([valid_urls[0]]):
            flash(
                f"Added {valid_urls[0]} to watchlist and launched immediate check!",
                "success",
            )
        elif launch_agent_args(["--watch-cycle"]):
            flash(
                f"Added {added_count} link(s) and started full watchlist scan!",
                "success",
            )
        else:
            flash(
                f"Added {added_count} link(s) to watchlist (agent is currently busy scanning).",
                "success",
            )
    else:
        flash(f"Added {added_count} link(s) to the 5-minute Auto-Pilot watchlist.", "success")

    return redirect(url_for("index"))


@app.route("/watchlist/toggle/<int:page_id>", methods=["POST"])
def toggle_watched_page(page_id: int):
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT enabled, label FROM watched_pages WHERE id = ?", (page_id,)
        ).fetchone()
        if row:
            new_state = 0 if row[0] else 1
            status_text = "Monitoring active" if new_state else "Paused"
            conn.execute(
                "UPDATE watched_pages SET enabled = ?, last_status = ? WHERE id = ?",
                (new_state, status_text, page_id),
            )
            conn.commit()
            flash(f"{'Resumed' if new_state else 'Paused'} monitoring for {row[1]}.", "success")
    return redirect(url_for("index"))


@app.route("/watchlist/delete/<int:page_id>", methods=["POST"])
def delete_watched_page(page_id: int):
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM watched_pages WHERE id = ?", (page_id,))
        conn.commit()
    flash("Removed link from watchlist.", "success")
    return redirect(url_for("index"))


@app.route("/watchlist/scan-one/<int:page_id>", methods=["POST"])
def scan_single_watched_page(page_id: int):
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT fb_url, label FROM watched_pages WHERE id = ?", (page_id,)
        ).fetchone()
    if not row:
        flash("Watched link not found.", "error")
        return redirect(url_for("index"))

    fb_url, label = row
    if launch_agent_args([fb_url]):
        flash(f"Checking {label} ({fb_url}) for new posts now...", "success")
    else:
        flash("The Playwright agent is currently running another check. Please wait a moment.", "error")
    return redirect(url_for("index"))


@app.route("/watchlist/scan-now", methods=["POST"])
def scan_all_watched_pages():
    if launch_agent_args(["--watch-cycle"]):
        flash("Launched full scan across all active Facebook & WhatsApp channels in your watchlist!", "success")
    else:
        flash("The Playwright agent is already running a scan!", "error")
    return redirect(url_for("index"))


@app.route("/watchlist/autopilot", methods=["POST"])
def update_autopilot_settings():
    global last_auto_trigger_ts
    autopilot_enabled = "1" if request.form.get("autopilot_enabled") == "1" else "0"
    poll_interval = request.form.get("poll_interval_minutes", "5").strip()
    if poll_interval not in ("2", "5", "10", "15"):
        poll_interval = "5"
    headless_mode = "1" if request.form.get("headless_mode") == "1" else "0"

    set_setting("autopilot_enabled", autopilot_enabled)
    set_setting("poll_interval_minutes", poll_interval)
    set_setting("headless_mode", headless_mode)
    last_auto_trigger_ts = time.time()

    state_label = f"ON (checking every {poll_interval} mins)" if autopilot_enabled == "1" else "OFF"
    mode_label = "Silent Background Mode" if headless_mode == "1" else "Visible Browser Window"
    flash(f"Auto-Pilot updated: {state_label} · {mode_label}.", "success")
    return redirect(url_for("index"))


@app.route("/repost-last", methods=["POST"])
def repost_last_route():
    if launch_agent_args(["--repost-last"]):
        flash("Agent is organizing and posting the last retrieved post via '+ Post News / Event'!", "success")
    else:
        flash("The Playwright agent is already running!", "error")
    return redirect(url_for("index"))


@app.route("/agent-status", methods=["GET"])
def agent_status():
    init_db()
    running = is_agent_running()
    exit_code = None if running or active_agent_proc is None else active_agent_proc.poll()
    autopilot_on = get_setting("autopilot_enabled", "1") == "1"
    interval_mins = max(1, int(get_setting("poll_interval_minutes", "5") or "5"))
    seconds_remaining = max(0, int((interval_mins * 60) - (time.time() - last_auto_trigger_ts)))

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        pages = [
            dict(r)
            for r in conn.execute(
                "SELECT id, fb_url, label, enabled, last_checked_at, last_status FROM watched_pages ORDER BY id DESC"
            ).fetchall()
        ]
        post_count = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]

    return jsonify(
        {
            "running": running,
            "exit_code": exit_code,
            "autopilot_enabled": autopilot_on,
            "poll_interval_minutes": interval_mins,
            "seconds_until_next_scan": seconds_remaining,
            "post_count": post_count,
            "watched_pages": pages,
        }
    )


@app.route("/", methods=["GET", "POST"])
def index():
    init_db()
    ensure_scheduler_started()

    if request.method == "POST":
        category = request.form.get("category", "News / Gist").strip() or "News / Gist"
        campus_city = request.form.get("campus_city", "").strip() or None
        headline = request.form.get("headline", "").strip()
        post_text = request.form.get("post_text", "").strip()
        youtube_link = request.form.get("youtube_link", "").strip()
        source_url = request.form.get("source_url", "").strip()

        image_file = request.files.get("post_image")
        saved_filename = None

        if image_file and image_file.filename:
            if allowed_file(image_file.filename):
                ext = image_file.filename.rsplit(".", 1)[1].lower()
                unique_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.{ext}"
                safe_name = secure_filename(unique_name)
                image_path = os.path.join(app.config["UPLOAD_FOLDER"], safe_name)
                image_file.save(image_path)
                saved_filename = safe_name
            else:
                flash("Unsupported image format. Allowed: png, jpg, jpeg, gif, webp.", "error")
                return redirect(url_for("index"))

        if not headline and not post_text and not saved_filename:
            flash("Please fill in Headline and Details before submitting.", "error")
            return redirect(url_for("index"))

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO posts (
                    author, category, campus_city, headline, content,
                    image_filename, youtube_link, source_url, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "israelobisesan44@gmail.com",
                    category,
                    campus_city,
                    headline,
                    post_text,
                    saved_filename,
                    youtube_link or None,
                    source_url or None,
                    timestamp,
                ),
            )
            conn.commit()

        flash("Post submitted via Elo54 Pulse ('Shine a Light on Your Campus or City')!", "success")
        return redirect(url_for("index"))

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        posts = conn.execute(
            """
            SELECT id, author, category, campus_city, headline, content,
                   image_filename, youtube_link, source_url, created_at
            FROM posts
            ORDER BY id DESC
            """
        ).fetchall()
        watched_pages = conn.execute(
            """
            SELECT id, fb_url, label, enabled, last_checked_at, last_status, created_at
            FROM watched_pages
            ORDER BY id DESC
            """
        ).fetchall()

    autopilot_enabled = get_setting("autopilot_enabled", "1") == "1"
    poll_interval_minutes = int(get_setting("poll_interval_minutes", "5") or "5")
    headless_mode = get_setting("headless_mode", "0") == "1"
    seconds_until_next = max(
        0, int((poll_interval_minutes * 60) - (time.time() - last_auto_trigger_ts))
    )

    return render_template(
        "index.html",
        posts=posts,
        watched_pages=watched_pages,
        campus_city_options=CAMPUS_CITY_OPTIONS,
        agent_running=is_agent_running(),
        autopilot_enabled=autopilot_enabled,
        poll_interval_minutes=poll_interval_minutes,
        headless_mode=headless_mode,
        seconds_until_next=seconds_until_next,
    )


if __name__ == "__main__":
    init_db()
    ensure_scheduler_started()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug_mode, use_reloader=False, threaded=True)
