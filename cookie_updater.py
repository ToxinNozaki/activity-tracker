"""
Cookie updater — checks for cookie updates via DM from the authorized user.

How to update your cookie:
  1. DM the bot your raw cookie value (just paste it — starts with _|WARNING)
     OR send:  cookie <value>
  2. The bot deletes the message instantly and updates the GitHub secret
  3. A new tracker run fires immediately so it comes back online right away
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
LOOKBACK_MINUTES   = 10

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
    r = _d("POST", "/users/@me/channels", json={"recipient_id": AUTHORIZED_USER_ID})
    return r.json().get("id") if r.ok else None


# ── GitHub helpers ───────────────────────────────────────────────────────────

def _gh_headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_PAT}",
        "Accept": "application/vnd.github.v3+json",
    }


def _encrypt(public_key_b64: str, value: str) -> str:
    from nacl import encoding, public as nacl_public
    pk  = nacl_public.PublicKey(b64decode(public_key_b64), encoding.RawEncoder)
    box = nacl_public.SealedBox(pk)
    return b64encode(box.encrypt(value.encode())).decode()


def _update_github_secret(name: str, value: str) -> bool:
    if not GITHUB_PAT:
        logging.warning("cookie_updater: GITHUB_PAT not set")
        return False
    r = requests.get(f"{_GITHUB}/repos/{REPO}/actions/secrets/public-key",
                     headers=_gh_headers(), timeout=10)
    if not r.ok:
        logging.error("cookie_updater: public key fetch failed: %s", r.text)
        return False
    key_data  = r.json()
    encrypted = _encrypt(key_data["key"], value)
    r2 = requests.put(
        f"{_GITHUB}/repos/{REPO}/actions/secrets/{name}",
        headers=_gh_headers(),
        json={"encrypted_value": encrypted, "key_id": key_data["key_id"]},
        timeout=10,
    )
    return r2.status_code in (201, 204)


def _trigger_new_run():
    """Fire an immediate tracker run so the new cookie takes effect now."""
    if not GITHUB_PAT:
        return
    try:
        requests.post(
            f"{_GITHUB}/repos/{REPO}/dispatches",
            headers=_gh_headers(),
            json={"event_type": "run-tracker"},
            timeout=10,
        )
        logging.info("cookie_updater: triggered immediate new run")
    except Exception as e:
        logging.warning("cookie_updater: could not trigger new run: %s", e)


# ── Cookie detection ─────────────────────────────────────────────────────────

def _extract_cookie(content: str) -> str | None:
    """
    Accept cookies in any of these formats:
      - Raw paste:  _|WARNING:-...-|_CAEA...
      - Prefixed:   cookie <value>
      - Old style:  !setcookie <value>  or  /setcookie <value>
    """
    # Raw cookie (Roblox cookies always start with _|WARNING)
    if content.startswith("_|WARNING"):
        return content

    # Explicit prefixes
    m = re.match(r'^(?:cookie|[/!]setcookie)\s+(\S+)', content, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return None


# ── Main check ───────────────────────────────────────────────────────────────

def check_for_cookie_update() -> bool:
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
        if str(msg.get("author", {}).get("id")) != AUTHORIZED_USER_ID:
            continue

        try:
            msg_time = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
            if msg_time < cutoff:
                continue
        except Exception:
            continue

        cookie = _extract_cookie(msg.get("content", "").strip())
        if not cookie:
            continue

        # Delete immediately — cookie must not stay in Discord
        _d("DELETE", f"/channels/{dm_channel}/messages/{msg['id']}")
        logging.info("cookie_updater: cookie message detected and deleted")

        ok = _update_github_secret("ROBLOX_COOKIE", cookie)

        if ok:
            _trigger_new_run()
            reply = "✅ **Cookie updated!** Firing a new tracker run right now — it'll be back online in seconds."
        else:
            reply = (
                "❌ **Failed to update cookie.**\n"
                "Make sure the `GITHUB_PAT` secret has `repo` scope "
                "(Settings → Secrets → GITHUB_PAT)."
            )

        _d("POST", f"/channels/{dm_channel}/messages", json={"content": reply})
        logging.info("cookie_updater: secret update %s", "OK" if ok else "FAILED")
        return ok

    return False
