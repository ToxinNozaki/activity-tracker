"""
Fortnite presence bot — runs as a persistent GitHub Actions job.

Connects to Epic's XMPP server via fortnitepy and:
  - Posts a status embed every 5 minutes (like the Roblox logger)
  - Also fires instant alerts the moment her status changes
  - Shows game, mode, party size, session timer, Discord timestamps

Exits at 5h 45m and self-restarts before GitHub's 6h hard limit.
"""

import asyncio
import base64
import json
import logging
import os
import time
import requests
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import fortnitepy

# fortnitepy's default iOS client (3446cd72...) was DISABLED by Epic.
# Override it with the Android game client, which still supports the
# device_auth grant (verified working).
_ANDROID_TOKEN = base64.b64encode(
    b"3f69e56c7649492c8cc29f1af08a8a12:b51ee9cb12234f50a69efa67ef53812e"
).decode()

# ── Config ────────────────────────────────────────────────────────────────────

TARGET           = "ReesieLuvsChan"
FORTNITE_CHANNEL = "1510146847530811473"
STATUS_CHANNEL   = "1510146836491665579"
ERROR_CHANNEL    = "1510142665453207715"
PING_USER_ID     = "1079478384901505045"
REPO             = "ToxinNozaki/activity-tracker"

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
GH_TOKEN  = os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_PAT", "")
_EASTERN  = ZoneInfo("America/New_York")

MAX_RUNTIME_SECS  = 5 * 3600 + 45 * 60   # 5h 45m — exit before GitHub's 6h kill
STATUS_INTERVAL   = 5 * 60               # post status every 5 minutes

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

def _discord_ts(unix: float) -> str:
    """Discord dynamic relative timestamp — renders as '5 minutes ago' live."""
    return f"<t:{int(unix)}:R>"

