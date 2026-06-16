"""
Launcher Watcher — runs ON the mini PC, reads the Epic launcher's social panel
by screenshot + OCR, and posts ReesieLuvsChan's status to Discord.

It reads your own screen — it never contacts Epic's servers. Keep the Epic
launcher open with the Friends/social panel pinned, and leave this running.

This is fully independent from the Roblox tracker (which runs on GitHub
Actions). If OCR or the launcher breaks here, the Roblox tracker is unaffected.

SETUP (on the mini PC):
  1. Install Python 3 from python.org (check "Add Python to PATH")
  2. Install Tesseract OCR (UB Mannheim build):
     https://github.com/UB-Mannheim/tesseract/wiki  -> default install path
  3. pip install mss pillow pytesseract requests
  4. Set env var:  setx DISCORD_BOT_TOKEN "your-token"   (then open a NEW terminal)
  5. python launcher_watcher.py
  6. Open watcher_debug/ after the first run, find the social panel coordinates,
     set CAPTURE_REGION below, and restart.
"""

import os
import re
import sys
import json
import time
import logging
from datetime import datetime
from pathlib import Path

import requests
import mss
import mss.tools
from PIL import Image
import pytesseract

# ── Config ────────────────────────────────────────────────────────────────────

TARGET            = "ReesieLuvsChan"          # who to watch (display only)
TARGET_KEY        = "reesie"                  # lowercase fragment used to find her row
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
FORTNITE_CHANNEL  = "1510146847530811473"     # status posts go here
ERROR_CHANNEL     = "1510142665453207715"     # watchdog alerts go here
PING_USER_ID      = "1079478384901505045"     # pinged on watchdog alerts
CHECK_EVERY_SECS  = 300                       # 5 minutes
STATE_FILE        = Path("launcher_watcher_state.json")
DEBUG_DIR         = Path("watcher_debug")     # screenshots + OCR dumps for tuning

# Require this many CONSECUTIVE offline reads before declaring her offline.
# Guards against a single noisy OCR frame flipping her to offline.
OFFLINE_CONFIRM        = 2
# If the panel is unreadable for this many consecutive cycles, alert the error
# channel once (e.g. launcher closed, PC locked, screen off). 6 * 5min = 30min.
UNREADABLE_ALERT_AFTER = 6

# Tesseract location (default UB Mannheim install). Adjust if you installed elsewhere.
_TESS = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if Path(_TESS).exists():
    pytesseract.pytesseract.tesseract_cmd = _TESS

# Optional: capture only a region of the screen instead of the whole thing.
# Leave as None to capture the full primary monitor. Once you know where the
# social panel sits, set e.g. {"left":1400,"top":120,"width":420,"height":820}
CAPTURE_REGION = None

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

# ── Discord ───────────────────────────────────────────────────────────────────

def _post(channel: str, payload: dict, *, what: str = "message") -> bool:
    """POST to Discord with retries + 429 handling. Returns True on success."""
    if not DISCORD_BOT_TOKEN or DISCORD_BOT_TOKEN.startswith("PASTE_"):
        logging.warning("No Discord bot token set — skipping %s", what)
        return False
    url = f"https://discord.com/api/v10/channels/{channel}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}",
               "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=15)
            if r.status_code == 429:  # rate limited — honour retry_after
                wait = 1.0
                try:
                    wait = float(r.json().get("retry_after", 1.0))
                except Exception:
                    pass
                logging.warning("Discord 429 — retrying %s in %.1fs", what, wait)
                time.sleep(min(wait, 10) + 0.5)
                continue
            if r.ok:
                return True
            # 5xx → retry; 4xx (other than 429) → give up, it won't fix itself
            logging.warning("Discord %s failed %s: %s", what, r.status_code,
                            r.text[:200])
            if r.status_code < 500:
                return False
        except Exception as e:
            logging.warning("Discord %s error (attempt %d): %s", what, attempt + 1, e)
        time.sleep(2 * (attempt + 1))
    return False


def post_status(embed: dict) -> bool:
    return _post(FORTNITE_CHANNEL, {"embeds": [embed]}, what="status")


