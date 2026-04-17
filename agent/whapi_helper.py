# agent/whapi_helper.py — Evolution API (migrado desde Whapi)
"""
Funciones para interactuar con Evolution API:
- Obtener chats y mensajes existentes
- Obtener nombres de contactos y avatares
- Descargar y transcribir audios
- Analizar estilo de Fedra
- Auto-clasificar conversaciones
- Enviar mensajes de todo tipo
"""

import os
import logging
import httpx
from anthropic import AsyncAnthropic

logger = logging.getLogger("agentkit")

EVOLUTION_URL = os.getenv("EVOLUTION_URL", "").rstrip("/")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "hefe")

anthropic = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

ETAPAS_VALIDAS = ["nuevo", "respondio", "interesado", "presupuesto", "seguimiento", "cerrado"]


def _h() -> dict:
    return {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}


def _phone(telefono: str) -> str:
    """Extrae el número de teléfono limpio."""
    return telefono.replace("@s.whatsapp.net", "").replace("@c.us", "")


def _jid(telefono: str) -> str:
    """Convierte número a formato JID de WhatsApp."""
    p = _phone(telefono)
    return p if "@" in p else f"{p}@s.whatsapp.net"


def _evo_msg_to_whapi(msg: dict) -> dict:
    """
    Convierte el formato de mensaje de Evolution API al formato compatible con el resto del código.
    Evolution: {key: {remoteJid, fromMe, id}, pushName, message, messageType, messageTimestamp}
    Whapi-compat: {id, from_me, type, text/audio/image/..., timestamp, from_name}
    """
    key = msg.get("key", {})
    message_type = msg.get("messageType", "")
    message = msg.get("message", {})
    msg_id = key.get("id", "")

    result = {
        "id": msg_id,
        "from_me": key.get("fromMe", False),
        "timestamp": msg.get("messageTimestamp", 0),
        "from_name": msg.get("pushName", ""),
        "_raw_evo": msg,  # Conservar raw para descargar media
    }

    if message_type in ("conversation", "extendedTextMessage"):
        result["type"] = "text"
        text = (
            message.get("conversation")
            or message.get("extendedTextMessage", {}).get("text", "")
        )
        result["text"] = {"body": text}

    elif message_type == "imageMessage":
        result["type"] = "image"
        img = message.get("imageMessage", {})
        result["image"] = {
            "id": msg_id,
            "caption": img.get("caption", ""),
            "mime_type": img.get("mimetype", "image/jpeg"),
        }

    elif message_type == "audioMessage":
        result["type"] = "audio"
        audio = message.get("audioMessage", {})
        result["audio"] = {
            "id": msg_id,
            "mime_type": audio.get("mimetype", "audio/ogg; codecs=opus"),
        }

    elif message_type == "videoMessage":
        result["type"] = "video"
        vid = message.get("videoMessage", {})
        result["video"] = {
            "id": msg_id,
            "caption": vid.get("caption", ""),
            "mime_type": vid.get("mimetype", "video/mp4"),
        }

    elif message_type == "documentMessage":
        result["type"] = "document"
        doc = message.get("documentMessage", {})
        result["document"] = {
            "id": msg_id,
            "file_name": doc.get("fileName", "documento"),
            "mime_type": doc.get("mimetype", "application/octet-stream"),
            "caption": doc.get("caption", ""),
        }

    elif message_type == "stickerMessage":
        result["type"] = "sticker"

    else:
        result["type"] = message_type or "unknown"

    return result


async def fetch_chats(count: int = 100) -> list[dict]:
    """Obtiene todos los chats individuales de WhatsApp via Evolution API."""
    if not EVOLUTION_URL:
        return []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{EVOLUTION_URL}/chat/findChats/{EVOLUTION_INSTANCE}",
                json={},
                headers=_h(),
            )
            data = r.json()
            chats_raw = data if isinstance(data, list) else data.get("chats", [])
            # Solo chats individuales (no grupos)
            return [
                c for c in chats_raw
                if c.get("id", "").endswith("@s.whatsapp.net")
            ][:count]
    except Exception as e:
        logger.error(f"Error fetch_chats Evolution: {e}")
        return []


