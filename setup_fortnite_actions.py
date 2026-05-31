"""
One-time setup — run via GitHub Actions (setup_fortnite_auth.yml).
Authenticates with Epic using the existing refresh token, gets device auth
via fortnitepy, and saves EPIC_DEVICE_AUTH as a GitHub Secret automatically.
"""

import asyncio
import base64
import json
import os
import subprocess
import requests
import fortnitepy

_CLIENT_ID     = "34a02cf8f4414e29b15921876da36f9a"
_CLIENT_SECRET = "daafbccc737745039dffe53d94fc76cf"
_BASIC         = base64.b64encode(f"{_CLIENT_ID}:{_CLIENT_SECRET}".encode()).decode()
_TOKEN_URL     = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token"
_EXCHANGE_URL  = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/exchange"
REPO           = "ToxinNozaki/activity-tracker"
GH_TOKEN       = os.environ.get("GITHUB_PAT", "")


def get_exchange_code() -> str:
    epic_auth = json.loads(os.environ["EPIC_AUTH_JSON"])

    # Step 1: refresh → access token
    r = requests.post(
        _TOKEN_URL,
        headers={"Authorization": f"Basic {_BASIC}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token",
              "refresh_token": epic_auth["refresh_token"]},
        timeout=15,
    )
    r.raise_for_status()
    access_token = r.json()["access_token"]
    print("Got access token.")

    # Step 2: access token → exchange code (single-use, 5 min TTL)
    r2 = requests.get(_EXCHANGE_URL,
                      headers={"Authorization": f"Bearer {access_token}"},
                      timeout=10)
    r2.raise_for_status()
    code = r2.json()["code"]
    print(f"Got exchange code: {code[:8]}...")
    return code


def save_secret(name: str, value: str):
    """Save a GitHub repository secret via gh CLI using GITHUB_PAT."""
    env = os.environ.copy()
    env["GH_TOKEN"] = GH_TOKEN
    result = subprocess.run(
        ["gh", "secret", "set", name,
         "--repo", REPO,
         "--body", value],
        env=env, capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"Secret {name} saved to GitHub.")
    else:
        print(f"Failed to save secret: {result.stderr}")
        raise RuntimeError(result.stderr)


def main():
    exchange_code = get_exchange_code()

    device_auth_result: dict = {}

    bot = fortnitepy.Client(
        auth=fortnitepy.ExchangeCodeAuth(code=exchange_code)
    )

    @bot.event
    async def event_device_auth_generate(details: dict, email: str):
        print(f"Device auth generated for {email}")
        device_auth_result["device_id"]  = details["device_id"]
        device_auth_result["account_id"] = details["account_id"]
        device_auth_result["secret"]     = details["secret"]
        await bot.close()

    @bot.event
    async def event_ready():
        print("Bot ready — waiting for device auth event...")
        # Give it 15s; if device_auth_generate didn't fire, close anyway
        await asyncio.sleep(15)
        if not device_auth_result:
            print("WARNING: device_auth_generate did not fire — closing")
        await bot.close()

    bot.run()

    if not device_auth_result:
        raise RuntimeError("Device auth was not generated — check exchange code")

    print("Saving EPIC_DEVICE_AUTH secret...")
    save_secret("EPIC_DEVICE_AUTH", json.dumps(device_auth_result))
    print("All done! EPIC_DEVICE_AUTH is set. You can now start the Fortnite bot.")


if __name__ == "__main__":
    main()
