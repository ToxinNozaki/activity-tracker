import os
import requests

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")

_API = "https://discord.com/api/v10"


def _post_message(payload: dict):
    if not BOT_TOKEN or not CHANNEL_ID:
        return
    requests.post(
        f"{_API}/channels/{CHANNEL_ID}/messages",
        headers={
            "Authorization": f"Bot {BOT_TOKEN}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=10,
    )


def _roblox_color(status: str) -> int:
    return {
        "In Game": 0x00B04F,
        "Online (Website)": 0x5865F2,
        "In Studio": 0xFFA500,
        "Offline": 0x747F8D,
    }.get(status, 0x747F8D)


def notify_roblox(data: dict, prev: dict | None = None):
    status = data.get("status", "Unknown")
    username = data.get("username", "?")
    ts = data.get("timestamp", "")

    if prev and prev.get("status") == status and prev.get("game") == data.get("game"):
        return

    fields = [{"name": "Status", "value": status, "inline": True}]

    if data.get("game"):
        fields.append({"name": "Playing", "value": data["game"], "inline": True})
    if data.get("game_url"):
        fields.append({"name": "Game Link", "value": data["game_url"], "inline": False})
    if data.get("last_location") and not data.get("game"):
        fields.append({"name": "Location", "value": data["last_location"], "inline": True})

    online_friends = data.get("online_friends", [])
    if online_friends:
        names = ", ".join(f["name"] for f in online_friends[:15])
        fields.append({
            "name": f"Her Online Friends ({len(online_friends)})",
            "value": names,
            "inline": False,
        })

    if data.get("error"):
        fields.append({"name": "Error", "value": data["error"], "inline": False})

    embed = {
        "title": f"Roblox — {username}",
        "color": _roblox_color(status),
        "fields": fields,
        "footer": {"text": f"Checked at {ts}"},
    }
    if data.get("user_id"):
        embed["thumbnail"] = {
            "url": f"https://www.roblox.com/headshot-thumbnail/image?userId={data['user_id']}&width=150&height=150&format=png"
        }

    _post_message({"embeds": [embed]})


def notify_epic(data: dict, prev: dict | None = None):
    username = data.get("username", "?")
    online = data.get("online", False)
    playing = data.get("playing", False)
    ts = data.get("timestamp", "")

    if prev and (
        prev.get("online") == online
        and prev.get("playing") == playing
        and prev.get("game_mode") == data.get("game_mode")
        and prev.get("party_size") == data.get("party_size")
    ):
        return

    if online and playing:
        color, status_label = 0x00B04F, "In Game"
    elif online:
        color, status_label = 0x5865F2, "Online"
    else:
        color, status_label = 0x747F8D, "Offline"

    fields = [{"name": "Status", "value": status_label, "inline": True}]

    if data.get("game_mode"):
        fields.append({"name": "Mode", "value": data["game_mode"], "inline": True})
    if data.get("party_size") is not None:
        party = f"{data['party_size']} / {data.get('party_max') or '?'}"
        fields.append({"name": "Party Size", "value": party, "inline": True})
    if data.get("status_text"):
        fields.append({"name": "Full Status", "value": data["status_text"], "inline": False})
    if data.get("error"):
        fields.append({"name": "Error", "value": data["error"], "inline": False})

    embed = {
        "title": f"Fortnite — {username}",
        "color": color,
        "fields": fields,
        "footer": {"text": f"Checked at {ts}"},
    }

    _post_message({"embeds": [embed]})


def notify_startup():
    _post_message({
        "embeds": [{
            "title": "Activity Tracker started",
            "description": "Tracking **Moonstar_dovetail** (Roblox) and **ReesieLuvsChan** (Fortnite).\nChecking every 5 minutes — I'll only post when something changes.",
            "color": 0x5865F2,
        }]
    })
