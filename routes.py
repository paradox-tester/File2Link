from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import sqlite3
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from aiohttp import web
from cryptography.fernet import Fernet, InvalidToken
from pyrogram import Client
from pyrogram.errors import RPCError

logger = logging.getLogger("file2link.routes")

CHUNK_SIZE = 1024 * 1024
DEFAULT_MAX_CONCURRENT_DOWNLOADS = 2
DEFAULT_TRAFFIC_LIMIT_BYTES = 95_000_000_000  # 95 GB decimal; deliberate safety cap.
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

_download_semaphore: asyncio.Semaphore | None = None
_traffic_lock: asyncio.Lock | None = None
_client: Client | None = None
_channel_id = 0
_fernet: Fernet | None = None
_link_secret = b""
_link_ttl_seconds = 0
_traffic_limit_bytes = DEFAULT_TRAFFIC_LIMIT_BYTES
_traffic_db_path = "traffic.db"
_traffic_db: sqlite3.Connection | None = None
_reset_day = 1
_active_downloads = 0
_reserved_bytes = 0
_boot_id = uuid.uuid4().hex
_COMPLETED_LINKS: set[str] = set()
_DOWNLOAD_RANGES: dict[str, list[tuple[int, int]]] = {}
_METADATA_CACHE: "OrderedDict[tuple[int, int], tuple[object, int, str, str]]" = OrderedDict()
_METADATA_CACHE_SIZE = 256


def _fernet_key(secret: bytes) -> bytes:
    # Fernet requires a 32-byte key encoded as urlsafe base64.
    return base64.urlsafe_b64encode(hashlib.sha256(secret).digest())


def create_file_token(channel_id: int, message_id: int, secret: bytes) -> str:
    """Create a standard Fernet token containing Telegram IDs, but never exposing them."""
    if not secret:
        raise ValueError("LINK_SECRET must not be empty")
    f = Fernet(_fernet_key(secret))
    payload = f"{int(channel_id)}:{int(message_id)}:{int(time.time())}".encode()
    return f.encrypt(payload).decode()


def decode_file_token(token: str, secret: bytes) -> tuple[int, int, int]:
    if not secret:
        raise ValueError("missing secret")
    try:
        payload = Fernet(_fernet_key(secret)).decrypt(token.encode(), ttl=None)
        c, m, t = payload.decode().split(":", 2)
        return int(c), int(m), int(t)
    except (InvalidToken, ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("invalid token") from exc


def configure(
    *,
    client: Client,
    channel_id: int,
    link_secret: bytes,
    max_concurrent_downloads: int = DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    traffic_limit_bytes: int = DEFAULT_TRAFFIC_LIMIT_BYTES,
    traffic_db_path: str = "traffic.db",
) -> None:
    global _download_semaphore, _traffic_lock, _client, _channel_id, _fernet, _link_secret
    global _link_ttl_seconds, _traffic_limit_bytes, _traffic_db_path, _traffic_db, _reset_day

    if not link_secret:
        raise RuntimeError("LINK_SECRET must be configured")
    _download_semaphore = asyncio.Semaphore(max(1, int(max_concurrent_downloads)))
    _traffic_lock = asyncio.Lock()
    _client = client
    _channel_id = int(channel_id)
    _link_secret = bytes(link_secret)
    _fernet = Fernet(_fernet_key(link_secret))
    _link_ttl_seconds = max(0, int(os.getenv("LINK_TTL_SECONDS", "0")))
    _traffic_limit_bytes = max(1, int(traffic_limit_bytes))
    _reset_day = min(28, max(1, int(os.getenv("TRAFFIC_RESET_DAY", "1"))))
    _traffic_db_path = traffic_db_path
    Path(_traffic_db_path).parent.mkdir(parents=True, exist_ok=True)
    _traffic_db = sqlite3.connect(_traffic_db_path, check_same_thread=False)
    _traffic_db.execute(
        "CREATE TABLE IF NOT EXISTS traffic_month (month TEXT PRIMARY KEY, egress_bytes INTEGER NOT NULL DEFAULT 0)"
    )
    _traffic_db.commit()
    _ensure_month_row()


def _month() -> str:
    now = time.localtime()
    if now.tm_mday >= _reset_day:
        return f"{now.tm_year:04d}-{now.tm_mon:02d}"
    month = now.tm_mon - 1
    year = now.tm_year
    if month == 0:
        month, year = 12, year - 1
    return f"{year:04d}-{month:02d}"


def _ensure_month_row() -> None:
    assert _traffic_db is not None
    _traffic_db.execute("INSERT OR IGNORE INTO traffic_month(month, egress_bytes) VALUES(?, 0)", (_month(),))
    _traffic_db.commit()


def _traffic_bytes_sync() -> int:
    assert _traffic_db is not None
    _ensure_month_row()
    row = _traffic_db.execute("SELECT egress_bytes FROM traffic_month WHERE month=?", (_month(),)).fetchone()
    return int(row[0] if row else 0)


async def _add_traffic(n: int) -> None:
    if n <= 0:
        return
    assert _traffic_lock is not None and _traffic_db is not None
    async with _traffic_lock:
        _traffic_db.execute(
            "UPDATE traffic_month SET egress_bytes=egress_bytes+? WHERE month=?", (int(n), _month())
        )
        _traffic_db.commit()


def traffic_bytes() -> int:
    return _traffic_bytes_sync()


async def _reserve_response(size: int) -> bool:
    global _reserved_bytes
    assert _traffic_lock is not None
    async with _traffic_lock:
        used = traffic_bytes()
        if used + _reserved_bytes + size > _traffic_limit_bytes:
            return False
        _reserved_bytes += size
        return True


async def _release_reservation(reserved: int) -> None:
    global _reserved_bytes
    assert _traffic_lock is not None
    async with _traffic_lock:
        _reserved_bytes = max(0, _reserved_bytes - reserved)


async def _get_media_message(client: Client, channel_id: int, message_id: int):
    key = (channel_id, message_id)
    cached = _METADATA_CACHE.get(key)
    if cached is not None:
        _METADATA_CACHE.move_to_end(key)
        return cached
    msg = await client.get_messages(channel_id, message_id, replies=0)
    if not msg or not msg.media:
        return None
    media = msg.document or msg.video or msg.audio or msg.voice or msg.animation or msg.video_note or msg.photo
    if media is None:
        return None
    file_size = getattr(media, "file_size", None)
    if file_size is None:
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
    if size <= 0:
        return 0, -1, 200
    if not value:
        return 0, size - 1, 200
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
        suffix = int(last)
        if suffix <= 0:
            raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})
        start, end = max(0, size - suffix), size - 1
    return start, end, 206