def post_watchdog(title: str, desc: str, color: int):
    _post(ERROR_CHANNEL, {
        "content": f"<@{PING_USER_ID}>",
        "embeds": [{"title": title, "description": desc, "color": color,
                    "footer": {"text": _now()}}],
    }, what="watchdog")


def _now() -> str:
    return datetime.now().strftime("%m/%d/%Y %I:%M %p")

# ── Screenshot + OCR ──────────────────────────────────────────────────────────

def grab_screenshot() -> Image.Image:
    with mss.mss() as sct:
        mon = CAPTURE_REGION if CAPTURE_REGION else sct.monitors[1]
        raw = sct.grab(mon)
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def ocr_lines(img: Image.Image) -> list[dict]:
    """
    Returns text lines with vertical position: [{"text": "...", "top": 123}, ...]
    top-to-bottom. Groups OCR words into lines by their y-coordinate.
    """
    img2 = img.resize((img.width * 2, img.height * 2))  # OCR is better on bigger text
    data = pytesseract.image_to_data(img2, output_type=pytesseract.Output.DICT)

    words = []
    for i in range(len(data["text"])):
        t = (data["text"][i] or "").strip()
        if not t:
            continue
        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = -1
        if conf < 30:
            continue
        words.append({"text": t, "top": data["top"][i], "left": data["left"][i]})

    words.sort(key=lambda w: (w["top"], w["left"]))
    lines, cur, cur_top = [], [], None
    for w in words:
        if cur_top is None or abs(w["top"] - cur_top) <= 18:
            cur.append(w)
            cur_top = w["top"] if cur_top is None else cur_top
        else:
            cur.sort(key=lambda x: x["left"])
            lines.append({"text": " ".join(x["text"] for x in cur), "top": cur_top})
            cur, cur_top = [w], w["top"]
    if cur:
        cur.sort(key=lambda x: x["left"])
        lines.append({"text": " ".join(x["text"] for x in cur), "top": cur_top})
    return lines


