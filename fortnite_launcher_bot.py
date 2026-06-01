"""
Epic Launcher presence bot — from scratch, no fortnitepy.

Replicates exactly what the Epic Games Launcher social tab does:
  1. Authenticate as launcherAppClient2 (the launcher's own client)
  2. Open the real Epic XMPP WebSocket
  3. SASL-auth, bind a launcher resource, go available
  4. Receive friends' presence pushes and read the status line
     (e.g. "Battle Royale Zero Unranked Duo - 100 Left")
  5. Report ReesieLuvsChan's status to Discord

Posts every 5 minutes + instant alerts on change, with session timer
and last-seen — same feature set as the Roblox logger.

Runs on GitHub Actions; exits at 5h45m and self-restarts.
"""

import asyncio
import base64
import json
import logging
import os
import re
import time
import uuid
import requests
import websockets
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Config ────────────────────────────────────────────────────────────────────

TARGET           = "ReesieLuvsChan"
FORTNITE_CHANNEL = "1510146847530811473"
STATUS_CHANNEL   = "1510146836491665579"
ERROR_CHANNEL    = "1510142665453207715"
PING_USER_ID     = "1079478384901505045"
REPO             = "ToxinNozaki/activity-tracker"

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
GH_TOKEN  = os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_PAT", "")
_EASTERN  = ZoneInfo("America/New_York")

# Epic OAuth clients
_ANDROID  = b"3f69e56c7649492c8cc29f1af08a8a12:b51ee9cb12234f50a69efa67ef53812e"
_LAUNCHER = b"34a02cf8f4414e29b15921876da36f9a:daafbccc737745039dffe53d94fc76cf"
_TOKEN_URL = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token"
_EXCH_URL  = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/exchange"
_ACCT_URL  = "https://account-public-service-prod.ol.epicgames.com"

# Epic XMPP
_XMPP_URL    = "wss://xmpp-service-prod.ol.epicgames.com"
_XMPP_DOMAIN = "prod.ol.epicgames.com"

MAX_RUNTIME_SECS = 5 * 3600 + 45 * 60   # exit before GitHub's 6h kill
STATUS_INTERVAL  = 5 * 60               # post every 5 min

# ── Logging ───────────────────────────────────────────────────────────────────

class _ETFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created, _EASTERN)
        return ct.strftime("%m/%d/%Y %I:%M:%S %p %Z")

_h = logging.StreamHandler()
_h.setFormatter(_ETFormatter("%(asctime)s %(levelname)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_h])

# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_et() -> str:
    return datetime.now(_EASTERN).strftime("%m/%d/%Y %I:%M %p %Z")

def _discord_ts(unix: float) -> str:
    return f"<t:{int(unix)}:R>"

def _fmt_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    if h and m:
        return f"{h}h {m}m"
    return f"{h}h" if h else (f"{m}m" if m else "< 1 min")

def _b64(creds: bytes) -> str:
    return base64.b64encode(creds).decode()

def _post(channel_id: str, payload: dict):
    if not BOT_TOKEN:
        return
    try:
        r = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {BOT_TOKEN}",
                     "Content-Type": "application/json"},
            json=payload, timeout=10,
        )
        if not r.ok:
            logging.warning("Discord %s -> %s %s", channel_id, r.status_code, r.text[:200])
    except Exception as e:
        logging.warning("Discord post error: %s", e)

def _trigger_restart():
    if not GH_TOKEN:
        return
    try:
        requests.post(
            f"https://api.github.com/repos/{REPO}/dispatches",
            headers={"Authorization": f"token {GH_TOKEN}",
                     "Accept": "application/vnd.github.v3+json"},
            json={"event_type": "run-fortnite-bot"}, timeout=10,
        )
    except Exception as e:
        logging.warning("Restart trigger failed: %s", e)

# ── Auth: become the launcher ────────────────────────────────────────────────

