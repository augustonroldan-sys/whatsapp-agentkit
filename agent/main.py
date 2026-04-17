# agent/main.py — Servidor FastAPI + Webhook
# Generado por AgentKit

"""
Servidor principal de Sofia para HeFe Uniformes.
Soporta Kommo CRM (WhatsApp + Instagram) y Meta Cloud API directa.
"""

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import (
    inicializar_db, guardar_mensaje, obtener_historial, esta_derivado,
    marcar_derivado, listar_conversaciones, actualizar_etapa,
    limpiar_historial, async_session, Conversacion,
    actualizar_nombre, upsert_conversacion,
    obtener_config, guardar_config,
    obtener_notas, guardar_notas,
    obtener_conversaciones_para_seguimiento, marcar_seguimiento,
    archivar_conversacion, desarchivar_conversacion,
    obtener_stats_sofia, obtener_telefonos_con_voz,
    guardar_avatar,
)
from agent.whapi_helper import (
    fetch_chats, fetch_mensajes, fetch_nombre_contacto,
    descargar_audio, transcribir_audio, clasificar_conversacion_con_ia,
    es_contacto_nuevo, analizar_estilo_fedra,
    descargar_media, extraer_texto_documento,
    enviar_texto_whapi, enviar_imagen_whapi, enviar_documento_whapi,
    enviar_sticker_whapi, reaccionar_whapi, editar_texto_whapi,
    fetch_avatar_contacto,
    configurar_webhook_evolution, obtener_qr_evolution,
)
from agent.providers import obtener_proveedor
from agent.tools import detectar_intencion_compra_ia, generar_mensaje_derivacion, enviar_alerta_telegram

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))


async def _loop_seguimiento():
    """Corre cada hora y envía mensajes de seguimiento automático."""
    import asyncio
    await asyncio.sleep(60)  # Esperar a que la app esté lista
    while True:
        try:
            activo = await obtener_config("seguimiento_activo", "false")
            if activo == "true":
                dias = int(await obtener_config("seguimiento_dias", "2"))
                mensaje = await obtener_config(
                    "seguimiento_mensaje",
                    "Hola! Quería saber si pudiste ver la información que te mandé. ¿Puedo ayudarte con algo más? 😊"
                )
                telefonos = await obtener_conversaciones_para_seguimiento(dias)
                for telefono in telefonos:
                    resp_id = await enviar_texto_whapi(telefono, mensaje)
                    await guardar_mensaje(telefono, "assistant", mensaje, message_id=resp_id)
                    await marcar_seguimiento(telefono)
                    logger.info(f"Seguimiento automático enviado a {telefono}")
        except Exception as e:
            logger.error(f"Error en loop seguimiento: {e}")
        await asyncio.sleep(3600)  # Revisar cada hora


async def _loop_sync_diario():
    """Sync completo de todos los chats una vez al día."""
    import asyncio
    await asyncio.sleep(120)  # Esperar 2 min al arrancar para hacer el primer sync
    while True:
        try:
            logger.info("Iniciando sync automático diario...")
            await _tarea_sincronizar()
            logger.info("Sync automático diario completado")
        except Exception as e:
            logger.error(f"Error en sync diario: {e}")
        await asyncio.sleep(86400)  # Repetir cada 24 horas


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    await inicializar_db()
    asyncio.create_task(_loop_seguimiento())
    asyncio.create_task(_loop_sync_diario())
    logger.info(f"Servidor AgentKit listo en puerto {PORT}")
    logger.info(f"Proveedor: {proveedor.__class__.__name__}")
    yield


