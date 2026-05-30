"""
Epic Games / Fortnite presence tracker.
Run setup_epic.py once first to create epic_auth.json.
The refresh token auto-renews on every run — no maintenance needed.
"""

import json
import base64
import requests
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_EASTERN = ZoneInfo("America/New_York")

def _now_et() -> str:
    return datetime.now(_EASTERN).strftime("%m/%d/%Y %I:%M %p %Z")

_CLIENT_ID = "34a02cf8f4414e29b15921876da36f9a"
_CLIENT_SECRET = "daafbccc737745039dffe53d94fc76cf"
_BASIC = base64.b64encode(f"{_CLIENT_ID}:{_CLIENT_SECRET}".encode()).decode()

AUTH_FILE = Path(__file__).parent / "epic_auth.json"
TOKEN_URL = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token"


def _token_headers():
    return {
        "Authorization": f"Basic {_BASIC}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _bearer_headers(token: str):
    return {"Authorization": f"Bearer {token}"}


def _refresh_access_token(refresh_token: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        headers=_token_headers(),
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_access_token() -> tuple[str, str]:
    """Returns (access_token, account_id). Saves updated refresh token each call."""
    if not AUTH_FILE.exists():
        raise RuntimeError("epic_auth.json not found — run setup_epic.py first.")

    auth = json.loads(AUTH_FILE.read_text())
    session = _refresh_access_token(auth["refresh_token"])

    # Save the new refresh token so it never expires
    auth["refresh_token"] = session["refresh_token"]
    AUTH_FILE.write_text(json.dumps(auth, indent=2))

    account_id = session.get("account_id") or session.get("accountId") or auth.get("accountId")
    return session["access_token"], account_id


def find_account_id(display_name: str, token: str) -> str | None:
    resp = requests.get(
        f"https://account-public-service-prod.ol.epicgames.com/account/api/public/account/displayName/{display_name}",
        headers=_bearer_headers(token),
        timeout=10,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("id")


def get_friends_presence(my_account_id: str, token: str) -> list[dict]:
    resp = requests.get(
        f"https://friends-public-service-prod06.ol.epicgames.com/friends/api/v1/{my_account_id}/summary",
        headers=_bearer_headers(token),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("friends", [])


def parse_fortnite_status(presence: dict) -> dict:
    status_text = presence.get("status", "")
    is_playing = presence.get("bIsPlaying", False)

    result = {
        "online": presence.get("bIsOnline", False),
        "playing": is_playing,
        "joinable": presence.get("bIsJoinable", False),
        "status_text": status_text,
        "game_mode": None,
        "party_size": None,
        "party_max": None,
    }

    # e.g. "Battle Royale Lobby - 2 of 4"
    if is_playing and " - " in status_text:
        mode_part, party_part = status_text.rsplit(" - ", 1)
        result["game_mode"] = mode_part.strip()
        if " of " in party_part:
            parts = party_part.split(" of ")
            try:
                result["party_size"] = int(parts[0].strip())
                result["party_max"] = int(parts[1].strip())
            except ValueError:
                pass

    props = presence.get("properties", {})
    if not result["game_mode"] and props.get("GameMode"):
        result["game_mode"] = props["GameMode"]

    return result


def check_epic_activity(target_display_name: str) -> dict:
    result = {
        "username": target_display_name,
        "timestamp": _now_et(),
        "online": False,
        "playing": False,
        "game_mode": None,
        "party_size": None,
        "party_max": None,
        "status_text": None,
        "error": None,
    }

    try:
        token, my_account_id = get_access_token()
        target_id = find_account_id(target_display_name, token)

        if not target_id:
            result["error"] = f"Epic account '{target_display_name}' not found"
            return result

        friends = get_friends_presence(my_account_id, token)
        target = next((f for f in friends if f.get("accountId") == target_id), None)

        if target is None:
            result["error"] = (
                f"'{target_display_name}' is not in your Epic friends list "
                "or has presence hidden"
            )
            return result

        parsed = parse_fortnite_status(target)
        result.update(parsed)

    except requests.HTTPError as e:
        result["error"] = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        result["error"] = str(e)

    return result
