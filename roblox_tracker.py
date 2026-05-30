import os
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_EASTERN = ZoneInfo("America/New_York")

def _now_et() -> str:
    return datetime.now(_EASTERN).strftime("%m/%d/%Y %I:%M %p %Z")

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


class CookieExpiredError(Exception):
    pass


def _check_auth(resp: requests.Response):
    if resp.status_code == 401:
        raise CookieExpiredError("Roblox cookie has expired")


def get_user_id(username: str) -> int | None:
    resp = requests.post(
        "https://users.roblox.com/v1/usernames/users",
        json={"usernames": [username], "excludeBannedUsers": True},
        headers=_headers(), timeout=10,
    )
    _check_auth(resp)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return data[0]["id"] if data else None


def get_presence(user_ids: list[int]) -> list[dict]:
    resp = requests.post(
        "https://presence.roblox.com/v1/presence/users",
        json={"userIds": user_ids},
        headers=_headers(), timeout=10,
    )
    _check_auth(resp)
    resp.raise_for_status()
    return resp.json().get("userPresences", [])


def get_friends_list(user_id: int) -> list[dict]:
    resp = requests.get(
        f"https://friends.roblox.com/v1/users/{user_id}/friends",
        headers=_headers(), timeout=10,
    )
    _check_auth(resp)
    if not resp.ok:
        return []
    return resp.json().get("data", [])


def get_usernames_by_ids(user_ids: list[int]) -> dict:
    """Returns {user_id: username} by querying the users API directly."""
    if not user_ids:
        return {}
    try:
        resp = requests.post(
            "https://users.roblox.com/v1/users",
            json={"userIds": user_ids, "excludeBannedUsers": False},
            headers=_headers(), timeout=10,
        )
        if resp.ok:
            return {item["id"]: item.get("name", f"User#{item['id']}")
                    for item in resp.json().get("data", [])}
    except Exception:
        pass
    return {}


def get_game_details(place_id: int) -> dict:
    resp = requests.get(
        f"https://games.roblox.com/v1/games/multiget-place-details?placeIds={place_id}",
        headers=_headers(), timeout=10,
    )
    if not resp.ok:
        return {}
    data = resp.json()
    return data[0] if data else {}


def get_universe_id(place_id: int) -> int | None:
    resp = requests.get(
        f"https://apis.roblox.com/universes/v1/places/{place_id}/universe",
        timeout=10,
    )
    if resp.ok:
        return resp.json().get("universeId")
    return None


def get_game_stats(universe_id: int) -> dict:
    resp = requests.get(
        f"https://games.roblox.com/v1/games?universeIds={universe_id}",
        timeout=10,
    )
    if resp.ok:
        data = resp.json().get("data", [])
        return data[0] if data else {}
    return {}


def get_server_player_count(universe_id: int, job_id: str) -> int | None:
    """Find her specific server. Checks Public then Friends (private/VIP) servers."""
    for server_type in ("Public", "Friends"):
        cursor = ""
        for _ in range(10):  # up to 1000 servers per type
            try:
                url = (f"https://games.roblox.com/v1/games/{universe_id}"
                       f"/servers/{server_type}?limit=100&sortOrder=Asc"
                       + (f"&cursor={cursor}" if cursor else ""))
                resp = requests.get(url, headers=_headers(), timeout=10)
                if not resp.ok:
                    break
                body = resp.json()
                for server in body.get("data", []):
                    if server.get("id") == job_id:
                        return server.get("playing")
                cursor = body.get("nextPageCursor") or ""
                if not cursor:
                    break
            except Exception:
                break
    return None


def get_user_thumbnails(user_ids: list[int]) -> dict:
    """Returns {user_id: image_url} for the given user IDs."""
    if not user_ids:
        return {}
    ids_str = ",".join(str(uid) for uid in user_ids)
    try:
        resp = requests.get(
            f"https://thumbnails.roblox.com/v1/users/avatar-headshot"
            f"?userIds={ids_str}&size=150x150&format=Png",
            timeout=10,
        )
        if resp.ok:
            return {
                item["targetId"]: item["imageUrl"]
                for item in resp.json().get("data", [])
                if item.get("state") == "Completed" and item.get("imageUrl")
            }
    except Exception:
        pass
    return {}


def check_roblox_activity(target_username: str, target_user_id: int | None = None) -> dict:
    result = {
        "username": target_username,
        "timestamp": _now_et(),
        "status": "Unknown",
        "game": None,
        "game_url": None,
        "server_player_count": None,
        "total_playing": None,
        "friends_presence": [],
        "all_friend_ids": [],
        "error": None,
        "cookie_expired": False,
    }

    try:
        user_id = target_user_id or get_user_id(target_username)
        if not user_id:
            result["error"] = f"User '{target_username}' not found"
            return result

        result["user_id"] = user_id

        # Her presence
        presences = get_presence([user_id])
        if not presences:
            result["error"] = "Could not fetch presence"
            return result

        presence = presences[0]
        ptype = presence.get("userPresenceType", 0)
        result["status"] = PRESENCE_TYPES.get(ptype, "Unknown")

        if ptype == 2:
            place_id     = presence.get("placeId")
            root_place_id = presence.get("rootPlaceId")
            job_id       = presence.get("gameId")
            last_location = presence.get("lastLocation", "")

            if place_id:
                details   = get_game_details(place_id)
                game_name = details.get("name") or last_location or "Unknown game"
                result["game"]          = game_name
                result["last_location"] = last_location
                if root_place_id:
                    result["game_url"] = f"https://www.roblox.com/games/{root_place_id}"

                universe_id = get_universe_id(place_id)
                if universe_id:
                    stats = get_game_stats(universe_id)
                    result["total_playing"] = stats.get("playing")
                    if job_id:
                        result["server_player_count"] = get_server_player_count(universe_id, job_id)

        # Her friends' presences
        friends = get_friends_list(user_id)
        friend_ids = [f["id"] for f in friends] if friends else []
        result["all_friend_ids"] = friend_ids

        # Batch in chunks of 50 (presence API limit)
        friend_presences = []
        for i in range(0, len(friend_ids), 50):
            chunk = friend_ids[i:i+50]
            try:
                friend_presences.extend(get_presence(chunk))
            except Exception:
                pass

        active = [p for p in friend_presences if p.get("userPresenceType", 0) in (1, 2)]
        active_ids = [p["userId"] for p in active if p.get("userId")]

        # Look up usernames directly from user IDs — avoids any field-name ambiguity
        name_map = get_usernames_by_ids(active_ids)
        # Fallback: names from the friends list response
        fallback_map = {f["id"]: f.get("name") or f.get("displayName", "") for f in friends}

        # Fetch avatars for active friends + tracked user in one call
        all_ids = list({user_id} | set(active_ids))
        thumbnail_map = get_user_thumbnails(all_ids)
        result["avatar_url"] = thumbnail_map.get(user_id)

        result["friends_presence"] = [
            {
                "user_id":    p.get("userId"),
                "name":       (name_map.get(p.get("userId"))
                               or fallback_map.get(p.get("userId"))
                               or f"User#{p.get('userId')}"),
                "status":     PRESENCE_TYPES.get(p.get("userPresenceType", 0), "Offline"),
                "game":       p.get("lastLocation") if p.get("userPresenceType") == 2 else None,
                "avatar_url": thumbnail_map.get(p.get("userId")),
            }
            for p in active
        ]

    except CookieExpiredError:
        result["cookie_expired"] = True
        result["error"] = "Cookie expired"
    except requests.HTTPError as e:
        result["error"] = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        result["error"] = str(e)

    return result
