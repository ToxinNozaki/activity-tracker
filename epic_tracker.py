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
        if resp.status_code == 400:
            # 400 always means the refresh token is invalid or expired
            # (Epic returns error codes like "invalid_grant", "invalid_token", etc.)
            raise RuntimeError(
                f"Epic refresh token is invalid or expired (HTTP 400). "
                f"Re-run setup_epic.py to generate a fresh token. Raw: {body}"
            )
        if resp.status_code == 401:
            raise RuntimeError(
                f"Epic client credentials rejected (HTTP 401). "
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

    # Persist the new refresh token immediately (guard against missing key)
    new_refresh = session.get("refresh_token")
    if new_refresh:
        auth["refresh_token"] = new_refresh
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


def _extract_entries(raw) -> list:
    """Normalise an API response into a flat list of presence/friend objects."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("friends", "presences", "statuses", "data", "items"):
            if isinstance(raw.get(key), list):
                return raw[key]
    return []


def get_presence(my_account_id: str, target_account_id: str, token: str) -> dict:
    """
    Fetch real-time presence for a specific friend.
    Tries multiple endpoints in order, logging every full response to Actions
    logs so failures are debuggable.  Always returns a dict.
    """
    _offline = {"accountId": target_account_id,
                "bIsOnline": False, "bIsPlaying": False, "status": ""}

    # ── Attempt 1: bulk friends presence ─────────────────────────────────────
    bulk_url = f"{_PRESENCE}/presence/api/v1/{my_account_id}/friends"
    bulk = requests.get(bulk_url, headers=_bearer(token), timeout=10)
    logging.info("Epic [1] bulk presence: HTTP %s  body: %s",
                 bulk.status_code, bulk.text[:2000])
    if bulk.ok:
        entries = _extract_entries(bulk.json())
        logging.info("Epic [1] entries found: %d  target: %s",
                     len(entries), target_account_id)
        match = next((e for e in entries
                      if e.get("accountId") == target_account_id), None)
        if match:
            logging.info("Epic [1] found target — online=%s playing=%s status=%s",
                         match.get("bIsOnline"), match.get("bIsPlaying"),
                         match.get("status", "")[:100])
            return match
        # Endpoint worked — target just isn't in the list (offline/invisible)
        logging.info("Epic [1] target not in list — treating as offline")
        return _offline

    # ── Attempt 2: friends list (non-summary) — may embed presence ───────────
    friends_url = f"{_FRIENDS}/friends/api/v1/{my_account_id}/friends"
    fr = requests.get(friends_url, headers=_bearer(token), timeout=10)
    logging.info("Epic [2] friends list: HTTP %s  body: %s",
                 fr.status_code, fr.text[:2000])
    if fr.ok:
        entries = _extract_entries(fr.json())
        match = next((e for e in entries
                      if e.get("accountId") == target_account_id), None)
        if match:
            # Check for embedded presence object
            presence = match.get("presence") or match.get("presenceRecord")
            if presence and isinstance(presence, dict):
                logging.info("Epic [2] embedded presence: %s", json.dumps(presence)[:500])
                return presence
            # Friend found but no presence — check top-level fields
            if "bIsOnline" in match or "status" in match:
                logging.info("Epic [2] top-level presence fields found")
                return match

    # ── Attempt 3: presence for specific friend (alternate URL format) ────────
    per_url = f"{_PRESENCE}/presence/api/v1/{target_account_id}"
    per = requests.get(per_url, headers=_bearer(token), timeout=10)
    logging.info("Epic [3] per-user presence: HTTP %s  body: %s",
                 per.status_code, per.text[:2000])
    if per.ok:
        data = per.json()
        if isinstance(data, dict) and ("bIsOnline" in data or "status" in data):
            return data

    # ── Attempt 4: last-online endpoint ──────────────────────────────────────
    last_url = f"{_PRESENCE}/presence/api/v1/{my_account_id}/last-online"
    last = requests.get(last_url, headers=_bearer(token), timeout=10)
    logging.info("Epic [4] last-online: HTTP %s  body: %s",
                 last.status_code, last.text[:2000])

    logging.warning("Epic: all presence endpoints exhausted — returning offline")
    return _offline


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