def _content_disposition(filename: str) -> str:
    fallback = filename.encode("ascii", "ignore").decode("ascii") or "archivo.bin"
    fallback = fallback.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="!#$&+-.^_`|~")}'


async def _auth_control(request: web.Request) -> None:
    supplied = request.headers.get("X-Control-Token", "")
    expected = os.getenv("CONTROL_TOKEN", "")
    if not expected or not hmac_compare(supplied, expected):
        raise web.HTTPUnauthorized()


def hmac_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


async def traffic_status(request: web.Request) -> web.Response:
    await _auth_control(request)
    used = traffic_bytes()
    return web.json_response(
        {
            "month": _month(),
            "egress_bytes": used,
            "egress_gb": used / 1_000_000_000,
            "limit_bytes": _traffic_limit_bytes,
            "limit_gb": _traffic_limit_bytes / 1_000_000_000,
            "boot_id": _boot_id,
            "disabled": os.getenv("WORKER_DISABLED", "0") == "1",
            "active_downloads": _active_downloads,
            "reserved_bytes": _reserved_bytes,
        }
    )


async def control_status(request: web.Request) -> web.Response:
    await _auth_control(request)
    used = traffic_bytes()
    return web.json_response(
        {
            "month": _month(),
            "egress_bytes": used,
            "egress_gb": used / 1_000_000_000,
            "limit_bytes": _traffic_limit_bytes,
            "limit_gb": _traffic_limit_bytes / 1_000_000_000,
            "boot_id": _boot_id,
            "disabled": os.getenv("WORKER_DISABLED", "0") == "1",
            "active_downloads": _active_downloads,
            "reserved_bytes": _reserved_bytes,
        }
    )


async def control_set_disabled(request: web.Request) -> web.Response:
    await _auth_control(request)
    data = await request.json()
    disabled = bool(data.get("disabled"))
    os.environ["WORKER_DISABLED"] = "1" if disabled else "0"
    return web.json_response({"disabled": disabled})


async def control_sync_usage(request: web.Request) -> web.Response:
    """Authoritative monthly usage sync from the CGNAT orchestrator.

    The worker never lowers its own counter. This protects against an orchestrator
    restart or stale poll accidentally reducing the local safety counter.
    """
    await _auth_control(request)
    data = await request.json()
    month = str(data.get("month", ""))
    value = int(data.get("egress_bytes", 0))
    if month != _month() or value < 0:
        raise web.HTTPBadRequest(text="Invalid monthly usage payload")
    assert _traffic_lock is not None and _traffic_db is not None
    async with _traffic_lock:
        current = traffic_bytes()
        if value > current:
            _traffic_db.execute("UPDATE traffic_month SET egress_bytes=? WHERE month=?", (value, month))
            _traffic_db.commit()
    return web.json_response({"egress_bytes": traffic_bytes()})


