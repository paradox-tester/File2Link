from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from urllib.parse import urljoin

from aiohttp import web
from pyrogram import Client, filters, utils as pyrogram_utils
from pyrogram.errors import PeerIdInvalid, RPCError
from pyrogram.types import Message

# Pyrogram 2.0.106 has an obsolete lower bound for modern channel IDs.
# Keep this compatibility shim for API calls that still pass through get_peer_type.
_ORIGINAL_GET_PEER_TYPE = pyrogram_utils.get_peer_type

def _get_peer_type_compat(peer_id: int) -> str:
    try:
        return _ORIGINAL_GET_PEER_TYPE(peer_id)
    except ValueError:
        if -1007852516352 <= peer_id < -1000000000000:
            return "channel"
        raise

pyrogram_utils.get_peer_type = _get_peer_type_compat

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

try:
    MAX_CONCURRENT_DOWNLOADS = max(1, int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2")))
except ValueError as exc:
    raise RuntimeError("MAX_CONCURRENT_DOWNLOADS debe ser un número entero") from exc

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("file2link")

bot = Client(
    "file-to-link-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

app = web.Application(client_max_size=0)


async def resolve_storage_channel() -> int:
    """Resolve the storage channel without relying on Pyrogram's old ID parser.

    For numeric IDs, dialogs are checked first. This is important for modern
    -100... channel IDs because Pyrogram 2.0.106 may reject them before Telegram
    is contacted. The bot must have access to the storage channel, which also
    makes the channel available in its dialogs/peer cache.
    """
    value = BIN_CHANNEL

    if value.lstrip("-").isdigit():
        wanted_id = int(value)
        logger.info("Resolviendo canal de almacenamiento por ID: %s", wanted_id)

        try:
            async for dialog in bot.get_dialogs():
                chat = dialog.chat
                if chat and int(chat.id) == wanted_id:
                    logger.info("Canal encontrado en diálogos: %s (ID: %s)", getattr(chat, "title", None), chat.id)
                    return int(chat.id)
        except Exception:
            logger.exception("Error consultando los diálogos para BIN_CHANNEL=%s", wanted_id)

        # The compatibility shim may allow get_chat() to proceed for modern IDs.
        try:
            chat = await bot.get_chat(wanted_id)
            logger.info("Canal encontrado mediante get_chat: %s (ID: %s)", getattr(chat, "title", None), chat.id)
            return int(chat.id)
        except (PeerIdInvalid, RPCError) as exc:
            raise RuntimeError(
                f"No se pudo resolver el canal {wanted_id}. "
                "Asegúrate de que el bot sea miembro/administrador del canal."
            ) from exc

    username = value if value.startswith("@") else value
    logger.info("Resolviendo canal de almacenamiento: %s", username)
    try:
        chat = await bot.get_chat(username)
        logger.info("Canal encontrado: %s (ID: %s)", getattr(chat, "title", None), chat.id)
        return int(chat.id)
    except (PeerIdInvalid, RPCError) as exc:
        raise RuntimeError(f"No se pudo resolver BIN_CHANNEL={value}") from exc


async def ensure_channel_id() -> int:
    current = getattr(bot, "CHANNEL_ID", None)
    if current is not None:
        return int(current)

    lock = getattr(bot, "_channel_resolve_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        bot._channel_resolve_lock = lock

    async with lock:
        current = getattr(bot, "CHANNEL_ID", None)
        if current is None:
            channel_id = await resolve_storage_channel()
            bot.CHANNEL_ID = int(channel_id)
            app["channel_id"] = int(channel_id)
            routes.configure(
                client=bot,
                channel_id=channel_id,
                max_concurrent_downloads=MAX_CONCURRENT_DOWNLOADS,
            )
        return int(bot.CHANNEL_ID)


def build_download_url(channel_id: int, message_id: int) -> str:
    return urljoin(BASE_URL + "/", f"download/{channel_id}/{message_id}")


@bot.on_message(filters.private & filters.command("start"))
async def start_cmd(client: Client, message: Message) -> None:
    logger.info("UPDATE RECEIVED: /start from user=%s", getattr(message.from_user, "id", "unknown"))
    await message.reply_text("✅ Bot activo y escuchando.")


@bot.on_message(filters.private & filters.text)
async def text_handler(client: Client, message: Message) -> None:
    logger.info("UPDATE RECEIVED: text from user=%s", getattr(message.from_user, "id", "unknown"))
    await message.reply_text("📩 Recibido tu mensaje de texto.")


@bot.on_message(filters.private & filters.media)
async def file_handler(client: Client, message: Message) -> None:
    user_id = getattr(message.from_user, "id", "unknown")
    logger.info("UPDATE RECEIVED: media from user=%s", user_id)

    if not (message.document or message.video or message.audio or message.photo or message.voice or message.animation or message.video_note):
        await message.reply_text("❌ Tipo de archivo no soportado.")
        return

    try:
        channel_id = await ensure_channel_id()
        file_message = await message.forward(channel_id)
        download_link = build_download_url(channel_id, file_message.id)
        await message.reply_text(
            "✅ ¡Enlace generado!\n\n"
            f"🔗 {download_link}\n\n"
            "📥 Descarga reanudable con JDownloader o cualquier gestor.\n"
            "⚡ El enlace es permanente mientras el archivo exista."
        )
        logger.info("Archivo procesado para usuario %s: %s", user_id, download_link)
    except (PeerIdInvalid, RPCError):
        logger.exception("Error de Telegram al procesar archivo para user=%s", user_id)
        await message.reply_text(
            "❌ No pude acceder al canal de almacenamiento. "
            "Verifica que el bot sea miembro/administrador del canal y que BIN_CHANNEL sea correcto."
        )
    except Exception:
        logger.exception("Error inesperado al procesar archivo para user=%s", user_id)
        await message.reply_text("❌ Ocurrió un error al procesar el archivo. Intenta de nuevo.")


async def health_check(request: web.Request) -> web.Response:
    # Healthcheck means the HTTP service is alive and Telegram is connected.
    # Channel resolution is intentionally not required here: a slow/failing
    # peer lookup must not make the Telegram bot appear dead or block updates.
    ready = bool(bot.is_connected)
    return web.Response(text="OK" if ready else "STARTING", status=200 if ready else 503)


async def start_web_server() -> web.AppRunner:
    app["telegram_client"] = bot
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = int(os.environ.get("PORT", "8000"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port, backlog=128)
    await site.start()
    logger.info("HTTP server listening on 0.0.0.0:%s", port)
    return runner


async def main() -> None:
    runner: web.AppRunner | None = None
    stop_event = asyncio.Event()

    def request_shutdown() -> None:
        if not stop_event.is_set():
            logger.info("Shutdown signal received")
            stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        logger.info("Starting Telegram client...")
        await bot.start()
        me = await bot.get_me()
        logger.info("Telegram connected as @%s (id=%s)", getattr(me, "username", None), me.id)
        logger.info("Update dispatcher is active; handlers registered")

        # Start HTTP immediately. Do NOT block bot startup on BIN_CHANNEL
        # resolution; it is resolved lazily when the first file arrives.
        runner = await start_web_server()
        logger.info("🚀 File2Link READY")
        logger.info("Storage channel configured as: %s", BIN_CHANNEL)

        await stop_event.wait()
    except Exception:
        logger.exception("Fatal error in main")
        raise
    finally:
        if runner is not None:
            await runner.cleanup()
        if bot.is_connected:
            await bot.stop()
        logger.info("File2Link stopped")


app.router.add_get("/health", health_check)
app.router.add_get("/download/{channel_id}/{message_id}", routes.serve_file)


if __name__ == "__main__":
    asyncio.run(main())
