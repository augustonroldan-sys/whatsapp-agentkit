# agent/memory.py — Memoria de conversaciones con SQLite
# Generado por AgentKit

"""
Sistema de memoria de Sofia. Guarda el historial de conversaciones
por número de teléfono usando SQLite (local) o PostgreSQL (producción).
"""

import os
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, select, Integer, Boolean
from dotenv import load_dotenv

load_dotenv()

# Configuración de base de datos
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")

# Si es PostgreSQL en producción, ajustar el esquema de URL
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    """Modelo de mensaje en la base de datos."""
    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))  # "user" o "assistant"
    content: Mapped[str] = mapped_column(Text)
    message_id: Mapped[str] = mapped_column(String(200), default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Conversacion(Base):
    """
    Estado de cada conversación.
    Permite saber si fue derivada a un humano (Sofia no interviene).
    """
    __tablename__ = "conversaciones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    nombre: Mapped[str] = mapped_column(String(200), default="")
    derivada: Mapped[bool] = mapped_column(Boolean, default=False)
    etapa: Mapped[str] = mapped_column(String(50), default="nuevo")
    cobro_pendiente: Mapped[bool] = mapped_column(Boolean, default=False)
    monto_cobro: Mapped[str] = mapped_column(String(200), default="")
    resumen: Mapped[str] = mapped_column(String(500), default="")
    contacto_existente: Mapped[bool] = mapped_column(Boolean, default=False)
    notas: Mapped[str] = mapped_column(Text, default="")
    seguimiento_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    actualizado: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Configuracion(Base):
    """Configuración del sistema — pares clave/valor."""
    __tablename__ = "configuracion"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    clave: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    valor: Mapped[str] = mapped_column(String(500), default="")


async def inicializar_db():
    """Crea las tablas si no existen y agrega columnas nuevas."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Migraciones suaves — agregan columnas si no existen
        migraciones = [
            "ALTER TABLE mensajes ADD COLUMN IF NOT EXISTS message_id VARCHAR(200) DEFAULT ''",
            "ALTER TABLE conversaciones ADD COLUMN IF NOT EXISTS notas TEXT DEFAULT ''",
            "ALTER TABLE conversaciones ADD COLUMN IF NOT EXISTS seguimiento_at TIMESTAMP",
        ]
        for sql in migraciones:
            try:
                await conn.execute(__import__("sqlalchemy").text(sql))
            except Exception:
                pass  # SQLite no soporta IF NOT EXISTS — ignorar


async def obtener_config(clave: str, default: str = "") -> str:
    """Lee un valor de configuración."""
    async with async_session() as session:
        result = await session.execute(select(Configuracion).where(Configuracion.clave == clave))
        cfg = result.scalar_one_or_none()
        return cfg.valor if cfg else default


async def guardar_config(clave: str, valor: str):
    """Guarda o actualiza un valor de configuración."""
    async with async_session() as session:
        result = await session.execute(select(Configuracion).where(Configuracion.clave == clave))
        cfg = result.scalar_one_or_none()
        if cfg:
            cfg.valor = valor
        else:
            session.add(Configuracion(clave=clave, valor=valor))
        await session.commit()


async def guardar_mensaje(telefono: str, role: str, content: str, message_id: str = ""):
    """Guarda un mensaje en el historial de conversación."""
    async with async_session() as session:
        mensaje = Mensaje(
            telefono=telefono,
            role=role,
            content=content,
            message_id=message_id,
            timestamp=datetime.utcnow()
        )
        session.add(mensaje)
        await session.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    """
    Recupera los últimos N mensajes de una conversación.

    Args:
        telefono: Número de teléfono del cliente
        limite: Máximo de mensajes a recuperar (default: 20)

    Returns:
        Lista de diccionarios con role y content
    """
    async with async_session() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.timestamp.desc())
            .limit(limite)
        )
        result = await session.execute(query)
        mensajes = result.scalars().all()

        # Invertir para orden cronológico (los más recientes están primero)
        mensajes.reverse()

        return [
            {"role": msg.role, "content": msg.content, "message_id": msg.message_id or ""}
            for msg in mensajes
        ]


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversación."""
    async with async_session() as session:
        query = select(Mensaje).where(Mensaje.telefono == telefono)
        result = await session.execute(query)
        mensajes = result.scalars().all()
        for msg in mensajes:
            await session.delete(msg)
        await session.commit()


async def esta_derivado(telefono: str) -> bool:
    """Retorna True si la conversación fue derivada a un humano."""
    async with async_session() as session:
        result = await session.execute(
            select(Conversacion).where(Conversacion.telefono == telefono)
        )
        conv = result.scalar_one_or_none()
        return conv.derivada if conv else False


async def marcar_derivado(telefono: str):
    """Marca la conversación como derivada a humano — Sofia deja de responder."""
    async with async_session() as session:
        result = await session.execute(
            select(Conversacion).where(Conversacion.telefono == telefono)
        )
        conv = result.scalar_one_or_none()
        if conv:
            conv.derivada = True
            conv.actualizado = datetime.utcnow()
        else:
            session.add(Conversacion(telefono=telefono, derivada=True))
        await session.commit()


async def actualizar_etapa(telefono: str, etapa: str):
    """Actualiza la etapa del pipeline de una conversación."""
    async with async_session() as session:
        result = await session.execute(
            select(Conversacion).where(Conversacion.telefono == telefono)
        )
        conv = result.scalar_one_or_none()
        if conv:
            conv.etapa = etapa
            conv.actualizado = datetime.utcnow()
            await session.commit()


async def listar_conversaciones() -> list[dict]:
    """Retorna todas las conversaciones con su último mensaje."""
    async with async_session() as session:
        result = await session.execute(
            select(Conversacion).order_by(Conversacion.actualizado.desc())
        )
        convs = result.scalars().all()
        output = []
        for conv in convs:
            ultimo = await session.execute(
                select(Mensaje)
                .where(Mensaje.telefono == conv.telefono)
                .order_by(Mensaje.timestamp.desc())
                .limit(1)
            )
            msg = ultimo.scalar_one_or_none()
            output.append({
                "telefono": conv.telefono,
                "nombre": conv.nombre or conv.telefono,
                "etapa": conv.etapa,
                "derivada": conv.derivada,
                "cobro_pendiente": conv.cobro_pendiente,
                "monto_cobro": conv.monto_cobro or "",
                "resumen": conv.resumen or "",
                "contacto_existente": conv.contacto_existente,
                "actualizado": conv.actualizado.isoformat(),
                "ultimo_mensaje": msg.content[:100] if msg else "",
                "ultimo_rol": msg.role if msg else "",
            })
        return output


async def actualizar_nombre(telefono: str, nombre: str):
    """Actualiza el nombre de un contacto."""
    async with async_session() as session:
        result = await session.execute(select(Conversacion).where(Conversacion.telefono == telefono))
        conv = result.scalar_one_or_none()
        if conv:
            conv.nombre = nombre
            conv.actualizado = datetime.utcnow()
            await session.commit()
        else:
            session.add(Conversacion(telefono=telefono, nombre=nombre))
            await session.commit()


async def upsert_conversacion(telefono: str, nombre: str = "", etapa: str = "nuevo",
                               cobro_pendiente: bool = False, monto_cobro: str = "",
                               resumen: str = "", contacto_existente: bool = False):
    """Crea o actualiza una conversación completa desde la sincronización."""
    async with async_session() as session:
        result = await session.execute(select(Conversacion).where(Conversacion.telefono == telefono))
        conv = result.scalar_one_or_none()
        if conv:
            if nombre:
                conv.nombre = nombre
            conv.etapa = etapa
            conv.cobro_pendiente = cobro_pendiente
            conv.monto_cobro = monto_cobro or ""
            conv.resumen = resumen
            conv.contacto_existente = contacto_existente
            conv.actualizado = datetime.utcnow()
        else:
            session.add(Conversacion(
                telefono=telefono,
                nombre=nombre,
                etapa=etapa,
                cobro_pendiente=cobro_pendiente,
                monto_cobro=monto_cobro or "",
                resumen=resumen,
                contacto_existente=contacto_existente,
            ))
        await session.commit()


async def obtener_conversaciones_para_seguimiento(dias: int) -> list[str]:
    """
    Retorna teléfonos que necesitan seguimiento:
    - No derivadas, no cerradas
    - Último mensaje fue de Sofia (assistant)
    - Ese mensaje tiene más de `dias` días
    - No recibieron seguimiento en los últimos `dias` días
    """
    cutoff = datetime.utcnow() - timedelta(days=dias)
    async with async_session() as session:
        result = await session.execute(
            select(Conversacion).where(
                Conversacion.derivada == False,
                Conversacion.etapa != "cerrado",
            )
        )
        convs = result.scalars().all()
        telefonos = []
        for conv in convs:
            # No re-enviar si ya mandamos seguimiento recientemente
            if conv.seguimiento_at and conv.seguimiento_at > cutoff:
                continue
            # Verificar que el último mensaje sea de Sofia y sea viejo
            ultimo = await session.execute(
                select(Mensaje)
                .where(Mensaje.telefono == conv.telefono)
                .order_by(Mensaje.timestamp.desc())
                .limit(1)
            )
            msg = ultimo.scalar_one_or_none()
            if msg and msg.role == "assistant" and msg.timestamp < cutoff:
                telefonos.append(conv.telefono)
        return telefonos


async def marcar_seguimiento(telefono: str):
    """Registra que se envió un seguimiento a este contacto ahora."""
    async with async_session() as session:
        result = await session.execute(select(Conversacion).where(Conversacion.telefono == telefono))
        conv = result.scalar_one_or_none()
        if conv:
            conv.seguimiento_at = datetime.utcnow()
            await session.commit()


async def obtener_notas(telefono: str) -> str:
    """Devuelve las notas privadas de una conversación."""
    async with async_session() as session:
        result = await session.execute(select(Conversacion).where(Conversacion.telefono == telefono))
        conv = result.scalar_one_or_none()
        return conv.notas if conv and conv.notas else ""


async def guardar_notas(telefono: str, notas: str):
    """Guarda o actualiza las notas privadas de una conversación."""
    async with async_session() as session:
        result = await session.execute(select(Conversacion).where(Conversacion.telefono == telefono))
        conv = result.scalar_one_or_none()
        if conv:
            conv.notas = notas
            conv.actualizado = datetime.utcnow()
        else:
            session.add(Conversacion(telefono=telefono, notas=notas))
        await session.commit()


async def reactivar_conversacion(telefono: str):
    """
    Reactiva a Sofia en una conversación previamente derivada.
    Llamar cuando el humano terminó de atender al cliente.
    """
    async with async_session() as session:
        result = await session.execute(
            select(Conversacion).where(Conversacion.telefono == telefono)
        )
        conv = result.scalar_one_or_none()
        if conv:
            conv.derivada = False
            conv.actualizado = datetime.utcnow()
            await session.commit()
