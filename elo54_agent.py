import os
import re
import sqlite3
import sys
import time
from typing import Optional, Tuple

from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeoutError

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "fb_profile")
SANDBOX_DB_PATH = os.path.join(BASE_DIR, "sandbox_posts.db")
UPLOADS_DIR = os.path.join(BASE_DIR, "static", "uploads")
ELO54_URL = os.getenv("ELO54_URL", "https://elo54.com").strip()
DEFAULT_FALLBACK_CITY = "Kano"

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def get_latest_retrieved_post() -> Tuple[int, str, Optional[str]]:
    """Fetch the most recent Facebook post stored in sandbox_posts.db."""
    if not os.path.exists(SANDBOX_DB_PATH):
        raise RuntimeError(f"Database not found at {SANDBOX_DB_PATH}. Run agent.py first.")

    with sqlite3.connect(SANDBOX_DB_PATH) as conn:
        row = conn.execute(
            "SELECT id, content, image_filename FROM posts ORDER BY id DESC LIMIT 1"
        ).fetchone()

    if not row:
        raise RuntimeError("No posts found in sandbox_posts.db. Retrieve a Facebook post first.")

    post_id, content, image_filename = row
    image_path = os.path.join(UPLOADS_DIR, image_filename) if image_filename else None
    if image_path and not os.path.exists(image_path):
        image_path = None

    return post_id, (content or "").strip(), image_path


def organize_post_for_elo54(raw_content: str) -> Tuple[str, str, Optional[str]]:
    """
    Organize raw Facebook post text into:
      - headline (factual, <= 140 chars, no ALL CAPS)
      - details (full clean post text without bare trailing URL)
      - source_link (first extracted http/https URL if present)
    """
    urls = URL_RE.findall(raw_content)
    source_link = urls[0].rstrip(".,;)") if urls else None

    # Remove standalone URL lines from body text
    body_lines = []
    for line in raw_content.splitlines():
        cleaned_line = URL_RE.sub("", line).strip()
        if cleaned_line:
            body_lines.append(cleaned_line)

    details = "\n\n".join(body_lines).strip() or raw_content.strip()

    # Build a clean headline from the first sentence/line (max 140 chars, no ALL CAPS)
    first_line = body_lines[0] if body_lines else details
    first_sentence = re.split(r"(?<=[.!?])\s+", first_line)[0].strip()
    headline = first_sentence if first_sentence else first_line

    # Convert ALL CAPS headlines into readable sentence/title case
    alpha_chars = [c for c in headline if c.isalpha()]
    if alpha_chars and sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars) > 0.65:
        headline = headline.capitalize()

    headline = headline.rstrip(".")
    if len(headline) > 140:
        headline = headline[:137].rsplit(" ", 1)[0] + "..."

    return headline, details, source_link


def select_perceived_location_or_kano(page: Page, full_text: str) -> str:
    """
    Inspect the 'Campus or City' dropdown on elo54.com, match any location
    mentioned in the post text, or default to 'Kano'.
    """
    select_el = page.locator("select").first
    if select_el.count() > 0 and select_el.is_visible(timeout=3000):
        options = select_el.evaluate(
            """(sel) => Array.from(sel.options).map(o => ({
                value: o.value,
                label: (o.textContent || '').trim()
            })).filter(o => o.value && !o.label.toLowerCase().includes('select '))"""
        )

        text_lower = full_text.lower()
        chosen = None

        # 1. Try matching a campus/city/state mentioned in the post text
        for opt in options:
            label_clean = opt["label"].lower()
            tokens = [t.strip() for t in re.split(r"[,()/ -]+", label_clean) if len(t.strip()) >= 4]
            if label_clean in text_lower or any(
                re.search(rf"\b{re.escape(tok)}\b", text_lower) for tok in tokens
            ):
                chosen = opt
                break

        # 2. Fallback to Kano
        if not chosen:
            for opt in options:
                if DEFAULT_FALLBACK_CITY.lower() in opt["label"].lower() or DEFAULT_FALLBACK_CITY.lower() in opt["value"].lower():
                    chosen = opt
                    break

        if not chosen and options:
            chosen = options[0]

        if chosen:
            select_el.select_option(value=chosen["value"])
            return chosen["label"]

    # Custom combobox/dropdown fallback if not a native <select>
    dropdown_trigger = page.locator(
        'text="Select a campus, city, or state...", [placeholder*="campus" i]'
    ).first
    if dropdown_trigger.is_visible(timeout=2000):
        dropdown_trigger.click()
        time.sleep(0.6)
        kano_opt = page.locator(f'text="{DEFAULT_FALLBACK_CITY}"').first
        if kano_opt.is_visible(timeout=2000):
            kano_opt.click()
            return DEFAULT_FALLBACK_CITY

    return DEFAULT_FALLBACK_CITY


