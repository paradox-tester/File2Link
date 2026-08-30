# File2Link - Railway

## Required Railway variables

- `API_ID`
- `API_HASH`
- `BOT_TOKEN`
- `BIN_CHANNEL` (for example `-1004306102730`)
- `BASE_URL`
- `MAX_CONCURRENT_DOWNLOADS` (default `2`)
- `LOG_LEVEL` (default `INFO`)

## Important Pyrogram 2.0.106 compatibility

This build updates Pyrogram's obsolete `MIN_CHANNEL_ID`/`MIN_CHAT_ID` constants at runtime. Pyrogram 2.0.106 otherwise rejects modern `-100...` channel IDs before it can resolve them.

The bot must be a member/admin of the storage channel. The code intentionally does **not** call `get_dialogs()` because Telegram's `messages.GetDialogs` is a user-only method and returns `BOT_METHOD_INVALID` for bot accounts. The configured channel is resolved directly with `get_chat()`.

## Railway

Start command: `python bot.py`
Health endpoint: `/health`
