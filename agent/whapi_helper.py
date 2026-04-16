# agent/whapi_helper.py — Utilidades para Whapi.cloud
"""
Funciones para interactuar con Whapi.cloud:
- Obtener chats y mensajes existentes
- Obtener nombres de contactos
- Descargar y transcribir audios
- Analizar estilo de Fedra
- Auto-clasificar conversaciones
"""

import os
import logging
import httpx
from anthropic import AsyncAnthropic

logger = logging.getLogger("agentkit")

WHAPI_TOKEN = os.getenv("WHAPI_TOKEN", "")
WHAPI_BASE = "https://gate.whapi.cloud"
anthropic = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

ETAPAS_VALIDAS = ["nuevo", "respondio", "interesado", "presupuesto", "seguimiento", "cerrado"]


async def fetch_chats(count: int = 100) -> list[dict]:
    """Obtiene todos los chats de WhatsApp via Whapi."""
    if not WHAPI_TOKEN:
        return []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{WHAPI_BASE}/chats",
                params={"count": count},
                headers={"Authorization": f"Bearer {WHAPI_TOKEN}"}
            )
            data = r.json()
            return [c for c in data.get("chats", []) if c.get("type") == "contact"]
    except Exception as e:
        logger.error(f"Error fetching chats de Whapi: {e}")
        return []


async def fetch_mensajes(chat_id: str, count: int = 50) -> list[dict]:
    """Obtiene los últimos N mensajes de un chat."""
    if not WHAPI_TOKEN:
        return []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{WHAPI_BASE}/messages/list/{chat_id}",
                params={"count": count},
                headers={"Authorization": f"Bearer {WHAPI_TOKEN}"}
            )
            data = r.json()
            return data.get("messages", [])
    except Exception as e:
        logger.error(f"Error fetching mensajes de Whapi para {chat_id}: {e}")
        return []


async def fetch_nombre_contacto(telefono: str) -> str:
    """Obtiene el nombre del contacto desde Whapi."""
    if not WHAPI_TOKEN:
        return ""
    chat_id = f"{telefono}@s.whatsapp.net" if "@" not in telefono else telefono
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{WHAPI_BASE}/chats/{chat_id}",
                headers={"Authorization": f"Bearer {WHAPI_TOKEN}"}
            )
            data = r.json()
            return data.get("name", "") or data.get("last_message", {}).get("from_name", "")
    except Exception as e:
        logger.error(f"Error fetching nombre contacto {telefono}: {e}")
        return ""


async def fetch_avatar_contacto(telefono: str) -> str:
    """Obtiene la URL de la foto de perfil del contacto desde Whapi."""
    if not WHAPI_TOKEN:
        return ""
    contact_id = f"{telefono}@s.whatsapp.net" if "@" not in telefono else telefono
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{WHAPI_BASE}/contacts/{contact_id}",
                headers={"Authorization": f"Bearer {WHAPI_TOKEN}"}
            )
            if r.status_code == 200:
                data = r.json()
                return data.get("avatar", "") or ""
    except Exception as e:
        logger.error(f"Error fetching avatar {telefono}: {e}")
    return ""


async def descargar_audio(media_id: str) -> bytes | None:
    """Descarga un archivo de audio desde Whapi."""
    if not WHAPI_TOKEN:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{WHAPI_BASE}/media/{media_id}",
                headers={"Authorization": f"Bearer {WHAPI_TOKEN}"}
            )
            if r.status_code == 200:
                return r.content
    except Exception as e:
        logger.error(f"Error descargando audio {media_id}: {e}")
    return None


async def transcribir_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str | None:
    """
    Transcribe un audio usando Groq Whisper (si GROQ_API_KEY está configurado)
    o OpenAI Whisper (si OPENAI_API_KEY está configurado).
    """
    # Intentar con Groq (más rápido y gratuito)
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
                        data={"model": "whisper-large-v3-turbo", "language": "es"}
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
                        data={"model": "whisper-1", "language": "es"}
                    )
                    return r.json().get("text", "")
        except Exception as e:
            logger.error(f"Error transcribiendo con OpenAI: {e}")

    return None


