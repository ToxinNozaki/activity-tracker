import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

_EASTERN = ZoneInfo("America/New_York")

def _now_et() -> str:
    return datetime.now(_EASTERN).strftime("%m/%d/%Y %I:%M %p %Z")

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

ROBLOX_CHANNEL_ID   = "1510146907060699156"
FORTNITE_CHANNEL_ID = "1510146847530811473"
STATUS_CHANNEL_ID   = "1510146836491665579"
ERROR_CHANNEL_ID    = "1510142665453207715"
PING_USER_ID        = "1079478384901505045"

_API = "https://discord.com/api/v10"


def _post(channel_id: str, payload: dict):
    if not BOT_TOKEN:
        print("WARNING: DISCORD_BOT_TOKEN not set")
        return
    requests.post(
        f"{_API}/channels/{channel_id}/messages",
        headers={"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"},
        json=payload,
        timeout=10,
    )


def _roblox_color(status: str) -> int:
    return {"In Game": 0x00B04F, "Online (Website)": 0x5865F2,
            "In Studio": 0xFFA500, "Offline": 0x747F8D}.get(status, 0x747F8D)


def notify_roblox(data: dict, prev: dict | None = None):
    status   = data.get("status", "Unknown")
    username = data.get("username", "?")
    ts       = data.get("timestamp", "")

    fields = [{"name": "Status", "value": status, "inline": True}]

    if data.get("game"):
        fields.append({"name": "Playing", "value": data["game"], "inline": True})
    if data.get("game_url"):
        fields.append({"name": "Game Link", "value": data["game_url"], "inline": False})

    if data.get("server_player_count") is not None:
        fields.append({"name": "Players in Her Server",
                       "value": str(data["server_player_count"]), "inline": True})

    if data.get("error"):
        fields.append({"name": "Warning", "value": data["error"], "inline": False})

    friends = data.get("friends_presence", [])
    in_game = [f for f in friends if f.get("status") == "In Game"]
    online  = [f for f in friends if f.get("status") in ("Online (Website)",)]

    def _friend_link(f: dict) -> str:
        uid = f.get("user_id")
        name = f.get("name", "?")
        if uid:
            return f"[{name}](https://www.roblox.com/users/{uid}/profile)"
        return f"**{name}**"

    # Show counts in main embed so the header is visible even if friends scroll offscreen
    if in_game:
        fields.append({"name": f"Friends In a Game ({len(in_game)})",
                       "value": " ".join(_friend_link(f) for f in in_game[:20]) or "—",
                       "inline": False})
    if online:
        fields.append({"name": f"Friends Online ({len(online)})",
                       "value": " ".join(_friend_link(f) for f in online[:10]) or "—",
                       "inline": False})

    main_embed = {
        "title": f"Roblox — {username}",
        "color": _roblox_color(status),
        "fields": fields,
        "footer": {"text": f"Logged at {ts}"},
    }
    if data.get("avatar_url"):
        main_embed["thumbnail"] = {"url": data["avatar_url"]}
    elif data.get("user_id"):
        main_embed["thumbnail"] = {
            "url": f"https://thumbnails.roblox.com/v1/users/avatar-headshot"
                   f"?userIds={data['user_id']}&size=150x150&format=Png"
        }

    embeds = [main_embed]

    # One mini-embed per friend (up to 9, Discord max is 10 total)
    shown = (in_game[:5] + online[:4])[:9]
    for f in shown:
        profile_url = f"https://www.roblox.com/users/{f['user_id']}/profile" if f.get("user_id") else None
        friend_embed = {
            "color": 0x00B04F if f.get("status") == "In Game" else 0x5865F2,
            "author": {
                "name": f"{f['name']} — {f.get('game') or f.get('status', 'Online')}",
                **({"url": profile_url} if profile_url else {}),
                **({"icon_url": f["avatar_url"]} if f.get("avatar_url") else {}),
            },
        }
        embeds.append(friend_embed)

    _post(ROBLOX_CHANNEL_ID, {"embeds": embeds})


def notify_epic(data: dict, prev: dict | None = None):
    username = data.get("username", "?")
    online   = data.get("online", False)
    playing  = data.get("playing", False)
    ts       = data.get("timestamp", "")

    color = 0x00B04F if (online and playing) else (0x5865F2 if online else 0x747F8D)
    label = "In Game" if (online and playing) else ("Online" if online else "Offline")

    fields = [{"name": "Status", "value": label, "inline": True}]
    if data.get("game_mode"):
        fields.append({"name": "Mode", "value": data["game_mode"], "inline": True})
    if data.get("party_size") is not None:
        fields.append({"name": "Party",
                       "value": f"{data['party_size']} / {data.get('party_max') or '?'}",
                       "inline": True})
    if data.get("status_text"):
        fields.append({"name": "Full Status", "value": data["status_text"], "inline": False})
    if data.get("error"):
        fields.append({"name": "Warning", "value": data["error"], "inline": False})

    _post(FORTNITE_CHANNEL_ID, {"embeds": [{
        "title": f"Fortnite — {username}",
        "color": color,
        "fields": fields,
        "footer": {"text": f"Logged at {ts}"},
    }]})


