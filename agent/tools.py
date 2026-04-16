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


def _keywords_posible_interes(mensaje: str) -> bool:
    """Pre-filtro rápido: detecta si el mensaje PODRÍA ser intención de compra."""
    # Señales de spam/publicidad — descartar inmediatamente
    spam_signals = ["www.", "http", ".com", ".net", "% off", "% OFF", "mayorista", "mayoristas",
                    "stockear", "proveedor", "proveedora", "te ofrezco", "te ofrecemos"]
    msg_lower = mensaje.lower()
    if any(s in msg_lower for s in spam_signals):
        return False
    # Palabras clave de intención real (más específicas)
    palabras_clave = [
        "presupuesto", "precio especial", "por mayor",
        "pedido", "comprar", "compro", "me interesa",
        "bordado", "estampado", "a medida", "para mi empresa",
        "por cantidad", "lote", "docena", "docenas",
        "cerrar", "confirmar", "encargar", "cuánto sale", "cuanto sale",
        "quiero encargar", "quiero pedir", "quiero comprar",
        "necesito uniformes", "necesito remeras", "necesito ropa",
        "cuántas unidades", "cuantas unidades",
    ]
    return any(p in msg_lower for p in palabras_clave)


def detectar_intencion_compra(mensaje: str) -> bool:
    """
    Pre-filtro sincrónico. Usar detectar_intencion_compra_ia para mayor precisión.
    """
    return _keywords_posible_interes(mensaje)


async def detectar_intencion_compra_ia(mensaje: str, historial: list = None) -> bool:
    """
    Usa Claude Haiku para determinar con contexto si hay intención real de compra.
    Solo se llama si el pre-filtro de keywords devuelve True.
    """
    if not _keywords_posible_interes(mensaje):
        return False

    import os
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", "").strip())

    contexto = ""
    if historial:
        ultimos = historial[-4:] if len(historial) > 4 else historial
        contexto = "\n".join([
            f"{'Cliente' if m['role'] == 'user' else 'Sofia'}: {m['content']}"
            for m in ultimos
        ])

    prompt = (
        f"HeFe Uniformes es un taller que fabrica uniformes de trabajo, escolares y deportivos en Argentina.\n\n"
        f"Mensaje del cliente: \"{mensaje}\"\n"
        f"{f'Conversación reciente:{chr(10)}{contexto}' if contexto else ''}\n\n"
        f"¿Este cliente está mostrando intención REAL de comprar uniformes a HeFe?\n"
        f"Respondé solo SI o NO.\n"
        f"NO si: es spam, publicidad, el cliente vende algo, habla de su propio negocio, o el mensaje es ambiguo."
    )

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": prompt}]
        )
        respuesta = response.content[0].text.strip().upper()
        logger.info(f"Intención compra IA: '{mensaje[:60]}' → {respuesta}")
        return "SI" in respuesta
    except Exception as e:
        logger.error(f"Error detectando intención IA: {e}")
        return _keywords_posible_interes(mensaje)  # fallback al pre-filtro


def generar_mensaje_derivacion() -> str:
    """Retorna el mensaje estándar para derivar a humano."""
    return (
        "Excelente, para darte un presupuesto detallado te comunico con Fedra o Agustina del equipo. "
        "Te van a contactar en breve 👍"
    )
