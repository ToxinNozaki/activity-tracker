"""
Fortnite presence bot — runs as a persistent GitHub Actions job.

Connects to Epic's XMPP server via fortnitepy and fires Discord
notifications the instant ReesieLuvsChan's status changes.
Exits after 5h 45m and triggers itself to restart before GitHub's 6h limit.
"""

import asyncio
import json
import logging
import os
import time
import requests
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import fortnitepy

# ── Config ────────────────────────────────────────────────────────────────────

TARGET             = "ReesieLuvsChan"
FORTNITE_CHANNEL   = "1510146847530811473"
STATUS_CHANNEL     = "1510146836491665579"
ERROR_CHANNEL      = "1510142665453207715"
PING_USER_ID       = "1079478384901505045"
REPO               = "ToxinNozaki/activity-tracker"

BOT_TOKEN  = os.environ.get("DISCORD_BOT_TOKEN", "")
GH_TOKEN   = os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_PAT", "")
_EASTERN   = ZoneInfo("America/New_York")

# Exit 15 min before GitHub's hard 6-hour kill to allow clean restart
MAX_RUNTIME_SECS = 5 * 3600 + 45 * 60   # 5h 45m

# ── Logging ───────────────────────────────────────────────────────────────────

class _ETFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created, _EASTERN)
        return ct.strftime("%m/%d/%Y %I:%M:%S %p %Z")

_h = logging.StreamHandler()
_h.setFormatter(_ETFormatter("%(asctime)s %(levelname)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_h])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_et() -> str:
    return datetime.now(_EASTERN).strftime("%m/%d/%Y %I:%M %p %Z")


def _fmt_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    if h and m:
        return f"{h}h {m}m"
    return f"{h}h" if h else f"{m}m"


def _post(channel_id: str, payload: dict):
    if not BOT_TOKEN:
        return
    try:
        r = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {BOT_TOKEN}",
                     "Content-Type": "application/json"},
            json=payload, timeout=10,
        )
        if not r.ok:
            logging.warning("Discord post %s → %s %s", channel_id,
                            r.status_code, r.text[:200])
    except Exception as e:
        logging.warning("Discord post error: %s", e)


def _trigger_restart():
    if not GH_TOKEN:
        logging.warning("No GH_TOKEN — cannot trigger restart")
        return
    try:
        r = requests.post(
            f"https://api.github.com/repos/{REPO}/dispatches",
            headers={"Authorization": f"token {GH_TOKEN}",
                     "Accept": "application/vnd.github.v3+json"},
            json={"event_type": "run-fortnite-bot"},
            timeout=10,
        )
        logging.info("Restart triggered: HTTP %s", r.status_code)
    except Exception as e:
        logging.warning("Restart trigger failed: %s", e)


def _parse_status(text: str) -> tuple[str | None, int | None, int | None]:
    """'Battle Royale Zero Unranked Solo - 100 Left' → (mode, size, max)"""
    mode = party_size = party_max = None
    if " - " in text:
        m, p = text.rsplit(" - ", 1)
        mode = m.strip()
        if " of " in p:
            try:
                a, b = p.split(" of ", 1)
                party_size = int(a.strip())
                party_max  = int(b.strip())
            except ValueError:
                pass
    return mode, party_size, party_max


# ── Bot ───────────────────────────────────────────────────────────────────────

