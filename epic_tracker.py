"""
Epic Games / Fortnite presence tracker.
Run setup_epic.py once first to create epic_auth.json.
The refresh token auto-renews on every run — no maintenance needed.
"""

import json
import base64
import logging
import requests
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_EASTERN = ZoneInfo("America/New_York")

def _now_et() -> str:
    return datetime.now(_EASTERN).strftime("%m/%d/%Y %I:%M %p %Z")

_CLIENT_ID     = "34a02cf8f4414e29b15921876da36f9a"
_CLIENT_SECRET = "daafbccc737745039dffe53d94fc76cf"
_BASIC         = base64.b64encode(f"{_CLIENT_ID}:{_CLIENT_SECRET}".encode()).decode()

AUTH_FILE  = Path(__file__).parent / "epic_auth.json"
TOKEN_URL  = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token"
_FRIENDS   = "https://friends-public-service-prod06.ol.epicgames.com"
_PRESENCE  = "https://presence-public-service-prod.ol.epicgames.com"
_ACCOUNT   = "https://account-public-service-prod.ol.epicgames.com"


def _token_headers():
    return {
        "Authorization": f"Basic {_BASIC}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Auth ──────────────────────────────────────────────────────────────────────

def _refresh_access_token(refresh_token: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        headers=_token_headers(),
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=15,
    )
    if not resp.ok:
        body = resp.text[:300]
        if resp.status_code == 400 and "expired" in body.lower():
            raise RuntimeError(
                f"Epic refresh token has expired (HTTP 400). "
                f"Re-run setup_epic.py to generate a fresh token. Raw: {body}"
            )
        if resp.status_code == 401:
            raise RuntimeError(
                f"Epic credentials rejected (HTTP 401). "
                f"The client ID/secret may be revoked. Raw: {body}"
            )
        resp.raise_for_status()
    return resp.json()


def get_access_token() -> tuple[str, str]:
    """Returns (access_token, account_id). Saves updated refresh token each call."""
    if not AUTH_FILE.exists():
        raise RuntimeError("epic_auth.json not found — run setup_epic.py first.")

    auth    = json.loads(AUTH_FILE.read_text())
    session = _refresh_access_token(auth["refresh_token"])

    # Persist the new refresh token immediately
    auth["refresh_token"] = session["refresh_token"]
    AUTH_FILE.write_text(json.dumps(auth, indent=2))

    account_id = (
        session.get("account_id")
        or session.get("accountId")
        or auth.get("accountId")
    )
    logging.info("Epic: authenticated as account %s", account_id)
    return session["access_token"], account_id


# ── Lookups ───────────────────────────────────────────────────────────────────

def find_account_id(display_name: str, token: str) -> str | None:
    resp = requests.get(
        f"{_ACCOUNT}/account/api/public/account/displayName/{display_name}",
        headers=_bearer(token),
        timeout=10,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("id")


def is_friend(my_account_id: str, target_account_id: str, token: str) -> bool:
    """Check if target_account_id is in our friends list."""
    resp = requests.get(
        f"{_FRIENDS}/friends/api/v1/{my_account_id}/summary",
        headers=_bearer(token),
        timeout=10,
    )
    resp.raise_for_status()
    friends = resp.json().get("friends", [])
    return any(f.get("accountId") == target_account_id for f in friends)


def get_presence(my_account_id: str, target_account_id: str, token: str) -> dict | None:
    """
    Fetch real-time presence for a specific friend.
    Tries three endpoints in order, logging every response to help debug.
    Returns the raw presence object, or None if unavailable.
    """

    # ── Attempt 1: bulk friends presence list ────────────────────────────────
    # Returns a list of presence objects for all online/recently-online friends.
    bulk_url = f"{_PRESENCE}/presence/api/v1/{my_account_id}/friends"
    bulk = requests.get(bulk_url, headers=_bearer(token), timeout=10)
    logging.info("Epic bulk presence: HTTP %s", bulk.status_code)
    if bulk.ok:
        try:
            entries = bulk.json()
            logging.info("Epic bulk presence entries: %d  raw: %s",
                         len(entries) if isinstance(entries, list) else -1,
                         json.dumps(entries)[:600])
            if isinstance(entries, list):
                match = next(
                    (e for e in entries if e.get("accountId") == target_account_id),
                    None,
                )
                if match:
                    logging.info("Epic: found target in bulk presence")
                    return match
                else:
                    logging.info("Epic: target not in bulk presence list "
                                 "(likely offline or invisible)")
                    # Return a synthetic offline object so the caller knows
                    # the request succeeded but she's not showing as online
                    return {"accountId": target_account_id, "bIsOnline": False,
                            "bIsPlaying": False, "status": ""}
        except Exception as e:
            logging.warning("Epic: bulk presence parse error: %s", e)
    else:
        logging.warning("Epic bulk presence failed: %s %s",
                        bulk.status_code, bulk.text[:300])

    # ── Attempt 2: per-friend last-online endpoint ───────────────────────────
    last_url = f"{_PRESENCE}/presence/api/v1/{my_account_id}/last-online"
    last = requests.get(last_url, headers=_bearer(token), timeout=10)
    logging.info("Epic last-online: HTTP %s  body: %s",
                 last.status_code, last.text[:300])
    if last.ok:
        try:
            data = last.json()
            if isinstance(data, list):
                match = next(
                    (e for e in data if e.get("accountId") == target_account_id),
                    None,
                )
                if match:
                    return {"accountId": target_account_id,
                            "bIsOnline": False, "bIsPlaying": False, "status": ""}
        except Exception:
            pass

    # ── Attempt 3: direct presence for my account (sometimes includes friends) ─
    own_url = f"{_PRESENCE}/presence/api/v1/{my_account_id}"
    own = requests.get(own_url, headers=_bearer(token), timeout=10)
    logging.info("Epic own presence: HTTP %s  body: %s",
                 own.status_code, own.text[:300])

    logging.warning("Epic: all presence endpoints exhausted — returning offline")
    return {"accountId": target_account_id,
            "bIsOnline": False, "bIsPlaying": False, "status": ""}


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_fortnite_status(presence: dict) -> dict:
    status_text = presence.get("status", "")
    is_playing  = presence.get("bIsPlaying", False)

    result = {
        "online":      presence.get("bIsOnline", False),
        "playing":     is_playing,
        "joinable":    presence.get("bIsJoinable", False),
        "status_text": status_text,
        "game_mode":   None,
        "party_size":  None,
        "party_max":   None,
    }

    # e.g. "Battle Royale Lobby - 2 of 4"
    if is_playing and " - " in status_text:
        mode_part, party_part = status_text.rsplit(" - ", 1)
        result["game_mode"] = mode_part.strip()
        if " of " in party_part:
            parts = party_part.split(" of ")
            try:
                result["party_size"] = int(parts[0].strip())
                result["party_max"]  = int(parts[1].strip())
            except ValueError:
                pass

    props = presence.get("properties", {})
    if not result["game_mode"] and props.get("GameMode"):
        result["game_mode"] = props["GameMode"]

    return result


# ── Main entry point ──────────────────────────────────────────────────────────

def check_epic_activity(target_display_name: str) -> dict:
    result = {
        "username":    target_display_name,
        "timestamp":   _now_et(),
        "online":      False,
        "playing":     False,
        "game_mode":   None,
        "party_size":  None,
        "party_max":   None,
        "status_text": None,
        "error":       None,
    }

    try:
        token, my_account_id = get_access_token()

        target_id = find_account_id(target_display_name, token)
        if not target_id:
            result["error"] = f"Epic account '{target_display_name}' not found"
            return result

        # Verify friendship first (presence endpoint requires it)
        if not is_friend(my_account_id, target_id, token):
            result["error"] = (
                f"'{target_display_name}' is not in your Epic friends list "
                "or has presence hidden"
            )
            return result

        # Fetch real-time presence (always returns a dict now — worst case offline)
        presence = get_presence(my_account_id, target_id, token)
        parsed = parse_fortnite_status(presence)
        result.update(parsed)
        logging.info("Epic: %s online=%s playing=%s mode=%s",
                     target_display_name,
                     result["online"], result["playing"], result["game_mode"])

    except requests.HTTPError as e:
        result["error"] = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        logging.error("Epic HTTP error: %s", result["error"])
    except Exception as e:
        result["error"] = str(e)
        logging.error("Epic error: %s", e)

    return result
