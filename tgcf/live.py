# tgcf/live.py
#
# Live forwarder/copy con soporte de ÁLBUMES (media groups).
# - Usa Telethon events.Album para recibir grupos y los reenvía/copía como álbum.
# - Separa por tipo (foto/video/doc) porque Telegram no permite mezclar tipos en un mismo grupo.
# - En copy: client.send_file(..., files=[...]) crea el media group (2–10 ítems). Se trocea en bloques de 10.
# - En forward: event.forward_to(dest) preserva el álbum si el origen lo permite (contenido no protegido).
#
# Configuración:
#   - TGCF_CONFIG   (opcional) ruta del JSON de config; por defecto: ./tgcf.config.json
#   - TGCF_SESSION  (opcional) string session; si no, usa archivo ./tgcf.session
#   - API_ID / API_HASH (si te autenticas por primera vez o renuevas sesión)
#
# JSON esperado (mínimo):
# {
#   "mode": "live",
#   "copy": true,
#   "forward": false,
#   "from_to": { "@origen": ["@staging"] }
# }
#
# Logs clave:
#   - "Listening sources: [...]"
#   - "ALBUM recibido: src=... gid=... items=..."
#   - "ALBUM enviado: src=... -> dest=... kind=... count=..."
#   - En singles: "Skip single of album gid=..." evita duplicados
#   - Errores con trace completo (fallback a ítems sueltos en álbumes)
#
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Union, Optional

from telethon import events
from telethon.tl.types import Message
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

# --------------------------------
# Logging
# --------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tgcf.live")
logging.getLogger("telethon").setLevel(logging.INFO)

# --------------------------------
# Config
# --------------------------------
@dataclass
class LiveConfig:
    copy: bool = True          # copiar (re-subir) en lugar de forward
    forward: bool = False      # forward nativo (mantiene origen si se permite)
    from_to: Dict[str, List[str]] = None

def _load_config(path: str) -> LiveConfig:
    if not os.path.exists(path):
        log.error("No encuentro config en %s", path)
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return LiveConfig(
        copy=bool(data.get("copy", True)),
        forward=bool(data.get("forward", False)),
        from_to=data.get("from_to", {}) or {},
    )

TGCF_CONFIG = os.getenv("TGCF_CONFIG", "tgcf.config.json")
config = _load_config(TGCF_CONFIG)

# --------------------------------
# Session / Client
# --------------------------------
API_ID = int(os.getenv("API_ID", "0") or "0")
API_HASH = os.getenv("API_HASH")
SESSION_STR = os.getenv("TGCF_SESSION")  # si está presente, usamos string session
SESSION_FILE = os.getenv("TGCF_SESSION_FILE", "tgcf.session")

def _make_client() -> TelegramClient:
    if SESSION_STR:
        if not API_ID or not API_HASH:
            log.error("Necesitas API_ID y API_HASH para usar TGCF_SESSION")
            sys.exit(1)
        log.info("Usando TGCF_SESSION (string session)")
        return TelegramClient(StringSession(SESSION_STR), API_ID, API_HASH)
    # archivo de sesión
    if API_ID and API_HASH:
        log.info("Usando archivo de sesión %s", SESSION_FILE)
        return TelegramClient(SESSION_FILE, API_ID, API_HASH)
    # Si no hay API_ID/Hash, igual ya existe el archivo de sesión con credenciales guardadas
    log.info("Intentando abrir sesión existente en %s (sin API_ID/API_HASH)", SESSION_FILE)
    return TelegramClient(SESSION_FILE, API_ID or 0, API_HASH or "")

client = _make_client()

# --------------------------------
# Resolución de chats (@username / -100id) a IDs numéricos
# --------------------------------
async def _resolve_chat_id(identifier: str) -> int:
    """
    Acepta @username, t.me/..., o -100xxxxxxxxxx y devuelve chat_id numérico.
    """
    identifier = identifier.strip()
    if identifier.startswith("t.me/") or identifier.startswith("https://t.me/"):
        # Telethon acepta directamente el URL también
        ent = await client.get_entity(identifier)
        return int(getattr(ent, "id"))
    if identifier.startswith("-100") or identifier.lstrip("-").isdigit():
        return int(identifier)
    if identifier.startswith("@"):
        ent = await client.get_entity(identifier)
        return int(getattr(ent, "id"))
    # fallback: intenta como nombre/username
    ent = await client.get_entity(identifier)
    return int(getattr(ent, "id"))

async def _resolve_from_to(from_to_conf: Dict[str, List[str]]) -> Dict[int, List[int]]:
    out: Dict[int, List[int]] = {}
    for src, dests in from_to_conf.items():
        src_id = await _resolve_chat_id(src)
        out[src_id] = []
        for d in dests:
            out[src_id].append(await _resolve_chat_id(d))
    return out

