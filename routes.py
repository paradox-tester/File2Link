"""HTTP file streaming from Telegram through aiohttp.

The Telegram client is owned by bot.py and is kept connected for the lifetime of
this process. Files are streamed chunk-by-chunk with Pyrogram's stream_media()
so the whole file is never loaded into RAM.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from typing import Optional
from urllib.parse import quote

from aiohttp import web
from pyrogram import Client
from pyrogram.errors import RPCError

logger = logging.getLogger(__name__)

# Pyrogram stream_media() uses chunks up to 1 MiB. Keeping this constant aligned
# with the documented maximum lets HTTP byte ranges map safely to Telegram chunks.
CHUNK_SIZE = 1024 * 1024

# Prevent an abusive client from opening an unbounded number of Telegram streams.
MAX_CONCURRENT_DOWNLOADS = 2
_DOWNLOAD_SEMAPHORE = None

# Small bounded metadata cache. It avoids repeated get_messages() calls when a
# download manager makes several Range/HEAD requests for the same file.
_METADATA_CACHE: "OrderedDict[tuple[int, int], tuple[object, int, str, str]]" = OrderedDict()
_METADATA_CACHE_SIZE = 256

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def configure(*, client: Client, channel_id: int, max_concurrent_downloads: int = MAX_CONCURRENT_DOWNLOADS) -> None:
    """Configure the HTTP layer with the already-running Telegram client."""
    global _DOWNLOAD_SEMAPHORE
    _DOWNLOAD_SEMAPHORE = asyncio.Semaphore(max(1, int(max_concurrent_downloads)))
    client.FILE2LINK_CHANNEL_ID = int(channel_id)


async def _get_media_message(client: Client, channel_id: int, message_id: int):
    key = (channel_id, message_id)
    cached = _METADATA_CACHE.get(key)
    if cached is not None:
        _METADATA_CACHE.move_to_end(key)
        return cached

    msg = await client.get_messages(channel_id, message_id, replies=0)
    if not msg or not msg.media:
        return None

    media = msg.document or msg.video or msg.audio or msg.voice or msg.animation or msg.video_note
    if media is None and msg.photo:
        media = msg.photo

    if media is None:
        return None

    file_size = getattr(media, "file_size", None)
    if file_size is None:
        # Telegram normally supplies file_size for downloadable media. If it is
        # absent, streaming can still work, but HTTP Range/Content-Length cannot
        # be implemented correctly, so fail rather than emit a corrupt response.
        raise web.HTTPNotImplemented(text="El medio no expone un tamaño descargable.")

    file_name = getattr(media, "file_name", None) or f"archivo_{message_id}.bin"
    mime_type = getattr(media, "mime_type", None) or "application/octet-stream"
    value = (msg, int(file_size), str(file_name), str(mime_type))
    _METADATA_CACHE[key] = value
    _METADATA_CACHE.move_to_end(key)
    while len(_METADATA_CACHE) > _METADATA_CACHE_SIZE:
        _METADATA_CACHE.popitem(last=False)
    return value


def _parse_range(value: Optional[str], size: int) -> tuple[int, int, int]:
    """Return (start, end, status), supporting standard single byte ranges."""
    if not value:
        return 0, size - 1, 200

    # Multiple ranges require multipart/byteranges. We deliberately reject them
    # rather than silently returning invalid content to download managers.
    match = _RANGE_RE.fullmatch(value.strip())
    if not match:
        raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})

    first, last = match.groups()
    if not first and not last:
        raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})

    if first:
        start = int(first)
        if start >= size:
            raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})
        end = int(last) if last else size - 1
        if end < start:
            raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})
        end = min(end, size - 1)
    else:
        # Suffix range: bytes=-N
        suffix = int(last)
        if suffix <= 0:
            raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})
        start = max(0, size - suffix)
        end = size - 1

    return start, end, 206


def _content_disposition(filename: str) -> str:
    """Create a safe Content-Disposition supporting Unicode filenames."""
    fallback = filename.encode("ascii", "ignore").decode("ascii") or "archivo.bin"
    fallback = fallback.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="!#$&+-.^_`|~")}'


async def serve_file(request: web.Request) -> web.StreamResponse:
    client: Client = request.app["telegram_client"]
    configured_channel = int(request.app["channel_id"])

    try:
        channel_id = int(request.match_info["channel_id"])
        message_id = int(request.match_info["message_id"])
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Identificador de archivo inválido.")

    if channel_id != configured_channel:
        raise web.HTTPNotFound(text="Archivo no encontrado")

    try:
        metadata = await _get_media_message(client, channel_id, message_id)
    except RPCError:
        logger.exception("Telegram RPC error reading %s/%s", channel_id, message_id)
        raise web.HTTPBadGateway(text="Telegram no pudo proporcionar el archivo.")
    except web.HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error reading %s/%s", channel_id, message_id)
        raise web.HTTPInternalServerError(text="Error interno del servidor")

    if metadata is None:
        raise web.HTTPNotFound(text="Archivo no encontrado")

    msg, file_size, file_name, mime_type = metadata
    try:
        start, end, status = _parse_range(request.headers.get("Range"), file_size)
    except web.HTTPRequestRangeNotSatisfiable:
        raise

    content_length = end - start + 1
    headers = {
        "Content-Type": mime_type,
        "Content-Disposition": _content_disposition(file_name),
        "Content-Length": str(content_length),
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    # HEAD is useful to download managers and avoids opening a Telegram stream.
    if request.method == "HEAD":
        return web.Response(status=status, headers=headers)

    if _DOWNLOAD_SEMAPHORE is None:
        raise web.HTTPServiceUnavailable(text="Servidor aún no está listo.")

    response = web.StreamResponse(status=status, headers=headers)
    await response.prepare(request)

    first_chunk = start // CHUNK_SIZE
    first_offset = start % CHUNK_SIZE
    chunks_needed = ((end + 1 + CHUNK_SIZE - 1) // CHUNK_SIZE) - first_chunk

    try:
        async with _DOWNLOAD_SEMAPHORE:
            chunk_index = 0
            bytes_remaining = content_length
            async for chunk in client.stream_media(msg, offset=first_chunk, limit=chunks_needed):
                if not chunk:
                    continue

                if chunk_index == 0 and first_offset:
                    chunk = chunk[first_offset:]

                if len(chunk) > bytes_remaining:
                    chunk = chunk[:bytes_remaining]

                if chunk:
                    await response.write(chunk)
                    bytes_remaining -= len(chunk)

                chunk_index += 1
                if bytes_remaining <= 0:
                    break

            if bytes_remaining > 0:
                logger.warning(
                    "Telegram stream ended early for %s/%s: %d bytes missing",
                    channel_id,
                    message_id,
                    bytes_remaining,
                )
                # Headers/body are already partially sent, so the only safe
                # action is to close the connection instead of fabricating data.
                response.force_close()
                return response

        await response.write_eof()
        return response

    except (ConnectionResetError, BrokenPipeError) as exc:
        logger.info("Client disconnected while downloading %s/%s: %s", channel_id, message_id, exc)
        response.force_close()
        return response
    except asyncio.CancelledError:
        response.force_close()
        raise
    except RPCError:
        logger.exception("Telegram stream error for %s/%s", channel_id, message_id)
        response.force_close()
        return response
    except Exception:
        logger.exception("Unexpected streaming error for %s/%s", channel_id, message_id)
        response.force_close()
        return response
