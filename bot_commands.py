"""
Server-channel bot commands.

Polls the commands channel each run and handles slash-style commands.
Type any command as a plain message — the bot responds within ~5 minutes.

Available commands:
  /restart  — trigger a new tracker run immediately and reset timers
  /status   — show current tracker status inline
  /help     — list all commands
"""

import os
import logging
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

COMMANDS_CHANNEL_ID = "1510249219087401060"
BOT_TOKEN           = os.environ.get("DISCORD_BOT_TOKEN", "")
GITHUB_PAT          = os.environ.get("GITHUB_PAT", "")
GH_TOKEN            = os.environ.get("GH_TOKEN", "")
REPO                = "ToxinNozaki/activity-tracker"

_DISCORD = "https://discord.com/api/v10"
_GITHUB  = "https://api.github.com"
_ET      = ZoneInfo("America/New_York")

# State key used to avoid double-processing the same command message
_LAST_CMD_ID = "last_handled_command_id"


# ── Discord helpers ───────────────────────────────────────────────────────────

def _d(method: str, path: str, **kwargs):
    return requests.request(
        method, f"{_DISCORD}{path}",
        headers={"Authorization": f"Bot {BOT_TOKEN}"},
        timeout=10, **kwargs,
    )


def _reply(content: str = "", embeds: list | None = None, reply_to: str | None = None):
    payload: dict = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds
    if reply_to:
        payload["message_reference"] = {"message_id": reply_to}
    r = _d("POST", f"/channels/{COMMANDS_CHANNEL_ID}/messages", json=payload)
    if not r.ok:
        logging.warning("bot_commands: reply failed %s: %s", r.status_code, r.text[:200])


# ── GitHub trigger ────────────────────────────────────────────────────────────

def _trigger_run() -> bool:
    token = GH_TOKEN or GITHUB_PAT
    if not token:
        return False
    try:
        r = requests.post(
            f"{_GITHUB}/repos/{REPO}/dispatches",
            headers={"Authorization": f"token {token}",
                     "Accept": "application/vnd.github.v3+json"},
            json={"event_type": "run-tracker"},
            timeout=10,
        )
        return r.status_code in (200, 204)
    except Exception as e:
        logging.warning("bot_commands: _trigger_run failed: %s", e)
        return False


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_ago(ts_str: str | None) -> str:
    """Plain relative time string — used for 'last run' display."""
    if not ts_str:
        return "unknown"
    try:
        past = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        mins = (datetime.now(timezone.utc) - past).total_seconds() / 60
        if mins < 1:
            return "just now"
        if mins < 60:
            return f"{int(mins)}m ago"
        h, m = divmod(int(mins), 60)
        return f"{h}h {m}m ago" if m else f"{h}h ago"
    except Exception:
        return "unknown"


def _fmt_discord_ts(ts_str: str | None) -> str:
    """Discord dynamic timestamp — renders as live relative time in the client."""
    if not ts_str:
        return "unknown"
    try:
        past = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return f"<t:{int(past.timestamp())}:R>"
    except Exception:
        return "unknown"


def _fmt_session(minutes: int | None) -> str:
    if minutes is None:
        return ""
    h, m = divmod(minutes, 60)
    return f" ({h}h {m}m)" if h else (f" ({m}m)" if m else " (< 1 min)")


# ── Command handlers ──────────────────────────────────────────────────────────

def _handle_restart(state: dict, reply_to: str | None = None):
    ok = _trigger_run()
    # Reset the last_run_ts so auto-recovery doesn't fire after the forced restart
    if ok:
        state["last_run_ts"] = datetime.now(timezone.utc).isoformat()
        _reply(embeds=[{
            "title": "🔄 Tracker Restarting",
            "description": (
                "New run triggered — tracker will check in and post "
                "an update in ~30 seconds."
            ),
            "color": 0x00B04F,
            "footer": {"text": datetime.now(_ET).strftime("%m/%d/%Y %I:%M %p %Z")},
        }], reply_to=reply_to)
        logging.info("bot_commands: /restart — new run triggered")
    else:
        _reply(embeds=[{
            "title": "❌ Restart Failed",
            "description": (
                "Could not trigger a new run.\n"
                "Make sure `GITHUB_PAT` has `repo` scope and is set in GitHub Secrets."
            ),
            "color": 0xFF0000,
            "footer": {"text": datetime.now(_ET).strftime("%m/%d/%Y %I:%M %p %Z")},
        }], reply_to=reply_to)