async def _record_range_and_invalidate(client: Client, token_key: str, channel_id: int, message_id: int, start: int, end: int, size: int) -> None:
    if token_key in _COMPLETED_LINKS or _traffic_lock is None:
        return
    complete = False
    async with _traffic_lock:
        if token_key in _COMPLETED_LINKS:
            return
        ranges = _DOWNLOAD_RANGES.setdefault(token_key, [])
        ranges.append((start, end))
        ranges.sort()
        merged: list[list[int]] = []
        for a, b in ranges:
            if not merged or a > merged[-1][1] + 1:
                merged.append([a, b])
            else:
                merged[-1][1] = max(merged[-1][1], b)
        _DOWNLOAD_RANGES[token_key] = [(a, b) for a, b in merged]
        complete = bool(merged and len(merged) == 1 and merged[0][0] == 0 and merged[0][1] >= size - 1)
        if complete:
            _COMPLETED_LINKS.add(token_key)
            _DOWNLOAD_RANGES.pop(token_key, None)
    if complete:
        try:
            await client.delete_messages(channel_id, message_id)
            _METADATA_CACHE.pop((channel_id, message_id), None)
            logger.info("Link invalidated after complete download: %s/%s", channel_id, message_id)
        except Exception:
            # The token remains invalid even if Telegram deletion temporarily fails.
            logger.exception("Could not delete storage message %s/%s after completion", channel_id, message_id)


async def serve_file(request: web.Request) -> web.StreamResponse:
    global _active_downloads
    if os.getenv("WORKER_DISABLED", "0") == "1":
        raise web.HTTPServiceUnavailable(text="Esta instancia no está disponible temporalmente.")
    if _client is None or _fernet is None:
        raise web.HTTPServiceUnavailable(text="Servidor aún no está listo.")

    token = request.match_info["token"]
    try:
        channel_id, message_id, created_at = decode_file_token(token, _link_secret)
    except ValueError:
        raise web.HTTPNotFound(text="Archivo no encontrado")

    if channel_id != _channel_id:
        raise web.HTTPNotFound(text="Archivo no encontrado")
    if _link_ttl_seconds and int(time.time()) - created_at > _link_ttl_seconds:
        raise web.HTTPGone(text="El enlace ha expirado.")
    if token in _COMPLETED_LINKS:
        raise web.HTTPNotFound(text="Archivo no encontrado")

    try:
        metadata = await _get_media_message(_client, channel_id, message_id)
    except RPCError:
        logger.exception("Telegram RPC error reading %s/%s", channel_id, message_id)
        raise web.HTTPBadGateway(text="Telegram no pudo proporcionar el archivo.")
    if metadata is None:
        raise web.HTTPNotFound(text="Archivo no encontrado")

    msg, file_size, file_name, mime_type = metadata
    start, end, status = _parse_range(request.headers.get("Range"), file_size)
    content_length = max(0, end - start + 1)
    headers = {
        "Content-Type": mime_type,
        "Content-Disposition": _content_disposition(file_name),
        "Content-Length": str(content_length),
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store, max-age=0",
        "X-Content-Type-Options": "nosniff",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    if request.method == "HEAD":
        return web.Response(status=status, headers=headers)
    if _download_semaphore is None:
        raise web.HTTPServiceUnavailable(text="Servidor aún no está listo.")

    # Reserve the entire HTTP response before streaming. This makes concurrent
    # downloads unable to overshoot the 95 GB worker cap.
    if not await _reserve_response(content_length):
        raise web.HTTPTooManyRequests(
            text="Esta instancia alcanzó su límite de tráfico mensual."
        )

    response = web.StreamResponse(status=status, headers=headers)
    await response.prepare(request)
    reserved = content_length
    sent = 0
    _active_downloads += 1
    try:
        async with _download_semaphore:
            first_chunk = start // CHUNK_SIZE
            first_offset = start % CHUNK_SIZE
            chunks_needed = ((end + 1 + CHUNK_SIZE - 1) // CHUNK_SIZE) - first_chunk
            async for chunk in _client.stream_media(msg, offset=first_chunk, limit=chunks_needed):
                if not chunk:
                    continue
                if sent == 0 and first_offset:
                    chunk = chunk[first_offset:]
                remaining = content_length - sent
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                if chunk:
                    await response.write(chunk)
                    await _add_traffic(len(chunk))
                    sent += len(chunk)
                if sent >= content_length:
                    break

            if sent != content_length:
                response.force_close()
                logger.warning("Telegram stream ended early for %s/%s: sent=%d expected=%d", channel_id, message_id, sent, content_length)
                return response

        await response.write_eof()
        await _record_range_and_invalidate(_client, token, channel_id, message_id, start, end, file_size)
        return response
    except asyncio.CancelledError:
        response.force_close()
        raise
    except (ConnectionResetError, BrokenPipeError):
        response.force_close()
        return response
    except RPCError:
        logger.exception("Telegram stream error for %s/%s", channel_id, message_id)
        response.force_close()
        return response
    except Exception:
        logger.exception("Unexpected streaming error for %s/%s", channel_id, message_id)
        response.force_close()
        return response
    finally:
        _active_downloads = max(0, _active_downloads - 1)
        await _release_reservation(reserved)
