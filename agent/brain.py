# agent/brain.py — Cerebro del agente: conexión con Claude API
# Generado por AgentKit

"""
Lógica de IA de Sofia. Lee el system prompt de prompts.yaml
y genera respuestas usando la API de Anthropic Claude.

Routing de modelos:
  - Haiku  → consultas simples (precios, horarios, saludos, catálogo)
  - Sonnet → tareas complejas (presupuestos, pedidos a medida, múltiples preguntas)
"""

import os
import base64
import yaml
import logging
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")

# Cliente de Anthropic
client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Modelos disponibles
MODELO_RAPIDO = "claude-haiku-4-5-20251001"   # Consultas simples — más barato
MODELO_POTENTE = "claude-sonnet-4-6"           # Tareas complejas — más capaz

# Palabras clave que indican una consulta compleja (usa Sonnet)
_PALABRAS_COMPLEJAS = [
    "presupuesto", "precio especial", "cantidad", "por mayor", "mayorista",
    "pedido", "bordado", "estampado", "a medida", "para mi empresa",
    "lote", "docena", "confirmar", "encargar", "cuántas unidades",
    "cuantas unidades", "necesito para", "quiero comprar", "me interesa",
    "varios", "varios colores", "varios talles", "personalizado",
]


def elegir_modelo(mensaje: str, historial: list[dict]) -> str:
    """
    Elige el modelo según la complejidad de la consulta.
    Haiku para preguntas simples, Sonnet para consultas complejas.
    """
    texto = mensaje.lower()

    # Si el historial es largo hay contexto acumulado → Sonnet
    if len(historial) > 6:
        return MODELO_POTENTE

    # Si el mensaje es largo puede ser complejo → Sonnet
    if len(mensaje) > 200:
        return MODELO_POTENTE

    # Si contiene palabras clave de compra/presupuesto → Sonnet
    if any(palabra in texto for palabra in _PALABRAS_COMPLEJAS):
        return MODELO_POTENTE

    # Para todo lo demás (precios, horarios, saludos, catálogo) → Haiku
    return MODELO_RAPIDO


def cargar_config_prompts() -> dict:
    """Lee toda la configuración desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    """Lee el system prompt desde config/prompts.yaml."""
    config = cargar_config_prompts()
    return config.get("system_prompt", "Sos Sofia, asistente de HeFe Uniformes. Respondé en español rioplatense.")


def obtener_mensaje_error() -> str:
    config = cargar_config_prompts()
    return config.get("error_message", "Lo siento, estoy teniendo un problema técnico. Por favor intentá de nuevo en unos minutos.")


def obtener_mensaje_fallback() -> str:
    config = cargar_config_prompts()
    return config.get("fallback_message", "Disculpá, no entendí bien tu consulta. ¿Podés reformularla?")


async def generar_respuesta(
    mensaje: str,
    historial: list[dict],
    imagen_bytes: bytes | None = None,
    imagen_mime: str = "image/jpeg",
) -> str:
    """
    Genera una respuesta usando Claude API (Sofia).
    Usa Haiku para consultas simples y Sonnet para las complejas.
    Si se recibe una imagen (imagen_bytes), usa Claude Vision con Sonnet.
    Lee el system prompt desde la DB si está configurado, sino desde YAML.
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    # Intentar leer system prompt desde DB (configurable desde el CRM)
    try:
        from agent.memory import obtener_config
        db_prompt = await obtener_config("system_prompt", "")
        system_prompt = db_prompt if db_prompt else cargar_system_prompt()
    except Exception:
        system_prompt = cargar_system_prompt()

    # Visión requiere Sonnet; de lo contrario elegir según complejidad
    modelo = MODELO_POTENTE if imagen_bytes else elegir_modelo(mensaje, historial)

    # Construir mensajes del historial (solo texto, sin imágenes previas)
    mensajes = [{"role": msg["role"], "content": msg["content"]} for msg in historial]

    # Mensaje actual: multimodal si hay imagen, texto si no
    if imagen_bytes:
        imagen_b64 = base64.standard_b64encode(imagen_bytes).decode("utf-8")
        mensajes.append({
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": imagen_mime,
                        "data": imagen_b64,
                    },
                },
                {"type": "text", "text": mensaje},
            ],
        })
    else:
        mensajes.append({"role": "user", "content": mensaje})

    try:
        response = await client.messages.create(
            model=modelo,
            max_tokens=1024,
            system=system_prompt,
            messages=mensajes
        )

        respuesta = response.content[0].text
        logger.info(f"[{modelo}] Respuesta generada ({response.usage.input_tokens} in / {response.usage.output_tokens} out)")
        return respuesta

    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error()
