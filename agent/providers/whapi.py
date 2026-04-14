# agent/providers/whapi.py — Adaptador para Whapi.cloud
# Generado por AgentKit

import os
import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")


class ProveedorWhapi(ProveedorWhatsApp):
    """
    Proveedor de WhatsApp usando Whapi.cloud (QR-based).
    El número sigue funcionando en el teléfono — no hay migración.
    """

    def __init__(self):
        self.token = os.getenv("WHAPI_TOKEN")
        self.url_base = "https://gate.whapi.cloud"

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Parsea el payload de Whapi.cloud."""
        body = await request.json()
        mensajes = []
        for msg in body.get("messages", []):
            # Ignorar mensajes propios
            if msg.get("from_me", False):
                continue
            tipo = msg.get("type", "")
            if tipo == "text":
                texto = msg.get("text", {}).get("body", "")
            else:
                continue  # por ahora solo texto
            mensajes.append(MensajeEntrante(
                telefono=msg.get("chat_id", "").replace("@s.whatsapp.net", ""),
                texto=texto,
                mensaje_id=msg.get("id", ""),
                es_propio=False,
            ))
        return mensajes

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envía mensaje via Whapi.cloud."""
        if not self.token:
            logger.warning("WHAPI_TOKEN no configurado — mensaje no enviado")
            return False
        # Whapi espera el número con @s.whatsapp.net o en formato directo
        destinatario = telefono if "@" in telefono else f"{telefono}@s.whatsapp.net"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.url_base}/messages/text",
                json={"to": destinatario, "body": mensaje},
                headers=headers,
            )
            if r.status_code not in (200, 201):
                logger.error(f"Error Whapi: {r.status_code} — {r.text}")
            return r.status_code in (200, 201)
