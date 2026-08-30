from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from urllib.parse import urljoin

from aiohttp import web
from pyrogram import Client, filters
from pyrogram.errors import PeerIdInvalid, RPCError
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

import routes


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Falta la variable de entorno requerida: {name}")
    return value

API_ID = int(required_env("API_ID"))
API_HASH = required_env("API_HASH")
BOT_TOKEN = required_env("BOT_TOKEN")
BIN_CHANNEL = required_env("BIN_CHANNEL").strip()
BASE_URL = required_env("BASE_URL").rstrip("/")
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2")))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("file2link")


def build_download_url(channel_id: int, message_id: int) -> str:
    return urljoin(BASE_URL + "/", f"download/{channel_id}/{message_id}")


async def resolve_channel_id(client: Client) -> int:
    """Resolve storage channel without relying on Pyrogram's old numeric peer parser."""
    configured = BIN_CHANNEL
    numeric_id = int(configured) if configured.lstrip("-").isdigit() else None
    wanted_username = configured.lstrip("@").lower()

    # A bot can only access chats that Telegram exposes to it. Compare the real
    # dialog chat IDs directly, avoiding get_chat()'s old peer-id range issue.
    async for dialog in client.get_dialogs():
        chat = dialog.chat
        username = (getattr(chat, "username", None) or "").lower()
        if (numeric_id is not None and chat.id == numeric_id) or (
            numeric_id is None and username == wanted_username
        ):
            logger.info("Canal de almacenamiento resuelto: %s (%s)", getattr(chat, "title", None), chat.id)
            return int(chat.id)

    # Username resolution is safe as it does not use the problematic numeric
    # channel-id conversion path.
    if numeric_id is None:
        chat = await client.get_chat(configured)
        return int(chat.id)

    raise RuntimeError(
        "No se pudo resolver BIN_CHANNEL mediante los diálogos del bot. "
        "Asegúrate de que el bot sea miembro del canal y que el ID sea correcto."
    )


async def start_cmd(client: Client, message: Message) -> None:
    logger.info("UPDATE /start recibido: chat=%s", message.chat.id)
    await message.reply_text("✅ Bot activo y listo. Envíame un archivo para generar su enlace.")


async def file_handler(client: Client, message: Message) -> None:
    logger.info("UPDATE media recibido: chat=%s message=%s", message.chat.id, message.id)
    channel_id = getattr(client, "CHANNEL_ID", None)
    if channel_id is None:
        await message.reply_text("❌ El canal de almacenamiento no está disponible.")
        return

    try:
        file_message = await message.forward(channel_id)
        link = build_download_url(channel_id, file_message.id)
        await message.reply_text(
            "✅ ¡Enlace generado!\n\n"
            f"🔗 {link}\n\n"
            "📥 Compatible con gestores de descarga y reanudación."
        )
        logger.info("Archivo reenviado correctamente: %s/%s", channel_id, file_message.id)
    except PeerIdInvalid:
        logger.exception("Peer inválido al reenviar archivo")
        await message.reply_text("❌ No pude acceder al canal de almacenamiento.")
    except RPCError:
        logger.exception("Error RPC al procesar archivo")
        await message.reply_text("❌ Telegram devolvió un error al procesar el archivo.")
    except Exception:
        logger.exception("Error inesperado al procesar archivo")
        await message.reply_text("❌ Ocurrió un error inesperado.")


async def health_check(request: web.Request) -> web.Response:
    client: Client | None = request.app.get("telegram_client")
    ready = bool(client and client.is_connected and getattr(client, "CHANNEL_ID", None) is not None)
    return web.Response(text="OK" if ready else "STARTING", status=200 if ready else 503)


async def main() -> None:
    # IMPORTANT: Pyrogram must be instantiated inside the asyncio.run() event
    # loop when asyncio.run() is used. Creating it at module import time binds
    # Pyrogram resources to a different loop and can leave the process alive
    # while handlers never receive updates.
    bot = Client(
        "file-to-link-bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        no_updates=False,
    )

    # Register handlers on the exact client instance that will be started.
    bot.add_handler(MessageHandler(start_cmd, filters.private & filters.command("start")), group=0)
    bot.add_handler(MessageHandler(file_handler, filters.private & filters.media), group=0)

    app = web.Application(client_max_size=0)
    app.router.add_get("/health", health_check)
    app.router.add_get("/download/{channel_id}/{message_id}", routes.serve_file)

    stop_event = asyncio.Event()
    runner: web.AppRunner | None = None

    def request_shutdown() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await bot.start()
        me = await bot.get_me()
        logger.info("Telegram conectado como @%s (%s)", me.username, me.id)

        channel_id = await resolve_channel_id(bot)
        bot.CHANNEL_ID = channel_id
        routes.configure(client=bot, channel_id=channel_id, max_concurrent_downloads=MAX_CONCURRENT_DOWNLOADS)

        app["telegram_client"] = bot
        app["channel_id"] = channel_id
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        port = int(os.environ.get("PORT", "8000"))
        await web.TCPSite(runner, "0.0.0.0", port, backlog=128).start()

        logger.info("HTTP escuchando en 0.0.0.0:%s", port)
        logger.info("Handlers registrados y dispatcher activo")
        logger.info("🚀 File2Link READY")
        await stop_event.wait()
    finally:
        if runner is not None:
            await runner.cleanup()
        if bot.is_connected:
            await bot.stop()
        logger.info("File2Link detenido")


if __name__ == "__main__":
    asyncio.run(main())