def _friend_embed(f: dict, color: int) -> dict:
    """Builds a mini-embed for one friend with their avatar icon and clickable profile link."""
    uid = f.get("user_id")
    name = f.get("name", "Unknown")
    profile_url = f"https://www.roblox.com/users/{uid}/profile" if uid else None
    embed = {"color": color}
    author: dict = {"name": name}
    if profile_url:
        author["url"] = profile_url
    if f.get("avatar_url"):
        author["icon_url"] = f["avatar_url"]
    embed["author"] = author
    if f.get("avatar_url"):
        embed["thumbnail"] = {"url": f["avatar_url"]}
    return embed


def notify_new_friends(new_friends: list[dict]):
    if not new_friends:
        return
    label = "New Friend Added" if len(new_friends) == 1 else f"{len(new_friends)} New Friends Added"
    names = ", ".join(
        f"[{f.get('name', '?')}](https://www.roblox.com/users/{f['user_id']}/profile)"
        if f.get("user_id") else f.get("name", "?")
        for f in new_friends
    )
    header = {
        "title": label,
        "description": names,
        "color": 0x00B04F,
        "footer": {"text": _now_et()},
    }
    embeds = [header] + [_friend_embed(f, 0x00B04F) for f in new_friends[:9]]
    _post(ROBLOX_CHANNEL_ID, {"embeds": embeds})


def notify_unfriended(removed_friends: list[dict]):
    if not removed_friends:
        return
    label = "Friend Removed" if len(removed_friends) == 1 else f"{len(removed_friends)} Friends Removed"
    names = ", ".join(
        f"[{f.get('name', '?')}](https://www.roblox.com/users/{f['user_id']}/profile)"
        if f.get("user_id") else f.get("name", "?")
        for f in removed_friends
    )
    header = {
        "title": label,
        "description": names,
        "color": 0xFF4444,
        "footer": {"text": _now_et()},
    }
    embeds = [header] + [_friend_embed(f, 0xFF4444) for f in removed_friends[:9]]
    _post(ROBLOX_CHANNEL_ID, {"embeds": embeds})


def notify_cookie_expired():
    _post(ERROR_CHANNEL_ID, {
        "content": f"<@{PING_USER_ID}>",
        "embeds": [{
            "title": "Roblox Cookie Expired — Action Required",
            "description": (
                "The Roblox cookie has expired. Tracking has stopped until it's updated.\n\n"
                "**Fix:**\n"
                "1. Go to roblox.com, log in\n"
                "2. Press `F12` → Application tab → Cookies → copy `.ROBLOSECURITY`\n"
                "3. Go to [GitHub Secrets](https://github.com/ToxinNozaki/activity-tracker/settings/secrets/actions)"
                " and update `ROBLOX_COOKIE`"
            ),
            "color": 0xFF0000,
            "footer": {"text": _now_et()},
        }],
    })


def notify_error(source: str, message: str, details: str = ""):
    embed = {
        "title": f"Error — {source}",
        "description": message,
        "color": 0xFF0000,
        "footer": {"text": _now_et()},
    }
    if details:
        embed["fields"] = [{"name": "Details", "value": details[:1000], "inline": False}]
    _post(ERROR_CHANNEL_ID, {"embeds": [embed]})


def notify_status(roblox_ok: bool, epic_ok: bool,
                  roblox_msg: str = "", epic_msg: str = ""):
    r = "✅ Connected" if roblox_ok else f"❌ {roblox_msg}"
    e = "✅ Connected" if epic_ok   else f"❌ {epic_msg}"
    color = 0x00B04F if (roblox_ok and epic_ok) else (0xFF0000 if not roblox_ok and not epic_ok else 0xFFA500)
    _post(STATUS_CHANNEL_ID, {"embeds": [{
        "title": "15-Minute Status Check",
        "description": f"**Roblox** — {r}\n**Fortnite** — {e}",
        "color": color,
        "footer": {"text": _now_et()},
    }]})


def notify_startup():
    _post(STATUS_CHANNEL_ID, {"embeds": [{
        "title": "Activity Tracker Online",
        "description": (
            "Now tracking **Moonstar_dovetail** on Roblox "
            "and **ReesieLuvsChan** on Fortnite.\n"
            "Activity updates on change · Status check every 15 min"
        ),
        "color": 0x5865F2,
        "footer": {"text": _now_et()},
    }]})
