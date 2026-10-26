# Booking.com availability watcher

Loads a Booking.com search (lowest price first) with headless Chromium, compares the
results with the previous run, and pushes a notification via ntfy.sh for every place
that is new — including ones that were booked and became free again.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install playwright
playwright install chromium
playwright install-deps chromium   # Linux server only (needs sudo)
```

### Phone notifications (ntfy)
1. Install the **ntfy** app (Android / iOS).
2. Subscribe to a topic with a long random name, e.g. `booking-k3j9x2m8q1w7`.
3. Put the same name in `NTFY_TOPIC` in `booking_watch.py` (topics on ntfy.sh are public to anyone who guesses the name).

### Config
Edit the block at the top of `booking_watch.py`: `CITY` (**set this, it's a placeholder**), `CHECKIN`, `CHECKOUT`, `ADULTS`, `ROOMS`, `APARTMENTS_ONLY`, `MAX_TOTAL_PRICE` (0 = no limit).

## Run
```bash
.venv/bin/python booking_watch.py
```
First run sends one summary push ("X places available, now watching"); later runs only notify about new places. State is in `state.json`, logs in `booking_watch.log`.

## Cron (every 10 minutes)
```cron
*/10 * * * * cd /path/to/Booking\ Scraper && .venv/bin/python booking_watch.py >> /dev/null 2>&1
```
Use absolute paths. Avoid going much below 10 minutes — more frequent requests make bot checks more likely.

## Notes
- Only the first results page (lowest-priced ~25 properties) is checked.
- If a CAPTCHA/bot check appears, it is logged and the run exits cleanly; nothing is bypassed and state is left untouched. If it happens often, run less frequently.
- A page that loads without cards and without a "no availability" message is treated as a failed load, so state isn't wiped.
- Booking.com's markup changes; if nothing is found, check the `data-testid` selectors in `scrape()`. Automated access may also be against Booking.com's terms — use it for personal, low-frequency monitoring.
- Not tested against the live site from here; do a first manual run and check the log.

## Free hosting with GitHub Actions (no server)
1. Create a GitHub repo and push this folder (`booking_watch.py`, `requirements.txt`, `.github/`, `.gitignore`, `README.md`).
2. Repo → Settings → Secrets and variables → Actions → New repository secret: name `NTFY_TOPIC`, value = your topic. (The topic in the script is then ignored, so it never lands in the repo.)
3. Repo → Settings → Actions → General → Workflow permissions: allow read and write (the workflow also declares `contents: write`).
4. Actions tab → *booking-watch* → **Run workflow** once to test and read the logs there.

Notes: `state.json` is committed back to the repo whenever it changes (this also keeps the schedule from being auto-disabled after 60 days of inactivity). Use a **public** repo for free unlimited minutes, but remember it exposes your city and dates; a private repo has a 2,000 min/month limit, which a 10-minute schedule exceeds. Scheduled runs are often delayed a few minutes. If Booking.com shows a bot check to GitHub's IPs, the log will say so and you should use a home machine or VPS instead.
