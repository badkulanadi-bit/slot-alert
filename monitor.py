#!/usr/bin/env python3
"""
Passive visa-slot alert. Watches PUBLIC third-party tracker pages / public Telegram
channel previews and alerts YOU (Telegram, email, optional WhatsApp) when their text changes.

It NEVER logs into usvisascheduling.com and never books anything. Do not add that.

  python3 monitor.py --test      # send a test alert to every configured channel
  python3 monitor.py             # check every URL in urls.txt once
  python3 monitor.py --dry-run   # print instead of sending

Secrets (env vars). Channels with missing settings are skipped:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  GMAIL_USER, GMAIL_APP_PASSWORD, ALERT_EMAIL_TO
  WHATSAPP_PHONE, CALLMEBOT_APIKEY
Optional: KEYWORDS (comma list), USE_BROWSER=1, HEARTBEAT_HOUR_UTC (e.g. 14) for a daily "still running" ping.
"""
import hashlib, html, json, os, random, re, smtplib, sys, time
import urllib.parse, urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("STATE_FILE", HERE / "state.json"))
URLS = HERE / "urls.txt"
UA = "Mozilla/5.0 (personal slot-alert script)"
def _kw(v): return [k.strip().lower() for k in v.split(",") if k.strip()]
KEYWORDS = _kw(os.environ.get("KEYWORDS", "h1b,h-1b,h1,h4,slot,available,open,emergency"))
SKIP = _kw(os.environ.get("SKIP", "no slot,no slots,not available,no appointment,none available"))
COOLDOWN_SEC = int(os.environ.get("COOLDOWN_SEC", "1800"))  # at most one alert per page per 30 min
_VOLATILE = re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\s?(am|pm)?\b|\b\d{1,2}\s(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b,?", re.I)


def fetch_text(url):
    if os.environ.get("USE_BROWSER") == "1":
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            pg = b.new_page()
            pg.goto(url, wait_until="networkidle", timeout=45000)
            txt = pg.inner_text("body")
            b.close()
            return txt
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return html.unescape(raw)


def lines_of(text):
    """Return (original_line, normalized_line) pairs. Normalized drops clock times / 'seen at'
    stamps so a page that only updates its timestamp does not look like a change."""
    out = []
    for ln in text.splitlines():
        ln = re.sub(r"\s+", " ", ln).strip()
        if len(ln) > 3:
            out.append((ln, re.sub(r"\s+", " ", _VOLATILE.sub("", ln)).strip()))
    return out


def parse_url_line(raw):
    """'URL | must: a,b | skip: c' -> (url, must_keywords, skip_keywords)"""
    parts = [p.strip() for p in raw.split("|")]
    must, skip = KEYWORDS, SKIP
    for p in parts[1:]:
        if p.lower().startswith("must:"): must = _kw(p[5:])
        if p.lower().startswith("skip:"): skip = SKIP + _kw(p[5:])
    return parts[0], must, skip


QUICK_LINKS = (
    "\n\nQuick links (type/bookmark these yourself; beware lookalike sites):\n"
    "Portal: https://www.usvisascheduling.com\n"
    "Wait times: https://travel.state.gov/content/travel/en/us-visas/visa-information-resources/wait-times.html"
)


def send_all(subject, body, dry, links=True):
    """Send via every configured channel; one failing channel never blocks the others."""
    if links:
        body += QUICK_LINKS
    if dry:
        print(f"[DRY-RUN] {subject}\n{body}\n")
        return
    e = os.environ.get
    sent = 0
    if e("TELEGRAM_BOT_TOKEN") and e("TELEGRAM_CHAT_ID"):
        try:
            data = urllib.parse.urlencode({"chat_id": e("TELEGRAM_CHAT_ID"),
                                           "text": f"{subject}\n\n{body}"[:3900],
                                           "disable_web_page_preview": "true"}).encode()
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{e('TELEGRAM_BOT_TOKEN')}/sendMessage", data, timeout=30)
            sent += 1
        except Exception as ex:
            print("telegram failed:", type(ex).__name__, file=sys.stderr)
    if e("GMAIL_USER") and e("GMAIL_APP_PASSWORD") and e("ALERT_EMAIL_TO"):
        try:
            m = EmailMessage()
            m["Subject"], m["From"], m["To"] = subject, e("GMAIL_USER"), e("ALERT_EMAIL_TO")
            m.set_content(body)
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
                s.login(e("GMAIL_USER"), e("GMAIL_APP_PASSWORD").replace(" ", ""))
                s.send_message(m)
            sent += 1
        except Exception as ex:
            print("email failed:", type(ex).__name__, file=sys.stderr)
    if e("WHATSAPP_PHONE") and e("CALLMEBOT_APIKEY"):
        try:
            q = urllib.parse.urlencode({"phone": e("WHATSAPP_PHONE"), "apikey": e("CALLMEBOT_APIKEY"),
                                        "text": f"{subject}\n{body}"[:1000]})
            urllib.request.urlopen("https://api.callmebot.com/whatsapp.php?" + q, timeout=30)
            sent += 1
        except Exception as ex:
            print("whatsapp failed:", type(ex).__name__, file=sys.stderr)
    if sent == 0:
        print("WARNING: no alert channel is configured or all failed", file=sys.stderr)
        sys.exit(1)


def main():
    dry = "--dry-run" in sys.argv
    if "--test" in sys.argv:
        send_all("Slot-alert test", "If you can read this, this channel works.", dry, links=False)
        return
    entries = [parse_url_line(u) for u in URLS.read_text().splitlines()
               if u.strip() and not u.startswith("#")]
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    for url, must, skip in entries:
        try:
            pairs = lines_of(fetch_text(url))
        except Exception as ex:  # keep going; one bad site shouldn't stop the rest
            print(f"fetch failed {url}: {type(ex).__name__}", file=sys.stderr)
            continue
        norm = [n for _, n in pairs]
        h = hashlib.sha256("\n".join(norm).encode()).hexdigest()
        prev = state.get(url)
        last_alert = prev.get("last_alert", 0) if prev else 0
        if prev is None:
            print(f"baseline saved: {url}")
        elif prev["hash"] != h:
            seen = set(prev["lines"])
            hits = [o for o, n in pairs if n not in seen
                    and any(k in o.lower() for k in must)
                    and not any(k in o.lower() for k in skip)]
            if hits and time.time() - last_alert > COOLDOWN_SEC:
                send_all("Possible visa slot update",
                         f"{url}\n\n" + "\n".join(f"- {l[:200]}" for l in hits[:8])
                         + "\n\nCheck the official portal yourself NOW.", dry)
                last_alert = time.time()
        state[url] = {"hash": h, "lines": norm[-400:], "last_alert": last_alert}
        time.sleep(random.uniform(1, 4))  # be polite to the sites
    hb = os.environ.get("HEARTBEAT_HOUR_UTC")
    now = datetime.now(timezone.utc)
    if hb and now.hour >= int(hb) and state.get("_heartbeat") != now.strftime("%Y-%m-%d"):
        send_all("Slot-alert is still running", f"Daily check-in, {now:%Y-%m-%d}. No action needed.", dry, links=False)
        state["_heartbeat"] = now.strftime("%Y-%m-%d")
    STATE.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
