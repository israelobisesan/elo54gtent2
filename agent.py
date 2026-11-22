import base64
import hashlib
import html
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse, unquote

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, Locator, TimeoutError as PlaywrightTimeoutError

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "fb_profile")
MEMORY_DB_PATH = os.path.join(BASE_DIR, "agent_memory.db")
SANDBOX_DB_PATH = os.path.join(BASE_DIR, "sandbox_posts.db")
UPLOADS_DIR = os.path.join(BASE_DIR, "static", "uploads")
WA_LIVE_IMG_PATH = os.path.join(BASE_DIR, "static", "wa_live.png")
WA_STATUS_JSON_PATH = os.path.join(BASE_DIR, "static", "wa_status.json")
TEMP_IMAGE_PATH = os.path.join(BASE_DIR, "temp_post_image.jpg")

DEFAULT_FB_TARGET_URL = os.getenv("FB_TARGET_URL", "https://www.facebook.com/bbcnews").strip()
SKIP_VIDEO_POSTS = os.getenv("SKIP_VIDEO_POSTS", "False").strip().lower() in ("true", "1", "yes")
APP_PORT = os.getenv("PORT", "5000").strip() or "5000"
TEST_SITE_URL = os.getenv("TEST_SITE_URL", f"http://127.0.0.1:{APP_PORT}/").strip()
DEFAULT_FALLBACK_CITY = "Kano"

UI_NOISE_LINES = {
    "like",
    "comment",
    "share",
    "send",
    "all reactions:",
    "most relevant",
    "write a comment…",
    "write a comment...",
    "view more comments",
    "see more",
    "see less",
    "read more",
    "view channel",
    "facebook",
    "reels",
    "more",
    "forwarded",
    "added you",
}

SEE_MORE_TAIL_RE = re.compile(
    r"(?:\s*(?:\.\.\.|…)?\s*\b(?:see\s+more|see\s+less|read\s+more)\b)+\s*$",
    re.IGNORECASE,
)

