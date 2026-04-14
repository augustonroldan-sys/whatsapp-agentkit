# agent/main.py — Servidor FastAPI + Webhook
# Generado por AgentKit

"""
Servidor principal de Sofia para HeFe Uniformes.
Soporta Kommo CRM (WhatsApp + Instagram) y Meta Cloud API directa.
"""

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, esta_derivado, marcar_derivado, listar_conversaciones, actualizar_etapa
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
    Recibe mensajes de WhatsApp o Instagram via Kommo (o Meta directa).
    Sofia responde automáticamente a todos los mensajes nuevos.
    Si detecta intención de presupuesto/compra → deriva a Fedra o Agustina.
    """
    try:
        mensajes = await proveedor.parsear_webhook(request)

        for msg in mensajes:
            if msg.es_propio or not msg.texto:
                continue

            logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")

            # Si la conversación ya fue derivada a un humano → Sofia no responde más
            if await esta_derivado(msg.telefono):
                logger.info(f"Conversación {msg.telefono} derivada a humano — Sofia no interviene")
                continue

            # Obtener historial antes de agregar el mensaje actual
            historial = await obtener_historial(msg.telefono)

            # Detectar si el cliente quiere presupuesto/compra ANTES de responder
            requiere_humano = detectar_intencion_compra(msg.texto)

            if requiere_humano:
                # Sofia envía el mensaje de derivación y luego cede el control
                respuesta = generar_mensaje_derivacion()

                await guardar_mensaje(msg.telefono, "user", msg.texto)
                await guardar_mensaje(msg.telefono, "assistant", respuesta)
                await marcar_derivado(msg.telefono)
                await enviar_alerta_telegram(msg.telefono, msg.texto)

                # Enviar respuesta al cliente
                talk_id = msg.__dict__.get("_talk_id")
                if talk_id and hasattr(proveedor, "enviar_mensaje_por_talk"):
                    await proveedor.enviar_mensaje_por_talk(talk_id, respuesta)
                    # Agregar nota en Kommo para el equipo
                    await proveedor.asignar_lead_a_humano(
                        talk_id=talk_id,
                        contacto_nombre=msg.telefono,
                        contexto=msg.texto
                    )
                else:
                    await proveedor.enviar_mensaje(msg.telefono, respuesta)

                logger.info(f"Conversación {msg.telefono} derivada a humano")
                continue

            # Consulta normal → Sofia responde con Claude
            respuesta = await generar_respuesta(msg.texto, historial)

            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)

            # Enviar respuesta (Kommo usa talk_id, Meta usa telefono)
            talk_id = msg.__dict__.get("_talk_id")
            if talk_id and hasattr(proveedor, "enviar_mensaje_por_talk"):
                await proveedor.enviar_mensaje_por_talk(talk_id, respuesta)
            else:
                await proveedor.enviar_mensaje(msg.telefono, respuesta)

            logger.info(f"Sofia respondió a {msg.telefono}: {respuesta}")

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