def run_bot(device_auth: dict):
    start    = time.time()
    sessions: dict = {}   # {friend_display_name: session_start_time}

    bot = fortnitepy.Client(
        auth=fortnitepy.DeviceAuth(
            device_id  = device_auth["device_id"],
            account_id = device_auth["account_id"],
            secret     = device_auth["secret"],
        )
    )

    # ── Ready ─────────────────────────────────────────────────────────────────

    @bot.event
    async def event_ready():
        logging.info("Fortnite bot ready — watching %s", TARGET)
        _post(STATUS_CHANNEL, {"embeds": [{
            "title":       "🎮 Fortnite Bot Online",
            "description": f"Live XMPP connection active — watching **{TARGET}** in real time.",
            "color":       0x00B04F,
            "footer":      {"text": _now_et()},
        }]})

        # Check her current presence on startup
        friend = discord_friend = None
        for f in bot.friends:
            if f.display_name == TARGET:
                friend = f
                break
        if friend:
            p = friend.last_presence
            if p and p.is_online:
                logging.info("She's already online when bot started")
                sessions[TARGET] = time.time()

        # Sleep until 15 min before our limit, then clean up and restart
        remaining = MAX_RUNTIME_SECS - (time.time() - start)
        if remaining > 0:
            await asyncio.sleep(remaining)

        logging.info("Runtime limit reached — restarting")
        _post(STATUS_CHANNEL, {"embeds": [{
            "title":       "🔄 Fortnite Bot Restarting",
            "description": "5h 45m runtime reached — reconnecting in ~30 seconds.",
            "color":       0xFFA500,
            "footer":      {"text": _now_et()},
        }]})
        _trigger_restart()
        await bot.close()

    # ── Presence events ───────────────────────────────────────────────────────

    @bot.event
    async def event_friend_presence(before: fortnitepy.FriendPresence | None,
                                    after:  fortnitepy.FriendPresence):
        if after.friend.display_name != TARGET:
            return

        was_online = before.is_online if before else False
        is_online  = after.is_online
        is_playing = after.is_playing
        status_txt = after.status or ""
        ts         = _now_et()

        logging.info("Presence update: was_online=%s → is_online=%s is_playing=%s | %s",
                     was_online, is_online, is_playing, status_txt[:80])

        # ── Came online ──────────────────────────────────────────────────────
        if is_online and not was_online:
            sessions[TARGET] = time.time()

            if is_playing:
                mode, psize, pmax = _parse_status(status_txt)
                fields = [{"name": "Status", "value": "🟢 In Game", "inline": True}]
                if mode:
                    fields.append({"name": "Mode", "value": mode, "inline": True})
                if psize and pmax:
                    fields.append({"name": "Party",
                                   "value": f"{psize} / {pmax}", "inline": True})
                _post(FORTNITE_CHANNEL, {"embeds": [{
                    "title":       f"Fortnite — {TARGET}",
                    "description": "Just came online — already in a game!",
                    "color":       0x00B04F,
                    "fields":      fields,
                    "footer":      {"text": ts},
                }]})
            else:
                _post(FORTNITE_CHANNEL, {"embeds": [{
                    "title":       f"Fortnite — {TARGET}",
                    "description": "Just came online.",
                    "color":       0x5865F2,
                    "fields":      [{"name": "Status",
                                     "value": "🔵 Online (Lobby)", "inline": True}],
                    "footer":      {"text": ts},
                }]})

        # ── Went offline ─────────────────────────────────────────────────────
        elif not is_online and was_online:
            duration_str = ""
            if TARGET in sessions:
                secs = time.time() - sessions.pop(TARGET)
                duration_str = f"\n**Session:** {_fmt_duration(secs)}"

            _post(FORTNITE_CHANNEL, {"embeds": [{
                "title":       f"Fortnite — {TARGET}",
                "description": f"Went offline.{duration_str}",
                "color":       0x747F8D,
                "fields":      [{"name": "Status",
                                 "value": "⚫ Offline", "inline": True}],
                "footer":      {"text": ts},
            }]})

        # ── Status update while online (game change / party change) ──────────
        elif is_online and was_online:
            mode, psize, pmax = _parse_status(status_txt)
            fields = [{"name": "Status",
                       "value": "🟢 In Game" if is_playing else "🔵 Online", "inline": True}]
            if mode:
                fields.append({"name": "Mode", "value": mode, "inline": True})
            if psize and pmax:
                fields.append({"name": "Party",
                               "value": f"{psize} / {pmax}", "inline": True})
            if TARGET in sessions:
                secs = time.time() - sessions[TARGET]
                fields.append({"name": "Session",
                               "value": _fmt_duration(secs), "inline": True})
            _post(FORTNITE_CHANNEL, {"embeds": [{
                "title":  f"Fortnite — {TARGET}",
                "color":  0x00B04F if is_playing else 0x5865F2,
                "fields": fields,
                "footer": {"text": ts},
            }]})

    # ── Error events ──────────────────────────────────────────────────────────

    @bot.event
    async def event_device_auth_generate(details: dict, email: str):
        logging.info("Device auth generated for %s", email)

    # ── Run ───────────────────────────────────────────────────────────────────

    try:
        bot.run()
    except fortnitepy.AuthException as e:
        logging.error("Auth failed: %s", e)
        _post(ERROR_CHANNEL, {
            "content": f"<@{PING_USER_ID}>",
            "embeds": [{
                "title":       "❌ Fortnite Bot Auth Failed",
                "description": (
                    f"`{e}`\n\n"
                    "The device auth credential may have been revoked. "
                    "Re-run `setup_fortnite_device_auth.py` and update "
                    "the `EPIC_DEVICE_AUTH` GitHub Secret."
                ),
                "color":       0xFF0000,
                "footer":      {"text": _now_et()},
            }],
        })
    except Exception as e:
        logging.error("Bot crashed: %s", e, exc_info=True)
        _post(ERROR_CHANNEL, {
            "content": f"<@{PING_USER_ID}>",
            "embeds": [{
                "title":       "❌ Fortnite Bot Crashed",
                "description": f"`{str(e)[:500]}`",
                "color":       0xFF0000,
                "footer":      {"text": _now_et()},
            }],
        })
        _trigger_restart()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Load device auth from env (GitHub Secret) or local file
    raw = os.environ.get("EPIC_DEVICE_AUTH", "")
    if not raw:
        f = Path("epic_device_auth.json")
        if f.exists():
            raw = f.read_text()
        else:
            logging.error("EPIC_DEVICE_AUTH not set and epic_device_auth.json not found")
            raise SystemExit(1)

    try:
        device_auth = json.loads(raw)
    except Exception as e:
        logging.error("Failed to parse EPIC_DEVICE_AUTH: %s", e)
        raise SystemExit(1)

    run_bot(device_auth)