def _handle_status(state: dict, reply_to: str | None = None):
    last_run = _fmt_ago(state.get("last_run_ts"))

    # Roblox
    roblox = state.get("roblox") or {}
    r_status = roblox.get("status", "Unknown")
    r_game   = roblox.get("game")
    r_line   = r_status
    if r_game:
        r_line += f" • {r_game}{_fmt_session(roblox.get('session_minutes'))}"
    elif r_status == "Offline":
        last_seen = _fmt_discord_ts(state.get("last_roblox_online_ts"))
        r_line += f" — last seen {last_seen}"

    # Fortnite
    epic = state.get("epic") or {}
    if epic.get("online") and epic.get("playing"):
        e_line = f"In Game{_fmt_session(epic.get('session_minutes'))}"
        if epic.get("game_mode"):
            e_line = f"{epic['game_mode']}{_fmt_session(epic.get('session_minutes'))}"
    elif epic.get("online"):
        e_line = "Online"
    else:
        last_seen = _fmt_discord_ts(state.get("last_epic_online_ts"))
        e_line = f"Offline — last seen {last_seen}"

    roblox_ok = state.get("last_roblox_ok", True)
    epic_ok   = state.get("last_epic_ok",   True)
    color = 0x00B04F if (roblox_ok and epic_ok) else (0xFFA500 if (roblox_ok or epic_ok) else 0xFF0000)

    _reply(embeds=[{
        "title": "📊 Tracker Status",
        "description": f"Last run: **{last_run}**",
        "fields": [
            {"name": "Roblox — Moonstar_dovetail", "value": r_line, "inline": False},
            {"name": "Fortnite — ReesieLuvsChan",  "value": e_line, "inline": False},
        ],
        "color": color,
        "footer": {"text": datetime.now(_ET).strftime("%m/%d/%Y %I:%M %p %Z")},
    }], reply_to=reply_to)


def _handle_help(reply_to: str | None = None):
    _reply(embeds=[{
        "title": "🤖 Bot Commands",
        "description": (
            "`/status` — Current tracker status for both platforms\n"
            "`/restart` — Trigger a new tracker run immediately\n"
            "`/help` — Show this message\n\n"
            "*To update your Roblox cookie, DM this bot the cookie value directly.*"
        ),
        "color": 0x5865F2,
        "footer": {"text": datetime.now(_ET).strftime("%m/%d/%Y %I:%M %p %Z")},
    }], reply_to=reply_to)


# ── Main entry point ──────────────────────────────────────────────────────────

def check_server_commands(state: dict) -> None:
    """
    Fetch recent messages from the commands channel and execute any unhandled
    slash-style commands. Tracks the last-handled message ID in state to
    prevent double-processing the same command across two consecutive runs.
    """
    if not BOT_TOKEN:
        return

    r = _d("GET", f"/channels/{COMMANDS_CHANNEL_ID}/messages", params={"limit": 15})
    if not r.ok:
        logging.warning("bot_commands: could not read channel %s: %s",
                        COMMANDS_CHANNEL_ID, r.status_code)
        return

    last_handled = state.get(_LAST_CMD_ID, "0")
    cutoff       = datetime.now(timezone.utc) - timedelta(minutes=10)

    # Messages come back newest-first
    for msg in r.json():
        msg_id = msg.get("id", "0")

        # Already handled this one or older
        if int(msg_id) <= int(last_handled):
            break

        # Skip bot's own messages
        if msg.get("author", {}).get("bot"):
            continue

        # Skip messages older than one run interval
        try:
            msg_time = datetime.fromisoformat(
                msg["timestamp"].replace("Z", "+00:00")
            )
            if msg_time < cutoff:
                break
        except Exception:
            continue

        # Normalise: strip leading / or ! and lowercase
        raw     = msg.get("content", "").strip()
        content = (raw.lstrip("/!").lower().split() or [""])[0]

        if content == "restart":
            state[_LAST_CMD_ID] = msg_id
            _handle_restart(state, reply_to=msg_id)
            logging.info("bot_commands: handled /restart")
            return
        elif content == "status":
            state[_LAST_CMD_ID] = msg_id
            _handle_status(state, reply_to=msg_id)
            logging.info("bot_commands: handled /status")
            return
        elif content == "help":
            state[_LAST_CMD_ID] = msg_id
            _handle_help(reply_to=msg_id)
            logging.info("bot_commands: handled /help")
            return


