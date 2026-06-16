"""
Launcher Watcher — runs ON the mini PC, reads the Epic launcher's social panel
by screenshot + OCR, and posts ReesieLuvsChan's status to Discord.

It reads your own screen — it never contacts Epic's servers. Keep the Epic
launcher open with the Friends/social panel pinned, and leave this running.

SETUP (on the mini PC):
  1. Install Python 3 from python.org (check "Add Python to PATH")
  2. Install Tesseract OCR (UB Mannheim build):
     https://github.com/UB-Mannheim/tesseract/wiki  -> default install path
  3. pip install mss pillow pytesseract requests
  4. Put your Discord bot token below (or set env DISCORD_BOT_TOKEN)
  5. python launcher_watcher.py
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

TARGET            = "ReesieLuvsChan"          # who to watch (matched loosely)
TARGET_KEY        = "reesie"                  # lowercase fragment used to find her row
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
FORTNITE_CHANNEL  = "1510146847530811473"
CHECK_EVERY_SECS  = 300                       # 5 minutes
STATE_FILE        = Path("launcher_watcher_state.json")
DEBUG_DIR         = Path("watcher_debug")     # screenshots + OCR dumps for tuning

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

def post_discord(embed: dict):
    if not DISCORD_BOT_TOKEN or DISCORD_BOT_TOKEN.startswith("PASTE_"):
        logging.warning("No Discord bot token set — skipping post")
        return
    try:
        r = requests.post(
            f"https://discord.com/api/v10/channels/{FORTNITE_CHANNEL}/messages",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                     "Content-Type": "application/json"},
            json={"embeds": [embed]}, timeout=10)
        if not r.ok:
            logging.warning("Discord post failed %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        logging.warning("Discord error: %s", e)

def _now() -> str:
    return datetime.now().strftime("%m/%d/%Y %I:%M %p")

# ── Screenshot + OCR ──────────────────────────────────────────────────────────

def grab_screenshot() -> Image.Image:
    with mss.mss() as sct:
        if CAPTURE_REGION:
            mon = CAPTURE_REGION
        else:
            mon = sct.monitors[1]  # primary monitor
        raw = sct.grab(mon)
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

def ocr_lines(img: Image.Image) -> list[dict]:
    """
    Returns a list of text lines with their vertical position:
      [{"text": "...", "top": 123}, ...]  top-to-bottom order.
    Groups OCR words into lines by their y-coordinate.
    """
    # Upscale a bit — OCR is more accurate on larger text.
    img2 = img.resize((img.width * 2, img.height * 2))
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

    # group into lines by 'top' proximity
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

# ── Interpret the social panel ────────────────────────────────────────────────

def interpret(lines: list[dict]) -> dict:
    """
    Decide TARGET's status from the OCR'd social panel.
    Returns {"online": bool, "status": str}.
    Logic: find the 'Online' and 'Offline' section headers by vertical position.
    If TARGET's name appears between them -> online, and the line right after her
    name is her status (e.g. 'Fortnite — Battle Royale' or 'Online').
    """
    online_top = offline_top = None
    for ln in lines:
        low = ln["text"].lower()
        if online_top is None and re.search(r"\bonline\b", low) and len(low) < 16:
            online_top = ln["top"]
        if offline_top is None and re.search(r"\boffline\b", low) and len(low) < 16:
            offline_top = ln["top"]

    # locate her name line
    her = None
    for idx, ln in enumerate(lines):
        if TARGET_KEY in ln["text"].lower().replace(" ", ""):
            her = (idx, ln)
            break

    if her is None:
        return {"online": False, "status": ""}

    idx, ln = her
    her_top = ln["top"]

    # Is she in the Online section? (above the Offline header, below Online header)
    in_online = True
    if offline_top is not None and her_top >= offline_top:
        in_online = False
    if online_top is not None and her_top < online_top:
        in_online = False

    if not in_online:
        return {"online": False, "status": ""}

    # Her status text = the next line just below her name
    status = ""
    if idx + 1 < len(lines):
        nxt = lines[idx + 1]
        if 0 < (nxt["top"] - her_top) < 60:
            status = nxt["text"].strip()
    return {"online": True, "status": status}

# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"online": None, "status": ""}

def save_state(st: dict):
    STATE_FILE.write_text(json.dumps(st))

# ── Main loop ─────────────────────────────────────────────────────────────────

def build_embed(online: bool, status: str, changed: str | None) -> dict:
    if online:
        playing = bool(status) and status.lower() not in ("online", "")
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
    if changed:
        embed["description"] = changed
    return embed

def main():
    logging.info("Launcher Watcher started — watching %s every %ds",
                 TARGET, CHECK_EVERY_SECS)
    DEBUG_DIR.mkdir(exist_ok=True)
    state = load_state()
    first = True

    while True:
        try:
            img = grab_screenshot()
            lines = ocr_lines(img)
            result = interpret(lines)

            # Always dump debug on the first cycle so you can verify OCR works
            if first:
                img.save(DEBUG_DIR / "screenshot.png")
                (DEBUG_DIR / "ocr.txt").write_text(
                    "\n".join(f'{l["top"]:>5}  {l["text"]}' for l in lines),
                    encoding="utf-8")
                logging.info("Saved debug screenshot + OCR dump to %s", DEBUG_DIR)
                logging.info("First read: online=%s status=%r",
                             result["online"], result["status"])
                first = False

            changed = None
            if state["online"] is None:
                changed = None  # first run, just record
            elif result["online"] and not state["online"]:
                changed = "🟢 Just came online!"
            elif not result["online"] and state["online"]:
                changed = "⚫ Just went offline."
            elif (result["online"] and state["online"]
                  and result["status"] and result["status"] != state.get("status")):
                changed = None  # status changed while online — post update silently

            # Post on any change (online flip, or status text change while online)
            should_post = (
                state["online"] is None
                or result["online"] != state["online"]
                or (result["online"] and result["status"] != state.get("status"))
            )
            if should_post:
                post_discord(build_embed(result["online"], result["status"], changed))
                logging.info("Posted: online=%s status=%r changed=%s",
                             result["online"], result["status"], changed)

            state = {"online": result["online"], "status": result["status"]}
            save_state(state)

        except Exception as e:
            logging.error("Cycle error: %s", e, exc_info=True)

        time.sleep(CHECK_EVERY_SECS)

if __name__ == "__main__":
    main()
