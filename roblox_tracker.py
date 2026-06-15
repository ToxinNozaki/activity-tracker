import os
import time
import logging
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
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.post(
                "https://presence.roblox.com/v1/presence/users",
                json={"userIds": user_ids},
                headers=_headers(), timeout=15,
            )
            _check_auth(resp)
            resp.raise_for_status()
            return resp.json().get("userPresences", [])
        except CookieExpiredError:
            raise
        except Exception as e:
            last_exc = e
            if attempt < 2:
                time.sleep(1.5)
    raise last_exc


def get_friends_list(user_id: int) -> list[dict]:
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.get(
                f"https://friends.roblox.com/v1/users/{user_id}/friends",
                headers=_headers(), timeout=15,
            )
            _check_auth(resp)
            if not resp.ok:
                return []
            return resp.json().get("data", [])
        except CookieExpiredError:
            raise
        except Exception as e:
            last_exc = e
            if attempt < 2:
                time.sleep(1.5)
    raise last_exc


def get_user_info_by_ids(user_ids: list[int]) -> dict:
    """
    Returns {user_id: {"name": username, "display_name": display_name}}.
    display_name falls back to name if not set.
    """
    if not user_ids:
        return {}
    try:
        resp = requests.post(
            "https://users.roblox.com/v1/users",
            json={"userIds": user_ids, "excludeBannedUsers": False},
            headers=_headers(), timeout=10,
        )
        if resp.ok:
            return {
                item["id"]: {
                    "name":         item.get("name", f"User#{item['id']}"),
                    "display_name": item.get("displayName") or item.get("name", f"User#{item['id']}"),
                }
                for item in resp.json().get("data", [])
            }
    except Exception:
        pass
    return {}


def get_usernames_by_ids(user_ids: list[int]) -> dict:
    """Returns {user_id: username}. Thin wrapper around get_user_info_by_ids."""
    return {uid: info["name"] for uid, info in get_user_info_by_ids(user_ids).items()}


def get_game_details(place_id: int) -> dict:
    try:
        resp = requests.get(
            f"https://games.roblox.com/v1/games/multiget-place-details?placeIds={place_id}",
            headers=_headers(), timeout=15,
        )
        if not resp.ok:
            return {}
        data = resp.json()
        return data[0] if data else {}
    except Exception:
        return {}


def get_universe_id(place_id: int) -> int | None:
    for attempt in range(2):
        try:
            resp = requests.get(
                f"https://apis.roblox.com/universes/v1/places/{place_id}/universe",
                timeout=15,
            )
            if resp.ok:
                return resp.json().get("universeId")
            return None
        except Exception:
            if attempt == 0:
                time.sleep(1)
    return None


def get_game_stats(universe_id: int) -> dict:
    for attempt in range(2):
        try:
            resp = requests.get(
                f"https://games.roblox.com/v1/games?universeIds={universe_id}",
                timeout=15,
            )
            if resp.ok:
                data = resp.json().get("data", [])
                return data[0] if data else {}
            return {}
        except Exception:
            if attempt == 0:
                time.sleep(1)
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


def get_full_avatar_url(user_id: int) -> str | None:
    """
    Full-body avatar thumbnail — URL hash changes whenever her outfit/accessories change.
    Used for avatar change detection.
    """
    try:
        r = requests.get(
            f"https://thumbnails.roblox.com/v1/users/avatar"
            f"?userIds={user_id}&size=420x420&format=Png&isCircular=false",
            timeout=10,
        )
        if r.ok:
            data = r.json().get("data", [])
            if data and data[0].get("state") == "Completed":
                return data[0].get("imageUrl")
    except Exception:
        pass
    return None