async def fetch_mensajes(chat_id: str, count: int = 50) -> list[dict]:
    """Obtiene los últimos N mensajes de un chat en formato compatible."""
    if not EVOLUTION_URL:
        return []
    jid = _jid(chat_id)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{EVOLUTION_URL}/chat/findMessages/{EVOLUTION_INSTANCE}",
                json={"where": {"key": {"remoteJid": jid}}, "limit": count},
                headers=_h(),
            )
            data = r.json()
            msgs_raw = data if isinstance(data, list) else data.get("messages", [])
            return [_evo_msg_to_whapi(m) for m in msgs_raw]
    except Exception as e:
        logger.error(f"Error fetch_mensajes Evolution {chat_id}: {e}")
        return []


async def fetch_nombre_contacto(telefono: str) -> str:
    """Obtiene el nombre del contacto desde Evolution API."""
    if not EVOLUTION_URL:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{EVOLUTION_URL}/chat/findChatByRemoteJid/{EVOLUTION_INSTANCE}",
                params={"remoteJid": _jid(telefono)},
                headers=_h(),
            )
            if r.status_code == 200:
                data = r.json()
                return data.get("name", "") or ""
    except Exception as e:
        logger.error(f"Error fetch_nombre_contacto Evolution {telefono}: {e}")
    return ""


async def fetch_avatar_contacto(telefono: str) -> str:
    """Obtiene la URL de la foto de perfil del contacto desde Evolution API."""
    if not EVOLUTION_URL:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{EVOLUTION_URL}/chat/fetchProfilePictureUrl/{EVOLUTION_INSTANCE}",
                json={"number": _phone(telefono)},
                headers=_h(),
            )
            if r.status_code == 200:
                data = r.json()
                return data.get("profilePictureUrl", "") or ""
    except Exception as e:
        logger.error(f"Error fetch_avatar Evolution {telefono}: {e}")
    return ""


async def _get_base64_media(message_id: str) -> tuple[bytes | None, str]:
    """
    Descarga media de Evolution API dado un message_id.
    Busca el mensaje completo y llama getBase64FromMediaMessage.
    Retorna (bytes, mime_type).
    """
    if not EVOLUTION_URL:
        return None, ""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # 1. Buscar el mensaje completo por ID
            r = await client.post(
                f"{EVOLUTION_URL}/chat/findMessages/{EVOLUTION_INSTANCE}",
                json={"where": {"key": {"id": message_id}}},
                headers=_h(),
            )
            msgs = r.json()
            msgs_list = msgs if isinstance(msgs, list) else msgs.get("messages", [])
            if not msgs_list:
                logger.warning(f"No se encontró mensaje con ID {message_id}")
                return None, ""

            full_msg = msgs_list[0]

            # 2. Obtener base64 del media
            r2 = await client.post(
                f"{EVOLUTION_URL}/chat/getBase64FromMediaMessage/{EVOLUTION_INSTANCE}",
                json={"message": full_msg, "convertToMp4": False},
                headers=_h(),
            )
            if r2.status_code == 200:
                resp = r2.json()
                import base64
                b64 = resp.get("base64", "")
                mime = resp.get("mimetype", "application/octet-stream")
                if b64:
                    return base64.b64decode(b64), mime
    except Exception as e:
        logger.error(f"Error descargando media Evolution {message_id}: {e}")
    return None, ""


async def descargar_audio(media_id: str) -> bytes | None:
    """Descarga un archivo de audio desde Evolution API dado el message_id."""
    data, _ = await _get_base64_media(media_id)
    return data


async def descargar_media(media_id: str) -> bytes | None:
    """Descarga cualquier archivo de media desde Evolution API dado el message_id."""
    data, _ = await _get_base64_media(media_id)
    return data


