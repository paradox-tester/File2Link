import os
import logging
import asyncio
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import RPCError, PeerIdInvalid
import routes

# --- Configuración desde variables de entorno ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BIN_CHANNEL = int(os.environ.get("BIN_CHANNEL"))
BASE_URL = os.environ.get("BASE_URL")

# --- Inicializar el bot ---
bot = Client(
    "file-to-link-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# --- Configurar logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Inicialización: Forzar sincronización del canal ---
async def init_bot():
    async with bot:
        try:
            logger.info(f"Verificando acceso al canal {BIN_CHANNEL}...")
            channel = await bot.get_chat(BIN_CHANNEL)
            logger.info(f"✅ Canal encontrado: {channel.title} (ID: {channel.id})")
            
            # Verificar si el bot es administrador
            if not hasattr(channel, 'permissions') or not channel.permissions:
                # Si no tiene permisos, puede que el bot solo sea miembro
                logger.warning("⚠️ El bot podría no ser administrador del canal")
            
            # Obtener el enlace del canal (opcional)
            try:
                invite_link = await bot.export_chat_invite_link(BIN_CHANNEL)
                logger.info(f"Enlace del canal: {invite_link}")
            except:
                logger.info("No se pudo obtener enlace (probablemente el bot no tiene permisos para exportar)")
                
            logger.info("✅ Canal sincronizado correctamente")
            
        except RPCError as e:
            logger.error(f"❌ Error al obtener el canal: {e}")
            logger.error(f"   Asegúrate de que el bot sea ADMINISTRADOR del canal")
            logger.error(f"   El ID debe ser un número negativo (ej: -1001234567890)")
            raise
        except Exception as e:
            logger.error(f"❌ Error inesperado: {e}")
            raise

# --- Manejador de archivos ---
@bot.on_message(filters.private & (filters.document | filters.video | filters.audio | filters.photo))
async def file_handler(client: Client, message: Message):
    try:
        logger.info(f"Archivo recibido de {message.from_user.id}")
        
        # 1. Reenviar el archivo al canal de almacenamiento
        file_message = await message.forward(BIN_CHANNEL)
        file_id = file_message.id
        logger.info(f"Archivo reenviado con ID: {file_id}")

        # 2. Generar enlace de descarga
        download_link = f"{BASE_URL}/download/{BIN_CHANNEL}/{file_id}"

        # 3. Responder al usuario
        await message.reply_text(
            f"✅ ¡Enlace generado!\n\n"
            f"🔗 {download_link}\n\n"
            f"📥 Descarga reanudable con JDownloader o cualquier gestor.\n"
            f"⚡ El enlace es permanente mientras el archivo exista."
        )
        logger.info(f"Enlace enviado: {download_link}")

    except PeerIdInvalid as e:
        logger.error(f"❌ Error: Peer ID inválido - {e}")
        logger.error("   Esto significa que Pyrogram no tiene acceso al canal.")
        logger.error("   Reinicia el bot para forzar la sincronización de la sesión.")
        await message.reply_text("❌ Error: El bot no tiene acceso al canal de almacenamiento. Reiniciando sesión...")
        
        # Forzar reinicio de la sesión para que Pyrogram re-sincronice
        await client.stop()
        await client.start()
        logger.info("Sesión reiniciada correctamente")
        
    except Exception as e:
        logger.error(f"Error inesperado: {e}", exc_info=True)
        await message.reply_text("❌ Ocurrió un error. Intenta de nuevo.")

# --- Servidor web ---
app = web.Application()
app.router.add_get('/download/{channel_id}/{message_id}', routes.serve_file)

async def health_check(request):
    return web.Response(text="OK", status=200)

app.router.add_get('/health', health_check)

async def start_web_server():
    port = int(os.environ.get("PORT", 8000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info(f"Servidor web iniciado en el puerto {port}")

# --- Punto de entrada ---
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    
    # 1. Inicializar (sincronizar canal)
    loop.run_until_complete(init_bot())
    
    # 2. Iniciar servidor web
    loop.create_task(start_web_server())
    
    # 3. Iniciar el bot
    logger.info("🚀 Bot iniciado correctamente")
    bot.run()