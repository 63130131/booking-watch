#!/usr/bin/env python3
"""Watch Booking.com for newly available accommodation and push to phone via ntfy.sh."""

import json
import logging
import os
import re
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# ============================ CONFIG (edit me) ============================
CITY = "Chiang Mai, Thailand"
CHECKIN = "2026-11-23"            # YYYY-MM-DD
CHECKOUT = "2026-11-26"           # YYYY-MM-DD
ADULTS = 1
ROOMS = 1
APARTMENTS_ONLY = False           # True = apartments only, False = any property type
MAX_PRICE_PER_NIGHT = 50          # EUR per night, 0 = no limit
MIN_PRICE_PER_NIGHT = 0           # EUR per night, 0 = no floor (raise to skip hostel beds)
CURRENCY = "EUR"

NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "change-me-to-a-long-random-string")  # env var wins; anyone who knows it can read it

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "booking_watch.log"
PAGE_TIMEOUT_MS = 45_000
# ==========================================================================

APARTMENT_FILTER = "ht_id=201"  # Booking.com property type id for apartments

# Booking.com serves headless browsers a page that falsely claims zero availability,
# so the browser has to be a real window (xvfb-run provides one on a headless server).
HEADLESS = False

# Narrow on purpose: the bare word "captcha" appears in normal page scripts and
# produced false positives. These only appear on an actual challenge page.
CHALLENGE_MARKERS = ("awswaf", "are you a robot", "verify you are human",
                     "unusual traffic", "access to this page has been denied")

# The results <h1> is the reliable signal: "Chiang Mai: 54 properties found"
# or "0 properties are available in and around this destination".
FOUND_RE = re.compile(r"([\d,]+)\s+propert(?:y|ies)\s+found", re.I)
ZERO_RE = re.compile(r"0\s+properties\s+are\s+available|no\s+properties\s+(?:are\s+)?available", re.I)

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
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(locale="en-US", viewport={"width": 1440, "height": 950})
        page = ctx.new_page()
        try:
            # Warm-up: without a homepage visit first, Booking.com strips the search
            # parameters off the deep link and returns an empty shell.
            page.goto("https://www.booking.com/", timeout=PAGE_TIMEOUT_MS,
                      wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            for sel in ('#onetrust-accept-btn-handler', '[aria-label="Dismiss sign-in info."]'):
                try:
                    page.locator(sel).first.click(timeout=2000)
                except PWTimeout:
                    pass
                except Exception:
                    pass

            page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            try:
                page.wait_for_selector('[data-testid="property-card"]', timeout=25_000)
            except PWTimeout:
                pass

            heading = ""
            if page.locator("h1").count():
                heading = page.locator("h1").first.inner_text().strip()
            log.info("Results heading: %r", heading)

            # Load the rest of page 1 (cards render lazily as you scroll).
            for _ in range(4):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1200)

            cards = page.locator('[data-testid="property-card"]')
            n = cards.count()
            if n == 0:
                # Only now look for a challenge: these markers also appear in the
                # scripts of perfectly normal pages, so they can't gate a success.
                html = page.content().lower()
                hit = next((m for m in CHALLENGE_MARKERS if m in html), None)
                if ZERO_RE.search(heading):
                    if HEADLESS:
                        log.warning("Zero results, but HEADLESS=True returns false zeros. "
                                    "Not trusting this; set HEADLESS=False.")
                        return None
                    log.info("Page loaded, genuinely no properties available.")
                    return []
                if hit:
                    log.warning("Bot check / challenge page detected (marker %r). "
                                "Not bypassing; exiting.", hit)
                    return None
                log.warning("No property cards and heading %r is not a zero-result "
                            "message; treating as failed load.", heading)
                return None
            if m := FOUND_RE.search(heading):
                log.info("Booking.com reports %s properties found; %d cards on page 1",
                         m.group(1), n)

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

    if MAX_PRICE_PER_NIGHT or MIN_PRICE_PER_NIGHT:
        nights = (date.fromisoformat(CHECKOUT) - date.fromisoformat(CHECKIN)).days
        lo = MIN_PRICE_PER_NIGHT * nights
        hi = (MAX_PRICE_PER_NIGHT * nights) if MAX_PRICE_PER_NIGHT else float("inf")
        kept = []
        for f in found:
            if f["price"] is None:  # couldn't read a price: keep it, better than missing it
                kept.append(f)
            elif lo <= f["price"] <= hi:
                f["per_night"] = round(f["price"] / nights)
                kept.append(f)
        log.info("%d of %d places within %s-%s EUR/night",
                 len(kept), len(found), MIN_PRICE_PER_NIGHT, MAX_PRICE_PER_NIGHT or "∞")
        found = kept
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
                per = f" (~{f['per_night']} EUR/night)" if f.get("per_night") else ""
                log.info("NEW: %s (%s)%s %s", f["name"], price, per, f["link"])
                notify(f"New place: {f['name']}",
                       f"{price} total{per}\n{CHECKIN} → {CHECKOUT}",
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
