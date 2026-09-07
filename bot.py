from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from urllib.parse import urljoin

from aiohttp import web
from pyrogram import Client, enums, filters, raw, utils
from pyrogram.errors import PeerIdInvalid, RPCError
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup

import routes


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Falta la variable de entorno requerida: {name}")
    return value


API_ID = int(required_env("API_ID"))
API_HASH = required_env("API_HASH")
BIN_CHANNEL = required_env("BIN_CHANNEL").strip()
BASE_URL = required_env("BASE_URL").rstrip("/")
LINK_SECRET = required_env("LINK_SECRET").encode()
CONTROL_TOKEN = required_env("CONTROL_TOKEN")
MAX_CONCURRENT_DOWNLOADS = max(1, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2")))
TRAFFIC_LIMIT_GB = float(os.getenv("TRAFFIC_LIMIT_GB", "95"))
TRAFFIC_LIMIT_BYTES = int(TRAFFIC_LIMIT_GB * 1_000_000_000)
TRAFFIC_DB_PATH = os.getenv("TRAFFIC_DB_PATH", "/data/traffic.db")
WORKER_ONLY = os.getenv("WORKER_ONLY", "0").strip() == "1"
WORKER_SESSION_STRING = os.getenv("WORKER_SESSION_STRING", "").strip()
WORKER_SESSION_NAME = os.getenv("WORKER_SESSION_NAME", "file2link-worker").strip() or "file2link-worker"
WORKER_SESSION_WORKDIR = os.getenv("WORKER_SESSION_WORKDIR", "/data").strip() or "/data"
REQUIRED_CHATS = [x.strip() for x in os.getenv("REQUIRED_CHATS", "").split(",") if x.strip()]


def ensure_session_workdir() -> str:
    """Return a writable directory for Pyrogram's SQLite session file.

    Railway volumes are normally mounted at /data, but a deployment without
    the volume (or with an unavailable mount) must not crash with
    'sqlite3.OperationalError: unable to open database file'.
    """
    candidates = []
    configured = WORKER_SESSION_WORKDIR
    if configured:
        candidates.append(configured)

    # Keep a safe writable fallback for deployments where /data is not
    # mounted. A persisted Railway volume at /data remains the preferred path.
    candidates.extend([
        os.path.join(os.getcwd(), ".file2link-data"),
        "/tmp/file2link-data",
    ])

    last_error = None
    for directory in candidates:
        try:
            os.makedirs(directory, exist_ok=True)
            probe = os.path.join(directory, ".write-test")
            with open(probe, "a", encoding="utf-8"):
                pass
            try:
                os.remove(probe)
            except OSError:
                pass
            if directory != configured:
                logger.warning(
                    "WORKER_SESSION_WORKDIR='%s' no está disponible; usando '%s'. "
                    "Para conservar la sesión entre reinicios, configura un "
                    "Railway Volume en /data o un WORKER_SESSION_WORKDIR persistente.",
                    configured,
                    directory,
                )
            return directory
        except OSError as exc:
            last_error = exc

    raise RuntimeError(
        "No se pudo crear un directorio escribible para la sesión de Pyrogram. "
        f"Configurado: '{configured}'. Último error: {last_error}"
    )

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("file2link")


def no_preview_options():
    """Compatibility helper for Telegram preview disabling."""
    try:
        from telegram import LinkPreviewOptions  # type: ignore
        return {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
    except Exception:
        return {"disable_web_page_preview": True}


def build_download_url(channel_id: int, message_id: int) -> str:
    token = routes.create_file_token(channel_id, message_id, LINK_SECRET)
    return urljoin(BASE_URL + "/", f"download/{token}")


async def resolve_channel_id(client: Client) -> int:
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
            chat = await client.get_chat(configured)
    except Exception as exc:
        raise RuntimeError(
            f"No se pudo resolver BIN_CHANNEL='{configured}'. Verifica el ID/usuario y que el bot tenga acceso al canal. Detalle: {exc}"
        ) from exc
    logger.info("Canal de almacenamiento resuelto: %s (%s)", chat.title, chat.id)
    return int(chat.id)


async def build_required_chats_keyboard(client: Client) -> InlineKeyboardMarkup | None:
    rows = []
    for ref in REQUIRED_CHATS:
        try:
            chat = await client.get_chat(ref)
            username = getattr(chat, "username", None)
            if username:
                url = f"https://t.me/{username}"
            else:
                # For private channels/groups Telegram only exposes this form to users
                # who can already access the chat; still useful as a best-effort button.
                url = f"https://t.me/c/{str(chat.id).replace('-100', '', 1)}"
            rows.append([InlineKeyboardButton(chat.title or str(ref), url=url)])
        except Exception:
            logger.exception("No se pudo cargar chat requerido %s", ref)
    return InlineKeyboardMarkup(rows) if rows else None


async def check_required_membership(client: Client, message: Message) -> bool:
    if not REQUIRED_CHATS:
        return True
    user_id = message.from_user.id if message.from_user else None
    if user_id is None:
        return False
    for ref in REQUIRED_CHATS:
        try:
            chat = await client.get_chat(ref)
            member = await client.get_chat_member(chat.id, user_id)
            allowed = member.status in {
                enums.ChatMemberStatus.MEMBER,
                enums.ChatMemberStatus.ADMINISTRATOR,
                enums.ChatMemberStatus.OWNER,
            }
            if member.status == enums.ChatMemberStatus.RESTRICTED:
                allowed = bool(member.is_member)
            if not allowed:
                await message.reply_text(
                    "🔒 Debes unirte a todos los canales/chats requeridos antes de usar el bot.",
                    reply_markup=await build_required_chats_keyboard(client),
                )
                return False
        except Exception:
            logger.exception("No se pudo comprobar membresía en %s", ref)
            await message.reply_text(
                "⚠️ No pude verificar tu membresía en uno de los chats requeridos. Inténtalo de nuevo en unos segundos."
            )
            return False
    return True


async def start_cmd(client: Client, message: Message) -> None:
    if not await check_required_membership(client, message):
        return
    await message.reply_text("✅ Bot activo y listo. Envíame un archivo para generar su enlace.")


async def file_handler(client: Client, message: Message) -> None:
    if not await check_required_membership(client, message):
        return
    channel_id = getattr(client, "CHANNEL_ID", None)
    if channel_id is None:
        await message.reply_text("❌ El canal de almacenamiento no está disponible.")
        return
    try:
        # Copy/forward into the storage channel; never expose Telegram's source
        # message to the user.
        file_message = await message.copy(channel_id)
        link = build_download_url(channel_id, file_message.id)
        await message.reply_text(
            f"✅ ¡Enlace generado!\n\n🔗 {link}\n\n📥 Compatible con gestores de descarga y reanudación.",
            **no_preview_options(),
        )
    except (PeerIdInvalid, RPCError):
        logger.exception("Telegram error while storing file")
        await message.reply_text("❌ No pude almacenar el archivo en este momento.")
    except Exception:
        logger.exception("Unexpected error while storing file")
        await message.reply_text("❌ Ocurrió un error inesperado.")


async def health_check(request: web.Request) -> web.Response:
    client: Client | None = request.app.get("telegram_client")
    ready = bool(client and client.is_connected and getattr(client, "CHANNEL_ID", None) is not None)
    return web.Response(text="OK" if ready else "STARTING", status=200 if ready else 503)


async def main() -> None:
    if WORKER_ONLY:
        # Worker mode deliberately uses the SAME bot identity as the main bot.
        session_workdir = ensure_session_workdir()
        session_name = WORKER_SESSION_NAME
        session_path = os.path.join(session_workdir, f"{session_name}.session")
        # A worker must not have a dedicated bot token and must never start a
        # user-account login flow (phone/code/2FA). Each worker creates/uses
        # its own MTProto session from the shared BOT_TOKEN, or loads a
        # previously generated session string/file. Incoming updates remain
        # disabled because workers only need MTProto file access.
        if WORKER_SESSION_STRING:
            bot = Client(
                WORKER_SESSION_NAME,
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=WORKER_SESSION_STRING,
                no_updates=True,
            )
        else:
            bot_token = os.getenv("BOT_TOKEN", "").strip()
            if os.path.isfile(session_path):
                bot = Client(
                    WORKER_SESSION_NAME,
                    api_id=API_ID,
                    api_hash=API_HASH,
                    workdir=session_workdir,
                    no_updates=True,
                )
            elif bot_token:
                # This is the same token used by the main bot, not a
                # worker-specific token. Pyrogram creates a separate local
                # MTProto session for this worker on first startup.
                bot = Client(
                    WORKER_SESSION_NAME,
                    api_id=API_ID,
                    api_hash=API_HASH,
                    bot_token=bot_token,
                    workdir=session_workdir,
                    no_updates=True,
                )
            else:
                raise RuntimeError(
                    "Worker Telegram authentication is not configured. Set "
                    "WORKER_SESSION_STRING, provide the persisted "
                    f"'{WORKER_SESSION_NAME}.session' file, or set the SAME "
                    "BOT_TOKEN used by the main bot. API_ID/API_HASH alone "
                    "cannot authenticate a Telegram client."
                )
        worker_only = True
    else:
        # Non-worker mode remains compatible with the original bot deployment.
        bot_token = required_env("BOT_TOKEN")
        bot = Client(
            "file-to-link-bot",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=bot_token,
            no_updates=False,
        )
        worker_only = False
    if not worker_only:
        bot.add_handler(MessageHandler(start_cmd, filters.private & filters.command("start")), group=0)
        bot.add_handler(MessageHandler(file_handler, filters.private & filters.media), group=0)

    app = web.Application(client_max_size=0)
    app.router.add_get("/health", health_check)
    app.router.add_get("/download/{token}", routes.serve_file)
    app.router.add_get("/stream/{token}", routes.serve_stream)
    app.router.add_get("/traffic", routes.traffic_status)
    app.router.add_get("/control/status", routes.control_status)
    app.router.add_post("/control/disable", routes.control_set_disabled)
    app.router.add_post("/control/sync-usage", routes.control_sync_usage)

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

        routes.configure(
            client=bot,
            channel_id=channel_id,
            link_secret=LINK_SECRET,
            max_concurrent_downloads=MAX_CONCURRENT_DOWNLOADS,
            traffic_limit_bytes=TRAFFIC_LIMIT_BYTES,
            traffic_db_path=TRAFFIC_DB_PATH,
        )

        app["telegram_client"] = bot
        app["channel_id"] = channel_id
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        port = int(os.getenv("PORT", "8000"))
        await web.TCPSite(runner, "0.0.0.0", port, backlog=128).start()
        logger.info("HTTP escuchando en 0.0.0.0:%s; worker_only=%s", port, worker_only)
        logger.info("File2Link READY")
        await stop_event.wait()
    finally:
        if runner is not None:
            await runner.cleanup()
        if bot.is_connected:
            await bot.stop()
        logger.info("File2Link detenido")


if __name__ == "__main__":
    asyncio.run(main())