async def transcribir_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str | None:
    """
    Transcribe un audio usando Groq Whisper (si GROQ_API_KEY está configurado)
    o OpenAI Whisper (si OPENAI_API_KEY está configurado).
    """
    groq_key = os.getenv("GROQ_API_KEY", "")
    openai_key = os.getenv("OPENAI_API_KEY", "")

    if groq_key:
        try:
            import tempfile
            ext = "ogg" if "ogg" in mime_type else "mp3" if "mp3" in mime_type else "wav"
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
                f.write(audio_bytes)
                tmp_path = f.name
            async with httpx.AsyncClient(timeout=30) as client:
                with open(tmp_path, "rb") as audio_file:
                    r = await client.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {groq_key}"},
                        files={"file": (f"audio.{ext}", audio_file, mime_type)},
                        data={"model": "whisper-large-v3-turbo", "language": "es"},
                    )
                    return r.json().get("text", "")
        except Exception as e:
            logger.error(f"Error transcribiendo con Groq: {e}")

    elif openai_key:
        try:
            import tempfile
            ext = "ogg" if "ogg" in mime_type else "mp3"
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
                f.write(audio_bytes)
                tmp_path = f.name
            async with httpx.AsyncClient(timeout=30) as client:
                with open(tmp_path, "rb") as audio_file:
                    r = await client.post(
                        "https://api.openai.com/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {openai_key}"},
                        files={"file": (f"audio.{ext}", audio_file, mime_type)},
                        data={"model": "whisper-1", "language": "es"},
                    )
                    return r.json().get("text", "")
        except Exception as e:
            logger.error(f"Error transcribiendo con OpenAI: {e}")

    return None


def extraer_texto_mensaje(msg: dict) -> str | None:
    """Extrae el texto de un mensaje normalizado (texto, audio transcripto, caption de imagen)."""
    tipo = msg.get("type", "")
    if tipo == "text":
        return msg.get("text", {}).get("body", "")
    elif tipo == "image":
        return msg.get("image", {}).get("caption", "")
    elif tipo == "video":
        return msg.get("video", {}).get("caption", "")
    return None


async def clasificar_conversacion_con_ia(chat_id: str, nombre: str, mensajes: list[dict]) -> dict:
    """
    Usa Claude para analizar una conversación de WhatsApp y determinar etapa del pipeline.
    """
    lineas = []
    for msg in mensajes[-30:]:
        tipo = msg.get("type", "")
        from_me = msg.get("from_me", False)
        rol = "Fedra" if from_me else "Cliente"

        if tipo == "text":
            texto = msg.get("text", {}).get("body", "")
        elif tipo in ("image", "video"):
            caption = msg.get(tipo, {}).get("caption", "")
            texto = f"[{tipo.upper()}]" + (f": {caption}" if caption else "")
        elif tipo == "audio":
            texto = "[AUDIO]"
        elif tipo == "document":
            texto = "[DOCUMENTO]"
        else:
            texto = f"[{tipo.upper()}]"

        if texto:
            lineas.append(f"{rol}: {texto}")

    if not lineas:
        return {"etapa": "nuevo", "cobro_pendiente": False, "resumen": "Sin mensajes"}

    conversacion_texto = "\n".join(lineas)

    prompt = f"""Analizá esta conversación de WhatsApp de HeFe Uniformes (taller de uniformes) con el contacto "{nombre}".

CONVERSACIÓN:
{conversacion_texto}

Respondé SOLO en JSON con este formato exacto:
{{
  "etapa": "nuevo|respondio|interesado|presupuesto|seguimiento|cerrado",
  "cobro_pendiente": true|false,
  "monto_cobro": "descripción del monto si hay cobro pendiente, sino null",
  "resumen": "1 frase describiendo el estado del cliente"
}}

Criterios para la etapa:
- nuevo: nunca respondió o solo saludó
- respondio: hubo intercambio pero sin interés claro
- interesado: preguntó por productos, precios o quiere saber más
- presupuesto: se le dio un presupuesto o está en negociación
- seguimiento: ya compró o hay un pedido en curso que hay que seguir
- cerrado: compra finalizada y entregada

cobro_pendiente: true si hay deuda, pago pendiente, o mención de dinero adeudado."""

    try:
        response = await anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        import json
        texto = response.content[0].text.strip()
        if "```" in texto:
            texto = texto.split("```")[1].replace("json", "").strip()
        resultado = json.loads(texto)
        if resultado.get("etapa") not in ETAPAS_VALIDAS:
            resultado["etapa"] = "nuevo"
        return resultado
    except Exception as e:
        logger.error(f"Error clasificando conversación {chat_id}: {e}")
        return {"etapa": "nuevo", "cobro_pendiente": False, "resumen": "Error al clasificar"}