def get_launcher_session(device_auth: dict) -> tuple[str, str]:
    """
    device_auth grant (Android, the only client that allows it)
      -> exchange code
      -> redeem as launcherAppClient2
    Returns (launcher_access_token, our_account_id).
    """
    r = requests.post(
        _TOKEN_URL,
        headers={"Authorization": f"basic {_b64(_ANDROID)}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "device_auth",
              "account_id": device_auth["account_id"],
              "device_id":  device_auth["device_id"],
              "secret":     device_auth["secret"]},
        timeout=15,
    )
    r.raise_for_status()
    android_token = r.json()["access_token"]

    r = requests.get(_EXCH_URL, headers={"Authorization": f"bearer {android_token}"}, timeout=10)
    r.raise_for_status()
    code = r.json()["code"]

    r = requests.post(
        _TOKEN_URL,
        headers={"Authorization": f"basic {_b64(_LAUNCHER)}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "exchange_code", "exchange_code": code},
        timeout=15,
    )
    r.raise_for_status()
    s = r.json()
    logging.info("Launcher session established as %s (account %s)",
                 s.get("displayName"), s.get("account_id"))
    return s["access_token"], s["account_id"]

def lookup_account_id(display_name: str, token: str) -> str | None:
    r = requests.get(
        f"{_ACCT_URL}/account/api/public/account/displayName/{display_name}",
        headers={"Authorization": f"bearer {token}"}, timeout=10,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("id")

_FRIENDS_SVC = "https://friends-public-service-prod06.ol.epicgames.com"

def get_friend_ids(account_id: str, token: str) -> list[str]:
    """Friend account IDs from Epic's friends HTTP service (not XMPP roster)."""
    for url in (f"{_FRIENDS_SVC}/friends/api/public/friends/{account_id}",
                f"{_FRIENDS_SVC}/friends/api/v1/{account_id}/friends"):
        try:
            r = requests.get(url, headers={"Authorization": f"bearer {token}"}, timeout=10)
            if r.ok:
                data = r.json()
                if isinstance(data, list):
                    ids = [f.get("accountId") for f in data if f.get("accountId")]
                    if ids:
                        logging.info("Fetched %d friend IDs", len(ids))
                        return ids
        except Exception as e:
            logging.warning("get_friend_ids failed (%s): %s", url, e)
    return []

# ── Presence parsing ──────────────────────────────────────────────────────────

_PRESENCE_RE = re.compile(r"<presence\b[^>]*>.*?</presence>|<presence\b[^>]*/>",
                          re.DOTALL | re.IGNORECASE)
_FROM_RE   = re.compile(r'from="([^"]+)"', re.IGNORECASE)
_TYPE_RE   = re.compile(r'type="([^"]+)"', re.IGNORECASE)
_STATUS_RE = re.compile(r"<status\b[^>]*>(.*?)</status>", re.DOTALL | re.IGNORECASE)

def _parse_presence(stanza: str) -> dict | None:
    """Return {account_id, online, playing, status_text} from a <presence> stanza."""
    m_from = _FROM_RE.search(stanza)
    if not m_from:
        return None
    account_id = m_from.group(1).split("@")[0]

    m_type = _TYPE_RE.search(stanza)
    ptype = m_type.group(1).lower() if m_type else "available"
    online = ptype != "unavailable"

    status_text = ""
    playing = False
    m_status = _STATUS_RE.search(stanza)
    if m_status:
        raw = m_status.group(1).strip()
        # unescape minimal XML entities
        raw = (raw.replace("&quot;", '"').replace("&amp;", "&")
                  .replace("&lt;", "<").replace("&gt;", ">").replace("&apos;", "'"))
        try:
            data = json.loads(raw)
            status_text = data.get("Status", "") or ""
            playing = bool(data.get("bIsPlaying", False))
        except Exception:
            status_text = raw[:120]
    return {"account_id": account_id, "online": online,
            "playing": playing, "status_text": status_text}

# ── Embed ─────────────────────────────────────────────────────────────────────

def _build_embed(st: dict) -> dict:
    online = st["is_online"]
    playing = st["is_playing"]
    status_text = st["status_text"]

    if playing:
        color, label = 0x00B04F, "In Game"
    elif online:
        color, label = 0x5865F2, "Online"
    else:
        color, label = 0x747F8D, "Offline"

    fields = [{"name": "Status", "value": label, "inline": True}]
    # The launcher status line — exactly what the social tab shows
    if online and status_text:
        fields.append({"name": "Details", "value": status_text, "inline": True})
    if st.get("session_start") and online:
        fields.append({"name": "Session",
                       "value": _fmt_duration(time.time() - st["session_start"]),
                       "inline": True})
    if not online and st.get("last_online_ts"):
        fields.append({"name": "Last Seen",
                       "value": _discord_ts(st["last_online_ts"]), "inline": True})

    return {"title": f"Fortnite — {TARGET}", "color": color,
            "fields": fields, "footer": {"text": f"Logged at {_now_et()}"}}

# ── Main XMPP client ──────────────────────────────────────────────────────────

async def run_client(device_auth: dict):
    start = time.time()
    token, my_account_id = get_launcher_session(device_auth)
    target_id = lookup_account_id(TARGET, token) or ""
    logging.info("Target %s account_id=%s", TARGET, target_id)
    friend_ids = get_friend_ids(my_account_id, token)

    st = {
        "is_online": False, "is_playing": False, "status_text": "",
        "session_start": None, "last_online_ts": None,
        "stanzas": 0, "presence_friends": set(),
        "roster_items": -1, "probes_sent": 0, "friend_ids": len(friend_ids),
    }

    resource = f"V2:launcher:WIN::{uuid.uuid4().hex.upper()}"
    plain = base64.b64encode(
        b"\x00" + my_account_id.encode() + b"\x00" + token.encode()
    ).decode()

    async with websockets.connect(
        _XMPP_URL, subprotocols=["xmpp"], ping_interval=60, ping_timeout=30,
        max_size=2**22, open_timeout=20,
    ) as ws:

        async def send(x): await ws.send(x)
        async def wait_for(substr, fail_substr=None, timeout=20):
            end = time.time() + timeout
            while time.time() < end:
                msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                logging.info("XMPP <- %s", msg[:160])
                if fail_substr and fail_substr in msg:
                    raise RuntimeError(f"XMPP handshake failure: {msg[:200]}")
                if substr in msg:
                    return msg
            raise asyncio.TimeoutError(f"timed out waiting for {substr}")

        # ── Handshake ────────────────────────────────────────────────────────
        open_stanza = (f'<open xmlns="urn:ietf:params:xml:ns:xmpp-framing" '
                       f'to="{_XMPP_DOMAIN}" version="1.0"/>')
        await send(open_stanza)
        await wait_for("mechanisms")
        await send(f'<auth xmlns="urn:ietf:params:xml:ns:xmpp-sasl" '
                   f'mechanism="PLAIN">{plain}</auth>')
        await wait_for("<success", fail_substr="<failure")
        logging.info("XMPP SASL auth OK")

        await send(open_stanza)                      # restart stream
        await wait_for("bind")                        # features w/ bind
        await send(f'<iq xmlns="jabber:client" type="set" id="bind1">'
                   f'<bind xmlns="urn:ietf:params:xml:ns:xmpp-bind">'
                   f'<resource>{resource}</resource></bind></iq>')
        await wait_for("bind1")
        logging.info("XMPP bound as launcher resource")

        # Request the roster FIRST. Per XMPP spec a session becomes a
        # "presence-aware" resource only after retrieving its roster; Epic
        # appears to gate friend-presence delivery on this. We skipped it
        # before, which is why we received zero friend presence.
        await send('<iq xmlns="jabber:client" type="get" id="roster1">'
                   '<query xmlns="jabber:iq:roster"/></iq>')
        try:
            roster_msg = await wait_for("roster1", timeout=12)
            st["roster_items"] = roster_msg.count("<item ")
            logging.info("Roster retrieved: %d items", st["roster_items"])
        except Exception as e:
            logging.warning("Roster request not answered: %s", e)

        # Go available with a REAL launcher-style status payload (not empty).
        # Epic may only reciprocate presence to a properly-formed launcher.
        status_json = json.dumps({
            "Status": "", "bIsPlaying": False, "bIsJoinable": False,
            "bHasVoiceSupport": False, "SessionId": "", "Properties": {},
        }).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        await send(f'<presence xmlns="jabber:client"><status>{status_json}</status></presence>')
        logging.info("Sent available launcher presence")

        # Epic doesn't use the XMPP roster for friends, so the server has no
        # subscriptions to push against. Explicitly PROBE each friend — the
        # server replies with current presence for any who are online.
        for fid in friend_ids:
            try:
                await send(f'<presence xmlns="jabber:client" type="probe" '
                           f'to="{fid}@{_XMPP_DOMAIN}"/>')
                st["probes_sent"] += 1
            except Exception:
                break
        logging.info("Sent %d presence probes", st["probes_sent"])

        _post(STATUS_CHANNEL, {"embeds": [{
            "title": "🎮 Fortnite Launcher Bot Online",
            "description": (f"Authenticated as the Epic Launcher — reading "
                            f"**{TARGET}**'s presence exactly like your social tab.\n"
                            f"Status every 5 min · Instant alerts on change."),
            "color": 0x00B04F, "footer": {"text": _now_et()},
        }]})

        # ── Background: keepalive ────────────────────────────────────────────
        async def keepalive():
            while True:
                await asyncio.sleep(60)
                try:
                    await ws.send(" ")
                except Exception:
                    return

        # ── Background: 5-minute status + initial diagnostic ─────────────────
        async def status_loop():
            # Wait a bit for the initial presence burst, then post + diagnose
            await asyncio.sleep(20)
            _post(ERROR_CHANNEL, {"embeds": [{
                "title": "🔧 Launcher Bot Diagnostic",
                "description": (
                    f"**Stanzas received:** {st['stanzas']}\n"
                    f"**Friends with presence:** {len(st['presence_friends'])}\n"
                    f"**Friend IDs fetched:** {st['friend_ids']} · "
                    f"**probes sent:** {st['probes_sent']}\n"
                    f"**XMPP roster items:** {st['roster_items']}\n"
                    f"**{TARGET} online:** {st['is_online']} · playing: {st['is_playing']}\n"
                    f"**Status line:** {st['status_text'] or '(none)'}"
                ),
                "color": 0xFFA500, "footer": {"text": _now_et()},
            }]})
            _post(FORTNITE_CHANNEL, {"embeds": [_build_embed(st)]})
            while True:
                await asyncio.sleep(STATUS_INTERVAL)
                _post(FORTNITE_CHANNEL, {"embeds": [_build_embed(st)]})
                logging.info("Periodic: online=%s playing=%s status=%r",
                             st["is_online"], st["is_playing"], st["status_text"][:60])

        # ── Background: runtime limit → restart ──────────────────────────────
        async def restart_timer():
            await asyncio.sleep(MAX_RUNTIME_SECS - (time.time() - start))
            _post(STATUS_CHANNEL, {"embeds": [{
                "title": "🔄 Launcher Bot Restarting",
                "description": "Runtime limit reached — reconnecting in ~30s.",
                "color": 0xFFA500, "footer": {"text": _now_et()}}]})
            _trigger_restart()
            await ws.close()

        asyncio.create_task(keepalive())
        asyncio.create_task(status_loop())
        asyncio.create_task(restart_timer())

        # ── Main receive loop ────────────────────────────────────────────────
        async for message in ws:
            for stanza in _PRESENCE_RE.findall(message):
                st["stanzas"] += 1
                p = _parse_presence(stanza)
                if not p or not p["account_id"]:
                    continue
                if p["account_id"] != my_account_id:
                    first = len(st["presence_friends"]) == 0
                    st["presence_friends"].add(p["account_id"])
                    if first:
                        # First-ever friend presence proves the launcher
                        # approach receives presence. One-time confirmation.
                        _post(ERROR_CHANNEL, {"embeds": [{
                            "title": "✅ Presence Delivery CONFIRMED",
                            "description": (
                                "The launcher bot just received real friend "
                                "presence over XMPP — the approach works! It will "
                                f"now report **{TARGET}** automatically whenever "
                                "she's online."
                            ),
                            "color": 0x00B04F, "footer": {"text": _now_et()},
                        }]})
                        logging.info("First friend presence received — approach works")
                # Only the target drives the channel posts
                if target_id and p["account_id"] != target_id:
                    continue
                if not target_id and TARGET.lower() not in stanza.lower():
                    continue

                was_online = st["is_online"]
                st["is_online"]   = p["online"]
                st["is_playing"]  = p["playing"]
                st["status_text"] = p["status_text"]
                logging.info("TARGET presence: online=%s playing=%s status=%r",
                             p["online"], p["playing"], p["status_text"][:80])

                if p["online"] and not was_online:
                    st["session_start"] = time.time()
                    embed = _build_embed(st); embed["description"] = "🟢 Just came online!"
                    _post(FORTNITE_CHANNEL, {"embeds": [embed]})
                elif not p["online"] and was_online:
                    st["last_online_ts"] = time.time(); st["session_start"] = None
                    embed = _build_embed(st); embed["description"] = "⚫ Just went offline."
                    _post(FORTNITE_CHANNEL, {"embeds": [embed]})
                elif p["online"]:
                    # status change while online (mode/party switch)
                    _post(FORTNITE_CHANNEL, {"embeds": [_build_embed(st)]})

# ── Entry point ───────────────────────────────────────────────────────────────

def _load_device_auth() -> dict:
    raw = os.environ.get("EPIC_DEVICE_AUTH", "")
    if not raw:
        f = Path("epic_device_auth.json")
        if f.exists():
            raw = f.read_text()
    if not raw:
        logging.error("EPIC_DEVICE_AUTH not set")
        raise SystemExit(1)
    return json.loads(raw)

def main():
    device_auth = _load_device_auth()
    try:
        asyncio.run(run_client(device_auth))
    except Exception as e:
        logging.error("Launcher bot crashed: %s", e, exc_info=True)
        _post(ERROR_CHANNEL, {
            "content": f"<@{PING_USER_ID}>",
            "embeds": [{"title": "❌ Launcher Bot Crashed",
                        "description": f"`{str(e)[:500]}`\nRestarting in 60s…",
                        "color": 0xFF0000, "footer": {"text": _now_et()}}]})
        time.sleep(60)
        _trigger_restart()

if __name__ == "__main__":
    main()
