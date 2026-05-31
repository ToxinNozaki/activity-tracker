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

def _extract_mode(status_text: str) -> str | None:
    """
    Pull a human-readable mode from the status text.
    'Battle Royale Zero Unranked Solo - 100 Left' → 'Battle Royale Zero Unranked Solo'
    (The trailing '- N Left' is players remaining in the match, not party size.)
    """
    if not status_text:
        return None
    body = status_text.removeprefix("Fortnite — ").strip()
    if " - " in body:
        body = body.rsplit(" - ", 1)[0].strip()
    return body or None

def _build_embed(is_online: bool, is_playing: bool, status_text: str,
                 party_size: int | None, party_max: int | None,
                 session_start: float | None, last_online_ts: float | None) -> dict:
    """Build a Roblox-style status embed for the Fortnite channel."""
    mode = _extract_mode(status_text)

    if is_playing:
        color = 0x00B04F   # green
        label = "In Game"
    elif is_online:
        color = 0x5865F2   # blurple
        label = "Online (Lobby)"
    else:
        color = 0x747F8D   # grey
        label = "Offline"

    fields = [{"name": "Status", "value": label, "inline": True}]

    if mode and is_playing:
        fields.append({"name": "Mode", "value": mode, "inline": True})

    if is_playing and party_size:
        pmax = f" / {party_max}" if party_max else ""
        fields.append({"name": "Party",
                       "value": f"{party_size}{pmax}", "inline": True})

    if session_start and is_online:
        secs = time.time() - session_start
        fields.append({"name": "Session",
                       "value": _fmt_duration(secs), "inline": True})

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

    # fortnitepy/aioxmpp touch asyncio.get_event_loop() during Client
    # construction; on Python 3.11+ that raises "no running event loop"
    # unless a loop is already set on this thread. Create one up front.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Shared state (mutated by events, read by periodic loop)
    state = {
        "is_online":      False,
        "is_playing":     False,
        "status_text":    "",
        "party_size":     None,
        "party_max":      None,
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

    # Epic shut down the public GraphQL endpoint that fortnitepy 3.6.9 calls
    # during login (account_graphql_get_clients_external_auths → 404).
    # We don't need external auths (linked Xbox/PSN), so stub it out with the
    # exact shape _setup_client_user expects: {'myAccount': {'externalAuths': []}}
    async def _stub_external_auths(*args, **kwargs):
        return {"myAccount": {"externalAuths": []}}
    bot.http.account_graphql_get_clients_external_auths = _stub_external_auths

    # ── Ready ─────────────────────────────────────────────────────────────────

    @bot.event
    async def event_ready():
        # Guard against re-entry: fortnitepy fires event_ready again on
        # reconnect. Without this, each reconnect spawns another status loop
        # and another restart timer → duplicate posts all night.
        if state.get("_ready_done"):
            logging.info("event_ready fired again (reconnect) — skipping re-init")
            return
        state["_ready_done"] = True

        logging.info("Fortnite bot ready — watching %s (%d friends)",
                     TARGET, len(list(bot.friends)))

        _post(STATUS_CHANNEL, {"embeds": [{
            "title":       "🎮 Fortnite Bot Online",
            "description": (
                f"Live XMPP connection — watching **{TARGET}** in real time.\n"
                f"Status posts every 5 minutes · Instant alerts on change."
            ),
            "color":  0x00B04F,
            "footer": {"text": _now_et()},
        }]})

        # Give Epic's XMPP a few seconds to push current friend presences,
        # then read her real status and post it immediately (don't wait 5 min).
        await asyncio.sleep(20)

        # ── DIAGNOSTIC: report what the bot can actually see ─────────────────
        try:
            all_friends = list(bot.friends)
            target = _find_target_friend()
            with_presence = [f for f in all_friends if f.last_presence is not None]
            sample = ", ".join(
                f"{f.display_name}({'on' if f.last_presence.available else 'off'})"
                for f in with_presence[:8]
            ) or "none"
            if target is not None:
                tp = target.last_presence
                tp_desc = ("None" if tp is None else
                           f"available={tp.available} playing={tp.playing} "
                           f"status={(tp.status or '')[:60]!r}")
            else:
                tp_desc = "TARGET NOT IN FRIEND LIST"
            _post(ERROR_CHANNEL, {"embeds": [{
                "title": "🔧 Fortnite Bot Diagnostic",
                "description": (
                    f"**Total friends:** {len(all_friends)}\n"
                    f"**Friends with presence data:** {len(with_presence)}\n"
                    f"**{TARGET} presence:** {tp_desc}\n"
                    f"**Sample presences:** {sample}"
                ),
                "color": 0xFFA500,
                "footer": {"text": _now_et()},
            }]})
        except Exception as e:
            logging.warning("Diagnostic failed: %s", e)

        _refresh_from_presence()
        embed = _build_embed(
            state["is_online"], state["is_playing"], state["status_text"],
            state["party_size"], state["party_max"],
            state["session_start"], state["last_online_ts"],
        )
        _post(FORTNITE_CHANNEL, {"embeds": [embed]})
        logging.info("Initial status posted: online=%s playing=%s",
                     state["is_online"], state["is_playing"])

        # Start the 5-minute periodic status loop
        asyncio.create_task(_status_loop())

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

    def _find_target_friend():
        for f in bot.friends:
            if (f.display_name or "").lower() == TARGET.lower():
                return f
        return None

    def _refresh_from_presence():
        """
        Re-read her live presence from the friend object and update state.
        fortnitepy keeps friend.last_presence updated from XMPP broadcasts,
        so this catches her status even when no change event fired (e.g. she
        was already online before the bot connected).
        """
        friend = _find_target_friend()
        if friend is None:
            logging.warning("Status refresh: '%s' not found in %d friends",
                            TARGET, len(list(bot.friends)))
            return
        p = friend.last_presence
        if p is None:
            logging.info("Status refresh: last_presence is None "
                         "(no XMPP broadcast received yet)")
            return

        new_online  = bool(p.available)
        new_playing = bool(p.playing)
        logging.info("Status refresh: available=%s playing=%s status=%r",
                     new_online, new_playing, (p.status or "")[:80])

        # Session timing transitions
        if new_online and not state["is_online"]:
            state["session_start"] = time.time()
        elif not new_online and state["is_online"]:
            state["last_online_ts"] = time.time()
            state["session_start"]  = None

        state["is_online"]   = new_online
        state["is_playing"]  = new_playing
        state["status_text"] = p.status or ""
        state["party_size"]  = p.party_size
        state["party_max"]   = p.max_party_size

    async def _status_loop():
        while True:
            await asyncio.sleep(STATUS_INTERVAL)
            try:
                _refresh_from_presence()
                embed = _build_embed(
                    state["is_online"],
                    state["is_playing"],
                    state["status_text"],
                    state["party_size"],
                    state["party_max"],
                    state["session_start"],
                    state["last_online_ts"],
                )
                _post(FORTNITE_CHANNEL, {"embeds": [embed]})
                logging.info("Periodic status: online=%s playing=%s mode=%s",
                             state["is_online"], state["is_playing"],
                             _extract_mode(state["status_text"]))
            except Exception as e:
                logging.warning("Periodic status error: %s", e)

    # ── Presence events (instant alerts on change) ────────────────────────────

    @bot.event
    async def event_friend_presence(before, after):
        if after.friend.display_name != TARGET:
            return

        was_online = before.available if before else state["is_online"]
        is_online  = after.available
        is_playing = after.playing
        status_txt = after.status or ""

        logging.info("Presence: %s → %s playing=%s | %s",
                     was_online, is_online, is_playing, status_txt[:80])

        # Update shared state
        state["is_online"]   = is_online
        state["is_playing"]  = is_playing
        state["status_text"] = status_txt
        state["party_size"]  = after.party_size
        state["party_max"]   = after.max_party_size

        if is_online and not was_online:
            state["session_start"] = time.time()
        elif not is_online and was_online:
            state["last_online_ts"] = time.time()
            state["session_start"]  = None

        embed = _build_embed(
            is_online, is_playing, status_txt,
            after.party_size, after.max_party_size,
            state["session_start"],
            state["last_online_ts"],
        )

        if is_online and not was_online:
            embed["description"] = "🟢 Just came online!"
        elif not is_online and was_online:
            embed["description"] = "⚫ Just went offline."

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
                "description": f"`{str(e)[:500]}`\nRestarting in 60s…",
                "color":        0xFF0000,
                "footer":      {"text": _now_et()},
            }],
        })
        # Cooldown before restarting so a persistent failure can't spin into
        # a rapid crash-loop that spams pings and burns Actions minutes.
        time.sleep(60)
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
