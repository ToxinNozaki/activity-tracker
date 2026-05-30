import os
import requests
from datetime import datetime

COOKIE = os.environ.get("ROBLOX_COOKIE", "")

PRESENCE_TYPES = {
    0: "Offline",
    1: "Online (Website)",
    2: "In Game",
    3: "In Studio",
}

def _headers():
    return {
        "Cookie": f".ROBLOSECURITY={COOKIE}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }


def get_user_id(username: str) -> int | None:
    resp = requests.post(
        "https://users.roblox.com/v1/usernames/users",
        json={"usernames": [username], "excludeBannedUsers": True},
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return data[0]["id"] if data else None


def get_presence(user_ids: list[int]) -> list[dict]:
    resp = requests.post(
        "https://presence.roblox.com/v1/presence/users",
        json={"userIds": user_ids},
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("userPresences", [])


def get_online_friends(user_id: int) -> list[dict]:
    resp = requests.get(
        f"https://friends.roblox.com/v1/users/{user_id}/friends/online",
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def get_game_details(place_id: int) -> dict:
    resp = requests.get(
        f"https://games.roblox.com/v1/games/multiget-place-details?placeIds={place_id}",
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return data[0] if data else {}


def check_roblox_activity(target_username: str, target_user_id: int | None = None) -> dict:
    result = {
        "username": target_username,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "status": "Unknown",
        "game": None,
        "game_url": None,
        "online_friends": [],
        "error": None,
    }

    try:
        # Prefer hardcoded user ID — avoids an extra API call and survives username changes
        user_id = target_user_id or get_user_id(target_username)
        if not user_id:
            result["error"] = f"User '{target_username}' not found"
            return result

        result["user_id"] = user_id

        presences = get_presence([user_id])
        if not presences:
            result["error"] = "Could not fetch presence data"
            return result

        presence = presences[0]
        ptype = presence.get("userPresenceType", 0)
        result["status"] = PRESENCE_TYPES.get(ptype, "Unknown")

        if ptype == 2:
            place_id = presence.get("placeId")
            root_place_id = presence.get("rootPlaceId")
            last_location = presence.get("lastLocation", "")

            if place_id:
                details = get_game_details(place_id)
                game_name = details.get("name") or last_location or "Unknown game"
                universe_id = details.get("universeId")
                result["game"] = game_name
                result["place_id"] = place_id
                result["last_location"] = last_location
                if root_place_id:
                    result["game_url"] = f"https://www.roblox.com/games/{root_place_id}"

        online_friends = get_online_friends(user_id)
        result["online_friends"] = [
            {
                "name": f.get("name", ""),
                "display_name": f.get("displayName", ""),
                "user_id": f.get("id"),
            }
            for f in online_friends
        ]

    except requests.HTTPError as e:
        result["error"] = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        result["error"] = str(e)

    return result
