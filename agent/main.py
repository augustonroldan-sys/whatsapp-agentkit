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
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import (
    inicializar_db, guardar_mensaje, obtener_historial, esta_derivado,
    marcar_derivado, listar_conversaciones, actualizar_etapa,
    limpiar_historial, async_session, Conversacion,
    actualizar_nombre, upsert_conversacion
)
from agent.whapi_helper import (
    fetch_chats, fetch_mensajes, fetch_nombre_contacto,
    descargar_audio, transcribir_audio, clasificar_conversacion_con_ia,
    es_contacto_nuevo, analizar_estilo_fedra,
    descargar_media, extraer_texto_documento
)
from agent.providers import obtener_proveedor
from agent.tools import detectar_intencion_compra, generar_mensaje_derivacion, enviar_alerta_telegram

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await inicializar_db()
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


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Recibe mensajes de WhatsApp.
    - Solo responde a contactos NUEVOS (sin historial con Fedra)
    - Transcribe audios automáticamente
    - Guarda el nombre real del contacto
    - Si detecta intención de compra → deriva a Fedra
    """
    try:
        body = await request.json()
        mensajes_raw = body.get("messages", [])

        for msg_raw in mensajes_raw:
            if msg_raw.get("from_me", False):
                continue

            telefono = msg_raw.get("from", "") or msg_raw.get("chat_id", "").replace("@s.whatsapp.net", "")
            if not telefono:
                continue

            tipo = msg_raw.get("type", "")
            nombre_contacto = msg_raw.get("from_name", "") or await fetch_nombre_contacto(telefono)

            # Guardar nombre real si lo tenemos
            if nombre_contacto:
                await actualizar_nombre(telefono, nombre_contacto)

            # Extraer texto según tipo de mensaje
            texto = None
            if tipo == "text":
                texto = msg_raw.get("text", {}).get("body", "")
            elif tipo == "audio":
                # Transcribir audio
                audio_info = msg_raw.get("audio", {})
                media_id = audio_info.get("id", "")
                mime_type = audio_info.get("mime_type", "audio/ogg")
                if media_id:
                    audio_bytes = await descargar_audio(media_id)
                    if audio_bytes:
                        texto = await transcribir_audio(audio_bytes, mime_type)
                        if texto:
                            logger.info(f"Audio transcripto de {telefono}: {texto}")
                        else:
                            texto = "[Audio recibido — no se pudo transcribir]"
                    else:
                        texto = "[Audio recibido — no se pudo descargar]"
                if not texto or texto.startswith("[Audio"):
                    await proveedor.enviar_mensaje(
                        telefono,
                        "Recibí tu audio 🎙️ Por ahora no puedo escucharlo. ¿Podés escribirme lo que necesitás?"
                    )
                    continue
            elif tipo == "image":
                caption = msg_raw.get("image", {}).get("caption", "")
                texto = caption if caption else None
            elif tipo == "video":
                caption = msg_raw.get("video", {}).get("caption", "")
                texto = caption if caption else None
            elif tipo == "document":
                doc_info = msg_raw.get("document", {})
                media_id = doc_info.get("id", "")
                nombre_doc = doc_info.get("file_name", "documento")
                mime_type = doc_info.get("mime_type", "")
                caption = doc_info.get("caption", "")

                # Guardar en CRM con link descargable
                url_media = f"https://gate.whapi.cloud/media/{media_id}" if media_id else ""
                texto_crm = f"[Documento: {nombre_doc}]({url_media})" if url_media else f"[Documento: {nombre_doc}]"
                await guardar_mensaje(telefono, "user", texto_crm)

                # Intentar extraer texto para que Sofia lo entienda
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
                # Igual guardamos el mensaje para el CRM
                await guardar_mensaje(telefono, "user", texto)
                continue

            historial = await obtener_historial(telefono)
            requiere_humano = detectar_intencion_compra(texto)

            if requiere_humano:
                respuesta = generar_mensaje_derivacion()
                await guardar_mensaje(telefono, "user", texto)
                await guardar_mensaje(telefono, "assistant", respuesta)
                await marcar_derivado(telefono)
                await enviar_alerta_telegram(nombre_contacto or telefono, texto)
                await proveedor.enviar_mensaje(telefono, respuesta)
                logger.info(f"Conversación {telefono} derivada a humano")
                continue

            # Sofia responde
            respuesta = await generar_respuesta(texto, historial)
            await guardar_mensaje(telefono, "user", texto)
            await guardar_mensaje(telefono, "assistant", respuesta)
            await proveedor.enviar_mensaje(telefono, respuesta)
            logger.info(f"Sofia respondió a {nombre_contacto or telefono}: {respuesta}")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


CRM_PASSWORD = os.getenv("CRM_PASSWORD", "hefe2026")


def verificar_password(x_password: str = None):
    if x_password != CRM_PASSWORD:
        raise HTTPException(status_code=401, detail="No autorizado")


@app.get("/api/conversaciones")
async def api_conversaciones(x_password: str = None):
    """CRM — lista todas las conversaciones."""
    verificar_password(x_password)
    return await listar_conversaciones()


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
    if not texto:
        raise HTTPException(status_code=400, detail="Mensaje vacío")
    await proveedor.enviar_mensaje(telefono, texto)
    await guardar_mensaje(telefono, "assistant", texto)
    logger.info(f"Mensaje manual enviado a {telefono}: {texto}")
    return {"status": "ok"}


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
            await limpiar_historial(telefono)
            for msg in sorted(mensajes, key=lambda m: m.get("timestamp", 0)):
                tipo = msg.get("type", "")
                rol = "assistant" if msg.get("from_me") else "user"
                if tipo == "text":
                    texto = msg.get("text", {}).get("body", "")
                elif tipo == "image":
                    caption = msg.get("image", {}).get("caption", "")
                    texto = f"[Imagen]{': ' + caption if caption else ''}"
                elif tipo == "video":
                    caption = msg.get("video", {}).get("caption", "")
                    texto = f"[Video]{': ' + caption if caption else ''}"
                elif tipo == "audio":
                    texto = "[Audio 🎙️]"
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


@app.post("/reactivar/{telefono}")
async def reactivar_sofia(telefono: str):
    """
    Endpoint para reactivar a Sofia en una conversación que fue derivada.
    Llamar desde Kommo o manualmente cuando el humano termina de atender.
    """
    from agent.memory import reactivar_conversacion
    await reactivar_conversacion(telefono)
    logger.info(f"Sofia reactivada para {telefono}")
    return {"status": "ok", "mensaje": f"Sofia reactivada para {telefono}"}
