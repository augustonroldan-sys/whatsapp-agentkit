# agent/tools.py — Herramientas de Sofia para HeFe Uniformes
# Generado por AgentKit

"""
Herramientas específicas de HeFe Uniformes.
Casos de uso: consultas de productos, presupuestos, derivación a humano.
"""

import os
import yaml
import logging
import httpx

logger = logging.getLogger("agentkit")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


async def enviar_alerta_telegram(contacto: str, mensaje: str) -> None:
    """Envía una alerta a Telegram cuando Sofia detecta un cliente interesado."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados")
        return
    texto = (
        f"🔥 *Cliente interesado en HeFe Uniformes*\n\n"
        f"📱 Contacto: `{contacto}`\n"
        f"💬 Mensaje: {mensaje}\n\n"
        f"👉 Sofia ya le avisó que Fedra o Agustina lo contactan."
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": texto,
                "parse_mode": "Markdown"
            })
    except Exception as e:
        logger.error(f"Error enviando alerta Telegram: {e}")


def cargar_info_negocio() -> dict:
    """Carga la información del negocio desde business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atención de HeFe Uniformes."""
    info = cargar_info_negocio()
    return {
        "horario": info.get("negocio", {}).get("horario", "L-V 8-12:30 y 16-20 hs / Sáb 9-13 hs"),
    }


def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca información relevante en los archivos de /knowledge.
    Retorna el contenido más relevante encontrado (productos, precios, etc.).
    """
    resultados = []
    knowledge_dir = "knowledge"

    if not os.path.exists(knowledge_dir):
        return "No hay archivos de conocimiento disponibles."

    for archivo in os.listdir(knowledge_dir):
        ruta = os.path.join(knowledge_dir, archivo)
        if archivo.startswith(".") or not os.path.isfile(ruta):
            continue
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
                if consulta.lower() in contenido.lower():
                    resultados.append(f"[{archivo}]: {contenido[:500]}")
        except (UnicodeDecodeError, IOError):
            continue

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontré información específica sobre eso en mis archivos."


def detectar_intencion_compra(mensaje: str) -> bool:
    """
    Detecta si el cliente tiene intención de compra o quiere un presupuesto.
    Retorna True si corresponde derivar a Fedra o Agustina.
    """
    palabras_clave = [
        "presupuesto", "precio especial", "cantidad", "por mayor", "mayorista",
        "pedido", "comprar", "compro", "quiero", "necesito", "cuánto sale",
        "cuanto sale", "me interesa", "bordado", "estampado", "a medida",
        "para mi empresa", "por cantidad", "lote", "docena", "docenas",
        "cerrar", "confirmar", "encargar"
    ]
    mensaje_lower = mensaje.lower()
    return any(palabra in mensaje_lower for palabra in palabras_clave)


def generar_mensaje_derivacion() -> str:
    """Retorna el mensaje estándar para derivar a humano."""
    return (
        "Excelente, para darte un presupuesto detallado te comunico con Fedra o Agustina del equipo. "
        "Te van a contactar en breve 👍"
    )