app = FastAPI(
    title="AgentKit — HeFe Uniformes (Sofia)",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def health_check():
    return {"status": "ok", "service": "agentkit-hefe-uniformes", "agente": "Sofia"}


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificación GET — requerido por Meta Cloud API."""
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


def _normalizar_msg_evolution(body: dict) -> list[dict]:
    """
    Convierte el payload de Evolution API al formato interno usado por el webhook.
    Evolution manda: {event, instance, data: {key, pushName, message, messageType, messageTimestamp}}
    Retorna lista de mensajes normalizados compatibles con el código existente.
    """
    event = body.get("event", "")
    if event != "messages.upsert":
        return []

    data = body.get("data", {})
    # data puede ser un objeto o una lista
    msgs = data if isinstance(data, list) else [data]

    result = []
    for msg in msgs:
        key = msg.get("key", {})
        if key.get("fromMe", False):
            continue  # Ignorar mensajes propios

        remote_jid = key.get("remoteJid", "")
        # Ignorar grupos
        if "@g.us" in remote_jid:
            continue

        telefono = remote_jid.replace("@s.whatsapp.net", "").replace("@c.us", "")
        if not telefono:
            continue

        message_type = msg.get("messageType", "")
        message = msg.get("message", {})
        msg_id = key.get("id", "")

        normalized = {
            "id": msg_id,
            "from_me": False,
            "from": telefono,
            "from_name": msg.get("pushName", ""),
            "timestamp": msg.get("messageTimestamp", 0),
            "_raw_evo": msg,  # conservar para descargar media
        }

        if message_type in ("conversation", "extendedTextMessage"):
            normalized["type"] = "text"
            text = (
                message.get("conversation")
                or message.get("extendedTextMessage", {}).get("text", "")
                or ""
            )
            normalized["text"] = {"body": text}

        elif message_type == "audioMessage":
            audio = message.get("audioMessage", {})
            normalized["type"] = "audio"
            normalized["audio"] = {
                "id": msg_id,
                "mime_type": audio.get("mimetype", "audio/ogg"),
            }

        elif message_type == "imageMessage":
            img = message.get("imageMessage", {})
            normalized["type"] = "image"
            normalized["image"] = {
                "id": msg_id,
                "caption": img.get("caption", ""),
                "mime_type": img.get("mimetype", "image/jpeg"),
            }

        elif message_type == "videoMessage":
            vid = message.get("videoMessage", {})
            normalized["type"] = "video"
            normalized["video"] = {
                "id": msg_id,
                "caption": vid.get("caption", ""),
                "mime_type": vid.get("mimetype", "video/mp4"),
            }

        elif message_type == "documentMessage":
            doc = message.get("documentMessage", {})
            normalized["type"] = "document"
            normalized["document"] = {
                "id": msg_id,
                "file_name": doc.get("fileName", "documento"),
                "mime_type": doc.get("mimetype", "application/octet-stream"),
                "caption": doc.get("caption", ""),
            }

        elif message_type == "stickerMessage":
            normalized["type"] = "sticker"

        else:
            normalized["type"] = message_type or "unknown"

        result.append(normalized)

    return result


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Recibe mensajes de WhatsApp via Evolution API.
    - Solo responde a contactos NUEVOS (sin historial con Fedra)
    - Transcribe audios automáticamente
    - Guarda el nombre real del contacto
    - Si detecta intención de compra → deriva a Fedra
    """
    try:
        body = await request.json()
        mensajes_raw = _normalizar_msg_evolution(body)

        for msg_raw in mensajes_raw:
            telefono = msg_raw.get("from", "")
            if not telefono:
                continue

            tipo = msg_raw.get("type", "")
            msg_id_entrante = msg_raw.get("id", "")
            nombre_contacto = msg_raw.get("from_name", "") or await fetch_nombre_contacto(telefono)

            # Guardar nombre real si lo tenemos
            if nombre_contacto:
                await actualizar_nombre(telefono, nombre_contacto)
            # Obtener avatar si no lo tenemos aún
            from agent.memory import obtener_avatar
            avatar_actual = await obtener_avatar(telefono)
            if not avatar_actual:
                avatar_url = await fetch_avatar_contacto(telefono)
                if avatar_url:
                    await guardar_avatar(telefono, avatar_url)

            # Extraer texto según tipo de mensaje
            texto = None
            imagen_bytes: bytes | None = None
            imagen_mime: str = "image/jpeg"

            if tipo == "text":
                texto = msg_raw.get("text", {}).get("body", "")

            elif tipo == "audio":
                audio_info = msg_raw.get("audio", {})
                media_id = audio_info.get("id", "")
                mime_type = audio_info.get("mime_type", "audio/ogg")
                if media_id:
                    await guardar_mensaje(telefono, "user", f"[Audio]({media_id})", message_id=msg_id_entrante)
                    audio_bytes = await descargar_audio(media_id)
                    if audio_bytes:
                        transcripcion = await transcribir_audio(audio_bytes, mime_type)
                        if transcripcion:
                            logger.info(f"Audio transcripto de {telefono}: {transcripcion}")
                            texto = transcripcion
                if not texto:
                    continue  # Sin transcripción — no responder

            elif tipo == "image":
                img_info = msg_raw.get("image", {})
                caption = img_info.get("caption", "")
                media_id = img_info.get("id", "")
                texto_crm = f"[Imagen]({media_id})" if media_id else "[Imagen]"
                if caption:
                    texto_crm += f" {caption}"
                await guardar_mensaje(telefono, "user", texto_crm, message_id=msg_id_entrante)
                imagen_mime = img_info.get("mime_type", "image/jpeg")
                if media_id:
                    imagen_bytes = await descargar_media(media_id)
                texto = caption if caption else "El cliente envió una imagen"

            elif tipo == "video":
                vid_info = msg_raw.get("video", {})
                caption = vid_info.get("caption", "")
                media_id = vid_info.get("id", "")
                texto_crm = f"[Video]({media_id})" if media_id else "[Video]"
                if caption:
                    texto_crm += f" {caption}"
                await guardar_mensaje(telefono, "user", texto_crm, message_id=msg_id_entrante)
                texto = caption if caption else "El cliente envió un video"

            elif tipo == "document":
                doc_info = msg_raw.get("document", {})
                media_id = doc_info.get("id", "")
                nombre_doc = doc_info.get("file_name", "documento")
                mime_type = doc_info.get("mime_type", "")
                caption = doc_info.get("caption", "")
                texto_crm = f"[Documento: {nombre_doc}]({media_id})" if media_id else f"[Documento: {nombre_doc}]"
                await guardar_mensaje(telefono, "user", texto_crm)
                if media_id and nombre_doc.lower().endswith((".pdf", ".docx", ".txt")):
                    archivo_bytes = await descargar_media(media_id)
                    if archivo_bytes:
                        contenido = await extraer_texto_documento(archivo_bytes, nombre_doc)
                        if contenido:
                            texto = f"El cliente envió un documento llamado '{nombre_doc}'. Contenido:\n\n{contenido[:3000]}"
                            if caption:
                                texto = f"{caption}\n\n{texto}"
                        else:
                            texto = caption or f"El cliente envió el archivo '{nombre_doc}' pero no pude leerlo."
                    else:
                        texto = caption or f"El cliente envió el archivo '{nombre_doc}'."
                else:
                    texto = caption or f"El cliente envió el archivo '{nombre_doc}'."

            if not texto:
                continue

            logger.info(f"Mensaje de {nombre_contacto or telefono}: {texto}")

            # Si ya fue derivada → Sofia no interviene
            if await esta_derivado(telefono):
                logger.info(f"Conversación {telefono} derivada — Sofia no interviene")
                continue

            # Verificar si es contacto nuevo (sin historial con Fedra)
            es_nuevo = await es_contacto_nuevo(telefono)
            if not es_nuevo:
                logger.info(f"Contacto {telefono} ya tiene historial con Fedra — Sofia no responde")
                await guardar_mensaje(telefono, "user", texto, message_id=msg_id_entrante)
                continue

            historial = await obtener_historial(telefono)
            requiere_humano = await detectar_intencion_compra_ia(texto, historial)

            if requiere_humano:
                respuesta = generar_mensaje_derivacion()
                await guardar_mensaje(telefono, "user", texto, message_id=msg_id_entrante)
                resp_id = await enviar_texto_whapi(telefono, respuesta)
                await guardar_mensaje(telefono, "assistant", respuesta, message_id=resp_id)
                await marcar_derivado(telefono)
                await enviar_alerta_telegram(nombre_contacto or telefono, texto)
                logger.info(f"Conversación {telefono} derivada a humano")
                continue

            # Sofia responde con delay configurable
            respuesta = await generar_respuesta(texto, historial, imagen_bytes=imagen_bytes, imagen_mime=imagen_mime)
            await guardar_mensaje(telefono, "user", texto, message_id=msg_id_entrante)

            import asyncio, random
            delay_modo = await obtener_config("delay_respuesta", "normal")
            rangos = {
                "inmediata": (0, 1),
                "rapida":    (2, 4),
                "normal":    (5, 8),
                "lenta":     (10, 15),
            }
            lo, hi = rangos.get(delay_modo, (5, 8))
            await asyncio.sleep(random.uniform(lo, hi))

            resp_id = await enviar_texto_whapi(telefono, respuesta)
            await guardar_mensaje(telefono, "assistant", respuesta, message_id=resp_id)
            logger.info(f"Sofia respondió a {nombre_contacto or telefono} (delay:{delay_modo}): {respuesta}")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


CRM_PASSWORD = os.getenv("CRM_PASSWORD", "hefe2026")


def verificar_password(x_password: str = None):
    if x_password != CRM_PASSWORD:
        raise HTTPException(status_code=401, detail="No autorizado")


@app.get("/api/conversaciones")
async def api_conversaciones(x_password: str = None, incluir_archivadas: bool = False):
    """CRM — lista todas las conversaciones."""
    verificar_password(x_password)
    return await listar_conversaciones(incluir_archivadas=incluir_archivadas)


@app.get("/api/conversaciones/{telefono}")
async def api_historial(telefono: str, x_password: str = None):
    """CRM — historial completo de una conversación."""
    verificar_password(x_password)
    historial = await obtener_historial(telefono, limite=50)
    return {"telefono": telefono, "mensajes": historial}


@app.put("/api/conversaciones/{telefono}/etapa")
async def api_actualizar_etapa(telefono: str, etapa: str, x_password: str = None):
    """CRM — actualiza la etapa del pipeline."""
    verificar_password(x_password)
    await actualizar_etapa(telefono, etapa)
    return {"status": "ok"}


@app.post("/api/conversaciones/{telefono}/enviar")
async def api_enviar_mensaje(telefono: str, x_password: str = None, request: Request = None):
    """CRM — Fedra envía un mensaje manual a un cliente."""
    verificar_password(x_password)
    body = await request.json()
    texto = body.get("mensaje", "").strip()
    quoted_id = body.get("quoted_id", "")
    if not texto:
        raise HTTPException(status_code=400, detail="Mensaje vacío")
    msg_id = await enviar_texto_whapi(telefono, texto, quoted_id=quoted_id)
    await guardar_mensaje(telefono, "assistant", texto, message_id=msg_id)
    logger.info(f"Mensaje manual enviado a {telefono}: {texto}")
    return {"status": "ok", "message_id": msg_id}


@app.post("/api/conversaciones/{telefono}/enviar-media")
async def api_enviar_media(telefono: str, x_password: str = None, request: Request = None):
    """CRM — envía imagen, documento o sticker a un cliente."""
    verificar_password(x_password)
    form = await request.form()
    tipo = str(form.get("tipo", "imagen"))
    caption = str(form.get("caption", ""))
    quoted_id = str(form.get("quoted_id", ""))
    archivo = form.get("archivo")
    if not archivo or not hasattr(archivo, "read"):
        raise HTTPException(status_code=400, detail="Sin archivo")
    archivo_bytes = await archivo.read()
    mime_type = archivo.content_type or "application/octet-stream"
    filename = archivo.filename or "archivo"

    if tipo == "imagen":
        msg_id = await enviar_imagen_whapi(telefono, archivo_bytes, mime_type, caption, quoted_id)
        texto_crm = f"[Imagen]{': ' + caption if caption else ''}"
    elif tipo == "documento":
        msg_id = await enviar_documento_whapi(telefono, archivo_bytes, mime_type, filename, caption, quoted_id)
        texto_crm = f"[Documento: {filename}]"
    elif tipo == "sticker":
        msg_id = await enviar_sticker_whapi(telefono, archivo_bytes, mime_type)
        texto_crm = "[Sticker]"
    else:
        raise HTTPException(status_code=400, detail="Tipo inválido")

    await guardar_mensaje(telefono, "assistant", texto_crm, message_id=msg_id)
    logger.info(f"Media ({tipo}) enviada a {telefono}")
    return {"status": "ok", "message_id": msg_id}


@app.post("/api/conversaciones/{telefono}/reaccionar")
async def api_reaccionar(telefono: str, x_password: str = None, request: Request = None):
    """CRM — envía una reacción emoji a un mensaje del cliente."""
    verificar_password(x_password)
    body = await request.json()
    message_id = body.get("message_id", "")
    emoji = body.get("emoji", "")
    if not message_id or not emoji:
        raise HTTPException(status_code=400, detail="Faltan message_id o emoji")
    ok = await reaccionar_whapi(message_id, emoji)
    return {"status": "ok" if ok else "error"}


@app.patch("/api/conversaciones/{telefono}/mensajes/{message_id}")
async def api_editar_mensaje(telefono: str, message_id: str, x_password: str = None, request: Request = None):
    """CRM — edita un mensaje de texto enviado por Fedra."""
    verificar_password(x_password)
    body = await request.json()
    nuevo_texto = body.get("texto", "").strip()
    if not nuevo_texto:
        raise HTTPException(status_code=400, detail="Texto vacío")
    ok = await editar_texto_whapi(message_id, nuevo_texto)
    if ok:
        from sqlalchemy import update as sql_update
        from agent.memory import Mensaje as MensajeModel
        async with async_session() as session:
            await session.execute(
                sql_update(MensajeModel)
                .where(MensajeModel.message_id == message_id)
                .values(content=nuevo_texto)
            )
            await session.commit()
    return {"status": "ok" if ok else "error"}


_sync_estado = {"corriendo": False, "ultimo": None}


async def _tarea_sincronizar():
    """Tarea en background que sincroniza todos los chats."""
    _sync_estado["corriendo"] = True
    resultados = []
    errores = []
    chats = await fetch_chats(count=200)

    for chat in chats:
        chat_id = chat.get("id", "")
        nombre = chat.get("name", "") or chat.get("last_message", {}).get("from_name", "")
        telefono = chat_id.replace("@s.whatsapp.net", "")
        if not telefono or not telefono.isdigit():
            continue
        try:
            mensajes = await fetch_mensajes(chat_id, count=100)
            tiene_msgs_de_fedra = any(m.get("from_me") for m in mensajes)
            clasificacion = await clasificar_conversacion_con_ia(chat_id, nombre, mensajes)
            await upsert_conversacion(
                telefono=telefono, nombre=nombre,
                etapa=clasificacion.get("etapa", "nuevo"),
                cobro_pendiente=clasificacion.get("cobro_pendiente", False),
                monto_cobro=clasificacion.get("monto_cobro") or "",
                resumen=clasificacion.get("resumen", ""),
                contacto_existente=tiene_msgs_de_fedra,
            )
            # Obtener foto de perfil
            avatar_url = await fetch_avatar_contacto(telefono)
            if avatar_url:
                await guardar_avatar(telefono, avatar_url)
            await limpiar_historial(telefono)
            for msg in sorted(mensajes, key=lambda m: m.get("timestamp", 0)):
                tipo = msg.get("type", "")
                rol = "assistant" if msg.get("from_me") else "user"
                if tipo == "text":
                    texto = msg.get("text", {}).get("body", "")
                elif tipo == "image":
                    img_data = msg.get("image", {})
                    caption = img_data.get("caption", "")
                    img_id = img_data.get("id", "")
                    texto = (f"[Imagen]({img_id})" if img_id else "[Imagen]") + (f" {caption}" if caption else "")
                elif tipo == "video":
                    vid_data = msg.get("video", {})
                    caption = vid_data.get("caption", "")
                    vid_id = vid_data.get("id", "")
                    texto = (f"[Video]({vid_id})" if vid_id else "[Video]") + (f" {caption}" if caption else "")
                elif tipo == "audio":
                    audio_id = msg.get("audio", {}).get("id", "")
                    texto = f"[Audio]({audio_id})" if audio_id else "[Audio 🎙️]"
                elif tipo == "document":
                    nombre_doc = msg.get("document", {}).get("file_name", "archivo")
                    texto = f"[Documento: {nombre_doc}]"
                elif tipo == "sticker":
                    texto = "[Sticker]"
                else:
                    texto = f"[{tipo}]"
                if texto:
                    await guardar_mensaje(telefono, rol, texto)
            resultados.append({"telefono": telefono, "nombre": nombre, "etapa": clasificacion.get("etapa")})
            logger.info(f"Sync {nombre} ({telefono}): {clasificacion.get('etapa')} — {len(mensajes)} msgs")
        except Exception as e:
            logger.error(f"Error sync {telefono}: {e}")
            errores.append(telefono)

    _sync_estado["corriendo"] = False
    _sync_estado["ultimo"] = {"sincronizados": len(resultados), "errores": len(errores), "chats": resultados}
    logger.info(f"Sincronización completa: {len(resultados)} chats, {len(errores)} errores")


@app.post("/sincronizar")
async def sincronizar_chats(background_tasks: BackgroundTasks, x_password: str = None):
    """Lanza la sincronización en background y retorna inmediatamente."""
    verificar_password(x_password)
    if _sync_estado["corriendo"]:
        return {"status": "ya_corriendo", "mensaje": "Sincronización en progreso..."}
    background_tasks.add_task(_tarea_sincronizar)
    return {"status": "iniciada", "mensaje": "Sincronizando en background. Consultá /sincronizar/estado para ver el progreso."}


@app.get("/sincronizar/estado")
async def estado_sincronizacion(x_password: str = None):
    """Retorna el estado actual de la sincronización."""
    verificar_password(x_password)
    return {
        "corriendo": _sync_estado["corriendo"],
        "ultimo": _sync_estado["ultimo"]
    }


@app.post("/analizar-estilo")
async def analizar_estilo(x_password: str = None):
    """
    Analiza el estilo de escritura de Fedra para que Sofia aprenda.
    Devuelve ejemplos de frases para usar en el system prompt.
    """
    verificar_password(x_password)
    ejemplos = await analizar_estilo_fedra(max_chats=30)
    if not ejemplos:
        return {"status": "ok", "mensaje": "No se encontraron mensajes de Fedra", "ejemplos": ""}
    return {"status": "ok", "ejemplos": ejemplos}


@app.post("/resetear/{telefono}")
async def resetear_conversacion(telefono: str, x_password: str = None):
    """
    Borra todo el historial y estado de una conversación.
    El número queda como nuevo para Sofia.
    """
    verificar_password(x_password)
    from sqlalchemy import delete as sql_delete
    await limpiar_historial(telefono)
    async with async_session() as session:
        await session.execute(sql_delete(Conversacion).where(Conversacion.telefono == telefono))
        await session.commit()
    logger.info(f"Conversación {telefono} reseteada completamente")
    return {"status": "ok", "mensaje": f"Conversación {telefono} reseteada"}


@app.get("/api/configuracion")
async def api_get_configuracion(x_password: str = None):
    """CRM — obtiene la configuración actual."""
    verificar_password(x_password)
    from agent.brain import cargar_system_prompt
    yaml_prompt = cargar_system_prompt()
    return {
        "delay_respuesta": await obtener_config("delay_respuesta", "normal"),
        "system_prompt": await obtener_config("system_prompt", yaml_prompt),
    }


@app.put("/api/configuracion")
async def api_set_configuracion(x_password: str = None, request: Request = None):
    """CRM — actualiza la configuración."""
    verificar_password(x_password)
    body = await request.json()
    for clave, valor in body.items():
        await guardar_config(clave, str(valor))
    return {"status": "ok"}


@app.get("/api/conversaciones/{telefono}/notas")
async def api_get_notas(telefono: str, x_password: str = None):
    """CRM — obtiene las notas privadas de una conversación."""
    verificar_password(x_password)
    notas = await obtener_notas(telefono)
    return {"telefono": telefono, "notas": notas}


@app.put("/api/conversaciones/{telefono}/notas")
async def api_guardar_notas(telefono: str, x_password: str = None, request: Request = None):
    """CRM — guarda las notas privadas de una conversación."""
    verificar_password(x_password)
    body = await request.json()
    notas = body.get("notas", "")
    await guardar_notas(telefono, notas)
    return {"status": "ok"}


@app.post("/seguimiento/forzar")
async def forzar_seguimiento(x_password: str = None):
    """Ejecuta el seguimiento ahora sin esperar el loop horario."""
    verificar_password(x_password)
    activo = await obtener_config("seguimiento_activo", "false")
    if activo != "true":
        return {"status": "desactivado", "mensaje": "El seguimiento automático está desactivado"}
    dias = int(await obtener_config("seguimiento_dias", "2"))
    mensaje = await obtener_config(
        "seguimiento_mensaje",
        "Hola! Quería saber si pudiste ver la información que te mandé. ¿Puedo ayudarte con algo más? 😊"
    )
    telefonos = await obtener_conversaciones_para_seguimiento(dias)
    enviados = []
    for telefono in telefonos:
        resp_id = await enviar_texto_whapi(telefono, mensaje)
        await guardar_mensaje(telefono, "assistant", mensaje, message_id=resp_id)
        await marcar_seguimiento(telefono)
        enviados.append(telefono)
        logger.info(f"Seguimiento forzado enviado a {telefono}")
    return {"status": "ok", "enviados": len(enviados), "telefonos": enviados}


@app.get("/seguimiento/preview")
async def preview_seguimiento(x_password: str = None):
    """Muestra qué contactos recibirían seguimiento ahora (sin enviar)."""
    verificar_password(x_password)
    dias = int(await obtener_config("seguimiento_dias", "2"))
    telefonos = await obtener_conversaciones_para_seguimiento(dias)
    return {"pendientes": len(telefonos), "telefonos": telefonos}


@app.get("/api/avatar/{telefono}")
async def api_avatar_proxy(telefono: str, x_password: str = None):
    """Proxy para servir la foto de perfil de un contacto."""
    verificar_password(x_password)
    from agent.memory import obtener_avatar
    avatar_url = await obtener_avatar(telefono)
    if not avatar_url:
        raise HTTPException(status_code=404, detail="Sin avatar")
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(avatar_url)
            if resp.status_code != 200:
                raise HTTPException(status_code=404, detail="Avatar no disponible")
            content_type = resp.headers.get("content-type", "image/jpeg")
            return StreamingResponse(
                iter([resp.content]),
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except Exception:
        raise HTTPException(status_code=404, detail="Error al obtener avatar")


@app.get("/api/media/{media_id}")
async def api_media_proxy(media_id: str, x_password: str = None):
    """Proxy para servir audio/media de Evolution API al CRM."""
    verificar_password(x_password)
    from agent.whapi_helper import _get_base64_media
    data, mime_type = await _get_base64_media(media_id)
    if not data:
        raise HTTPException(status_code=404, detail="Media no encontrado")
    return StreamingResponse(
        iter([data]),
        media_type=mime_type or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.post("/api/conversaciones/{telefono}/resumen")
async def api_generar_resumen(telefono: str, x_password: str = None):
    """CRM — genera un resumen IA de la conversación y lo guarda."""
    verificar_password(x_password)
    historial = await obtener_historial(telefono, limite=50)
    if not historial:
        return {"status": "ok", "resumen": "Sin mensajes para resumir"}

    # Filtrar solo mensajes de texto (no media)
    lineas = []
    for msg in historial:
        if not msg["content"].startswith("["):
            rol = "Cliente" if msg["role"] == "user" else "Sofia"
            lineas.append(f"{rol}: {msg['content']}")
    texto_conv = "\n".join(lineas[:60])

    if not texto_conv.strip():
        return {"status": "ok", "resumen": "Solo hay mensajes multimedia en esta conversación"}

    from agent.brain import client, MODELO_RAPIDO
    response = await client.messages.create(
        model=MODELO_RAPIDO,
        max_tokens=150,
        system="Resumí conversaciones de ventas en 1-2 oraciones en español rioplatense. Solo el resumen, sin introducción.",
        messages=[{"role": "user", "content": f"Resumí:\n\n{texto_conv[:3000]}"}],
    )
    resumen = response.content[0].text.strip()

    from sqlalchemy import update as sql_update
    async with async_session() as session:
        await session.execute(
            sql_update(Conversacion)
            .where(Conversacion.telefono == telefono)
            .values(resumen=resumen)
        )
        await session.commit()

    logger.info(f"Resumen IA generado para {telefono}")
    return {"status": "ok", "resumen": resumen}


@app.post("/api/conversaciones/{telefono}/archivar")
async def api_archivar(telefono: str, x_password: str = None):
    """CRM — archiva una conversación (queda oculta por defecto)."""
    verificar_password(x_password)
    await archivar_conversacion(telefono)
    logger.info(f"Conversación {telefono} archivada")
    return {"status": "ok"}


@app.post("/api/conversaciones/{telefono}/desarchivar")
async def api_desarchivar(telefono: str, x_password: str = None):
    """CRM — desarchiva una conversación."""
    verificar_password(x_password)
    await desarchivar_conversacion(telefono)
    logger.info(f"Conversación {telefono} desarchivada")
    return {"status": "ok"}


@app.get("/api/stats/sofia")
async def api_stats_sofia(x_password: str = None):
    """CRM — estadísticas de actividad de Sofia."""
    verificar_password(x_password)
    return await obtener_stats_sofia()


_resync_voz_estado = {"corriendo": False, "ultimo": None}


@app.post("/resync-audios")
async def resync_audios_viejos(background_tasks: BackgroundTasks, x_password: str = None):
    """Re-sincroniza las conversaciones que tienen mensajes [voz] viejos."""
    verificar_password(x_password)
    if _resync_voz_estado["corriendo"]:
        return {"status": "ya_corriendo"}

    async def _tarea_resync():
        _resync_voz_estado["corriendo"] = True
        telefonos = await obtener_telefonos_con_voz()
        procesados, errores = 0, 0
        for telefono in telefonos:
            chat_id = f"{telefono}@s.whatsapp.net"
            try:
                mensajes = await fetch_mensajes(chat_id, count=100)
                await limpiar_historial(telefono)
                for msg in sorted(mensajes, key=lambda m: m.get("timestamp", 0)):
                    tipo = msg.get("type", "")
                    rol = "assistant" if msg.get("from_me") else "user"
                    if tipo == "text":
                        texto = msg.get("text", {}).get("body", "")
                    elif tipo == "audio":
                        audio_id = msg.get("audio", {}).get("id", "")
                        texto = f"[Audio]({audio_id})" if audio_id else "[Audio 🎙️]"
                    elif tipo == "image":
                        img_id = msg.get("image", {}).get("id", "")
                        caption = msg.get("image", {}).get("caption", "")
                        texto = (f"[Imagen]({img_id})" if img_id else "[Imagen]") + (f" {caption}" if caption else "")
                    elif tipo == "video":
                        vid_id = msg.get("video", {}).get("id", "")
                        caption = msg.get("video", {}).get("caption", "")
                        texto = (f"[Video]({vid_id})" if vid_id else "[Video]") + (f" {caption}" if caption else "")
                    elif tipo == "document":
                        nombre_doc = msg.get("document", {}).get("file_name", "archivo")
                        texto = f"[Documento: {nombre_doc}]"
                    elif tipo == "sticker":
                        texto = "[Sticker]"
                    else:
                        texto = f"[{tipo}]"
                    if texto:
                        await guardar_mensaje(telefono, rol, texto)
                procesados += 1
            except Exception as e:
                logger.error(f"Error resync-audios {telefono}: {e}")
                errores += 1
        _resync_voz_estado["corriendo"] = False
        _resync_voz_estado["ultimo"] = {"procesados": procesados, "errores": errores, "total": len(telefonos)}
        logger.info(f"Resync audios completo: {procesados} conversaciones, {errores} errores")

    background_tasks.add_task(_tarea_resync)
    return {"status": "iniciado", "mensaje": "Re-sincronizando audios en background"}


@app.get("/resync-audios/estado")
async def estado_resync_audios(x_password: str = None):
    """Estado del resync de audios."""
    verificar_password(x_password)
    return _resync_voz_estado


@app.post("/reactivar/{telefono}")
async def reactivar_sofia(telefono: str):
    """Reactiva a Sofia en una conversación que fue derivada."""
    from agent.memory import reactivar_conversacion
    await reactivar_conversacion(telefono)
    logger.info(f"Sofia reactivada para {telefono}")
    return {"status": "ok", "mensaje": f"Sofia reactivada para {telefono}"}


@app.post("/pausar/{telefono}")
async def pausar_sofia(telefono: str, x_password: str = None):
    """Pausa a Sofia en una conversación — Fedra toma el control."""
    verificar_password(x_password)
    await marcar_derivado(telefono)
    logger.info(f"Sofia pausada para {telefono}")
    return {"status": "ok", "mensaje": f"Sofia pausada para {telefono}"}


@app.get("/evolution/qr")
async def evolution_qr(x_password: str = None):
    """Devuelve el QR para conectar WhatsApp a Evolution API."""
    verificar_password(x_password)
    return await obtener_qr_evolution()


@app.post("/evolution/setup-webhook")
async def evolution_setup_webhook(x_password: str = None, request: Request = None):
    """Configura el webhook de Evolution API apuntando a esta instancia de Sofia."""
    verificar_password(x_password)
    body = await request.json() if request else {}
    # Usar la URL provista o autodetectar desde la request
    webhook_url = body.get("webhook_url", "")
    if not webhook_url:
        # Intentar construir desde env
        server_url = os.getenv("SERVER_URL", "")
        webhook_url = f"{server_url}/webhook" if server_url else ""
    if not webhook_url:
        raise HTTPException(status_code=400, detail="Proveer webhook_url o configurar SERVER_URL")
    ok = await configurar_webhook_evolution(webhook_url)
    return {"status": "ok" if ok else "error", "webhook_url": webhook_url}