def extraer_texto_mensaje(msg: dict) -> str | None:
    """Extrae el texto de un mensaje de Whapi (texto, audio transcripto, caption de imagen)."""
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
    Usa Claude para analizar una conversación de WhatsApp y determinar:
    - Etapa del pipeline (nuevo/respondio/interesado/presupuesto/seguimiento/cerrado)
    - Si tiene cobro pendiente
    - Resumen del estado
    """
    # Construir texto de la conversación
    lineas = []
    for msg in mensajes[-30:]:  # últimos 30 mensajes
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
            messages=[{"role": "user", "content": prompt}]
        )
        import json
        texto = response.content[0].text.strip()
        # Limpiar si viene con markdown
        if "```" in texto:
            texto = texto.split("```")[1].replace("json", "").strip()
        resultado = json.loads(texto)
        # Validar etapa
        if resultado.get("etapa") not in ETAPAS_VALIDAS:
            resultado["etapa"] = "nuevo"
        return resultado
    except Exception as e:
        logger.error(f"Error clasificando conversación {chat_id}: {e}")
        return {"etapa": "nuevo", "cobro_pendiente": False, "resumen": "Error al clasificar"}


async def descargar_media(media_id: str) -> bytes | None:
    """Descarga cualquier archivo de media desde Whapi."""
    if not WHAPI_TOKEN:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{WHAPI_BASE}/media/{media_id}",
                headers={"Authorization": f"Bearer {WHAPI_TOKEN}"}
            )
            if r.status_code == 200:
                return r.content
    except Exception as e:
        logger.error(f"Error descargando media {media_id}: {e}")
    return None


async def extraer_texto_documento(archivo_bytes: bytes, nombre: str) -> str | None:
    """
    Extrae el texto de un PDF o Word para que Sofia lo pueda leer.
    Soporta: .pdf, .docx, .doc, .txt
    """
    nombre_lower = nombre.lower()
    try:
        if nombre_lower.endswith(".pdf"):
            import pdfplumber
            import io
            texto_paginas = []
            with pdfplumber.open(io.BytesIO(archivo_bytes)) as pdf:
                for pagina in pdf.pages[:10]:  # máximo 10 páginas
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
    """Envía texto via Whapi. Retorna el message_id."""
    destinatario = f"{telefono}@s.whatsapp.net" if "@" not in telefono else telefono
    payload: dict = {"to": destinatario, "body": texto}
    if quoted_id:
        payload["context"] = {"message_id": quoted_id}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{WHAPI_BASE}/messages/text",
                json=payload,
                headers={"Authorization": f"Bearer {WHAPI_TOKEN}"}
            )
            return r.json().get("id", "")
    except Exception as e:
        logger.error(f"Error enviando texto Whapi: {e}")
        return ""


async def enviar_imagen_whapi(telefono: str, imagen_bytes: bytes, mime_type: str,
                               caption: str = "", quoted_id: str = "") -> str:
    """Envía una imagen via Whapi. Retorna el message_id."""
    import base64
    destinatario = f"{telefono}@s.whatsapp.net" if "@" not in telefono else telefono
    b64 = base64.b64encode(imagen_bytes).decode()
    payload: dict = {
        "to": destinatario,
        "image": {"data": f"data:{mime_type};base64,{b64}"},
        "caption": caption,
    }
    if quoted_id:
        payload["context"] = {"message_id": quoted_id}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{WHAPI_BASE}/messages/image",
                json=payload,
                headers={"Authorization": f"Bearer {WHAPI_TOKEN}"}
            )
            return r.json().get("id", "")
    except Exception as e:
        logger.error(f"Error enviando imagen Whapi: {e}")
        return ""


async def enviar_documento_whapi(telefono: str, doc_bytes: bytes, mime_type: str,
                                  filename: str, caption: str = "", quoted_id: str = "") -> str:
    """Envía un documento via Whapi. Retorna el message_id."""
    import base64
    destinatario = f"{telefono}@s.whatsapp.net" if "@" not in telefono else telefono
    b64 = base64.b64encode(doc_bytes).decode()
    payload: dict = {
        "to": destinatario,
        "document": {
            "data": f"data:{mime_type};base64,{b64}",
            "filename": filename,
        },
        "caption": caption,
    }
    if quoted_id:
        payload["context"] = {"message_id": quoted_id}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{WHAPI_BASE}/messages/document",
                json=payload,
                headers={"Authorization": f"Bearer {WHAPI_TOKEN}"}
            )
            return r.json().get("id", "")
    except Exception as e:
        logger.error(f"Error enviando documento Whapi: {e}")
        return ""


async def enviar_sticker_whapi(telefono: str, sticker_bytes: bytes, mime_type: str = "image/webp") -> str:
    """Envía un sticker via Whapi. Retorna el message_id."""
    import base64
    destinatario = f"{telefono}@s.whatsapp.net" if "@" not in telefono else telefono
    b64 = base64.b64encode(sticker_bytes).decode()
    payload = {
        "to": destinatario,
        "sticker": {"data": f"data:{mime_type};base64,{b64}"},
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{WHAPI_BASE}/messages/sticker",
                json=payload,
                headers={"Authorization": f"Bearer {WHAPI_TOKEN}"}
            )
            return r.json().get("id", "")
    except Exception as e:
        logger.error(f"Error enviando sticker Whapi: {e}")
        return ""


async def reaccionar_whapi(message_id: str, emoji: str) -> bool:
    """Envía una reacción a un mensaje de WhatsApp."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{WHAPI_BASE}/messages/reaction/{message_id}",
                json={"emoji": emoji},
                headers={"Authorization": f"Bearer {WHAPI_TOKEN}"}
            )
            return r.status_code in (200, 201)
    except Exception as e:
        logger.error(f"Error enviando reacción Whapi: {e}")
        return False


async def editar_texto_whapi(message_id: str, nuevo_texto: str) -> bool:
    """Edita un mensaje de texto enviado por nosotros."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.patch(
                f"{WHAPI_BASE}/messages/text/{message_id}",
                json={"body": nuevo_texto},
                headers={"Authorization": f"Bearer {WHAPI_TOKEN}"}
            )
            return r.status_code in (200, 201)
    except Exception as e:
        logger.error(f"Error editando mensaje Whapi: {e}")
        return False


async def es_contacto_nuevo(telefono: str, dias: int = 30) -> bool:
    """
    Determina si un contacto es nuevo o ya tiene historial con Fedra.
    Retorna True si es nuevo (Sofia debe responder).
    Retorna False si Fedra ya le escribió antes (Sofia no interviene).
    """
    chat_id = f"{telefono}@s.whatsapp.net"
    mensajes = await fetch_mensajes(chat_id, count=20)

    if not mensajes:
        return True  # Sin historial → es nuevo

    # Verificar si hay mensajes enviados por Fedra (from_me=True)
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
    """
    Analiza los mensajes enviados por Fedra para entender su estilo de escritura.
    Retorna ejemplos de frases que Sofia puede aprender.
    """
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

    # Tomar muestra representativa
    muestra = mensajes_fedra[:50]
    return "\n".join(f"- {m}" for m in muestra)
