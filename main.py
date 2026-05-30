import os
import json
import logging
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.blocking import BlockingScheduler
from roblox_tracker import check_roblox_activity
from epic_tracker import check_epic_activity
from discord_notifier import notify_roblox, notify_epic, notify_startup

LOG_FILE = Path(__file__).parent / "logs" / "activity.log"
LOG_FILE.parent.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

# Roblox: Moonstar_dovetail (display: Itsreesie) — user ID hardcoded so
# tracking survives username changes
ROBLOX_TARGET = "Moonstar_dovetail"
ROBLOX_TARGET_ID = 2622410591

EPIC_TARGET = "ReesieLuvsChan"
INTERVAL = int(os.environ.get("CHECK_INTERVAL_MINUTES", "5"))

# Track previous states so Discord only pings on changes
_prev = {"roblox": None, "epic": None}


def check_all():
    if ROBLOX_TARGET:
        data = check_roblox_activity(ROBLOX_TARGET, target_user_id=ROBLOX_TARGET_ID)
        logging.info("ROBLOX | %s", json.dumps(data))
        try:
            notify_roblox(data, _prev["roblox"])
        except Exception as e:
            logging.warning("Discord notify failed: %s", e)
        _prev["roblox"] = data

    if EPIC_TARGET:
        data = check_epic_activity(EPIC_TARGET)
        logging.info("EPIC   | %s", json.dumps(data))
        try:
            notify_epic(data, _prev["epic"])
        except Exception as e:
            logging.warning("Discord notify failed: %s", e)
        _prev["epic"] = data


if __name__ == "__main__":
    print(f"Starting tracker — checking every {INTERVAL} minute(s)")
    print(f"Roblox target : {ROBLOX_TARGET} (ID: {ROBLOX_TARGET_ID})")
    print(f"Epic target   : {EPIC_TARGET or '(not set)'}")
    print(f"Log file      : {LOG_FILE}")

    notify_startup()
    check_all()  # run immediately on start

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(check_all, "interval", minutes=INTERVAL)
    scheduler.start()