def _letters(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def _is_header(text: str, word: str) -> bool:
    """True if a line is the 'ONLINE'/'OFFLINE' section header (ignoring any count)."""
    return _letters(text) == word

# ── Interpret the social panel ────────────────────────────────────────────────

def interpret(lines: list[dict]) -> dict:
    """
    Returns {"state": "online"|"offline"|"unknown", "status": str}.

      "unknown"  -> the panel could not be read (no Online/Offline headers found).
                    Caller must NOT treat this as offline.
      "offline"  -> panel readable, she's in the offline section or not listed.
      "online"   -> panel readable, she's in the online section; "status" is the
                    line under her name (e.g. "Battle Royale Zero ..." or "Online").
    """
    online_top = offline_top = None
    for ln in lines:
        if online_top is None and _is_header(ln["text"], "online"):
            online_top = ln["top"]
        if offline_top is None and _is_header(ln["text"], "offline"):
            offline_top = ln["top"]

    # If neither section header was found, the social panel isn't on screen /
    # readable. Do NOT guess "offline" — report unknown so the caller skips.
    if online_top is None and offline_top is None:
        return {"state": "unknown", "status": ""}

    # locate her name line (normalise away spaces/punctuation OCR may inject)
    her = None
    for idx, ln in enumerate(lines):
        if TARGET_KEY in _letters(ln["text"]):
            her = (idx, ln)
            break

    if her is None:
        return {"state": "offline", "status": ""}  # readable panel, not in online list

    idx, ln = her
    her_top = ln["top"]

    in_online = True
    if offline_top is not None and her_top >= offline_top:
        in_online = False
    if online_top is not None and her_top < online_top:
        in_online = False

    if not in_online:
        return {"state": "offline", "status": ""}

    status = ""
    if idx + 1 < len(lines):
        nxt = lines[idx + 1]
        if 0 < (nxt["top"] - her_top) < 60:
            status = nxt["text"].strip()
    return {"state": "online", "status": status}

# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(st: dict):
    STATE_FILE.write_text(json.dumps(st))

# ── Embed ─────────────────────────────────────────────────────────────────────

def build_embed(online: bool, status: str, headline: str | None) -> dict:
    if online:
        playing = bool(status) and _letters(status) not in ("online", "")
        color = 0x00B04F if playing else 0x5865F2
        fields = [{"name": "Status", "value": "In Game" if playing else "Online",
                   "inline": True}]
        if status:
            fields.append({"name": "Details", "value": status, "inline": True})
    else:
        color = 0x747F8D
        fields = [{"name": "Status", "value": "Offline", "inline": True}]

    embed = {"title": f"Fortnite — {TARGET}", "color": color, "fields": fields,
             "footer": {"text": f"Read from launcher at {_now()}"}}
    if headline:
        embed["description"] = headline
    return embed

# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    logging.info("Launcher Watcher started — watching %s every %ds",
                 TARGET, CHECK_EVERY_SECS)
    DEBUG_DIR.mkdir(exist_ok=True)

    state = load_state()
    prev          = state.get("state")            # "online" / "offline" / None
    prev_status   = state.get("status", "")
    offline_strk  = 0                              # consecutive offline reads
    unreadable    = 0                              # consecutive unknown reads
    alerted_down  = False                          # watchdog alert already sent?
    first         = True

    while True:
        try:
            img    = grab_screenshot()
            lines  = ocr_lines(img)
            result = interpret(lines)

            # Dump debug on the first cycle so you can tune CAPTURE_REGION.
            if first:
                img.save(DEBUG_DIR / "screenshot.png")
                (DEBUG_DIR / "ocr.txt").write_text(
                    "\n".join(f'{l["top"]:>5}  {l["text"]}' for l in lines),
                    encoding="utf-8")
                logging.info("Saved debug screenshot + OCR dump to %s/", DEBUG_DIR)
                logging.info("First read: state=%s status=%r",
                             result["state"], result["status"])
                first = False

            # ── Unreadable panel: never treat as offline ──────────────────────
            if result["state"] == "unknown":
                unreadable += 1
                logging.warning("Panel unreadable (%d in a row) — skipping cycle",
                                unreadable)
                if unreadable == UNREADABLE_ALERT_AFTER and not alerted_down:
                    img.save(DEBUG_DIR / "unreadable.png")
                    post_watchdog(
                        "⚠️ Can't read the Epic launcher",
                        f"The social panel has been unreadable for "
                        f"~{unreadable * CHECK_EVERY_SECS // 60} minutes. "
                        "Check that the mini PC is awake, unlocked, and the Epic "
                        "launcher is open with the Friends panel pinned.",
                        0xFF6600)
                    alerted_down = True
                time.sleep(CHECK_EVERY_SECS)
                continue

            # Panel readable again after a sustained outage → recovery note.
            if alerted_down:
                post_watchdog("✅ Launcher readable again",
                              "OCR is reading the social panel again. Resuming.",
                              0x00B04F)
            unreadable, alerted_down = 0, False

            # ── Debounce offline flips ────────────────────────────────────────
            if result["state"] == "offline" and prev == "online":
                offline_strk += 1
                if offline_strk < OFFLINE_CONFIRM:
                    logging.info("Read offline (%d/%d) — waiting for confirmation",
                                 offline_strk, OFFLINE_CONFIRM)
                    time.sleep(CHECK_EVERY_SECS)
                    continue
            offline_strk = 0

            online    = result["state"] == "online"
            status    = result["status"]
            prev_online = (prev == "online")

            # ── Decide whether/what to post ───────────────────────────────────
            headline   = None
            should_post = False
            if prev is None:                       # first observation — record + post
                should_post = True
            elif online and not prev_online:
                headline, should_post = "🟢 Just came online!", True
            elif not online and prev_online:
                headline, should_post = "⚫ Just went offline.", True
            elif online and status != prev_status:  # status changed while online
                should_post = True                 # silent update (no headline)

            if should_post:
                if post_status(build_embed(online, status, headline)):
                    logging.info("Posted: state=%s status=%r headline=%s",
                                 result["state"], status, headline)
                # Only advance saved state on a successful-or-attempted post.

            prev, prev_status = result["state"], status
            save_state({"state": prev, "status": prev_status})

        except Exception as e:
            logging.error("Cycle error: %s", e, exc_info=True)

        time.sleep(CHECK_EVERY_SECS)


if __name__ == "__main__":
    main()
