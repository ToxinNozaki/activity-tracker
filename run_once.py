"""
Single-check version for GitHub Actions cloud deployment.
GitHub runs this every 5 minutes — no persistent process needed.
"""

import os
import json
import logging
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from roblox_tracker import check_roblox_activity
from epic_tracker import check_epic_activity
from discord_notifier import notify_roblox, notify_epic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

STATE_FILE = Path(".state.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"roblox": None, "epic": None}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state))


def main():
    # If running in GitHub Actions, write epic_auth.json from secret if not cached
    epic_secret = os.environ.get("EPIC_AUTH_JSON", "")
    auth_file = Path("epic_auth.json")
    if epic_secret and not auth_file.exists():
        auth_file.write_text(epic_secret)

    state = load_state()

    roblox_data = check_roblox_activity("Moonstar_dovetail", target_user_id=2622410591)
    logging.info("ROBLOX: %s", json.dumps(roblox_data))
    try:
        notify_roblox(roblox_data, state.get("roblox"))
    except Exception as e:
        logging.warning("Discord notify failed: %s", e)
    state["roblox"] = roblox_data

    epic_data = check_epic_activity("ReesieLuvsChan")
    logging.info("EPIC: %s", json.dumps(epic_data))
    try:
        notify_epic(epic_data, state.get("epic"))
    except Exception as e:
        logging.warning("Discord notify failed: %s", e)
    state["epic"] = epic_data

    save_state(state)
    logging.info("Done.")


if __name__ == "__main__":
    main()