# --------------------------------
# Utilidades de envío
# --------------------------------
def _bucket_kind(m: Message) -> str:
    # Telegram no permite mezclar arbitrariamente tipos en un mismo media group
    if getattr(m, "photo", None):
        return "photo"
    mt = (getattr(getattr(m, "document", None), "mime_type", "") or "")
    return "video" if mt.startswith("video/") else "doc"

async def _send_single(dest: int, msg: Message, *, copy: bool):
    """
    Envía un mensaje suelto. Si copy=True, re-sube; si False, forward.
    """
    if copy:
        await client.send_message(
            dest,
            message=msg.message or "",
            file=getattr(msg, "media", None),
            link_preview=getattr(msg, "media", None) is None,  # previsualiza links si es solo texto
        )
    else:
        await client.forward_messages(dest, msg.id, msg.chat_id)

async def _send_album_copy(dest: int, items: List[Message], caption: Optional[str]):
    """
    Envía un álbum (copy) troceando a 10 y separando por tipo.
    """
    # bucket por tipo
    buckets: Dict[str, List[Message]] = {}
    for m in items:
        if getattr(m, "media", None):
            buckets.setdefault(_bucket_kind(m), []).append(m)

    for kind, bucket in buckets.items():
        files = [m.media for m in bucket if getattr(m, "media", None)]
        if not files:
            continue
        cap = caption
        i = 0
        while i < len(files):
            part = files[i : i + 10]  # Telegram: 2–10 ítems por media group
            await client.send_file(dest, part, caption=cap, supports_streaming=True)
            cap = None  # caption solo en el primer grupo
            i += 10

# --------------------------------
# === Handlers ===
# --------------------------------
def attach_handlers(from_to_ids: Dict[int, List[int]], copy_mode: bool, forward_mode: bool):
    """
    Registra handlers para singles y álbumes.
    """
    src_ids = list(from_to_ids.keys())
    log.info("Listening sources: %s", src_ids)

    @client.on(events.NewMessage(chats=src_ids))
    async def on_new_message(event: events.NewMessage.Event):
        chat = event.chat_id
        msg: Message = event.message

        # Evita duplicados: si pertenece a un álbum, lo gestionará `on_album`
        if getattr(msg, "grouped_id", None):
            log.debug("Skip single of album gid=%s mid=%s", msg.grouped_id, msg.id)
            return

        dests = from_to_ids.get(chat, [])
        if not dests:
            return

        for d in dests:
            try:
                await _send_single(d, msg, copy=copy_mode and not forward_mode)
                log.info("SINGLE enviado: from %s -> %s mid=%s", chat, d, msg.id)
            except Exception as e:
                log.exception("Fallo enviando SINGLE from=%s to=%s mid=%s: %s", chat, d, msg.id, e)

    @client.on(events.Album(chats=src_ids))
    async def on_album(event: events.Album.Event):
        src = event.chat_id
        items: List[Message] = list(event.messages)
        gid = getattr(event, "grouped_id", None)
        caption = next((m.message for m in items if m.message), None)
        dests = from_to_ids.get(src, [])

        log.info("ALBUM recibido: src=%s gid=%s items=%d dests=%s", src, gid, len(items), dests)

        if not dests:
            return

        for d in dests:
            try:
                if forward_mode and not copy_mode:
                    # forward del álbum completo (si el origen lo permite)
                    await event.forward_to(d)
                    log.info("ALBUM reenviado (forward): src=%s -> dest=%s gid=%s items=%d", src, d, gid, len(items))
                else:
                    # copy: reconstruye álbum con send_file(files=[...])
                    await _send_album_copy(d, items, caption)
                    log.info("ALBUM enviado (copy): src=%s -> dest=%s gid=%s items=%d", src, d, gid, len(items))
            except Exception as e:
                log.exception("Fallo enviando ALBUM src=%s dest=%s gid=%s: %s", src, d, gid, e)
                # Fallback: no pierdas contenido
                cap = caption
                for idx, m in enumerate(items):
                    try:
                        await client.send_file(d, getattr(m, "media", None), caption=cap if idx == 0 else None, supports_streaming=True)
                    except Exception:
                        log.exception("Fallo enviando ítem suelto del álbum (src=%s dest=%s mid=%s)", src, d, getattr(m, "id", None))
                    cap = None

# --------------------------------
# Main (modo live)
# --------------------------------
async def main():
    await client.connect()
    if not await client.is_user_authorized():
        # Solo si no hay sesión previa; te pedirá login si API_ID/HASH están
        log.warning("Sesión no autorizada. Iniciando flujo de login...")
        await client.send_code_request("ME")  # placeholder; normalmente usas número de teléfono
        # En App Platform, lo normal es traer una string session ya autorizada.

    # Resuelve mapping de @usernames/IDs a IDs numéricos
    from_to_ids = await _resolve_from_to(config.from_to)
    attach_handlers(from_to_ids, copy_mode=config.copy, forward_mode=config.forward)

    log.info("Live mode listo. Esperando eventos…")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