async def extraer_texto_documento(archivo_bytes: bytes, nombre: str) -> str | None:
    """Extrae el texto de un PDF, Word o TXT."""
    nombre_lower = nombre.lower()
    try:
        if nombre_lower.endswith(".pdf"):
            import pdfplumber
            import io
            texto_paginas = []
            with pdfplumber.open(io.BytesIO(archivo_bytes)) as pdf:
                for pagina in pdf.pages[:10]:
                    texto = pagina.extract_text()
                    if texto:
                        texto_paginas.append(texto)
            return "\n\n".join(texto_paginas) if texto_paginas else None

        elif nombre_lower.endswith(".docx"):
            import docx
            import io
            doc = docx.Document(io.BytesIO(archivo_bytes))
            parrafos = [p.text for p in doc.paragraphs if p.text.strip()]
            return "\n".join(parrafos) if parrafos else None

        elif nombre_lower.endswith(".txt"):
            return archivo_bytes.decode("utf-8", errors="ignore")

    except Exception as e:
        logger.error(f"Error extrayendo texto de {nombre}: {e}")

    return None


async def enviar_texto_whapi(telefono: str, texto: str, quoted_id: str = "") -> str:
    """Envía texto via Evolution API. Retorna el message_id."""
    if not EVOLUTION_URL:
        return ""
    payload: dict = {"number": _phone(telefono), "text": texto}
    if quoted_id:
        payload["quoted"] = {"key": {"id": quoted_id}}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}",
                json=payload,
                headers=_h(),
            )
            data = r.json()
            if r.status_code not in (200, 201):
                logger.error(f"Error enviando texto Evolution: {r.status_code} {data}")
                return ""
            return data.get("key", {}).get("id", "")
    except Exception as e:
        logger.error(f"Error enviando texto Evolution: {e}")
        return ""


async def enviar_imagen_whapi(
    telefono: str, imagen_bytes: bytes, mime_type: str,
    caption: str = "", quoted_id: str = ""
) -> str:
    """Envía una imagen via Evolution API. Retorna el message_id."""
    import base64
    b64 = base64.b64encode(imagen_bytes).decode()
    payload: dict = {
        "number": _phone(telefono),
        "mediatype": "image",
        "media": f"data:{mime_type};base64,{b64}",
        "caption": caption,
    }
    if quoted_id:
        payload["quoted"] = {"key": {"id": quoted_id}}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{EVOLUTION_URL}/message/sendMedia/{EVOLUTION_INSTANCE}",
                json=payload,
                headers=_h(),
            )
            return r.json().get("key", {}).get("id", "")
    except Exception as e:
        logger.error(f"Error enviando imagen Evolution: {e}")
        return ""


async def enviar_documento_whapi(
    telefono: str, doc_bytes: bytes, mime_type: str,
    filename: str, caption: str = "", quoted_id: str = ""
) -> str:
    """Envía un documento via Evolution API. Retorna el message_id."""
    import base64
    b64 = base64.b64encode(doc_bytes).decode()
    payload: dict = {
        "number": _phone(telefono),
        "mediatype": "document",
        "media": f"data:{mime_type};base64,{b64}",
        "fileName": filename,
        "caption": caption,
    }
    if quoted_id:
        payload["quoted"] = {"key": {"id": quoted_id}}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{EVOLUTION_URL}/message/sendMedia/{EVOLUTION_INSTANCE}",
                json=payload,
                headers=_h(),
            )
            return r.json().get("key", {}).get("id", "")
    except Exception as e:
        logger.error(f"Error enviando documento Evolution: {e}")
        return ""


async def enviar_sticker_whapi(
    telefono: str, sticker_bytes: bytes, mime_type: str = "image/webp"
) -> str:
    """Envía un sticker via Evolution API. Retorna el message_id."""
    import base64
    b64 = base64.b64encode(sticker_bytes).decode()
    payload = {
        "number": _phone(telefono),
        "sticker": f"data:{mime_type};base64,{b64}",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{EVOLUTION_URL}/message/sendSticker/{EVOLUTION_INSTANCE}",
                json=payload,
                headers=_h(),
            )
            return r.json().get("key", {}).get("id", "")
    except Exception as e:
        logger.error(f"Error enviando sticker Evolution: {e}")
        return ""


async def reaccionar_whapi(message_id: str, emoji: str, telefono: str = "") -> bool:
    """Envía una reacción a un mensaje de WhatsApp via Evolution API."""
    if not EVOLUTION_URL:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{EVOLUTION_URL}/message/sendReaction/{EVOLUTION_INSTANCE}",
                json={
                    "key": {
                        "remoteJid": _jid(telefono) if telefono else "",
                        "fromMe": False,
                        "id": message_id,
                    },
                    "reaction": emoji,
                },
                headers=_h(),
            )
            return r.status_code in (200, 201)
    except Exception as e:
        logger.error(f"Error enviando reacción Evolution: {e}")
        return False


