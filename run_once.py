"""
Single-check version for GitHub Actions cloud deployment.
Runs every 5 minutes. Sends Discord status check every 15 minutes.
"""

import os
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from roblox_tracker import check_roblox_activity, CookieExpiredError, get_usernames_by_ids, get_user_thumbnails
from cookie_updater import check_for_cookie_update
from epic_tracker import check_epic_activity
from discord_notifier import (
    notify_roblox, notify_epic, notify_error,
    notify_cookie_expired, notify_status, notify_new_friends, notify_unfriended,
    notify_daily_summary, notify_weekly_games, notify_peak_hours,
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
                start = datetime.fromisoformat(game_start_ts)
                roblox_data["session_minutes"] = int(
                    (datetime.now(timezone.utc) - start).total_seconds() / 60
                )
            else:
                state["current_game"]       = current_game
                state["current_game_start"] = datetime.now(timezone.utc).isoformat()
                roblox_data["session_minutes"] = 0
        else:
            state["current_game"]       = None
            state["current_game_start"] = None

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

        # ── New friend detection ─────────────────────────────────────────────
        current_ids = set(roblox_data.get("all_friend_ids", []))
        prev_ids    = set(state.get("roblox_friend_ids", []))

        if prev_ids and current_ids:  # skip first-ever run (no baseline yet)
            new_ids     = current_ids - prev_ids
            removed_ids = prev_ids - current_ids

            if new_ids or removed_ids:
                all_changed = list(new_ids | removed_ids)
                name_map  = get_usernames_by_ids(all_changed)
                thumb_map = get_user_thumbnails(all_changed)

                def _build(uid):
                    return {
                        "user_id":    uid,
                        "name":       name_map.get(uid, f"User#{uid}"),
                        "avatar_url": thumb_map.get(uid),
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
                start = datetime.fromisoformat(ft_session_ts)
                epic_data["session_minutes"] = int(
                    (datetime.now(timezone.utc) - start).total_seconds() / 60
                )
            else:
                state["fortnite_session_start"] = datetime.now(timezone.utc).isoformat()
                epic_data["session_minutes"] = 0
        else:
            state["fortnite_session_start"] = None

        if epic_data.get("error"):
            epic_ok  = False
            epic_msg = epic_data["error"]
            # Don't spam errors if she's just not in friends list
            if "not in your Epic friends" not in epic_msg:
                notify_error("Fortnite", epic_data["error"])
        else:
            notify_epic(epic_data, state.get("epic"))

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
