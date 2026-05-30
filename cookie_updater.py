"""
Cookie updater — checks for /setcookie DMs from the authorized user.

Usage: DM the bot with:   /setcookie <your .ROBLOSECURITY cookie>

The next tracker run (within 5 min) will:
  1. Delete the DM message immediately
  2. Encrypt and push the new value to the ROBLOX_COOKIE GitHub secret
  3. Reply confirming success or failure
"""

import os
import re
import logging
import requests
from base64 import b64decode, b64encode
from datetime import datetime, timezone, timedelta

BOT_TOKEN          = os.environ.get("DISCORD_BOT_TOKEN", "")
GITHUB_PAT         = os.environ.get("GITHUB_PAT", "")
AUTHORIZED_USER_ID = "1079478384901505045"
REPO               = "ToxinNozaki/activity-tracker"
LOOKBACK_MINUTES   = 10   # only act on commands from the last 10 minutes

_DISCORD = "https://discord.com/api/v10"
_GITHUB  = "https://api.github.com"


# ── Discord helpers ──────────────────────────────────────────────────────────

def _d(method: str, path: str, **kwargs):
    return requests.request(
        method, f"{_DISCORD}{path}",
        headers={"Authorization": f"Bot {BOT_TOKEN}"},
        timeout=10, **kwargs,
    )


def _get_or_create_dm() -> str | None:
    """Return the DM channel ID between the bot and the authorized user."""
    r = _d("POST", "/users/@me/channels", json={"recipient_id": AUTHORIZED_USER_ID})
    return r.json().get("id") if r.ok else None


# ── GitHub secret update ─────────────────────────────────────────────────────

def _encrypt(public_key_b64: str, value: str) -> str:
    from nacl import encoding, public as nacl_public
    pk  = nacl_public.PublicKey(b64decode(public_key_b64), encoding.RawEncoder)
    box = nacl_public.SealedBox(pk)
    return b64encode(box.encrypt(value.encode())).decode()


def _update_github_secret(name: str, value: str) -> bool:
    if not GITHUB_PAT:
        logging.warning("GITHUB_PAT not set — cannot update secret")
        return False
    headers = {
        "Authorization": f"token {GITHUB_PAT}",
        "Accept": "application/vnd.github.v3+json",
    }
    # Get repo public key for encryption
    r = requests.get(f"{_GITHUB}/repos/{REPO}/actions/secrets/public-key",
                     headers=headers, timeout=10)
    if not r.ok:
        logging.error("GitHub public key fetch failed: %s", r.text)
        return False
    key_data   = r.json()
    encrypted  = _encrypt(key_data["key"], value)
    r2 = requests.put(
        f"{_GITHUB}/repos/{REPO}/actions/secrets/{name}",
        headers=headers,
        json={"encrypted_value": encrypted, "key_id": key_data["key_id"]},
        timeout=10,
    )
    return r2.status_code in (201, 204)


# ── Main check ───────────────────────────────────────────────────────────────

def check_for_cookie_update() -> bool:
    """
    Polls the bot's DM with the authorized user for a /setcookie command.
    Deletes the message immediately and updates the GitHub secret if found.
    Returns True if a cookie was updated.
    """
    if not BOT_TOKEN:
        return False

    dm_channel = _get_or_create_dm()
    if not dm_channel:
        logging.warning("cookie_updater: could not get DM channel")
        return False

    r = _d("GET", f"/channels/{dm_channel}/messages", params={"limit": 20})
    if not r.ok:
        return False

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)

    for msg in r.json():
        # Must be from the authorized user
        if str(msg.get("author", {}).get("id")) != AUTHORIZED_USER_ID:
            continue

        # Must be recent
        try:
            msg_time = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
            if msg_time < cutoff:
                continue
        except Exception:
            continue

        content = msg.get("content", "").strip()
        match   = re.match(r'^[/!]setcookie\s+(\S+)', content, re.IGNORECASE)
        if not match:
            continue

        cookie = match.group(1).strip()

        # Delete the message right away — cookie should not stay in Discord
        _d("DELETE", f"/channels/{dm_channel}/messages/{msg['id']}")
        logging.info("cookie_updater: /setcookie received, message deleted")

        # Push to GitHub Secrets
        ok = _update_github_secret("ROBLOX_COOKIE", cookie)

        reply = (
            "✅ **Cookie updated!** It will take effect on the next tracker run."
            if ok else
            "❌ **Failed to update cookie.** Make sure the `GITHUB_PAT` secret is set "
            "with `repo` / secrets-write scope."
        )
        _d("POST", f"/channels/{dm_channel}/messages", json={"content": reply})
        logging.info("cookie_updater: secret update %s", "OK" if ok else "FAILED")
        return ok

    return False
