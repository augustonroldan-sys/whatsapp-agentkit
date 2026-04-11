# agent/providers/kommo.py — Adaptador para Kommo CRM
# Generado por AgentKit

"""
Proveedor de mensajes via Kommo CRM.
Kommo actúa como hub central: conecta WhatsApp Business e Instagram DMs
en una sola integración. Sofia recibe mensajes de ambos canales y responde
a través de la API de Kommo.

Flujo:
  Cliente (WhatsApp o Instagram)
      → Kommo CRM
      → webhook POST /webhook  (este servidor)
      → Sofia genera respuesta
      → Kommo API (envía al canal correcto)
      → Cliente recibe respuesta
"""

import os
import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")


class ProveedorKommo(ProveedorWhatsApp):
    """
    Proveedor de mensajes via Kommo CRM.
    Cubre WhatsApp Business e Instagram DMs desde una sola integración.
    """

    def __init__(self):
        self.token = os.getenv("KOMMO_ACCESS_TOKEN")
        self.subdominio = os.getenv("KOMMO_SUBDOMAIN")          # ej: new1775853543
        self.bot_user_id = os.getenv("KOMMO_BOT_USER_ID", "")

        # URL base usando el subdominio de la cuenta
        self.base_url = f"https://{self.subdominio}.kommo.com/api/v4"

    def _headers(self) -> dict:
        """Headers de autenticación para la API de Kommo."""
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """
        Parsea el payload de Kommo cuando llega un mensaje nuevo.

        Kommo envía los mensajes entrantes con esta estructura:
        {
          "message": {
            "add": [{
              "id": "...",
              "talk_id": 123,
              "chat_id": "...",
              "contact": {"id": 456, "name": "Juan"},
              "text": "Hola, quiero info de uniformes",
              "author": {"id": "...", "type": "contact"},
              "created_at": 1234567890
            }]
          }
        }
        """
        try:
            body = await request.json()
        except Exception:
            return []

        mensajes = []
        for msg in body.get("message", {}).get("add", []):
            # Solo procesar mensajes entrantes (type "contact" = cliente)
            autor = msg.get("author", {})
            if autor.get("type") != "contact":
                continue

            texto = msg.get("text", "").strip()
            if not texto:
                continue

            # Usamos talk_id como identificador de conversación
            talk_id = str(msg.get("talk_id", ""))
            contact_id = str(msg.get("contact", {}).get("id", ""))
            # Identificador único: contact_id para historial, talk_id para responder
            telefono = f"kommo_{contact_id}"

            mensajes.append(MensajeEntrante(
                telefono=telefono,
                texto=texto,
                mensaje_id=str(msg.get("id", "")),
                es_propio=False,
                # Guardamos talk_id en mensaje_id para usarlo al responder
            ))

            # Guardamos talk_id en el contexto para enviarlo en enviar_mensaje
            # Lo hacemos via un atributo temporal en el objeto
            mensajes[-1].__dict__["_talk_id"] = talk_id

        return mensajes

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """
        Envía una respuesta via Kommo API al talk correspondiente.
        El talk_id se extrae del telefono si fue guardado previamente.
        """
        if not self.token or not self.base_url:
            logger.warning("KOMMO_API_TOKEN o KOMMO_SUBDOMAIN no configurados")
            return False

        # talk_id se pasa como parte del telefono: "kommo_{contact_id}:{talk_id}"
        # Ver enviar_mensaje_por_talk para el flujo normal
        logger.warning(f"Llamada a enviar_mensaje sin talk_id para {telefono}")
        return False

    async def enviar_mensaje_por_talk(self, talk_id: str, mensaje: str) -> bool:
        """
        Envía un mensaje a un talk específico de Kommo.
        Este es el método principal que usa main.py para responder.
        """
        if not self.token or not self.base_url:
            logger.warning("KOMMO_API_TOKEN o KOMMO_SUBDOMAIN no configurados")
            return False

        url = f"{self.base_url}/talks/{talk_id}/messages"
        payload = {"text": mensaje}

        # Si hay un usuario bot configurado, firmamos el mensaje como ese usuario
        if self.bot_user_id:
            payload["author"] = {"id": int(self.bot_user_id), "type": "user"}

        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, headers=self._headers())
            if r.status_code not in (200, 201):
                logger.error(f"Error Kommo API (enviar mensaje): {r.status_code} — {r.text}")
            return r.status_code in (200, 201)

    async def asignar_lead_a_humano(self, talk_id: str, contacto_nombre: str, contexto: str) -> bool:
        """
        Cuando Sofia detecta intención de compra/presupuesto:
        - Agrega una nota al talk con el contexto de la conversación
        - Etiqueta el lead para que Fedra/Agustina lo tomen

        Args:
            talk_id: ID del talk en Kommo
            contacto_nombre: Nombre del cliente
            contexto: Resumen de lo que quiere el cliente
        """
        if not self.token or not self.base_url:
            return False

        nota = (
            f"Sofia (IA) detectó intención de compra/presupuesto.\n"
            f"Cliente: {contacto_nombre}\n"
            f"Contexto: {contexto}\n"
            f"→ Requiere seguimiento de Fedra o Agustina."
        )

        url = f"{self.base_url}/talks/{talk_id}/notes"
        payload = {"text": nota, "note_type": 4}  # type 4 = nota común

        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, headers=self._headers())
            if r.status_code not in (200, 201):
                logger.error(f"Error Kommo API (agregar nota): {r.status_code} — {r.text}")
                return False

        logger.info(f"Lead {talk_id} anotado en Kommo para seguimiento humano")
        return True
