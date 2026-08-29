import os
import logging
import asyncio
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import PeerIdInvalid, RPCError
import routes

# --- Configuración desde variables de entorno ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BIN_CHANNEL = os.environ.get("BIN_CHANNEL")  # Puede ser ID numérico o username (sin @)
BASE_URL = os.environ.get("BASE_URL")

# --- Inicializar el bot ---
bot = Client(
    "file-to-link-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# --- Configurar logging detallado ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Función para obtener el ID del canal usando múltiples estrategias ---
async def get_channel_id():
    """
    Obtiene el ID numérico del canal a partir de lo que se proporcione en BIN_CHANNEL.
    Estrategias:
    1. Intentar con el valor tal cual (puede ser ID o username).
    2. Si falla por caché, recorrer los diálogos para forzar actualización.
    3. Si es un username, obtenerlo directamente por username.
    4. Si todo falla, lanzar excepción (que será capturada en init_bot).
    """
    try:
        # Estrategia 1: Obtener el chat directamente con el valor proporcionado
        chat = await bot.get_chat(BIN_CHANNEL)
        logger.info(f"✅ Canal encontrado: {chat.title} (ID: {chat.id})")
        return chat.id
    except PeerIdInvalid:
        logger.warning("⚠️ No se pudo obtener el canal por ID directo. Intentando con diálogos...")
        # Estrategia 2: Recorrer los diálogos para actualizar la caché
        async for dialog in bot.get_dialogs():
            if str(dialog.chat.id) == str(BIN_CHANNEL) or dialog.chat.username == BIN_CHANNEL:
                logger.info(f"✅ Canal encontrado en diálogos: {dialog.chat.title} (ID: {dialog.chat.id})")
                return dialog.chat.id
        # Estrategia 3: Si el valor parece un username (no empieza con -), intentar obtenerlo así
        if not BIN_CHANNEL.startswith('-'):
            try:
                chat = await bot.get_chat(BIN_CHANNEL)
                logger.info(f"✅ Canal encontrado por username: {chat.title} (ID: {chat.id})")
                return chat.id
            except Exception as e:
                logger.error(f"Error al obtener por username: {e}")
        # Si todo falla, lanzar excepción
        raise ValueError("No se pudo encontrar el canal. Verifica el ID o username.")
    except RPCError as e:
        logger.error(f"Error de RPC al obtener el canal: {e}")
        raise
    except Exception as e:
        logger.error(f"Error inesperado: {e}")
        raise

# --- Inicialización del bot ---
async def init_bot():
    """Inicializa el bot y obtiene el ID del canal. Si falla, no crashea."""
    async with bot:
        try:
            channel_id = await get_channel_id()
            # Guardar el ID en el objeto bot para usarlo después
            bot.CHANNEL_ID = channel_id
            logger.info(f"✅ Canal configurado correctamente con ID: {channel_id}")
        except Exception as e:
            logger.error(f"❌ Error crítico al configurar el canal: {e}")
            logger.error("   El bot seguirá funcionando, pero no podrá procesar archivos.")
            bot.CHANNEL_ID = None  # Indicar que no está configurado

# --- Manejador de archivos ---
@bot.on_message(filters.private & (filters.document | filters.video | filters.audio | filters.photo))
async def file_handler(client: Client, message: Message):
    # Verificar si el canal está configurado
    if not hasattr(client, 'CHANNEL_ID') or client.CHANNEL_ID is None:
        await message.reply_text(
            "❌ El bot no está configurado correctamente. Contacta al administrador."
        )
        return

    try:
        # Reenviar el archivo al canal de almacenamiento
        file_message = await message.forward(client.CHANNEL_ID)
        file_id = file_message.id

        # Generar enlace de descarga
        download_link = f"{BASE_URL}/download/{client.CHANNEL_ID}/{file_id}"

        # Responder al usuario
        await message.reply_text(
            f"✅ ¡Enlace generado!\n\n"
            f"🔗 {download_link}\n\n"
            f"📥 Descarga reanudable con JDownloader o cualquier gestor.\n"
            f"⚡ El enlace es permanente mientras el archivo exista."
        )
        logger.info(f"Archivo procesado para usuario {message.from_user.id}, enlace: {download_link}")

    except PeerIdInvalid:
        logger.error("Error: Peer ID inválido al procesar archivo.")
        await message.reply_text(
            "❌ Error: El bot no tiene acceso al canal de almacenamiento. Verifica permisos."
        )
    except RPCError as e:
        logger.error(f"Error de Telegram: {e}")
        await message.reply_text("❌ Error al comunicarse con Telegram. Intenta de nuevo.")
    except Exception as e:
        logger.error(f"Error inesperado: {e}", exc_info=True)
        await message.reply_text("❌ Ocurrió un error inesperado. Intenta de nuevo.")

# --- Servidor web para servir archivos ---
app = web.Application()
app.router.add_get('/download/{channel_id}/{message_id}', routes.serve_file)

# Endpoint de salud para Railway
async def health_check(request):
    return web.Response(text="OK", status=200)

app.router.add_get('/health', health_check)

async def start_web_server():
    """Inicia el servidor web en el puerto asignado por Railway."""
    port = int(os.environ.get("PORT", 8000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info(f"Servidor web iniciado en el puerto {port}")

# --- Punto de entrada ---
if __name__ == "__main__":
    loop = asyncio.get_event_loop()

    # 1. Inicializar el bot (obtener el canal)
    loop.run_until_complete(init_bot())

    # 2. Iniciar el servidor web
    loop.create_task(start_web_server())

    # 3. Iniciar el bot (long polling)
    logger.info("🚀 Bot iniciado correctamente")
    bot.run()