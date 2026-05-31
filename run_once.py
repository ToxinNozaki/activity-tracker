"""
Single-check version for GitHub Actions cloud deployment.
Runs every 5 minutes. Sends Discord status check every 15 minutes.
"""

import os
import json
import logging
import requests as _requests
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from roblox_tracker import (check_roblox_activity, get_user_info_by_ids,
                            get_user_thumbnails, check_roblox_health)
from cookie_updater import check_for_cookie_update
from bot_commands import check_server_commands
from epic_tracker import check_epic_activity
from discord_notifier import (
    notify_roblox, notify_epic, notify_error,
    notify_cookie_expired, notify_status, notify_new_friends, notify_unfriended,
    notify_daily_summary, notify_weekly_games, notify_peak_hours,
    notify_credential_invalid, notify_server_hop, notify_avatar_changed,
    notify_missed_runs, notify_roblox_api_down, notify_squad_changed,
)

_EASTERN = ZoneInfo("America/New_York")

class _EasternFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created, _EASTERN)
        return ct.strftime(datefmt or "%m/%d/%Y %I:%M:%S %p %Z")

_handler = logging.StreamHandler()
_handler.setFormatter(_EasternFormatter("%(asctime)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])

STATE_FILE = Path(".state.json")
STATUS_INTERVAL_MINUTES = 15


def check_credentials():
    """
    Validate each secret/token against its respective API.
    Posts a pinged error embed for any that are invalid.
    """
    _DISCORD_API = "https://discord.com/api/v10"
    _GITHUB_API  = "https://api.github.com"

    bot_token  = os.environ.get("DISCORD_BOT_TOKEN", "")
    github_pat = os.environ.get("GITHUB_PAT", "")

    # ── Discord bot token ────────────────────────────────────────────────────
    if bot_token:
        try:
            r = _requests.get(
                f"{_DISCORD_API}/users/@me",
                headers={"Authorization": f"Bot {bot_token}"},
                timeout=10,
            )
            if r.status_code == 401:
                logging.error("Credential check: Discord Bot Token is INVALID")
                # Bot token is broken so we can't DM — this will surface in
                # the GitHub Actions failure notification instead.
            else:
                logging.info("Credential check: Discord Bot Token OK")
        except Exception as e:
            logging.warning("Credential check: Discord Bot Token unreachable: %s", e)
    else:
        logging.warning("Credential check: DISCORD_BOT_TOKEN not set")

    # ── GitHub PAT ───────────────────────────────────────────────────────────
    if github_pat:
        try:
            r = _requests.get(
                f"{_GITHUB_API}/user",
                headers={"Authorization": f"token {github_pat}",
                         "Accept": "application/vnd.github.v3+json"},
                timeout=10,
            )
            if r.status_code == 401:
                logging.error("Credential check: GitHub PAT is INVALID")
                notify_credential_invalid(
                    "GitHub PAT",
                    "Generate a new token with `repo` scope at "
                    "[GitHub → Settings → Developer Settings → Personal Access Tokens]"
                    "(https://github.com/settings/tokens), "
                    "then update the `GITHUB_PAT` secret at "
                    "https://github.com/ToxinNozaki/activity-tracker/settings/secrets/actions"
                )
            else:
                logging.info("Credential check: GitHub PAT OK")
        except Exception as e:
            logging.warning("Credential check: GitHub PAT unreachable: %s", e)
    else:
        logging.warning("Credential check: GITHUB_PAT not set")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"roblox": None, "epic": None, "last_status_ts": None}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, default=str))


def minutes_since(ts_str: str | None) -> float:
    if not ts_str:
        return 9999
    try:
        past = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        now  = datetime.now(timezone.utc)
        return (now - past).total_seconds() / 60
    except Exception:
        return 9999