def check_roblox_health() -> tuple[bool, str]:
    """
    Hit a public (no-auth) Roblox endpoint to distinguish API outages from cookie issues.
    Returns (is_healthy, detail_message).
    """
    try:
        r = requests.get(
            "https://games.roblox.com/v1/games?universeIds=1",
            timeout=10,
        )
        if r.status_code < 500:
            return True, "Roblox API is reachable"
        return False, f"Roblox API returned HTTP {r.status_code}"
    except requests.exceptions.Timeout:
        return False, "Roblox API timed out"
    except Exception as e:
        return False, f"Roblox API unreachable: {e}"


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
            place_id      = presence.get("placeId")
            root_place_id = presence.get("rootPlaceId")
            job_id        = presence.get("gameId")
            last_location = presence.get("lastLocation", "")

            result["game_id"] = job_id  # server instance UUID for same-server detection

            if place_id:
                # Prefer the EXPERIENCE (universe) name — that's the real game
                # name players see. The place name from multiget-place-details
                # is often generic ("Game"), which is why it showed wrong before.
                game_name   = None
                universe_id = get_universe_id(place_id)
                if universe_id:
                    stats = get_game_stats(universe_id)
                    game_name = stats.get("name")
                    result["total_playing"] = stats.get("playing")
                    if job_id:
                        result["server_player_count"] = get_server_player_count(universe_id, job_id)
                if not game_name:
                    details = get_game_details(place_id)   # fallback: place name
                    game_name = details.get("name")

                result["game"]          = game_name or last_location or "Unknown game"
                result["last_location"] = last_location
                if root_place_id:
                    result["game_url"] = f"https://www.roblox.com/games/{root_place_id}"

        # Her friends' presences
        friends = get_friends_list(user_id)
        friend_ids = [f["id"] for f in friends] if friends else []
        result["all_friend_ids"] = friend_ids

        # Batch presence in chunks of 50. Retry each chunk so a transient
        # timeout doesn't silently drop ~50 friends from this run.
        friend_presences = []
        for i in range(0, len(friend_ids), 50):
            chunk = friend_ids[i:i+50]
            for attempt in range(3):
                try:
                    friend_presences.extend(get_presence(chunk))
                    break
                except Exception as e:
                    if attempt < 2:
                        time.sleep(1)
                    else:
                        logging.warning("Roblox: presence chunk %d-%d dropped "
                                        "after 3 tries: %s", i, i + len(chunk), e)

        active = [p for p in friend_presences if p.get("userPresenceType", 0) in (1, 2)]
        active_ids = [p["userId"] for p in active if p.get("userId")]
        logging.info("Roblox friends: %d total · %d presences fetched · %d active",
                     len(friend_ids), len(friend_presences), len(active))

        # Fetch user info (name + displayName) for active friends AND the tracked user
        all_ids = list({user_id} | set(active_ids))
        info_map = get_user_info_by_ids(all_ids)

        # Fallback: names from the friends list response
        fallback_map = {f["id"]: f.get("name") or f.get("displayName", "") for f in friends}

        # Store the tracked user's display name in the result
        user_info = info_map.get(user_id, {})
        result["display_name"] = user_info.get("display_name") or target_username

        # Fetch avatars in one call
        thumbnail_map = get_user_thumbnails(all_ids)
        result["avatar_url"]      = thumbnail_map.get(user_id)
        result["full_avatar_url"] = get_full_avatar_url(user_id)  # for avatar change detection

        result["friends_presence"] = [
            {
                "user_id":      p.get("userId"),
                "name":         (info_map.get(p.get("userId"), {}).get("name")
                                 or fallback_map.get(p.get("userId"))
                                 or f"User#{p.get('userId')}"),
                "display_name": (info_map.get(p.get("userId"), {}).get("display_name")
                                 or fallback_map.get(p.get("userId"))
                                 or f"User#{p.get('userId')}"),
                "status":       PRESENCE_TYPES.get(p.get("userPresenceType", 0), "Offline"),
                "game":         p.get("lastLocation") if p.get("userPresenceType") == 2 else None,
                "game_id":      p.get("gameId"),  # server instance UUID
                "avatar_url":   thumbnail_map.get(p.get("userId")),
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
