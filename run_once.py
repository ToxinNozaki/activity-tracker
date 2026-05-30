"""
Single-check version for GitHub Actions cloud deployment.
Runs every 5 minutes. Sends Discord status check every 15 minutes.
"""

import os
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from roblox_tracker import check_roblox_activity, CookieExpiredError
from epic_tracker import check_epic_activity
from discord_notifier import (
    notify_roblox, notify_epic, notify_error,
    notify_cookie_expired, notify_status,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

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

    state = load_state()
    now_iso = datetime.now(timezone.utc).isoformat()

    roblox_ok  = True
    epic_ok    = True
    roblox_msg = ""
    epic_msg   = ""

    # ── Roblox ──────────────────────────────────────────────────────────────
    try:
        roblox_data = check_roblox_activity("Moonstar_dovetail", target_user_id=2622410591)
        logging.info("ROBLOX: %s", json.dumps(roblox_data))

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

    except Exception as e:
        roblox_ok  = False
        roblox_msg = str(e)
        notify_error("Roblox", "Unexpected error", str(e))
        logging.error("Roblox error: %s", e)

    # ── Epic / Fortnite ──────────────────────────────────────────────────────
    try:
        epic_data = check_epic_activity("ReesieLuvsChan")
        logging.info("EPIC: %s", json.dumps(epic_data))

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

    # ── 15-minute status check ───────────────────────────────────────────────
    if minutes_since(state.get("last_status_ts")) >= STATUS_INTERVAL_MINUTES:
        notify_status(roblox_ok, epic_ok, roblox_msg, epic_msg)
        state["last_status_ts"] = now_iso
        logging.info("Status check sent.")

    save_state(state)
    logging.info("Done.")


if __name__ == "__main__":
    main()
