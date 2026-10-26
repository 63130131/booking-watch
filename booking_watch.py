#!/usr/bin/env python3
"""Watch Booking.com for newly available accommodation and push to phone via ntfy.sh."""

import json
import logging
import os
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# ============================ CONFIG (edit me) ============================
CITY = "Ljubljana, Slovenia"      # TODO: set your "City, Country"
CHECKIN = "2026-11-23"            # YYYY-MM-DD
CHECKOUT = "2026-11-26"           # YYYY-MM-DD
ADULTS = 2
ROOMS = 1
APARTMENTS_ONLY = True            # True = apartments only, False = any property type
MAX_TOTAL_PRICE = 0               # EUR for the whole stay, 0 = no limit
CURRENCY = "EUR"

NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "change-me-to-a-long-random-string")  # env var wins; anyone who knows it can read it

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "booking_watch.log"
PAGE_TIMEOUT_MS = 45_000
# ==========================================================================

APARTMENT_FILTER = "ht_id=201"  # Booking.com property type id for apartments

CAPTCHA_MARKERS = ("captcha", "awswaf", "are you a robot", "verify you are human",
                   "bot check", "unusual traffic")
EMPTY_RE = re.compile(r"no availability|0 properties|no properties|couldn.t find any", re.I)

log = logging.getLogger("booking_watch")


def build_url() -> str:
    params = {
        "ss": CITY,
        "checkin": CHECKIN,
        "checkout": CHECKOUT,
        "group_adults": ADULTS,
        "group_children": 0,
        "no_rooms": ROOMS,
        "order": "price",  # lowest price first
        "selected_currency": CURRENCY,
        "lang": "en-us",
    }
    if APARTMENTS_ONLY:
        params["nflt"] = APARTMENT_FILTER
    return "https://www.booking.com/searchresults.html?" + urlencode(params)


def parse_price(text: str):
    """'€ 1,234' / 'EUR 1.234' / '345 €' -> int, or None."""
    m = re.search(r"\d[\d.,\s ]*", text or "")
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    return int(digits) if digits else None


def scrape(url: str):
    """Return list of dicts (id, name, price, link), or None if blocked / page failed."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            try:
                page.wait_for_selector('[data-testid="property-card"]', timeout=20_000)
            except PWTimeout:
                pass
            html = page.content().lower()
            if any(m in html for m in CAPTCHA_MARKERS) and \
                    page.locator('[data-testid="property-card"]').count() == 0:
                log.warning("CAPTCHA / bot check detected. Not bypassing; exiting.")
                return None

            cards = page.locator('[data-testid="property-card"]')
            n = cards.count()
            if n == 0:
                if EMPTY_RE.search(page.inner_text("body")):
                    log.info("Page loaded, no properties available.")
                    return []
                log.warning("No property cards and no 'empty' message; treating as failed load.")
                return None

            results = []
            for i in range(n):
                c = cards.nth(i)
                try:
                    name = c.locator('[data-testid="title"]').first.inner_text(timeout=2000).strip()
                    href = c.locator('[data-testid="title-link"]').first.get_attribute("href", timeout=2000)
                    price_txt = ""
                    pl = c.locator('[data-testid="price-and-discounted-price"]')
                    if pl.count():
                        price_txt = pl.last.inner_text(timeout=2000)
                except PWTimeout:
                    continue
                if not href:
                    continue
                path = urlparse(href).path
                pid = path.rstrip("/").split("/")[-1].removesuffix(".html")
                # Strip tracking query params, keep dates/guests so the link shows availability
                link = f"https://www.booking.com{path}?" + urlencode({
                    "checkin": CHECKIN, "checkout": CHECKOUT,
                    "group_adults": ADULTS, "no_rooms": ROOMS, "group_children": 0,
                })
                results.append({"id": pid, "name": name,
                                "price": parse_price(price_txt), "price_txt": price_txt.strip(),
                                "link": link})
            return results
        finally:
            browser.close()


def notify(title: str, message: str, click: str = None, priority: int = 3):
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message,
               "priority": priority, "tags": ["house"]}
    if click:
        payload["click"] = click
    req = urllib.request.Request(
        NTFY_SERVER, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def load_state():
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()])

    url = build_url()
    log.info("Run start: %s", url)
    try:
        found = scrape(url)
    except Exception as e:  # network/browser errors: log, keep old state
        log.error("Scrape failed: %s", e)
        return 1
    if found is None:
        return 0  # CAPTCHA or failed load: exit cleanly, state untouched

    if MAX_TOTAL_PRICE:
        found = [f for f in found if f["price"] is not None and f["price"] <= MAX_TOTAL_PRICE]
    current = {f["id"]: f for f in found}
    log.info("%d places available", len(current))

    state = load_state()
    try:
        if state is None:
            notify(f"Watching {CITY}",
                   f"{len(current)} places available for {CHECKIN} → {CHECKOUT}. Now watching.",
                   click=url)
        else:
            previous = set(state.get("ids", []))
            for pid in current.keys() - previous:
                f = current[pid]
                price = f["price_txt"] or "price n/a"
                log.info("NEW: %s (%s) %s", f["name"], price, f["link"])
                notify(f"New place: {f['name']}", f"{price} for the stay\n{CHECKIN} → {CHECKOUT}",
                       click=f["link"], priority=4)
    except Exception as e:
        # Don't save state, so we retry notifying on the next run
        log.error("Notification failed: %s", e)
        return 1

    STATE_FILE.write_text(json.dumps(
        {"updated": datetime.now().isoformat(timespec="seconds"), "ids": sorted(current)},
        ensure_ascii=False, indent=2), "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