def main():
    # Write epic_auth.json from secret if running in GitHub Actions and file missing
    epic_secret = os.environ.get("EPIC_AUTH_JSON", "")
    auth_file = Path("epic_auth.json")
    if epic_secret and not auth_file.exists():
        auth_file.write_text(epic_secret)

    # Check for /setcookie DM command before anything else
    check_for_cookie_update()

    state = load_state()
    now_iso = datetime.now(timezone.utc).isoformat()

    # ── Auto-recovery: warn if we missed runs ────────────────────────────────
    # Guard: last_run_ts being None means this is the first ever run — skip
    if state.get("last_run_ts"):
        gap = minutes_since(state.get("last_run_ts"))
        if gap > 15:
            last_warn = state.get("last_recovery_warning_ts")
            if not last_warn or minutes_since(last_warn) > 30:
                notify_missed_runs(gap)
                state["last_recovery_warning_ts"] = now_iso
                logging.warning("Auto-recovery: %.0f minute gap detected", gap)

    # ── Credential check — rate-limited to once per hour to avoid spam ───────
    if minutes_since(state.get("last_credential_check_ts")) >= 60:
        check_credentials()
        state["last_credential_check_ts"] = now_iso

    # ── Handle server channel commands (/restart, /status, /help) ───────────
    check_server_commands(state)

    roblox_ok  = True
    epic_ok    = True
    roblox_msg = ""
    epic_msg   = ""

    prev_roblox_ok = state.get("last_roblox_ok", True)
    prev_epic_ok   = state.get("last_epic_ok",   True)

    # ── Roblox ──────────────────────────────────────────────────────────────
    try:
        roblox_data = check_roblox_activity("Moonstar_dovetail", target_user_id=2622410591)
        logging.info("ROBLOX: %s", json.dumps(roblox_data))

        # ── Session timer ────────────────────────────────────────────────────
        current_game  = roblox_data.get("game")
        prev_game     = state.get("current_game")
        game_start_ts = state.get("current_game_start")

        if current_game:
            if current_game == prev_game and game_start_ts:
                try:
                    start = datetime.fromisoformat(game_start_ts)
                    roblox_data["session_minutes"] = int(
                        (datetime.now(timezone.utc) - start).total_seconds() / 60
                    )
                except Exception:
                    state["current_game_start"] = datetime.now(timezone.utc).isoformat()
                    roblox_data["session_minutes"] = 0
            else:
                state["current_game"]       = current_game
                state["current_game_start"] = datetime.now(timezone.utc).isoformat()
                roblox_data["session_minutes"] = 0
        else:
            state["current_game"]       = None
            state["current_game_start"] = None

        # ── Server hop detection ─────────────────────────────────────────────
        current_game_id = roblox_data.get("game_id")
        prev_game_id    = state.get("current_game_id")
        if (current_game and current_game == prev_game   # same game
                and current_game_id and prev_game_id     # both have server IDs
                and current_game_id != prev_game_id):    # but different server
            notify_server_hop(current_game, roblox_data.get("server_player_count"))
            logging.info("Server hop detected in %s", current_game)
        state["current_game_id"] = current_game_id

        # ── Last seen tracking ───────────────────────────────────────────────
        if roblox_data.get("status") not in ("Offline", "Unknown", None):
            state["last_roblox_online_ts"] = now_iso
        roblox_data["last_seen_ts"] = state.get("last_roblox_online_ts")

        if roblox_data.get("cookie_expired"):
            notify_cookie_expired()
            roblox_ok  = False
            roblox_msg = "Cookie expired — update GitHub secret"
        elif roblox_data.get("error"):
            roblox_ok  = False
            roblox_msg = roblox_data["error"]
            notify_error("Roblox", roblox_data["error"])
        else:
            notify_roblox(roblox_data, state.get("roblox"))

        state["roblox"] = roblox_data

        # ── Avatar change detection ──────────────────────────────────────────
        new_avatar = roblox_data.get("full_avatar_url")
        prev_avatar = state.get("last_avatar_url")
        if new_avatar and prev_avatar and new_avatar != prev_avatar:
            notify_avatar_changed(
                roblox_data.get("username", "Moonstar_dovetail"),
                roblox_data.get("user_id", 2622410591),
                new_avatar,
            )
            logging.info("Avatar change detected")
        if new_avatar:
            state["last_avatar_url"] = new_avatar

        # ── New friend detection ─────────────────────────────────────────────
        current_ids = set(roblox_data.get("all_friend_ids", []))
        prev_ids    = set(state.get("roblox_friend_ids", []))

        if prev_ids and current_ids:  # skip first-ever run (no baseline yet)
            new_ids     = current_ids - prev_ids
            removed_ids = prev_ids - current_ids

            if new_ids or removed_ids:
                all_changed = list(new_ids | removed_ids)
                info_map  = get_user_info_by_ids(all_changed)
                thumb_map = get_user_thumbnails(all_changed)

                def _build(uid):
                    info = info_map.get(uid, {})
                    return {
                        "user_id":      uid,
                        "name":         info.get("name", f"User#{uid}"),
                        "display_name": info.get("display_name", ""),
                        "avatar_url":   thumb_map.get(uid),
                    }

                if new_ids:
                    new_friends = [_build(uid) for uid in new_ids]
                    notify_new_friends(new_friends)
                    logging.info("New friends: %s", [f["name"] for f in new_friends])

                if removed_ids:
                    removed = [_build(uid) for uid in removed_ids]
                    notify_unfriended(removed)
                    logging.info("Unfriended: %s", [f["name"] for f in removed])

        if current_ids:
            state["roblox_friend_ids"] = list(current_ids)

        # ── Daily stats ──────────────────────────────────────────────────────
        today_str = datetime.now(_EASTERN).strftime("%Y-%m-%d")
        daily     = state.get("daily_stats") or {}

        if daily.get("date") != today_str:
            # Day rolled over — post yesterday's summary if it has data
            if daily.get("date") and daily.get("online_minutes", 0) > 0:
                notify_daily_summary(daily)
                logging.info("Daily summary posted for %s", daily["date"])
            daily = {"date": today_str, "online_minutes": 0,
                     "in_game_minutes": 0, "games": {}, "friends_seen": []}

        status = roblox_data.get("status", "Offline")
        if status in ("Online (Website)", "In Game", "In Studio"):
            daily["online_minutes"] = daily.get("online_minutes", 0) + 5
        if status == "In Game" and current_game:
            daily["in_game_minutes"] = daily.get("in_game_minutes", 0) + 5
            daily.setdefault("games", {})[current_game] = (
                daily["games"].get(current_game, 0) + 5
            )
        friends_seen = set(daily.get("friends_seen", []))
        for f in roblox_data.get("friends_presence", []):
            if f.get("name"):
                friends_seen.add(f["name"])
        daily["friends_seen"] = list(friends_seen)
        state["daily_stats"]  = daily

        # ── Hourly activity (peak hours) ─────────────────────────────────────
        if status in ("Online (Website)", "In Game", "In Studio"):
            hour_key = str(datetime.now(_EASTERN).hour)
            ha = state.setdefault("hourly_activity", {})
            ha[hour_key] = ha.get(hour_key, 0) + 5

        # ── Weekly game stats + peak hours ───────────────────────────────────
        if status == "In Game" and current_game:
            gs = state.setdefault("game_stats", {})
            gs[current_game] = gs.get(current_game, 0) + 5

        if minutes_since(state.get("last_weekly_ts")) >= 7 * 24 * 60:
            gs = state.get("game_stats") or {}
            ha = state.get("hourly_activity") or {}
            notify_weekly_games(gs)
            notify_peak_hours(ha)
            logging.info("Weekly summary posted.")
            state["game_stats"]      = {}
            state["hourly_activity"] = {}
            state["last_weekly_ts"]  = now_iso

    except Exception as e:
        roblox_ok  = False
        roblox_msg = str(e)
        # Distinguish API outage from other errors
        healthy, health_msg = check_roblox_health()
        if not healthy:
            notify_roblox_api_down(health_msg)
        else:
            notify_error("Roblox", "Unexpected error", str(e))
        logging.error("Roblox error: %s", e)

    # ── Epic / Fortnite ──────────────────────────────────────────────────────
    try:
        epic_data = check_epic_activity("ReesieLuvsChan")
        logging.info("EPIC: %s", json.dumps(epic_data))

        # ── Fortnite session timer ───────────────────────────────────────────
        ft_online      = epic_data.get("online", False)
        ft_session_ts  = state.get("fortnite_session_start")

        if ft_online:
            if ft_session_ts:
                try:
                    start = datetime.fromisoformat(ft_session_ts)
                    epic_data["session_minutes"] = int(
                        (datetime.now(timezone.utc) - start).total_seconds() / 60
                    )
                except Exception:
                    state["fortnite_session_start"] = datetime.now(timezone.utc).isoformat()
                    epic_data["session_minutes"] = 0
            else:
                state["fortnite_session_start"] = datetime.now(timezone.utc).isoformat()
                epic_data["session_minutes"] = 0
        else:
            state["fortnite_session_start"] = None

        # ── Last seen tracking ───────────────────────────────────────────────
        if ft_online:
            state["last_epic_online_ts"] = now_iso
        epic_data["last_seen_ts"] = state.get("last_epic_online_ts")

        if epic_data.get("error"):
            epic_ok  = False
            epic_msg = epic_data["error"]
            # Don't spam errors if she's just not in friends list
            if "not in your Epic friends" not in epic_msg:
                notify_error("Fortnite", epic_data["error"])
        else:
            notify_epic(epic_data, state.get("epic"))

            # ── Squad detection ──────────────────────────────────────────────
            new_party = epic_data.get("party_size")
            old_party = state.get("epic_party_size")
            if ft_online and new_party is not None and new_party != old_party:
                notify_squad_changed(
                    epic_data.get("username", "ReesieLuvsChan"),
                    old_party, new_party,
                    epic_data.get("party_max"),
                )
                logging.info("Squad changed: %s → %s", old_party, new_party)
            state["epic_party_size"] = new_party if ft_online else None

        state["epic"] = epic_data

    except Exception as e:
        epic_ok  = False
        epic_msg = str(e)
        notify_error("Fortnite", "Unexpected error", str(e))
        logging.error("Epic error: %s", e)

    # ── Status updates ───────────────────────────────────────────────────────
    status_changed = (roblox_ok != prev_roblox_ok) or (epic_ok != prev_epic_ok)
    periodic_due   = minutes_since(state.get("last_status_ts")) >= STATUS_INTERVAL_MINUTES

    if status_changed or periodic_due:
        notify_status(roblox_ok, epic_ok, roblox_msg, epic_msg)
        state["last_status_ts"] = now_iso
        if status_changed:
            logging.info("Status CHANGED — posted immediately.")
        else:
            logging.info("15-minute status check sent.")

    state["last_roblox_ok"] = roblox_ok
    state["last_epic_ok"]   = epic_ok
    state["last_run_ts"]    = now_iso   # used by auto-recovery on next run

    save_state(state)
    logging.info("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        logging.critical("FATAL: %s", traceback.format_exc())
        try:
            notify_error("Tracker", "Fatal unhandled exception", traceback.format_exc()[-1000:])
        except Exception:
            pass
        raise
