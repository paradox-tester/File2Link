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

# Parche de compatibilidad para IDs de canal modernos
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

# -----------------------------------------------------------------------------
# Configuración
# -----------------------------------------------------------------------------

def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Falta la variable de entorno requerida: {name}")
    return value

API_ID = int(required_env("API_ID"))
API_HASH = required_env("API_HASH")
BOT_TOKEN = required_env("BOT_TOKEN")
BIN_CHANNEL = required_env("BIN_CHANNEL")
BASE_URL = required_env("BASE_URL").rstrip("/")

try:
    MAX_CONCURRENT_DOWNLOADS = max(1, int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2")))
except ValueError as exc:
    raise RuntimeError("MAX_CONCURRENT_DOWNLOADS debe ser un número entero") from exc

# Logs a stdout
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
for handler in logging.root.handlers:
    handler.setStream(sys.stdout)

logger = logging.getLogger("file2link")

bot = Client(
    "file-to-link-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

app = web.Application(client_max_size=0)

# -----------------------------------------------------------------------------
# Handlers de prueba (diagnóstico)
# -----------------------------------------------------------------------------

@bot.on_message(filters.private & filters.command("start"))
async def start_cmd(client, message):
    await message.reply("✅ Bot activo y escuchando.")
    logger.info("Comando /start recibido de %s", message.from_user.id)

@bot.on_message(filters.private & filters.text)
async def text_handler(client, message):
    await message.reply("📩 Recibido tu mensaje de texto.")
    logger.info("Texto recibido de %s: %s", message.from_user.id, message.text)

# -----------------------------------------------------------------------------
# Handler principal de archivos (ahora con filters.media)
# -----------------------------------------------------------------------------

@bot.on_message(filters.private & filters.media)
async def file_handler(client: Client, message: Message) -> None:
    logger.info("Archivo recibido de %s", message.from_user.id)

    # Verificar que sea un tipo soportado
    if not (message.document or message.video or message.audio or message.photo or message.voice or message.animation or message.video_note):
        await message.reply("❌ Tipo de archivo no soportado.")
        return

    channel_id = getattr(client, "CHANNEL_ID", None)
    if channel_id is None:
        await message.reply_text("❌ El bot no está configurado correctamente. Contacta al administrador.")
        return

    try:
        file_message = await message.forward(channel_id)
        download_link = build_download_url(channel_id, file_message.id)
        await message.reply_text(
            "✅ ¡Enlace generado!\n\n"
            f"🔗 {download_link}\n\n"
            "📥 Descarga reanudable con JDownloader o cualquier gestor.\n"
            "⚡ El enlace es permanente mientras el archivo exista."
        )
        logger.info("Archivo procesado para usuario %s: %s", getattr(message.from_user, "id", "unknown"), download_link)
    except PeerIdInvalid:
        logger.exception("Peer ID inválido al procesar archivo")
        await message.reply_text("❌ El bot no tiene acceso al canal de almacenamiento. Verifica sus permisos.")
    except RPCError:
        logger.exception("Error de Telegram al procesar archivo")
        await message.reply_text("❌ Error al comunicarse con Telegram. Intenta de nuevo.")
    except Exception:
        logger.exception("Error inesperado al procesar archivo")
        await message.reply_text("❌ Ocurrió un error inesperado. Intenta de nuevo.")

# -----------------------------------------------------------------------------
# Funciones auxiliares y arranque
# -----------------------------------------------------------------------------

def build_download_url(channel_id: int, message_id: int) -> str:
    return urljoin(BASE_URL + "/", f"download/{channel_id}/{message_id}")

async def get_channel_id() -> int:
    value = BIN_CHANNEL.strip()
    try:
        chat = await bot.get_chat(int(value) if value.lstrip("-").isdigit() else value)
        logger.info("Canal encontrado: %s (ID: %s)", getattr(chat, "title", None), chat.id)
        return int(chat.id)
    except (PeerIdInvalid, RPCError) as first_error:
        logger.warning("No se pudo resolver BIN_CHANNEL directamente: %s", first_error)
    async for dialog in bot.get_dialogs():
        username = getattr(dialog.chat, "username", None)
        if str(dialog.chat.id) == value or (username and username.lower() == value.lstrip("@").lower()):
            logger.info("Canal encontrado en diálogos: %s (ID: %s)", dialog.chat.title, dialog.chat.id)
            return int(dialog.chat.id)
    raise RuntimeError("No se pudo encontrar BIN_CHANNEL. Verifica el ID/username y los permisos del bot.")

async def health_check(request: web.Request) -> web.Response:
    bot_ready = bot.is_connected and getattr(bot, "CHANNEL_ID", None) is not None
    return web.Response(text="OK" if bot_ready else "STARTING", status=200 if bot_ready else 503)

async def start_web_server(channel_id: int) -> web.AppRunner:
    app["telegram_client"] = bot
    app["channel_id"] = int(channel_id)
    routes.configure(
        client=bot,
        channel_id=channel_id,
        max_concurrent_downloads=MAX_CONCURRENT_DOWNLOADS,
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = int(os.environ.get("PORT", "8000"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port, backlog=128)
    await site.start()
    logger.info("Servidor HTTP iniciado en 0.0.0.0:%s", port)
    return runner

async def main() -> None:
    runner: web.AppRunner | None = None
    stop_event = asyncio.Event()

    def request_shutdown() -> None:
        if not stop_event.is_set():
            logger.info("Señal de apagado recibida; cerrando limpiamente...")
            stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await bot.start()
        # Forzar eliminación de cualquier webhook previo
        await bot.set_webhook()
        logger.info("Webhook eliminado (modo polling)")

        channel_id = await get_channel_id()
        bot.CHANNEL_ID = channel_id
        runner = await start_web_server(channel_id)
        logger.info("🚀 File2Link iniciado correctamente")

        await stop_event.wait()
    finally:
        if runner is not None:
            await runner.cleanup()
        if bot.is_connected:
            await bot.stop()
        logger.info("File2Link detenido")

app.router.add_get("/health", health_check)
app.router.add_get("/download/{channel_id}/{message_id}", routes.serve_file)

if __name__ == "__main__":
    asyncio.run(main())