COMMENT_UI_LINE_RE = re.compile(
    r"^(?:"
    r"view\s+(?:all\s+|more\s+|previous\s+)?\d*\s*(?:replies|reply|comments?)"
    r"|\d+(?:[.,]\d+)?[kmb]?\s+(?:comments?|replies|reply|shares?|views?|reactions?|followers?)"
    r"|\d{1,2}:\d{2}\s*(?:am|pm)?"
    r"|most\s+relevant"
    r"|all\s+comments"
    r"|top\s+fan"
    r"|author"
    r"|follow"
    r"|following"
    r"|original\s+audio"
    r"|forwarded"
    r")$",
    re.IGNORECASE,
)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def release_stale_profile_lock() -> None:
    """
    Ensure no orphaned Playwright Chromium process is still holding the lock on
    `fb_profile` before launching a new persistent context.
    """
    try:
        if os.name == "nt":
            ps_cmd = (
                "Get-CimInstance Win32_Process -Filter \"Name = 'chrome.exe'\" | "
                "Where-Object { $_.CommandLine -like '*fb_profile*' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
            )
        else:
            subprocess.run(
                ["pkill", "-9", "-f", "fb_profile"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
            )
            for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                lock_path = os.path.join(PROFILE_DIR, lock_name)
                if os.path.lexists(lock_path):
                    try:
                        os.remove(lock_path)
                    except OSError:
                        pass
    except Exception:
        pass


def is_whatsapp_url(url: str) -> bool:
    try:
        host = (urlparse(url.strip()).netloc or "").lower()
        return "whatsapp.com" in host
    except Exception:
        return False


def get_sandbox_setting(key: str, default: str = "") -> str:
    if not os.path.exists(SANDBOX_DB_PATH):
        return default
    try:
        with sqlite3.connect(SANDBOX_DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else default
    except Exception:
        return default


def get_enabled_watched_pages() -> List[Tuple[int, str, str]]:
    if not os.path.exists(SANDBOX_DB_PATH):
        return []
    try:
        with sqlite3.connect(SANDBOX_DB_PATH) as conn:
            rows = conn.execute(
                "SELECT id, fb_url, label FROM watched_pages WHERE enabled = 1 ORDER BY id ASC"
            ).fetchall()
            return [(r[0], r[1].strip(), r[2]) for r in rows if r[1].strip()]
    except Exception:
        return []


def update_watched_page_status(target_url: str, status: str, new_label: Optional[str] = None) -> None:
    if not os.path.exists(SANDBOX_DB_PATH):
        return
    now_str = datetime.now().strftime("%H:%M:%S")
    clean_url = target_url.strip().rstrip("/")
    try:
        with sqlite3.connect(SANDBOX_DB_PATH) as conn:
            for candidate in (clean_url, target_url.strip()):
                if new_label:
                    conn.execute(
                        """
                        UPDATE watched_pages
                        SET last_checked_at = ?, last_status = ?, label = ?
                        WHERE fb_url = ?
                        """,
                        (now_str, status, new_label, candidate),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE watched_pages
                        SET last_checked_at = ?, last_status = ?
                        WHERE fb_url = ?
                        """,
                        (now_str, status, candidate),
                    )
            conn.commit()
    except Exception:
        pass


def init_memory_db() -> None:
    with sqlite3.connect(MEMORY_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_posts (
                post_hash TEXT PRIMARY KEY,
                post_preview TEXT,
                published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def is_post_seen(post_hash: str) -> bool:
    with sqlite3.connect(MEMORY_DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_posts WHERE post_hash = ?", (post_hash,)
        ).fetchone()
        return row is not None


def mark_post_seen(post_hash: str, post_text: str) -> None:
    preview = (post_text[:200] + "...") if len(post_text) > 200 else post_text
    with sqlite3.connect(MEMORY_DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_posts (post_hash, post_preview) VALUES (?, ?)",
            (post_hash, preview),
        )
        conn.commit()


def human_delay(min_sec: float = 0.6, max_sec: float = 1.4) -> None:
    time.sleep(random.uniform(min_sec, max_sec))


def dismiss_cookie_or_popup_dialogs(page: Page) -> None:
    popup_selectors = [
        'div[role="dialog"] [aria-label="Close"]',
        'div[role="dialog"] div[aria-label="Close"]',
        'button:has-text("Allow all cookies")',
        'button:has-text("Decline optional cookies")',
        'div[role="button"]:has-text("Allow all cookies")',
        'div[role="button"]:has-text("Not Now")',
        'div[role="button"]:has-text("Not now")',
    ]
    for sel in popup_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=800):
                btn.click(timeout=1500)
                human_delay(0.5, 1.0)
        except Exception:
            continue


def navigate_to_target_page(page: Page, target_url: str) -> None:
    print(f"[1/5] Navigating directly to Facebook page: {target_url} ...")
    page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
    human_delay(2.5, 4.0)
    dismiss_cookie_or_popup_dialogs(page)

    print("      Scrolling down ~450px to render the latest feed post...")
    page.mouse.wheel(0, 450)
    human_delay(2.0, 3.0)
    dismiss_cookie_or_popup_dialogs(page)


def expand_see_more(article: Locator) -> None:
    see_more_candidates = [
        'div[role="button"]:has-text("See more")',
        'span:has-text("See more")',
    ]
    for selector in see_more_candidates:
        try:
            btn = article.locator(selector).first
            if btn.is_visible(timeout=1000):
                btn.scroll_into_view_if_needed(timeout=2000)
                btn.click(timeout=2000)
                print("      Clicked 'See more' to expand full caption.")
                human_delay(0.8, 1.4)
                return
        except Exception:
            continue


def clean_facebook_redirect_url(raw_href: str) -> Optional[str]:
    if not raw_href:
        return None
    try:
        parsed = urlparse(raw_href)
        host = (parsed.netloc or "").lower()

        if "facebook.com" in host and parsed.path.startswith("/l.php"):
            qs = parse_qs(parsed.query)
            if "u" in qs and qs["u"]:
                target = unquote(qs["u"][0])
            else:
                return None
        elif host and not any(fb_domain in host for fb_domain in ("facebook.com", "fb.com", "fb.watch", "fbcdn.net")):
            target = raw_href
        else:
            return None

        target_parsed = urlparse(target)
        if target_parsed.scheme not in ("http", "https"):
            return None

        query_params = parse_qs(target_parsed.query, keep_blank_values=True)
        query_params.pop("fbclid", None)
        clean_query = urlencode(query_params, doseq=True)
        cleaned = urlunparse(
            (
                target_parsed.scheme,
                target_parsed.netloc,
                target_parsed.path,
                target_parsed.params,
                clean_query,
                "",
            )
        )
        return cleaned
    except Exception:
        return None


def extract_external_link(article: Locator) -> Optional[str]:
    hrefs = article.evaluate(
        """(articleEl) => {
            const links = [];
            const anchorEls = articleEl.querySelectorAll('a[href]');
            for (const a of anchorEls) {
                const parentArticle = a.closest('[role="article"]');
                if (parentArticle && parentArticle !== articleEl) {
                    continue;
                }
                if (a.closest('form') || a.closest('[aria-label*="comment" i]')) {
                    continue;
                }
                if (a.href) {
                    links.push(a.href);
                }
            }
            return links;
        }"""
    )

    for href in hrefs:
        cleaned = clean_facebook_redirect_url(href)
        if cleaned:
            return cleaned
    return None


def extract_clean_caption(article: Locator) -> str:
    raw_blocks = article.evaluate(
        """(articleEl) => {
            const cleanNodeText = (node) => {
                const clone = node.cloneNode(true);
                const buttons = clone.querySelectorAll('[role="button"], span, div');
                for (const b of buttons) {
                    const t = (b.innerText || b.textContent || '').trim().toLowerCase();
                    if (
                        t === 'see more' ||
                        t === 'see less' ||
                        t === '… see more' ||
                        t === '... see more' ||
                        /^view\\s+(all\\s+|more\\s+|previous\\s+)?\\d*\\s*(replies|reply|comments?)$/i.test(t)
                    ) {
                        b.remove();
                    }
                }
                return (clone.innerText || clone.textContent || '').trim();
            };

            const dedicatedMsg = articleEl.querySelector(
                '[data-ad-preview="message"], [data-ad-comet-preview="message"]'
            );
            if (dedicatedMsg) {
                const msgText = cleanNodeText(dedicatedMsg);
                if (msgText) {
                    return [msgText];
                }
            }

            const actionBar = articleEl.querySelector(
                '[aria-label="Like"], [aria-label="Comment"], [aria-label="Leave a comment"], [aria-label="Share"]'
            );
            const results = [];
            const candidates = articleEl.querySelectorAll('div[dir="auto"], span[dir="auto"]');

            for (const el of candidates) {
                const parentArticle = el.closest('[role="article"]');
                if (parentArticle && parentArticle !== articleEl) {
                    continue;
                }
                if (el.closest('[role="button"]') || el.closest('button')) {
                    continue;
                }
                if (el.closest('form') || el.closest('[aria-label*="comment" i]') || el.closest('ul')) {
                    continue;
                }
                if (
                    actionBar &&
                    (actionBar.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)
                ) {
                    continue;
                }
                const parentAnchor = el.closest('a[href]');
                if (parentAnchor) {
                    const href = parentAnchor.getAttribute('href') || '';
                    const isExternalCard = href.includes('l.facebook.com/l.php') ||
                        (href.startsWith('http') && !href.includes('facebook.com'));
                    if (isExternalCard) {
                        continue;
                    }
                }
                if (el.querySelector('div[dir="auto"]')) {
                    continue;
                }

                const text = cleanNodeText(el);
                if (text) {
                    results.push(text);
                }
            }
            return results;
        }"""
    )

    cleaned_lines = []
    seen_lines = set()

    for block in raw_blocks:
        for line in block.splitlines():
            stripped = SEE_MORE_TAIL_RE.sub("", line).strip()
            if not stripped:
                continue
            if stripped.lower() in UI_NOISE_LINES:
                continue
            if COMMENT_UI_LINE_RE.match(stripped):
                continue
            if stripped.isdigit():
                continue
            if stripped not in seen_lines:
                seen_lines.add(stripped)
                cleaned_lines.append(stripped)

    return "\n".join(cleaned_lines).strip()


def extract_post_media(article: Locator) -> Tuple[Optional[str], bool]:
    media_info = article.evaluate(
        """(articleEl) => {
            const directPhotos = [];
            const linkThumbnails = [];

            const imgEls = articleEl.querySelectorAll('img');
            for (const img of imgEls) {
                const parentArticle = img.closest('[role="article"]');
                if (parentArticle && parentArticle !== articleEl) {
                    continue;
                }
                const src = img.currentSrc || img.src || '';
                if (!src.startsWith('http')) {
                    continue;
                }
                const rect = img.getBoundingClientRect();
                if (src.includes('scontent') && rect.width > 200 && rect.height > 200) {
                    directPhotos.push(src);
                } else if (
                    (src.includes('fbcdn.net') || src.includes('external') || src.includes('scontent')) &&
                    rect.width > 140 &&
                    rect.height > 120
                ) {
                    linkThumbnails.push(src);
                }
            }

            const svgImgs = articleEl.querySelectorAll('svg image');
            for (const svgImg of svgImgs) {
                const parentArticle = svgImg.closest('[role="article"]');
                if (parentArticle && parentArticle !== articleEl) {
                    continue;
                }
                const href = svgImg.getAttribute('xlink:href') || svgImg.getAttribute('href') || '';
                const rect = svgImg.getBoundingClientRect();
                if (href.startsWith('http') && rect.width > 140 && rect.height > 120) {
                    linkThumbnails.push(href);
                }
            }

            const hasVideo = Boolean(
                articleEl.querySelector('video') ||
                articleEl.querySelector('[aria-label*="video" i]') ||
                articleEl.querySelector('[data-video-id]')
            );

            return {
                directPhotos,
                linkThumbnails,
                hasVideo,
            };
        }"""
    )

    direct_photos = media_info.get("directPhotos", [])
    link_thumbnails = media_info.get("linkThumbnails", [])
    has_video = bool(media_info.get("hasVideo", False))

    if direct_photos:
        return direct_photos[0], has_video
    if link_thumbnails:
        print("      No direct photo found; using external link preview thumbnail as post photo.")
        return link_thumbnails[0], has_video
    return None, has_video


def download_image(image_url: str, output_path: str) -> Optional[str]:
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(image_url, headers=headers, timeout=20)
        resp.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(resp.content)
        print(f"      Downloaded post image -> {output_path}")
        return output_path
    except Exception as exc:
        print(f"      [!] Failed to download image: {exc}")
        return None


def find_and_extract_latest_post(page: Page) -> Tuple[str, Optional[str], Optional[str]]:
    print("[2/5] Locating latest post container ([role='article'])...")
    page.wait_for_selector('[role="article"]', timeout=30000)

    max_index_to_scan = 6
    scroll_attempts = 0

    while scroll_attempts < 4:
        dismiss_cookie_or_popup_dialogs(page)
        articles = page.locator('[role="article"]')
        count = min(articles.count(), max_index_to_scan)

        for idx in range(count):
            article = articles.nth(idx)
            try:
                if not article.is_visible():
                    continue
                article.scroll_into_view_if_needed(timeout=3000)
                human_delay(0.6, 1.2)
            except Exception:
                continue

            expand_see_more(article)
            caption = extract_clean_caption(article)
            external_link = extract_external_link(article)
            first_image_url, has_video = extract_post_media(article)

            if not caption and not first_image_url and not external_link:
                continue

            print(
                f"      Candidate post #{idx + 1}: {len(caption)} chars, "
                f"image={bool(first_image_url)}, link={bool(external_link)}, video={has_video}"
            )

            if first_image_url:
                if os.path.exists(TEMP_IMAGE_PATH):
                    os.remove(TEMP_IMAGE_PATH)
                downloaded = download_image(first_image_url, TEMP_IMAGE_PATH)
                return caption, downloaded, external_link

            if has_video and not first_image_url:
                if SKIP_VIDEO_POSTS:
                    print("      Post contains a video and SKIP_VIDEO_POSTS=True; skipping to next post...")
                    continue
                else:
                    print("      Post contains a video and no photo; neglecting video and using text caption only.")
                    return caption, None, external_link

            return caption, None, external_link

        scroll_attempts += 1
        print("      Scrolling further down to load feed posts...")
        page.mouse.wheel(0, 600)
        human_delay(1.8, 2.5)

    raise RuntimeError("Could not find an eligible post on the page.")


# ==============================================================================
# WhatsApp Channel Support (Public Single-Post Preview + Linked WhatsApp Web)
# ==============================================================================

def fetch_whatsapp_public_metadata(target_url: str) -> Tuple[Optional[str], Optional[str], Optional[str], bool]:
    """
    Fetch the public https://whatsapp.com/channel/... HTML to extract:
      - channel_name (from og:title)
      - og_description (channel bio OR specific post caption if single-post link)
      - og_image (channel icon OR specific post photo if single-post link)
      - is_single_post_link (True when URL is /channel/<channel_id>/<post_id>)
    """
    parsed = urlparse(target_url.strip())
    path_parts = [p for p in (parsed.path or "").split("/") if p]
    is_single_post = len(path_parts) >= 3 and path_parts[0].lower() == "channel"

    channel_name = None
    og_desc = None
    og_image = None

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(target_url, headers=headers, timeout=12)
        if resp.ok:
            title_m = re.search(
                r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
                resp.text,
                re.IGNORECASE,
            )
            if title_m:
                raw_t = html.unescape(title_m.group(1)).strip()
                channel_name = re.sub(
                    r"\s*\|\s*WhatsApp\s+Channel\s*$", "", raw_t, flags=re.IGNORECASE
                ).strip()

            desc_m = re.search(
                r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']',
                resp.text,
                re.IGNORECASE,
            )
            if desc_m:
                og_desc = html.unescape(desc_m.group(1)).strip()

            img_m = re.search(
                r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
                resp.text,
                re.IGNORECASE,
            )
            if img_m:
                og_image = html.unescape(img_m.group(1)).strip()
    except Exception as exc:
        print(f"      [!] Could not fetch WhatsApp public metadata: {exc}")

    return channel_name, og_desc, og_image, is_single_post


def dismiss_whatsapp_dialogs(page: Page) -> None:
    """Dismiss any modal dialog (e.g. 'What's new on WhatsApp Web' / 'Continue') covering WhatsApp Web."""
    for _ in range(3):
        dialog = page.locator('div[role="dialog"][aria-modal="true"], div[role="dialog"]').first
        if not dialog.is_visible(timeout=900):
            break
        clicked = False
        for sel in (
            'div[role="dialog"] button:has-text("Continue")',
            'div[role="dialog"] [aria-label="Close"]',
            'div[role="dialog"] button:has-text("OK")',
            'div[role="dialog"] button',
            'div[role="dialog"] div[role="button"]',
        ):
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=500):
                    btn.click(timeout=1500, force=True)
                    print(f"      [WhatsApp Web] Dismissed dialog via {sel}")
                    clicked = True
                    time.sleep(0.7)
                    break
            except Exception:
                continue
        if not clicked:
            try:
                page.keyboard.press("Escape")
                time.sleep(0.5)
            except Exception:
                break


def extract_latest_whatsapp_web_message(
    page: Page, channel_name: str, channel_url: str
) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Inside an authenticated WhatsApp Web session (`https://web.whatsapp.com`),
    open `channel_name` in Preview Mode (without clicking 'Follow') and extract
    the newest message at the bottom of the channel (caption, external link, and image).
    """
    print("      [WhatsApp Web] Opening https://web.whatsapp.com/ ...")
    if "web.whatsapp.com" not in page.url:
        page.goto("https://web.whatsapp.com/", wait_until="domcontentloaded", timeout=60000)

    try:
        page.wait_for_selector(
            '#pane-side, [aria-label="Channels"], [data-icon*="newsletter"], [aria-label="Chats"], [aria-label="Chat list"], canvas',
            timeout=60000,
        )
    except PlaywrightTimeoutError:
        raise RuntimeError("WhatsApp Web timed out while loading.")

    if (
        page.locator('canvas[aria-label*="Scan"]').count() > 0
        and page.locator('#pane-side, [aria-label="Chats"], [aria-label="Channels"]').count() == 0
    ):
        raise RuntimeError("Waiting for WhatsApp sync")

    # Wait briefly for any startup modal ('What's new on WhatsApp Web') to mount, then dismiss it
    human_delay(2.8, 3.6)
    dismiss_whatsapp_dialogs(page)
    channel_opened = False

    # Open Channels drawer & check list or search "Find channels" directory (Preview Mode, no Follow)
    print(f"      [WhatsApp Web] Opening Channels drawer to preview '{channel_name}' (without following)...")
    channel_tab_selectors = [
        '[aria-label="Channels"]',
        '[title="Channels"]',
        'span[data-icon="newsletter-tab"]',
        'span[data-icon="newsletter-outline"]',
        'div[role="button"][aria-label*="Channel" i]',
    ]
    for sel in channel_tab_selectors:
        loc = page.locator(sel).first
        if loc.is_visible(timeout=1200):
            loc.click(force=True)
            human_delay(1.5, 2.2)
            break

    dismiss_whatsapp_dialogs(page)

    # Check if already visible in followed Channels list
    direct_item = page.locator(f'span[title="{channel_name}"]').first
    if direct_item.is_visible(timeout=2000):
        direct_item.click()
        print(f"      [WhatsApp Web] Opened '{channel_name}' from Channels list!")
        channel_opened = True
        human_delay(1.8, 2.5)

    # For unfollowed channels, open directly by invite link via "(You)" Self-Chat in-app link click!
    if not channel_opened:
        print(f"      [WhatsApp Web] Opening unfollowed channel '{channel_name}' via in-app invite link ({channel_url})...")
        for chat_tab_sel in ('[aria-label="Chats"]', '[title="Chats"]', 'span[data-icon="chat"]'):
            ct = page.locator(chat_tab_sel).first
            if ct.is_visible(timeout=1200):
                ct.click(force=True)
                human_delay(1.0, 1.5)
                break

        self_chat_opened = False
        you_row = page.locator('#pane-side span:has-text("(You)")').first
        if you_row.is_visible(timeout=1500):
            you_row.click()
            self_chat_opened = True
            human_delay(1.2, 1.8)
        else:
            chat_search = page.locator('input[aria-label="Search or start a new chat"], input[placeholder*="chat" i]').first
            if chat_search.is_visible(timeout=2500):
                chat_search.click()
                chat_search.fill("(You)")
                human_delay(1.8, 2.5)
                you_match = page.locator('#pane-side span:has-text("(You)")').first
                if you_match.is_visible(timeout=3500):
                    you_match.click()
                    self_chat_opened = True
                    human_delay(1.5, 2.2)

        if self_chat_opened:
            page.wait_for_selector("#main", timeout=15000)
            invite_code = [p for p in channel_url.rstrip("/").split("/") if p][-1]
            existing_link = page.locator(f'#main a[href*="{invite_code}"]').last
            if not existing_link.is_visible(timeout=1500):
                msg_box = page.locator('#main footer div[contenteditable="true"]').first
                msg_box.click()
                msg_box.fill(channel_url)
                human_delay(1.0, 1.5)
                page.keyboard.press("Enter")
                print(f"      [WhatsApp Web] Sent invite link {channel_url} to '(You)' self-chat...")
                human_delay(2.5, 3.2)

            try:
                page.screenshot(path=WA_LIVE_IMG_PATH)
            except Exception:
                pass

            # Click "View channel" or the invite link anchor inside the message bubble
            view_channel_btn = page.locator(
                '#main :text-is("View channel"), #main div[role="button"]:has-text("View channel"), #main button:has-text("View channel")'
            ).last
            link_anchor = page.locator(f'#main a[href*="{invite_code}"]').last

            if view_channel_btn.is_visible(timeout=2000):
                view_channel_btn.click()
                print("      [WhatsApp Web] Clicked 'View channel' button in self-chat!")
                channel_opened = True
            elif link_anchor.is_visible(timeout=2500):
                link_anchor.evaluate("el => { el.removeAttribute('target'); el.removeAttribute('rel'); }")
                link_anchor.click()
                print("      [WhatsApp Web] Clicked in-app channel invite link in self-chat!")
                channel_opened = True

            if channel_opened:
                # Wait until #main switches away from the '(You)' self-chat to the actual channel header
                try:
                    page.wait_for_function(
                        "() => { const h = document.querySelector('#main header'); return h && !h.innerText.includes('(You)'); }",
                        timeout=15000,
                    )
                except Exception:
                    raise RuntimeError(f"Channel '{channel_name}' did not open from self-chat link.")
                human_delay(2.5, 3.5)

            try:
                page.screenshot(path=WA_LIVE_IMG_PATH)
            except Exception:
                pass

    if not channel_opened:
        raise RuntimeError(
            f"Could not locate '{channel_name}' in WhatsApp Web Channels directory."
        )

    # 3. Wait for #main preview feed to load (NOTE: We intentionally do NOT click 'Follow')
    page.wait_for_selector("#main", timeout=15000)
    human_delay(1.5, 2.2)

    # Click any "Read more" buttons on the latest messages
    try:
        read_more_btns = page.locator('#main span[role="button"]:has-text("Read more"), #main div[role="button"]:has-text("Read more")')
        if read_more_btns.count() > 0:
            read_more_btns.last.click(timeout=1500)
            human_delay(0.5, 0.9)
    except Exception:
        pass

    # 4. Extract the bottom-most message's text, external link, and image (converting blob: images via canvas)
    msg_data = page.evaluate(
        """() => {
            const mainEl = document.querySelector('#main');
            if (!mainEl) return null;

            const rows = Array.from(
                mainEl.querySelectorAll('div[role="row"], div.message-in, div[data-id]')
            );
            // Walk backwards from the bottom-most message bubble
            for (let i = rows.length - 1; i >= 0; i--) {
                const row = rows[i];

                // Prefer dedicated selectable-text caption spans; avoid grabbing both parent and child nodes
                let candidates = Array.from(row.querySelectorAll('span.selectable-text.copyable-text'));
                if (candidates.length === 0) {
                    candidates = Array.from(row.querySelectorAll('.selectable-text, span[dir="ltr"], div[dir="auto"]'));
                }

                // Filter out any candidate that contains another candidate (to prevent parent+child duplication)
                const leafNodes = candidates.filter(
                    el => !candidates.some(other => other !== el && el.contains(other))
                );

                // If there are nodes outside of a link-preview anchor, prefer those over the truncated link preview box
                const nonPreviewNodes = leafNodes.filter(el => !el.closest('a[href]'));
                const targetNodes = nonPreviewNodes.length > 0 ? nonPreviewNodes : leafNodes;

                const lines = [];
                const seenLines = new Set();
                for (const tn of targetNodes) {
                    if (tn.closest('[role="button"]')) continue;
                    const raw = (tn.innerText || '').trim();
                    if (!raw) continue;
                    for (const line of raw.split(/\\r?\\n/)) {
                        const cleanLine = line.trim();
                        if (!cleanLine) continue;
                        const norm = cleanLine.toLowerCase();
                        if (!seenLines.has(norm)) {
                            seenLines.add(norm);
                            lines.push(cleanLine);
                        }
                    }
                }
                const fullText = lines.join('\\n').trim();

                // Check for external link
                let externalLink = null;
                const anchors = row.querySelectorAll('a[href^="http"]');
                for (const a of anchors) {
                    if (!a.href.includes('whatsapp.com')) {
                        externalLink = a.href;
                        break;
                    }
                }

                // Check for large message photo (including blob: URLs)
                let imageDataUrl = null;
                const imgs = row.querySelectorAll('img');
                for (const img of imgs) {
                    const rect = img.getBoundingClientRect();
                    if (rect.width > 130 && rect.height > 110 && img.complete && img.naturalWidth > 130) {
                        try {
                            const canvas = document.createElement('canvas');
                            canvas.width = img.naturalWidth;
                            canvas.height = img.naturalHeight;
                            const ctx = canvas.getContext('2d');
                            ctx.drawImage(img, 0, 0);
                            imageDataUrl = canvas.toDataURL('image/jpeg', 0.92);
                            break;
                        } catch (e) {
                            if (img.src && img.src.startsWith('http')) {
                                imageDataUrl = img.src;
                                break;
                            }
                        }
                    }
                }

                if (fullText || imageDataUrl) {
                    return { fullText, externalLink, imageDataUrl };
                }
            }
            return null;
        }"""
    )

    if not msg_data:
        raise RuntimeError(f"No messages found inside WhatsApp Channel '{channel_name}'.")

    raw_text = msg_data.get("fullText", "") or ""
    external_link = msg_data.get("externalLink")
    image_data = msg_data.get("imageDataUrl")

    # Clean noise lines (timestamps, reaction counts, "Forwarded")
    cleaned_lines = []
    for line in raw_text.splitlines():
        s = SEE_MORE_TAIL_RE.sub("", line).strip()
        if not s or s.lower() in UI_NOISE_LINES or COMMENT_UI_LINE_RE.match(s) or s.isdigit():
            continue
        cleaned_lines.append(s)
    caption = "\n".join(cleaned_lines).strip()

    saved_image_path = None
    if image_data:
        if os.path.exists(TEMP_IMAGE_PATH):
            os.remove(TEMP_IMAGE_PATH)
        if image_data.startswith("data:image"):
            b64_part = image_data.split(",", 1)[1]
            with open(TEMP_IMAGE_PATH, "wb") as f:
                f.write(base64.b64decode(b64_part))
            saved_image_path = TEMP_IMAGE_PATH
            print(f"      [WhatsApp Web] Extracted channel image -> {TEMP_IMAGE_PATH}")
        elif image_data.startswith("http"):
            saved_image_path = download_image(image_data, TEMP_IMAGE_PATH)

    return caption, saved_image_path, external_link


def process_whatsapp_channel_url(page: Page, target_url: str) -> bool:
    """
    Process a WhatsApp Channel link (`https://whatsapp.com/channel/...`):
      1. Resolves the public channel name (and updates the watchlist label).
      2. If it is a direct link to a single post (`/channel/<id>/<post_id>`),
         extracts the post text and image straight from the public preview without login.
      3. If it is a main channel link (`/channel/<id>`), uses the linked WhatsApp Web
         session in `./fb_profile` to grab the latest post at the bottom of the channel.
    """
    update_watched_page_status(target_url, "Checking WhatsApp Channel...")
    channel_name, og_desc, og_image, is_single_post = fetch_whatsapp_public_metadata(target_url)

    if channel_name:
        update_watched_page_status(target_url, f"Scanning {channel_name}...", new_label=channel_name)

    try:
        if is_single_post and og_desc:
            print(f"      [WhatsApp Direct Post Link] Extracting public post preview for '{channel_name or target_url}'...")
            caption = og_desc
            downloaded_image = download_image(og_image, TEMP_IMAGE_PATH) if og_image else None
            urls = URL_RE.findall(caption)
            external_link = urls[0].rstrip(".,;)") if urls else target_url
        else:
            if not channel_name:
                raise RuntimeError("Could not resolve WhatsApp Channel name from link.")
            caption, downloaded_image, external_link = extract_latest_whatsapp_web_message(
                page, channel_name, target_url
            )
            if not external_link:
                urls = [u.rstrip(".,;)") for u in URL_RE.findall(caption) if "whatsapp.com" not in u.lower()]
                external_link = urls[0] if urls else None

        if not caption and not downloaded_image:
            update_watched_page_status(target_url, "No eligible post found", new_label=channel_name)
            return False

        if not caption:
            caption = f"(Image update from {channel_name or 'WhatsApp Channel'})"

        dedup_key = f"WA|{caption}|{external_link or ''}"
        post_hash = hashlib.sha256(dedup_key.encode("utf-8")).hexdigest()

        if is_post_seen(post_hash):
            print(f"      WhatsApp post already published (SHA-256: {post_hash[:12]}...). Skipping.")
            update_watched_page_status(target_url, "Up to date (no new post)", new_label=channel_name)
            return False

        publish_to_elo54_frontpage(
            page,
            caption,
            downloaded_image,
            external_link,
            source_context=f"{channel_name or ''}\n{og_desc or ''}\n{target_url}",
        )
        mark_post_seen(post_hash, caption)
        update_watched_page_status(target_url, "✅ Published new WhatsApp post!", new_label=channel_name)
        return True

    except Exception as exc:
        print(f"      [!] WhatsApp Channel check failed: {exc}")
        update_watched_page_status(target_url, f"⚠️ {str(exc)[:60]}", new_label=channel_name)
        return False
    finally:
        if os.path.exists(TEMP_IMAGE_PATH):
            try:
                os.remove(TEMP_IMAGE_PATH)
            except OSError:
                pass


def write_wa_status(state: str, message: str) -> None:
    try:
        with open(WA_STATUS_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {"state": state, "message": message, "updated_at": time.time()},
                f,
            )
    except Exception:
        pass


def connect_whatsapp_interactive() -> int:
    """
    Launch persistent Chromium at https://web.whatsapp.com/ using ./fb_profile
    and stream live screenshots of the QR code / WhatsApp Web screen to
    `static/wa_live.png` so the user can scan the QR code directly inside the
    http://127.0.0.1:5000 dashboard modal.
    """
    os.makedirs(PROFILE_DIR, exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, "static"), exist_ok=True)
    release_stale_profile_lock()
    write_wa_status("starting", "Launching WhatsApp Web and loading QR code...")

    with sync_playwright() as p:
        print(f"[WhatsApp Connect] Launching persistent Chromium at: {PROFILE_DIR}")
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            viewport={"width": 1100, "height": 820},
            args=["--disable-notifications", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto("https://web.whatsapp.com/", wait_until="domcontentloaded", timeout=60000)
            start_ts = time.time()
            connected_since = None

            while time.time() - start_ts < 240:
                try:
                    page.screenshot(path=WA_LIVE_IMG_PATH)
                except Exception:
                    pass

                # Check if logged into WhatsApp Web
                is_logged_in = (
                    page.locator('#pane-side, [aria-label="Channels"], [data-icon*="newsletter"], [aria-label="Chats"], [aria-label="Chat list"]').count() > 0
                    and page.locator('canvas[aria-label*="Scan" i]').count() == 0
                )

                if is_logged_in:
                    if connected_since is None:
                        connected_since = time.time()
                        print("[WhatsApp Connect] WhatsApp Web is linked! Saving session...")
                    write_wa_status(
                        "connected",
                        "✅ WhatsApp Web is connected and saved! You can close this popup and add any WhatsApp Channel link.",
                    )
                    # Keep running for 8 seconds after login so IndexedDB flushes completely
                    if time.time() - connected_since >= 8:
                        break
                else:
                    has_qr = page.locator('canvas[aria-label*="Scan" i], canvas').count() > 0
                    if has_qr:
                        write_wa_status(
                            "qr_ready",
                            "📱 Scan this QR code right now with your phone (WhatsApp → Linked Devices → Link a Device).",
                        )
                    else:
                        write_wa_status("loading", "Loading WhatsApp Web QR code...")

                time.sleep(1.5)

            return 0
        except Exception as exc:
            write_wa_status("error", f"Error: {exc}")
            return 1
        finally:
            try:
                context.close()
            except Exception:
                pass


# ==============================================================================
# Elo54 Pulse Modal Organizer & Publisher
# ==============================================================================

def organize_post_for_elo54(raw_caption: str, explicit_link: Optional[str] = None) -> Tuple[str, str, Optional[str]]:
    urls_in_text = URL_RE.findall(raw_caption)
    source_link = explicit_link or (urls_in_text[0].rstrip(".,;)") if urls_in_text else None)

    empty_social_or_domain_re = re.compile(
        r"^(?:"
        r"(?:linkedin|facebook|twitter|x|tiktok|youtube|instagram|telegram|whatsapp|website|portal|link|source|subscribe|don't forget to subscribe)\s*[:\-–—]?"
        r"|(?:www\.)?[a-z0-9\-]+\.(?:com|org|net|edu\.ng|gov\.ng|ng|co\.uk|tv|io)"
        r"|select\s+your\s+answer"
        r")$",
        re.IGNORECASE,
    )

    body_lines = []
    seen_norm = set()

    for line in raw_caption.splitlines():
        cleaned = URL_RE.sub("", line).strip()
        cleaned = SEE_MORE_TAIL_RE.sub("", cleaned).strip()
        cleaned = re.sub(
            r"(?:\s*(?:read\s+more|full\s+story|more\s+info|learn\s+more|details|source|link|don't\s+forget\s+to\s+subscribe)\s*[:\-–—]?\s*)+$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        # Skip truncated link-preview summary lines ending with "[…]" or "..."
        if cleaned.endswith("[…]") or cleaned.endswith("[...]"):
            continue
        if not cleaned or COMMENT_UI_LINE_RE.match(cleaned) or empty_social_or_domain_re.match(cleaned):
            continue

        norm = re.sub(r"\s+", " ", cleaned.lower()).rstrip(".-–— ")
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        body_lines.append(cleaned)

    # Second pass: only remove a long line (>= 35 chars) if another line starts with that exact prefix
    filtered_lines = []
    for line in body_lines:
        norm = re.sub(r"\s+", " ", line.lower()).rstrip(".-–— ")
        if len(norm) >= 35 and any(
            other != line
            and re.sub(r"\s+", " ", other.lower()).startswith(norm)
            and len(other) > len(line) + 5
            for other in body_lines
        ):
            continue
        filtered_lines.append(line)

    details = "\n\n".join(filtered_lines).strip() or raw_caption.strip()

    first_line = filtered_lines[0] if filtered_lines else details
    first_sentence = re.split(r"(?<=[.!?])\s+", first_line)[0].strip()
    headline = first_sentence if first_sentence else first_line

    alpha_chars = [c for c in headline if c.isalpha()]
    if alpha_chars and sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars) > 0.65:
        headline = headline.capitalize()

    headline = headline.rstrip(".")
    if len(headline) > 140:
        headline = headline[:137].rsplit(" ", 1)[0] + "..."

    # If Details starts with the exact same sentence as Headline and has more paragraphs, avoid repeating the headline in line 1
    if len(filtered_lines) > 1 and filtered_lines[0].rstrip(".").lower() == headline.lower():
        details = "\n\n".join(filtered_lines[1:]).strip()

    return headline, details, source_link


# 4-Tier Location Rules: (Canonical Display Label, [regex/keyword triggers])
LOCATION_RULES = [
    # Tier 1: Nigerian Universities, Polytechnics & Campuses
    ("University of Lagos (UNILAG)", [r"\bunilag\b", r"university of lagos", r"\bakoka\b"]),
    ("Federal University Oye-Ekiti (FUOYE)", [r"\bfuoye\b", r"federal university oye", r"oye[\s\-]+ekiti"]),
    ("University of Ibadan (UI)", [r"university of ibadan", r"\bu\.i\b", r"\bui\s+ibadan\b"]),
    ("Federal University of Technology, Akure (FUTA)", [r"\bfuta\b", r"federal university of technology,?\s*akure"]),
    ("Obafemi Awolowo University (OAU Ife)", [r"\boau\b", r"obafemi awolowo university", r"ile[\s\-]+ife"]),
    ("Ahmadu Bello University (ABU Zaria)", [r"\babu\s+zaria\b", r"ahmadu bello university"]),
    ("Bayero University Kano (BUK)", [r"\bbuk\b", r"bayero university"]),
    ("University of Nigeria, Nsukka (UNN)", [r"\bunn\b", r"university of nigeria", r"\bnsukka\b"]),
    ("University of Benin (UNIBEN)", [r"\buniben\b", r"university of benin"]),
    ("University of Ilorin (UNILORIN)", [r"\bunilorin\b", r"university of ilorin"]),
    ("Lagos State University (LASU)", [r"\blasu\b", r"lagos state university"]),
    ("University of Port Harcourt (UNIPORT)", [r"\buniport\b", r"university of port harcourt"]),
    ("Nnamdi Azikiwe University (UNIZIK)", [r"\bunizik\b", r"nnamdi azikiwe university"]),
    ("Federal University of Technology, Owerri (FUTO)", [r"\bfuto\b", r"federal university of technology,?\s*owerri"]),
    ("Federal University of Agriculture, Abeokuta (FUNAAB)", [r"\bfunaab\b", r"federal university of agriculture,?\s*abeokuta"]),
    ("University of Calabar (UNICAL)", [r"\bunical\b", r"university of calabar"]),
    ("University of Uyo (UNIUYO)", [r"\buniuyo\b", r"university of uyo"]),
    ("Ekiti State University (EKSU)", [r"\beksu\b", r"ekiti state university"]),
    ("Ladoke Akintola University of Technology (LAUTECH)", [r"\blautech\b", r"ladoke akintola", r"\bogbomoso\b"]),
    ("Olabisi Onabanjo University (OOU)", [r"\boou\b", r"olabisi onabanjo", r"ago[\s\-]+iwoye"]),
    ("Delta State University (DELSU)", [r"\bdelsu\b", r"delta state university", r"\babraka\b"]),
    ("Ambrose Alli University (AAU Ekpoma)", [r"\baau\b", r"ambrose alli", r"\bekpoma\b"]),
    ("Kwara State University (KWASU)", [r"\bkwasu\b", r"kwara state university"]),
    ("University of Jos (UNIJOS)", [r"\bunijos\b", r"university of jos"]),
    ("University of Maiduguri (UNIMAID)", [r"\bunimaid\b", r"university of maiduguri"]),
    ("Usmanu Danfodiyo University (UDUS)", [r"\budus\b", r"usmanu danfodiyo"]),
    ("Federal University of Technology, Minna (FUTMINNA)", [r"\bfutminna\b"]),
    ("Covenant University, Ota", [r"covenant university"]),
    ("Babcock University", [r"babcock university"]),
    ("Afe Babalola University (ABUAD)", [r"\babuad\b", r"afe babalola"]),
    ("Yaba College of Technology (YABATECH)", [r"\byabatech\b", r"yaba college of technology"]),
    ("Auchi Polytechnic", [r"\bauchipoly\b", r"auchi polytechnic"]),
    ("Federal Polytechnic, Nekede", [r"poly\s+nekede", r"\bfpno\b", r"\bnekede\b"]),

    # Tier 2: All 36 Nigerian States + FCT + Major Cities
    ("Abuja (FCT)", [r"\babuja\b", r"\bfct\b", r"\baso\s+rock\b", r"\bgarki\b", r"\bwuse\b", r"\bmaitama\b"]),
    ("Lagos", [r"\blagos\b", r"\bikeja\b", r"\blekki\b", r"\bikoyi\b", r"\byaba\b", r"\bsurulere\b", r"\bvictoria\s+island\b", r"\bbadagry\b", r"\bikorodu\b", r"\bepe\b"]),
    ("Ibadan", [r"\bibadan\b"]),
    ("Port Harcourt", [r"port\s+harcourt", r"\bph\s+city\b"]),
    ("Ado-Ekiti, Ekiti", [r"ado[\s\-]+ekiti", r"\bekiti\b"]),
    ("Akure, Ondo", [r"\bakure\b", r"\bondo\b"]),
    ("Abeokuta, Ogun", [r"\babeokuta\b", r"\bogun\b", r"\bsagamu\b", r"\bijebu\b"]),
    ("Osogbo, Osun", [r"\bosogbo\b", r"\bosun\b", r"\bede\b"]),
    ("Ilorin, Kwara", [r"\bilorin\b", r"\bkwara\b"]),
    ("Benin City", [r"benin\s+city", r"\bedo\s+state\b"]),
    ("Warri, Delta", [r"\bwarri\b"]),
    ("Asaba, Delta", [r"\basaba\b", r"\bdelta\s+state\b"]),
    ("Enugu", [r"\benugu\b"]),
    ("Awka, Anambra", [r"\bawka\b", r"\banambra\b"]),
    ("Onitsha, Anambra", [r"\bonitsha\b", r"\bnnewi\b"]),
    ("Owerri, Imo", [r"\bowerri\b", r"\bimo\s+state\b"]),
    ("Umuahia, Abia", [r"\bumuahia\b", r"\babia\b"]),
    ("Aba, Abia", [r"\baba\b"]),
    ("Calabar, Cross River", [r"\bcalabar\b", r"cross\s+river"]),
    ("Uyo, Akwa Ibom", [r"\buyo\b", r"akwa\s+ibom"]),
    ("Yenagoa, Bayelsa", [r"\byenagoa\b", r"\bbayelsa\b"]),
    ("Abakaliki, Ebonyi", [r"\babakaliki\b", r"\bebonyi\b"]),
    ("Kaduna", [r"\bkaduna\b"]),
    ("Zaria, Kaduna", [r"\bzaria\b"]),
    ("Kano", [r"\bkano\b"]),
    ("Katsina", [r"\bkatsina\b"]),
    ("Sokoto", [r"\bsokoto\b"]),
    ("Maiduguri, Borno", [r"\bmaiduguri\b", r"\bborno\b"]),
    ("Jos, Plateau", [r"\bjos\b", r"\bplateau\b"]),
    ("Makurdi, Benue", [r"\bmakurdi\b", r"\bbenue\b"]),
    ("Lokoja, Kogi", [r"\blokoja\b", r"\bkogi\b"]),
    ("Minna, Niger", [r"\bminna\b", r"\bniger\s+state\b"]),
    ("Lafia, Nasarawa", [r"\blafia\b", r"\bnasarawa\b"]),
    ("Bauchi", [r"\bbauchi\b"]),
    ("Gombe", [r"\bgombe\b"]),
    ("Yola, Adamawa", [r"\byola\b", r"\badamawa\b"]),
    ("Jalingo, Taraba", [r"\bjalingo\b", r"\btaraba\b"]),
    ("Damaturu, Yobe", [r"\bdamaturu\b", r"\byobe\b"]),
    ("Gusau, Zamfara", [r"\bgusau\b", r"\bzamfara\b"]),
    ("Birnin Kebbi, Kebbi", [r"birnin\s+kebbi", r"\bkebbi\b"]),
    ("Dutse, Jigawa", [r"\bdutse\b", r"\bjigawa\b"]),

    # Tier 3: Global / Foreign Cities, US States & Countries
    ("Florida, USA", [r"\bflorida\b", r"\bmiami\b", r"\borlando\b", r"brevard\s+county", r"\btampa\b"]),
    ("San Francisco, USA", [r"san\s+francisco", r"\bsilicon\s+valley\b"]),
    ("Seattle, USA", [r"\bseattle\b", r"seattle[\s\-]+tacoma"]),
    ("California, USA", [r"\bcalifornia\b", r"los\s+angeles", r"\bhollywood\b", r"san\s+diego"]),
    ("New York, USA", [r"new\s+york", r"\bmanhattan\b", r"\bbrooklyn\b", r"\bwall\s+street\b"]),
    ("Washington, D.C., USA", [r"washington,?\s*d\.?c\.?", r"capitol\s+hill", r"white\s+house", r"\bu\.?s\.?\s+senate\b", r"\bpentagon\b"]),
    ("Arizona, USA", [r"\barizona\b", r"\bphoenix\b"]),
    ("Ohio, USA", [r"\bcincinnati\b", r"\bohio\b", r"\bcleveland\b"]),
    ("Texas, USA", [r"\btexas\b", r"\bhouston\b", r"\bdallas\b", r"\baustin\b"]),
    ("Chicago, USA", [r"\bchicago\b", r"\billinois\b"]),
    ("Atlanta, USA", [r"\batlanta\b", r"\bgeorgia\b"]),
    ("United Kingdom", [r"\buk\b", r"united\s+kingdom", r"\bbritain\b", r"\bbritish\b", r"\blondon\b", r"\bmanchester\b", r"\bbrexit\b", r"prime\s+minister\s+theresa\s+may", r"andy\s+burnham"]),
    ("North Atlantic", [r"north\s+atlantic", r"\biceland\b"]),
    ("Iran", [r"\biran\b", r"\btehran\b"]),
    ("Italy", [r"\bitaly\b", r"\brome\b", r"\bmilan\b"]),
    ("Spain", [r"\bspain\b", r"\bmadrid\b", r"\bbarcelona\b"]),
    ("France", [r"\bfrance\b", r"\bparis\b"]),
    ("Germany", [r"\bgermany\b", r"\bberlin\b"]),
    ("South Africa", [r"south\s+africa", r"\bjohannesburg\b"]),
    ("Ghana", [r"\bghana\b", r"\baccra\b"]),
    ("Kenya", [r"\bkenya\b", r"\bnairobi\b"]),
    ("Egypt", [r"\begypt\b", r"\bcairo\b"]),
    ("Morocco", [r"\bmorocco\b", r"\brabat\b"]),
    ("Canada", [r"\bcanada\b", r"\btoronto\b", r"\bottawa\b"]),
    ("India", [r"\bindia\b", r"\bmumbai\b", r"\bdelhi\b", r"\bchennai\b"]),
    ("China", [r"\bchina\b", r"\bbeijing\b", r"\bshanghai\b"]),
    ("Russia", [r"\brussia\b", r"\bmoscow\b"]),
    ("Ukraine", [r"\bukraine\b", r"\bkyiv\b"]),
    ("Israel", [r"\bisrael\b", r"\btel\s+aviv\b", r"\bjerusalem\b"]),
    ("Palestine / Gaza", [r"\bgaza\b", r"\bpalestine\b"]),
    ("Saudi Arabia", [r"saudi\s+arabia", r"\briyadh\b"]),
    ("UAE / Dubai", [r"\bdubai\b", r"\buae\b", r"abu\s+dhabi"]),
    ("Australia", [r"\baustralia\b", r"\bsydney\b", r"\bmelbourne\b"]),
    ("Japan", [r"\bjapan\b", r"\btokyo\b"]),
    ("Brazil", [r"\bbrazil\b"]),
    ("Nigeria", [r"\bnigeria\b", r"\bnigerian\b", r"\bnysc\b", r"\bnelfund\b"]),
    ("United States", [r"\busa\b", r"united\s+states"]),
]


def detect_actual_location(full_text: str, source_context: str = "") -> str:
    """
    Detect the actual location (Campus -> Nigerian City/State -> Global Place)
    only when explicitly mentioned in the post text or channel/page name.
    If not sure, returns empty string ("") instead of guessing or defaulting to Kano.
    """
    text_lower = (full_text or "").lower()
    for canonical_label, patterns in LOCATION_RULES:
        for pat in patterns:
            if re.search(pat, text_lower, flags=re.IGNORECASE):
                return canonical_label

    ctx_lower = (source_context or "").lower()
    if ctx_lower:
        for canonical_label, patterns in LOCATION_RULES:
            for pat in patterns:
                if re.search(pat, ctx_lower, flags=re.IGNORECASE):
                    return canonical_label

    prep_match = re.search(
        r"\b(?:in|at|from|across|throughout)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2}\s+(?:County|State|City|University|College|Polytechnic|Island|Airport))",
        full_text or "",
    )
    if prep_match:
        return prep_match.group(1).strip()

    return ""


def select_perceived_location_or_kano(page: Page, full_text: str, source_context: str = "") -> str:
    detected = detect_actual_location(full_text, source_context)
    select_el = page.locator("#campus-city")

    if not detected:
        select_el.evaluate(
            """(sel) => {
                sel.value = '';
                sel.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        return ""

    # Dynamically ensure the detected location exists as an <option> in #campus-city and select it
    select_el.evaluate(
        """(sel, loc) => {
            let opt = Array.from(sel.options).find(o => o.value.toLowerCase() === loc.toLowerCase());
            if (!opt) {
                opt = document.createElement('option');
                opt.value = loc;
                opt.textContent = loc;
                sel.appendChild(opt);
            }
            sel.value = opt.value;
            sel.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        detected,
    )
    return detected


def publish_to_elo54_frontpage(
    page: Page,
    raw_caption: str,
    image_path: Optional[str],
    external_link: Optional[str] = None,
    source_context: str = "",
) -> None:
    headline, details, source_link = organize_post_for_elo54(raw_caption, external_link)
    if not headline or headline.lower().startswith("http"):
        print("      [!] Skipping post with no non-URL text headline.")
        return

    print(f"[4/5] Opening Elo54 Pulse Frontpage: {TEST_SITE_URL} ...")
    page.goto(TEST_SITE_URL, wait_until="domcontentloaded", timeout=30000)
    human_delay(0.8, 1.2)

    print("      Clicking '+ Post News / Event' to open the modal...")
    page.click("#open-post-modal-btn")
    page.wait_for_selector("#post-modal-backdrop.open", timeout=10000)
    human_delay(0.5, 0.9)

    print("[5/5] Organizing and filling 'Shine a Light on Your Campus or City' modal...")
    news_tab = page.locator('.cat-tab[data-cat="News / Gist"]').first
    if news_tab.is_visible():
        news_tab.click()
        human_delay(0.3, 0.5)

    chosen_city = select_perceived_location_or_kano(
        page, raw_caption, source_context=f"{source_context}\n{source_link or ''}"
    )
    print(f"      Campus or City : {chosen_city}")
    human_delay(0.3, 0.6)

    print(f"      Headline       : {headline}")
    page.fill("#post-headline", headline)
    human_delay(0.4, 0.7)

    print(f"      Details        : {details[:90]}{'...' if len(details) > 90 else ''}")
    page.fill("#post-text", details)
    human_delay(0.4, 0.7)

    if image_path and os.path.exists(image_path):
        print(f"      Image          : {image_path}")
        page.set_input_files("#post-image", image_path)
        human_delay(0.4, 0.7)

    if source_link:
        print(f"      Source Link    : {source_link}")
        page.fill("#source-link", source_link)
        human_delay(0.4, 0.7)

    print("      Clicking 'Send for Review'...")
    page.click("#publish-btn")
    page.wait_for_selector("#live-feed .post-card", timeout=15000)
    print("      Success! Organized post published to Elo54 Pulse Live Feed.")
    human_delay(1.8, 2.5)


def process_single_watched_url(page: Page, target_url: str) -> bool:
    """Route URL to either the WhatsApp Channel scraper or the Facebook Page scraper."""
    if is_whatsapp_url(target_url):
        return process_whatsapp_channel_url(page, target_url)

    update_watched_page_status(target_url, "Scanning page...")
    try:
        navigate_to_target_page(page, target_url)
        caption, downloaded_image, external_link = find_and_extract_latest_post(page)

        if not caption and not downloaded_image and not external_link:
            update_watched_page_status(target_url, "No eligible post found")
            return False

        if not caption:
            caption = f"(Image post from {target_url})"

        print("[3/5] Checking SQLite deduplication database (agent_memory.db)...")
        dedup_key = f"{caption}|{external_link or ''}"
        post_hash = hashlib.sha256(dedup_key.encode("utf-8")).hexdigest()

        if is_post_seen(post_hash):
            print(f"      Post already published (SHA-256: {post_hash[:12]}...). Skipping.")
            update_watched_page_status(target_url, "Up to date (no new post)")
            return False

        fb_title = ""
        try:
            fb_title = page.title()
        except Exception:
            pass

        publish_to_elo54_frontpage(
            page,
            caption,
            downloaded_image,
            external_link,
            source_context=f"{fb_title}\n{target_url}",
        )
        mark_post_seen(post_hash, caption)
        update_watched_page_status(target_url, "✅ Published new post!")
        print(f"      Recorded post hash {post_hash[:12]}... in agent_memory.db.")
        return True

    except Exception as exc:
        print(f"      [!] Error checking {target_url}: {exc}")
        update_watched_page_status(target_url, f"Scan error: {str(exc)[:45]}")
        return False
    finally:
        if os.path.exists(TEMP_IMAGE_PATH):
            try:
                os.remove(TEMP_IMAGE_PATH)
            except OSError:
                pass


def get_last_retrieved_post_from_db() -> Tuple[str, Optional[str], Optional[str]]:
    with sqlite3.connect(SANDBOX_DB_PATH) as conn:
        row = conn.execute(
            "SELECT content, image_filename, source_url FROM posts ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        raise RuntimeError("No existing post found in sandbox_posts.db.")
    content, image_filename, source_url = row
    image_path = os.path.join(UPLOADS_DIR, image_filename) if image_filename else None
    if image_path and not os.path.exists(image_path):
        image_path = None
    return content or "", image_path, source_url


def run_browser_session(mode: str, explicit_url: Optional[str] = None) -> int:
    init_memory_db()
    os.makedirs(PROFILE_DIR, exist_ok=True)
    release_stale_profile_lock()
    headless = get_sandbox_setting("headless_mode", "0") == "1"

    with sync_playwright() as p:
        print(f"[0/5] Launching persistent Chromium context (headless={headless}) at: {PROFILE_DIR}")
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=headless,
            viewport={"width": 1280, "height": 880},
            args=["--disable-notifications", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            if mode == "repost_last":
                caption, image_path, external_link = get_last_retrieved_post_from_db()
                publish_to_elo54_frontpage(page, caption, image_path, external_link)
                return 0

            if mode == "watch_cycle":
                pages = get_enabled_watched_pages()
                if not pages:
                    print("[Watch Cycle] No enabled links in watchlist.")
                    return 0
                print(f"[Watch Cycle] Scanning {len(pages)} active link(s)...")
                new_posts = 0
                for idx, (_, url, label) in enumerate(pages, start=1):
                    print(f"\n--- [{idx}/{len(pages)}] Checking {label} ({url}) ---")
                    if process_single_watched_url(page, url):
                        new_posts += 1
                    if idx < len(pages):
                        human_delay(2.0, 4.0)
                print(f"\n[Watch Cycle Complete] Published {new_posts} new post(s) across {len(pages)} link(s).")
                return 0

            target_url = explicit_url or DEFAULT_FB_TARGET_URL
            process_single_watched_url(page, target_url)
            return 0

        finally:
            try:
                context.close()
            except Exception:
                pass


def main() -> int:
    arg = sys.argv[1].strip() if len(sys.argv) > 1 else ""

    if arg == "--connect-whatsapp":
        return connect_whatsapp_interactive()

    if arg == "--repost-last":
        return run_browser_session("repost_last")

    if arg == "--watch-cycle":
        return run_browser_session("watch_cycle")

    if arg == "--daemon":
        print("=== Starting Standalone 5-Minute Multi-Page Auto-Pilot Daemon ===")
        while True:
            interval_mins = max(1, int(get_sandbox_setting("poll_interval_minutes", "5") or "5"))
            run_browser_session("watch_cycle")
            print(f"[Daemon] Sleeping for {interval_mins} minutes until next check...")
            time.sleep(interval_mins * 60)

    if arg.startswith("http"):
        return run_browser_session("single", explicit_url=arg)

    if get_enabled_watched_pages():
        return run_browser_session("watch_cycle")
    return run_browser_session("single", explicit_url=DEFAULT_FB_TARGET_URL)


if __name__ == "__main__":
    sys.exit(main())
