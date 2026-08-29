# File2Link - Railway

Variables required in Railway:
- API_ID
- API_HASH
- BOT_TOKEN
- BIN_CHANNEL
- BASE_URL

Optional:
- MAX_CONCURRENT_DOWNLOADS (default 2)
- LOG_LEVEL (default INFO)

For BIN_CHANNEL, use the full Telegram channel ID, e.g. -1004306102730, or the @username if the channel has one. The bot must have access to the storage channel.

This build pins Python 3.11.14 and includes a compatibility fix for modern Telegram channel IDs that Pyrogram 2.0.106 can otherwise reject as `Peer id invalid`.
