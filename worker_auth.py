"""Optional one-time exporter for a File2Link worker bot session.

This utility intentionally uses the EXISTING main bot token. It does not ask
for a phone number, Telegram login code or 2FA password.

It is useful when workers should not carry BOT_TOKEN at runtime: run it once,
store the resulting WORKER_SESSION_STRING on the worker, and remove BOT_TOKEN
from the worker environment.
"""

from __future__ import annotations

import os
from pyrogram import Client
from dotenv import load_dotenv

load_dotenv()


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


api_id = int(required("API_ID"))
api_hash = required("API_HASH")
bot_token = required("BOT_TOKEN")

app = Client(
    "file2link-worker-session-generator",
    api_id=api_id,
    api_hash=api_hash,
    bot_token=bot_token,
    in_memory=True,
    no_updates=True,
)

print("=" * 60)
print("File2Link worker - bot session exporter")
print("=" * 60)
print("Uses the existing main BOT_TOKEN.")
print("No phone number, login code or 2FA is requested.")
print()

app.start()
try:
    me = app.get_me()
    session = app.export_session_string()
    print(f"Authorized as: @{me.username or 'no_username'} ({me.id})")
    print()
    print("WORKER_SESSION_STRING=")
    print(session)
    print()
    print("Store this value on the worker and remove BOT_TOKEN from that worker if desired.")
finally:
    app.stop()