def _fmt_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    if h and m:
        return f"{h}h {m}m"
    return f"{h}h" if h else (f"{m}m" if m else "< 1 min")

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
            logging.warning("Discord %s → %s %s", channel_id,
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
    """
    'Fortnite — Battle Royale Zero Unranked Solo - 2 of 4'
      → game='Fortnite', mode='Battle Royale Zero Unranked Solo', party=2, max=4
    'Battle Royale Zero Unranked Solo - 100 Left'
      → mode='Battle Royale Zero Unranked Solo', party=None, max=None
    """
    mode = party_size = party_max = None
    if not text:
        return mode, party_size, party_max

    # Strip leading "Fortnite — " if present
    body = text.removeprefix("Fortnite — ").strip()

    if " - " in body:
        m, p = body.rsplit(" - ", 1)
        mode = m.strip()
        if " of " in p:
            try:
                a, b = p.split(" of ", 1)
                party_size = int(a.strip())
                party_max  = int(b.strip())
            except ValueError:
                pass
    elif body:
        mode = body

    return mode, party_size, party_max

def _build_embed(is_online: bool, is_playing: bool, status_text: str,
                 session_start: float | None, last_online_ts: float | None) -> dict:
    """Build a Roblox-style status embed for the Fortnite channel."""
    mode, party_size, party_max = _parse_status(status_text)

    if is_playing:
        color  = 0x00B04F   # green
        label  = "In Game"
    elif is_online:
        color  = 0x5865F2   # blurple
        label  = "Online (Lobby)"
    else:
        color  = 0x747F8D   # grey
        label  = "Offline"

    fields = [{"name": "Status", "value": label, "inline": True}]

    if mode and is_playing:
        fields.append({"name": "Mode", "value": mode, "inline": True})

    if party_size is not None and party_max is not None and is_playing:
        fields.append({"name": "Party",
                       "value": f"{party_size} / {party_max}", "inline": True})

    # Session timer
    if session_start and is_online:
        secs = time.time() - session_start
        fields.append({"name": "Session",
                       "value": _fmt_duration(secs), "inline": True})

    # Last seen (Discord dynamic timestamp)
    if not is_online and last_online_ts:
        fields.append({"name": "Last Seen",
                       "value": _discord_ts(last_online_ts), "inline": True})

    return {
        "title":  f"Fortnite — {TARGET}",
        "color":  color,
        "fields": fields,
        "footer": {"text": f"Logged at {_now_et()}"},
    }


# ── Bot ───────────────────────────────────────────────────────────────────────

def run_bot(device_auth: dict):
    start = time.time()

    # Shared state (mutated by events, read by periodic loop)
    state = {
        "is_online":      False,
        "is_playing":     False,
        "status_text":    "",
        "session_start":  None,   # float timestamp when she came online
        "last_online_ts": None,   # float timestamp when she last went offline
    }

    bot = fortnitepy.Client(
        auth=fortnitepy.DeviceAuth(
            device_id  = device_auth["device_id"],
            account_id = device_auth["account_id"],
            secret     = device_auth["secret"],
            ios_token  = _ANDROID_TOKEN,   # disabled iOS client → Android client
        )
    )

    # ── Ready ─────────────────────────────────────────────────────────────────

    @bot.event
    async def event_ready():
        logging.info("Fortnite bot ready — watching %s", TARGET)

        # Seed state from current presence if she's already online
        for f in bot.friends:
            if f.display_name == TARGET:
                p = f.last_presence
                if p and p.is_online:
                    state["is_online"]   = True
                    state["is_playing"]  = p.is_playing
                    state["status_text"] = p.status or ""
                    state["session_start"] = time.time()
                    logging.info("She's already online on startup: %s", p.status)
                break

        _post(STATUS_CHANNEL, {"embeds": [{
            "title":       "🎮 Fortnite Bot Online",
            "description": (
                f"Live XMPP connection — watching **{TARGET}** in real time.\n"
                f"Status posts every 5 minutes · Instant alerts on change."
            ),
            "color":  0x00B04F,
            "footer": {"text": _now_et()},
        }]})

        # Start the 5-minute periodic status loop
        asyncio.get_event_loop().create_task(_status_loop())

        # Exit cleanly before GitHub's 6h hard kill
        remaining = MAX_RUNTIME_SECS - (time.time() - start)
        if remaining > 0:
            await asyncio.sleep(remaining)

        logging.info("Runtime limit reached — triggering restart")
        _post(STATUS_CHANNEL, {"embeds": [{
            "title":       "🔄 Fortnite Bot Restarting",
            "description": "5h 45m runtime reached — reconnecting in ~30 seconds.",
            "color":  0xFFA500,
            "footer": {"text": _now_et()},
        }]})
        _trigger_restart()
        await bot.close()

    # ── 5-minute periodic status post ─────────────────────────────────────────

    async def _status_loop():
        while True:
            await asyncio.sleep(STATUS_INTERVAL)
            try:
                embed = _build_embed(
                    state["is_online"],
                    state["is_playing"],
                    state["status_text"],
                    state["session_start"],
                    state["last_online_ts"],
                )
                _post(FORTNITE_CHANNEL, {"embeds": [embed]})
                logging.info("Periodic status: online=%s playing=%s mode=%s",
                             state["is_online"], state["is_playing"],
                             _parse_status(state["status_text"])[0])
            except Exception as e:
                logging.warning("Periodic status error: %s", e)

    # ── Presence events (instant alerts on change) ────────────────────────────

    @bot.event
    async def event_friend_presence(before: fortnitepy.FriendPresence | None,
                                    after:  fortnitepy.FriendPresence):
        if after.friend.display_name != TARGET:
            return

        was_online = before.is_online if before else state["is_online"]
        is_online  = after.is_online
        is_playing = after.is_playing
        status_txt = after.status or ""

        logging.info("Presence: %s → %s playing=%s | %s",
                     was_online, is_online, is_playing, status_txt[:80])

        # Update shared state
        state["is_online"]   = is_online
        state["is_playing"]  = is_playing
        state["status_text"] = status_txt

        if is_online and not was_online:
            # She just came online — start session timer
            state["session_start"] = time.time()

        elif not is_online and was_online:
            # She just went offline — record last-online time
            state["last_online_ts"] = time.time()
            state["session_start"]  = None

        # Build and post an instant embed on any presence change
        embed = _build_embed(
            is_online, is_playing, status_txt,
            state["session_start"],
            state["last_online_ts"],
        )

        # Add a description for meaningful transitions
        desc = None
        if is_online and not was_online:
            desc = "🟢 Just came online!"
        elif not is_online and was_online:
            secs = (time.time() - (state.get("_prev_session_start") or time.time()))
            desc = "⚫ Just went offline."

        if desc:
            embed["description"] = desc

        # Keep prev session for offline description
        if is_online and not was_online:
            state["_prev_session_start"] = state["session_start"]

        _post(FORTNITE_CHANNEL, {"embeds": [embed]})

    # ── Run ───────────────────────────────────────────────────────────────────

    try:
        bot.run()
    except fortnitepy.AuthException as e:
        logging.error("Auth failed: %s", e)
        _post(ERROR_CHANNEL, {
            "content": f"<@{PING_USER_ID}>",
            "embeds": [{
                "title": "❌ Fortnite Bot Auth Failed",
                "description": (
                    f"`{e}`\n\n"
                    "The device auth credential may have been revoked.\n"
                    "Re-run `setup_fortnite_device_auth.py` locally and "
                    "update the `EPIC_DEVICE_AUTH` GitHub Secret."
                ),
                "color":  0xFF0000,
                "footer": {"text": _now_et()},
            }],
        })
    except Exception as e:
        logging.error("Bot crashed: %s", e, exc_info=True)
        _post(ERROR_CHANNEL, {
            "content": f"<@{PING_USER_ID}>",
            "embeds": [{
                "title":       "❌ Fortnite Bot Crashed",
                "description": f"`{str(e)[:500]}`",
                "color":        0xFF0000,
                "footer":      {"text": _now_et()},
            }],
        })
        _trigger_restart()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
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