def main() -> int:
    post_id, raw_content, image_path = get_latest_retrieved_post()
    headline, details, source_link = organize_post_for_elo54(raw_content)

    print(f"=== Preparing Post #{post_id} for Elo54 ===")
    print(f"Headline    : {headline}")
    print(f"Details     : {details}")
    print(f"Source Link : {source_link or '(none)'}")
    print(f"Image Path  : {image_path or '(none)'}")
    print("==========================================")

    with sync_playwright() as p:
        print(f"[1/4] Launching persistent Chromium session ({PROFILE_DIR})...")
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 880},
            args=["--disable-notifications"],
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            print(f"[2/4] Navigating to {ELO54_URL} ...")
            page.goto(ELO54_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2.5)

            print("[3/4] Clicking '+ Post News / Event'...")
            post_btn = page.locator(
                'button:has-text("Post News"), a:has-text("Post News"), :text-matches("\\\\+\\\\s*Post News", "i")'
            ).first
            post_btn.wait_for(state="visible", timeout=20000)
            post_btn.click()
            time.sleep(1.5)

            # If sign-in modal appears instead of the post form, wait for the user to sign in
            if page.locator('text="Sign in to Elo54"').is_visible(timeout=2000):
                print("      [!] 'Sign in to Elo54' detected. Please complete sign-in in the open browser window...")
                page.wait_for_selector('text="Shine a Light on Your Campus or City"', timeout=300000)

            print("[4/4] Filling out 'Shine a Light on Your Campus or City' form...")
            # 1. Category: News / Gist
            news_gist_btn = page.locator('button:has-text("News / Gist"), div[role="button"]:has-text("News / Gist")').first
            if news_gist_btn.is_visible(timeout=2000):
                news_gist_btn.click()
                time.sleep(0.4)

            # 2. Campus or City (Perceived location -> fallback to Kano)
            selected_location = select_perceived_location_or_kano(page, raw_content)
            print(f"      Selected Campus/City: {selected_location}")
            time.sleep(0.4)

            # 3. Headline
            headline_input = page.locator(
                'input[placeholder*="Wellness" i], input[name*="headline" i], input[name*="title" i], label:has-text("Headline") + input, label:has-text("Headline") ~ input'
            ).first
            if not headline_input.is_visible(timeout=1500):
                headline_input = page.locator('input[type="text"]').first
            headline_input.fill(headline)
            time.sleep(0.4)

            # 4. Details
            details_textarea = page.locator("textarea").first
            details_textarea.fill(details)
            time.sleep(0.4)

            # 5. Image attachment
            if image_path and os.path.exists(image_path):
                file_input = page.locator('input[type="file"]').first
                file_input.set_input_files(image_path)
                print(f"      Attached image: {image_path}")
                time.sleep(0.5)

            # 6. Source / Registration Link
            if source_link:
                source_input = page.locator(
                    'input[placeholder="https://..."], input[name*="source" i], label:has-text("Source") ~ input'
                ).first
                if source_input.is_visible(timeout=2000):
                    source_input.fill(source_link)
                    print(f"      Filled Source Link: {source_link}")
                    time.sleep(0.4)

            print("\n[READY] All post fields have been organized and filled!")
            print("        Please tick the Cloudflare 'Verify you are human' box (if prompted)")
            print("        and click 'Send for Review' in the open browser window.")
            print("        Close the browser window when you are done.")

            page.wait_for_event("close", timeout=600000)
            return 0

        except PlaywrightTimeoutError as exc:
            print(f"[ERROR] Timed out waiting for element on elo54.com: {exc}")
            return 1
        except Exception as exc:
            print(f"[ERROR] Elo54 agent encountered an error: {exc}")
            return 1
        finally:
            context.close()


if __name__ == "__main__":
    sys.exit(main())
