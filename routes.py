import os
import logging
from aiohttp import web
from pyrogram import Client
from pyrogram.errors import RPCError

# --- Configuración (misma que en bot.py) ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Cliente de Pyrogram para acceder al canal (independiente)
bot = Client("file-server", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

async def serve_file(request):
    """Endpoint que sirve el archivo desde Telegram con soporte de Range (reanudable)."""
    channel_id = int(request.match_info['channel_id'])
    message_id = int(request.match_info['message_id'])

    try:
        async with bot:
            msg = await bot.get_messages(channel_id, message_id)

        if not msg or not msg.document:
            return web.Response(status=404, text="Archivo no encontrado")

        file = msg.document
        file_name = file.file_name or "archivo.bin"
        file_size = file.file_size

        # --- Manejo de Range Requests (para reanudar descargas) ---
        range_header = request.headers.get('Range')
        start, end = 0, file_size - 1

        if range_header:
            range_value = range_header.split('=')[1]
            if '-' in range_value:
                parts = range_value.split('-')
                start = int(parts[0]) if parts[0] else 0
                end = int(parts[1]) if parts[1] else file_size - 1

        # --- Descargar el archivo completo (simplificado) ---
        # Para producción, se podría descargar solo el rango, pero con in_memory es más sencillo.
        async with bot:
            file_data = await bot.download_media(msg, in_memory=True)

        # --- Responder con el rango solicitado ---
        headers = {
            'Content-Type': 'application/octet-stream',
            'Content-Disposition': f'attachment; filename="{file_name}"',
            'Content-Length': str(end - start + 1),
            'Accept-Ranges': 'bytes',
        }

        if range_header:
            headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
            status = 206  # Partial Content
        else:
            status = 200

        return web.Response(
            body=file_data[start:end+1],
            status=status,
            headers=headers
        )

    except RPCError as e:
        logging.error(f"Error de Telegram: {e}")
        return web.Response(status=500, text="Error al obtener el archivo")
    except Exception as e:
        logging.error(f"Error inesperado: {e}")
        return web.Response(status=500, text="Error interno del servidor")