async def editar_texto_whapi(
    message_id: str, nuevo_texto: str, telefono: str = ""
) -> bool:
    """Edita un mensaje de texto enviado por nosotros via Evolution API."""
    if not EVOLUTION_URL:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{EVOLUTION_URL}/chat/updateMessage/{EVOLUTION_INSTANCE}",
                json={
                    "number": _phone(telefono) if telefono else "",
                    "key": {
                        "id": message_id,
                        "fromMe": True,
                        "remoteJid": _jid(telefono) if telefono else "",
                    },
                    "text": nuevo_texto,
                },
                headers=_h(),
            )
            return r.status_code in (200, 201)
    except Exception as e:
        logger.error(f"Error editando mensaje Evolution: {e}")
        return False


async def es_contacto_nuevo(telefono: str, dias: int = 30) -> bool:
    """
    Determina si un contacto es nuevo o ya tiene historial con Fedra.
    Retorna True si es nuevo (Sofia debe responder).
    """
    jid = _jid(telefono)
    mensajes = await fetch_mensajes(jid, count=20)

    if not mensajes:
        return True

    import time
    hace_N_dias = time.time() - (dias * 86400)
    mensajes_de_fedra = [
        m for m in mensajes
        if m.get("from_me") and m.get("timestamp", 0) > hace_N_dias
    ]

    if mensajes_de_fedra:
        logger.info(f"Contacto {telefono} ya tiene {len(mensajes_de_fedra)} msgs de Fedra → Sofia no interviene")
        return False

    return True


async def analizar_estilo_fedra(max_chats: int = 20) -> str:
    """Analiza los mensajes enviados por Fedra para entender su estilo de escritura."""
    chats = await fetch_chats(count=max_chats)
    mensajes_fedra = []

    for chat in chats[:max_chats]:
        chat_id = chat.get("id", "")
        mensajes = await fetch_mensajes(chat_id, count=30)
        for msg in mensajes:
            if msg.get("from_me") and msg.get("type") == "text":
                texto = msg.get("text", {}).get("body", "")
                if texto and len(texto) > 5:
                    mensajes_fedra.append(texto)

    if not mensajes_fedra:
        return ""

    muestra = mensajes_fedra[:50]
    return "\n".join(f"- {m}" for m in muestra)


async def configurar_webhook_evolution(webhook_url: str) -> bool:
    """Configura el webhook en Evolution API para recibir mensajes."""
    if not EVOLUTION_URL:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{EVOLUTION_URL}/webhook/set/{EVOLUTION_INSTANCE}",
                json={
                    "url": webhook_url,
                    "webhook_by_events": False,
                    "webhook_base64": False,
                    "events": ["MESSAGES_UPSERT"],
                },
                headers=_h(),
            )
            ok = r.status_code in (200, 201)
            if ok:
                logger.info(f"Webhook configurado en Evolution: {webhook_url}")
            else:
                logger.error(f"Error configurando webhook: {r.status_code} {r.text}")
            return ok
    except Exception as e:
        logger.error(f"Error configurando webhook Evolution: {e}")
        return False


async def obtener_qr_evolution() -> dict:
    """Obtiene el estado de conexión y QR si la instancia no está conectada."""
    if not EVOLUTION_URL:
        return {"error": "EVOLUTION_URL no configurado"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Estado de conexión
            r_state = await client.get(
                f"{EVOLUTION_URL}/instance/connectionState/{EVOLUTION_INSTANCE}",
                headers=_h(),
            )
            state_data = r_state.json()
            state = state_data.get("instance", {}).get("state", "")

            if state == "open":
                return {"connected": True, "state": "open"}

            # Solicitar QR
            r_qr = await client.get(
                f"{EVOLUTION_URL}/instance/connect/{EVOLUTION_INSTANCE}",
                headers=_h(),
            )
            return {"connected": False, "state": state, **r_qr.json()}
    except Exception as e:
        logger.error(f"Error obteniendo QR Evolution: {e}")
        return {"error": str(e)}
