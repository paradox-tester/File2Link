import os
import logging
import asyncio
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import Message
import routes  # Importa el módulo con el manejador de descargas

# --- Configuración desde variables de entorno ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BIN_CHANNEL = int(os.environ.get("BIN_CHANNEL"))  # ID del canal privado (negativo)
BASE_URL = os.environ.get("BASE_URL")  # Ej: https://tu-app.railway.app

# --- Inicializar el bot ---
bot = Client(
    "file-to-link-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# --- Configurar logging ---
logging.basicConfig(level=logging.INFO)

# --- Manejador de archivos ---
@bot.on_message(filters.private & (filters.document | filters.video | filters.audio | filters.photo))
async def file_handler(client: Client, message: Message):
    try:
        # 1. Reenviar el archivo al canal de almacenamiento
        file_message = await message.forward(BIN_CHANNEL)
        file_id = file_message.id

        # 2. Generar enlace de descarga
        download_link = f"{BASE_URL}/download/{BIN_CHANNEL}/{file_id}"

        # 3. Responder al usuario
        await message.reply_text(
            f"✅ ¡Enlace generado!\n\n"
            f"🔗 {download_link}\n\n"
            f"📥 Descarga reanudable con JDownloader o cualquier gestor.\n"
            f"⚡ El enlace es permanente mientras el archivo exista."
        )

    except Exception as e:
        logging.error(f"Error al procesar archivo: {e}")
        await message.reply_text("❌ Ocurrió un error. Intenta de nuevo.")

# --- Servidor web para servir archivos ---
app = web.Application()
app.router.add_get('/download/{channel_id}/{message_id}', routes.serve_file)

# Endpoint de salud para Railway (evita que el servicio se duerma)
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
    logging.info(f"Servidor web iniciado en el puerto {port}")

# --- Punto de entrada ---
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(start_web_server())
    # Ejecutar el bot (long polling)
    bot.run()