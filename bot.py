from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from urllib.parse import urljoin

from aiohttp import web
from pyrogram import Client, filters, raw, utils
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
    """Resolve the storage channel id.

    Two separate problems collide here:

    1) get_dialogs() (messages.GetDialogs) is a *user-account-only* method.
       Bots always get "400 BOT_METHOD_INVALID" calling it - that was the
       direct cause of the crash-on-every-startup loop.

    2) get_chat() with a bare numeric id runs it through
       pyrogram.utils.get_peer_type(), which hardcodes a 32-bit-era id range
       (channels below -1002147483647 are rejected outright). Telegram's
       newer, longer channel ids fall below that, so on a cold session
       get_chat() raises a plain ValueError("Peer id invalid: ...") for a
       perfectly valid id - this is the "modern channel id" issue the
       README already mentions.

    channels.GetChannels, invoked directly, sidesteps that range check
    entirely, and Pyrogram's invoke() auto-caches whatever channel it gets
    back (same mechanism get_chat() itself relies on) - so the follow-up
    get_chat() call resolves normally. Any RPC failure here (bot not an
    admin, wrong id, etc.) is converted into one clear message instead of
    crashing with a bare Telegram error code.
    """
    configured = BIN_CHANNEL
    try:
        if configured.lstrip("-").isdigit():
            numeric_id = int(configured)
            await client.invoke(
                raw.functions.channels.GetChannels(
                    id=[raw.types.InputChannel(channel_id=utils.get_channel_id(numeric_id), access_hash=0)]
                )
            )
            chat = await client.get_chat(numeric_id)
        else:
            # Username resolution (contacts.ResolveUsername) is unaffected by
            # the numeric id range check above.
            chat = await client.get_chat(configured)
    except Exception as exc:
        raise RuntimeError(
            f"No se pudo resolver BIN_CHANNEL='{configured}'. Verifica que el bot "
            "sea ADMINISTRADOR del canal y que el ID/usuario sea correcto. "
            f"Detalle: {exc}"
        ) from exc

    logger.info("Canal de almacenamiento resuelto: %s (%s)", chat.title, chat.id)
    return int(chat.id)


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
