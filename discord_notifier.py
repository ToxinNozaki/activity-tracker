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
    try:
        r = requests.post(
            f"{_API}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        if not r.ok:
            import logging
            logging.warning("Discord API error %s posting to %s: %s",
                            r.status_code, channel_id, r.text[:200])
    except Exception as e:
        import logging
        logging.warning("Discord post failed: %s", e)


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

    if data.get("session_minutes") is not None and data.get("status") == "In Game":
        m = data["session_minutes"]
        h, rem = divmod(m, 60)
        dur = f"{h}h {rem}m" if (h and rem) else (f"{h}h" if h else (f"{m}m" if m else "< 1 min"))
        fields.append({"name": "Session", "value": dur, "inline": True})

    if data.get("error"):
        fields.append({"name": "Warning", "value": data["error"], "inline": False})

    friends   = data.get("friends_presence", [])
    her_game_id = data.get("game_id")  # None if she's not in a game

    # Split friends into three buckets
    same_server = [
        f for f in friends
        if f.get("status") == "In Game"
        and her_game_id
        and f.get("game_id") == her_game_id
    ]
    in_game = [
        f for f in friends
        if f.get("status") == "In Game" and f not in same_server
    ]
    online  = [f for f in friends if f.get("status") == "Online (Website)"]

    def _friend_link(f: dict) -> str:
        uid = f.get("user_id")
        name = f.get("name", "?")
        if uid:
            return f"[{name}](https://www.roblox.com/users/{uid}/profile)"
        return f"**{name}**"

    if same_server:
        fields.append({"name": f"👥 In Her Server ({len(same_server)})",
                       "value": " ".join(_friend_link(f) for f in same_server[:20]) or "—",
                       "inline": False})
    if in_game:
        fields.append({"name": f"🎮 Friends In a Game ({len(in_game)})",
                       "value": " ".join(_friend_link(f) for f in in_game[:20]) or "—",
                       "inline": False})
    if online:
        fields.append({"name": f"🌐 Friends Online ({len(online)})",
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
    # Priority: same-server first, then in-game elsewhere, then online
    shown = (same_server[:3] + in_game[:3] + online[:3])[:9]
    for f in shown:
        profile_url = f"https://www.roblox.com/users/{f['user_id']}/profile" if f.get("user_id") else None
        if f in same_server:
            color = 0xFFD700  # gold — in her server
            label = f"{f['name']} — 👥 Same Server"
        elif f.get("status") == "In Game":
            color = 0x00B04F  # green — in game elsewhere
            label = f"{f['name']} — {f.get('game') or 'In Game'}"
        else:
            color = 0x5865F2  # blurple — online
            label = f"{f['name']} — Online"
        friend_embed = {
            "color": color,
            "author": {
                "name": label,
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

    if data.get("session_minutes") is not None and online:
        m = data["session_minutes"]
        h, rem = divmod(m, 60)
        dur = f"{h}h {rem}m" if (h and rem) else (f"{h}h" if h else (f"{m}m" if m else "< 1 min"))
        fields.append({"name": "Session", "value": dur, "inline": True})

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
            "title": "🍪 Roblox Cookie Expired — Action Required",
            "description": (
                "The Roblox cookie has expired. Tracking has paused until it's updated.\n\n"
                "**⚡ Quick fix (easiest):**\n"
                "1. Go to roblox.com and log in\n"
                "2. Press `F12` → **Application** tab → **Cookies** → copy the `.ROBLOSECURITY` value\n"
                "3. **DM this bot** the cookie — just paste it directly (it starts with `_|WARNING`)\n"
                "↳ The bot will update the secret and restart tracking automatically.\n\n"
                "**🔧 Manual fix (GitHub Secrets):**\n"
                "Go to [GitHub Secrets](https://github.com/ToxinNozaki/activity-tracker/settings/secrets/actions) "
                "and update `ROBLOX_COOKIE` manually."
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


def notify_credential_invalid(name: str, fix: str):
    """
    Ping the user in the error channel when a specific credential stops working.
    `name` — human-readable name shown in the title (e.g. "Discord Bot Token")
    `fix`  — one-line fix instruction shown in the embed
    """
    _post(ERROR_CHANNEL_ID, {
        "content": f"<@{PING_USER_ID}>",
        "embeds": [{
            "title": f"🔑 Credential Invalid — {name}",
            "description": (
                f"**{name}** is no longer working. "
                "Tracking may be degraded until it's fixed.\n\n"
                f"**Fix:** {fix}"
            ),
            "color": 0xFF6600,
            "footer": {"text": _now_et()},
        }],
    })


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


def _fmt_time(minutes: int) -> str:
    if minutes <= 0:
        return "0 min"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if (h and m) else (f"{h}h" if h else f"{m}m")


def notify_daily_summary(stats: dict):
    date_str = stats.get("date", "")
    try:
        from datetime import datetime as _dt
        label = _dt.strptime(date_str, "%Y-%m-%d").strftime("%A, %B %d, %Y")
    except Exception:
        label = date_str

    fields = [
        {"name": "Total Online",  "value": _fmt_time(stats.get("online_minutes", 0)),  "inline": True},
        {"name": "Time In Game",  "value": _fmt_time(stats.get("in_game_minutes", 0)), "inline": True},
    ]

    games = stats.get("games", {})
    if games:
        medals = ["🥇", "🥈", "🥉"] + ["▸"] * 20
        top    = sorted(games.items(), key=lambda x: x[1], reverse=True)[:10]
        lines  = "\n".join(f"{medals[i]} **{g}** — {_fmt_time(m)}" for i, (g, m) in enumerate(top))
        fields.append({"name": "Games Played", "value": lines, "inline": False})

    friends = [f for f in stats.get("friends_seen", []) if f]
    if friends:
        fields.append({
            "name":   f"Friends Seen ({len(friends)})",
            "value":  ", ".join(sorted(friends)[:30]),
            "inline": False,
        })

    _post(ROBLOX_CHANNEL_ID, {"embeds": [{
        "title":  f"📊 Daily Summary — {label}",
        "color":  0x5865F2,
        "fields": fields,
        "footer": {"text": _now_et()},
    }]})


def notify_weekly_games(game_stats: dict):
    if not game_stats:
        return
    medals = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, 11)]
    top    = sorted(game_stats.items(), key=lambda x: x[1], reverse=True)[:10]
    lines  = "\n".join(f"{medals[i]} **{g}** — {_fmt_time(m)}" for i, (g, m) in enumerate(top))
    _post(ROBLOX_CHANNEL_ID, {"embeds": [{
        "title":       "🏆 Weekly Most Played Games",
        "description": lines,
        "color":       0xFFD700,
        "footer":      {"text": _now_et()},
    }]})


def notify_peak_hours(hourly: dict):
    """Posts a 24-hour bar chart of activity by hour (ET)."""
    if not hourly:
        return
    data    = {int(k): v for k, v in hourly.items()}
    max_val = max(data.values(), default=1) or 1

    # Build bar chart
    lines = []
    peak_hour, peak_mins = max(data.items(), key=lambda x: x[1], default=(0, 0))
    for h in range(24):
        mins   = data.get(h, 0)
        filled = round(mins / max_val * 10)
        bar    = "█" * filled + "░" * (10 - filled)
        label  = f"{h % 12 or 12} {'AM' if h < 12 else 'PM':>2}"
        marker = " ◄ peak" if h == peak_hour and peak_mins > 0 else ""
        lines.append(f"`{label}` {bar} {mins}m{marker}")

    # Group totals
    def _grp(start, end):
        return sum(data.get(h, 0) for h in range(start, end))

    fields = [
        {"name": "🌙 Night (12–6 AM)",      "value": _fmt_time(_grp(0, 6)),   "inline": True},
        {"name": "🌅 Morning (6 AM–12 PM)",  "value": _fmt_time(_grp(6, 12)),  "inline": True},
        {"name": "☀️ Afternoon (12–6 PM)",   "value": _fmt_time(_grp(12, 18)), "inline": True},
        {"name": "🌆 Evening (6 PM–12 AM)",  "value": _fmt_time(_grp(18, 24)), "inline": True},
        {"name": "⏰ Peak Hour",
         "value": f"{peak_hour % 12 or 12} {'AM' if peak_hour < 12 else 'PM'} ({_fmt_time(peak_mins)})",
         "inline": True},
    ]

    _post(ROBLOX_CHANNEL_ID, {"embeds": [{
        "title":       "🕐 Peak Hours This Week",
        "description": "\n".join(lines),
        "color":       0x9B59B6,
        "fields":      fields,
        "footer":      {"text": _now_et()},
    }]})


def notify_server_hop(game_name: str, player_count: int | None):
    count_str = f"\n**Players in new server:** {player_count}" if player_count else ""
    _post(ROBLOX_CHANNEL_ID, {"embeds": [{
        "title": "🔀 Server Hop Detected",
        "description": f"Switched to a different **{game_name}** server.{count_str}",
        "color": 0xFFA500,
        "footer": {"text": _now_et()},
    }]})


def notify_avatar_changed(username: str, user_id: int, new_avatar_url: str):
    embed = {
        "title": f"👗 Avatar Changed — {username}",
        "description": "Her Roblox avatar has been updated.",
        "color": 0xE91E63,
        "footer": {"text": _now_et()},
    }
    if new_avatar_url:
        embed["image"] = {"url": new_avatar_url}
    _post(ROBLOX_CHANNEL_ID, {"embeds": [embed]})


def notify_missed_runs(minutes_gap: float):
    missed = max(1, int(minutes_gap // 5) - 1)
    _post(ERROR_CHANNEL_ID, {
        "content": f"<@{PING_USER_ID}>",
        "embeds": [{
            "title": "⚠️ Tracker Was Down",
            "description": (
                f"The tracker was offline for **~{int(minutes_gap)} minutes** "
                f"(~{missed} missed run{'s' if missed != 1 else ''}).\n"
                "Tracking has resumed. Check [GitHub Actions]"
                "(https://github.com/ToxinNozaki/activity-tracker/actions) for details."
            ),
            "color": 0xFF6600,
            "footer": {"text": _now_et()},
        }],
    })


def notify_roblox_api_down(details: str):
    _post(ERROR_CHANNEL_ID, {
        "content": f"<@{PING_USER_ID}>",
        "embeds": [{
            "title": "🔴 Roblox API Appears Down",
            "description": (
                "Could not reach the Roblox API — this looks like a Roblox outage, "
                "not a cookie issue. Tracking will resume automatically when the API recovers.\n\n"
                f"**Details:** `{details}`"
            ),
            "color": 0xFF0000,
            "footer": {"text": _now_et()},
        }],
    })


def notify_squad_changed(username: str, old_size: int | None, new_size: int,
                         party_max: int | None):
    cap = f"/{party_max}" if party_max else ""
    if new_size == 1:
        desc  = f"**{username}** is now playing **solo**."
        color = 0x747F8D
        title = "🎯 Playing Solo"
    elif old_size is None or old_size == 1:
        desc  = f"**{username}** joined a squad — **{new_size}{cap}** players."
        color = 0x00B04F
        title = "👥 Joined a Squad"
    else:
        desc  = f"**{username}**'s squad changed to **{new_size}{cap}** players."
        color = 0x5865F2
        title = "👥 Squad Changed"
    _post(FORTNITE_CHANNEL_ID, {"embeds": [{
        "title": title,
        "description": desc,
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
