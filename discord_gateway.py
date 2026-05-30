"""
Maintains a Discord Gateway WebSocket connection to keep the bot status
set to "online". A background daemon thread sends heartbeats so the main
thread can do other work without worrying about the connection dropping.

Usage:
    presence = DiscordPresence()
    if presence.connect():
        # bot is now "online" in Discord
        ...do work...
        presence.close()  # bot goes offline
"""

import json
import logging
import os
import threading
import time

import websocket  # pip install websocket-client

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
_GATEWAY  = "wss://gateway.discord.gg/?v=10&encoding=json"


class DiscordPresence:
    """Opens a Gateway connection and holds the bot status at 'online'."""

    def __init__(self):
        self._ws: websocket.WebSocket | None = None
        self._hb_interval: float = 41.25   # Discord default fallback (ms → s already)
        self._hb_thread: threading.Thread | None = None
        self._running = False

    # ── public ────────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """
        Connect to the Gateway, send IDENTIFY with status=online, and start
        the heartbeat thread. Returns True on success.
        """
        if not BOT_TOKEN:
            logging.warning("discord_gateway: BOT_TOKEN not set — skipping presence")
            return False
        try:
            self._ws = websocket.WebSocket()
            self._ws.connect(_GATEWAY, timeout=15)

            # ① Receive HELLO and grab heartbeat interval
            hello = json.loads(self._ws.recv())
            self._hb_interval = hello["d"]["heartbeat_interval"] / 1000  # ms → s

            # ② Send first heartbeat immediately (Discord expects this)
            self._send({"op": 1, "d": None})

            # ③ IDENTIFY with online presence
            self._send({
                "op": 2,
                "d": {
                    "token": BOT_TOKEN,
                    "intents": 0,
                    "properties": {
                        "os":      "linux",
                        "browser": "tracker",
                        "device":  "tracker",
                    },
                    "presence": {
                        "status":     "online",
                        "afk":        False,
                        "activities": [],
                        "since":      None,
                    },
                },
            })

            # ④ Start background heartbeat thread
            self._running = True
            self._hb_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True, name="gw-heartbeat"
            )
            self._hb_thread.start()

            logging.info("discord_gateway: connected — bot status set to online")
            return True

        except Exception as e:
            logging.warning("discord_gateway: connection failed: %s", e)
            return False

    def close(self):
        """Disconnect from the Gateway (bot will appear offline shortly after)."""
        self._running = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self._ws = None
        logging.info("discord_gateway: disconnected")

    # ── internal ──────────────────────────────────────────────────────────────

    def _send(self, payload: dict):
        try:
            if self._ws:
                self._ws.send(json.dumps(payload))
        except Exception as e:
            logging.debug("discord_gateway: send error: %s", e)

    def _heartbeat_loop(self):
        while self._running:
            time.sleep(self._hb_interval)
            if not self._running:
                break
            self._send({"op": 1, "d": None})
            logging.debug("discord_gateway: heartbeat sent")
