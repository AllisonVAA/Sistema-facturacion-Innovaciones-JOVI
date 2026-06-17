"""
database/storage.py — Persistencia SQLite para Innovaciones JOVI.

Tablas:
  facturas     — registro de cada venta de Loyverse y su estado de facturación.
  consecutivos — contador secuencial por tipo de comprobante (nunca se repite).
"""
import json
import logging
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from config import settings

logger = logging.getLogger(__name__)


# ── Enums ─────────────────────────────────────────────────────────────────────

class EstadoFactura(str, Enum):
    PENDIENTE  = "pendiente"
    ENVIADA    = "enviada"
    ACEPTADA   = "aceptada"
    RECHAZADA  = "rechazada"   # rechazo definitivo de Hacienda, no se reintenta
    ERROR      = "error"       # error técnico, se reintenta


class TipoError(str, Enum):
    VALIDACION = "validacion"   # datos incorrectos/faltantes → no reintenta
    CONEXION   = "conexion"     # fallo de red/API → reintenta
    DESCONOCIDO = "desconocido"


# ── Conexión ──────────────────────────────────────────────────────────────────

def _get_connection() -> sqlite3.Connection:
    db_path = Path(settings.DATABASE_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    # Habilita WAL para mejor concurrencia si se ejecutan múltiples procesos
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── Inicialización ────────────────────────────────────────────────────────────

def init_db() -> None:
    """Crea todas las tablas e índices si no existen."""
    with _get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS facturas (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                -- Identificadores
                loyverse_receipt_id     TEXT    NOT NULL UNIQUE,
                loyverse_receipt_number TEXT,
                -- Datos Hacienda
                clave_hacienda          TEXT,
                consecutivo             TEXT,
                tipo_comprobante        TEXT,
                -- Receptor
                receptor_cedula         TEXT,
                receptor_nombre         TEXT,
                receptor_email          TEXT,
                -- Montos
                total_comprobante       REAL,
                total_impuesto          REAL,
                -- Estado del proceso
                fecha_emision           TEXT,
                estado                  TEXT    NOT NULL DEFAULT 'pendiente',
                intentos                INTEGER NOT NULL DEFAULT 0,
                error_tipo              TEXT,
                -- Archivos y respuestas
                xml_path                TEXT,
                respuesta_hacienda      TEXT,
                detalle_error           TEXT,
                -- Email
                email_enviado           INTEGER NOT NULL DEFAULT 0,
                email_enviado_en        TEXT,
                -- Auditoría
                creado_en               TEXT    NOT NULL,
                actualizado_en          TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_facturas_estado
                ON facturas (estado);

            CREATE INDEX IF NOT EXISTS idx_facturas_fecha
                ON facturas (fecha_emision);

            -- Contador secuencial por tipo de comprobante.
            -- Una fila por tipo (01, 04, etc.).
            CREATE TABLE IF NOT EXISTS consecutivos (
                tipo_comprobante  TEXT NOT NULL,
                sucursal          TEXT NOT NULL,
                terminal          TEXT NOT NULL,
                ultimo_numero     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (tipo_comprobante, sucursal, terminal)
            );
            """
        )
        conn.commit()
    logger.debug("Base de datos inicializada en %s", settings.DATABASE_PATH)


# ── Consecutivos ──────────────────────────────────────────────────────────────

def obtener_siguiente_consecutivo(
    tipo: str, sucursal: str, terminal: str
) -> str:
    """
    Incrementa y retorna el siguiente número consecutivo de forma atómica.

    El consecutivo de Hacienda tiene 20 caracteres:
      tipo(2) + sucursal(3) + terminal(5) + numero(10)

    IMPORTANTE: una vez asignado, el número NO se reutiliza aunque la factura
    sea rechazada o falle, porque Hacienda lo registra en su sistema.
    """
    with _get_connection() as conn:
        # INSERT OR IGNORE crea la fila si no existe
        conn.execute(
            """
            INSERT OR IGNORE INTO consecutivos (tipo_comprobante, sucursal, terminal, ultimo_numero)
            VALUES (?, ?, ?, 0)
            """,
            (tipo, sucursal, terminal),
        )
        cursor = conn.execute(
            """
            UPDATE consecutivos
            SET    ultimo_numero = ultimo_numero + 1
            WHERE  tipo_comprobante = ? AND sucursal = ? AND terminal = ?
            RETURNING ultimo_numero
            """,
            (tipo, sucursal, terminal),
        )
        nuevo_numero = cursor.fetchone()["ultimo_numero"]
        conn.commit()

    tipo_fmt      = tipo.zfill(2)
    sucursal_fmt  = sucursal.zfill(3)
    terminal_fmt  = terminal.zfill(5)
    numero_fmt    = str(nuevo_numero).zfill(10)
    consecutivo   = f"{tipo_fmt}{sucursal_fmt}{terminal_fmt}{numero_fmt}"

    if len(consecutivo) != 20:
        raise ValueError(f"Consecutivo invalido ({len(consecutivo)} chars): {consecutivo}")

    logger.debug("Consecutivo asignado: %s", consecutivo)
    return consecutivo


# ── CRUD de facturas ──────────────────────────────────────────────────────────

def ya_fue_procesada(receipt_id: str) -> bool:
    """
    True si la venta ya está en estado ACEPTADA o ENVIADA.
    Previene duplicados en ejecuciones repetidas del día.
    """
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT estado FROM facturas WHERE loyverse_receipt_id = ?",
            (receipt_id,),
        ).fetchone()
    if row is None:
        return False
    return row["estado"] in (EstadoFactura.ACEPTADA, EstadoFactura.ENVIADA)


def registrar_factura(
    receipt_id: str,
    receipt_number: str | None,
    fecha_emision: str,
    tipo_comprobante: str,
    receptor_cedula: str | None = None,
    receptor_nombre: str | None = None,
    receptor_email: str | None = None,
    total_comprobante: float = 0.0,
    total_impuesto: float = 0.0,
) -> None:
    """
    Inserta un registro PENDIENTE. Si el receipt_id ya existe
    (caso de reintento), actualiza los metadatos sin duplicar.
    """
    ahora = datetime.now(timezone.utc).isoformat()
    with _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO facturas (
                loyverse_receipt_id, loyverse_receipt_number, fecha_emision,
                tipo_comprobante, receptor_cedula, receptor_nombre, receptor_email,
                total_comprobante, total_impuesto,
                estado, creado_en, actualizado_en
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pendiente', ?, ?)
            ON CONFLICT(loyverse_receipt_id) DO UPDATE SET
                actualizado_en  = excluded.actualizado_en,
                receptor_cedula = COALESCE(excluded.receptor_cedula, receptor_cedula),
                receptor_email  = COALESCE(excluded.receptor_email, receptor_email)
            """,
            (
                receipt_id, receipt_number, fecha_emision,
                tipo_comprobante, receptor_cedula, receptor_nombre, receptor_email,
                total_comprobante, total_impuesto,
                ahora, ahora,
            ),
        )
        conn.commit()


def actualizar_estado(
    receipt_id: str,
    estado: EstadoFactura,
    *,
    clave: str | None = None,
    consecutivo: str | None = None,
    xml_path: str | None = None,
    respuesta: dict[str, Any] | None = None,
    error: str | None = None,
    tipo_error: TipoError | None = None,
    total_comprobante: float | None = None,
    total_impuesto: float | None = None,
) -> None:
    """Actualiza el estado de una factura tras el intento de envío."""
    ahora = datetime.now(timezone.utc).isoformat()
    with _get_connection() as conn:
        conn.execute(
            """
            UPDATE facturas
            SET estado             = ?,
                clave_hacienda     = COALESCE(?, clave_hacienda),
                consecutivo        = COALESCE(?, consecutivo),
                xml_path           = COALESCE(?, xml_path),
                respuesta_hacienda = COALESCE(?, respuesta_hacienda),
                detalle_error      = COALESCE(?, detalle_error),
                error_tipo         = COALESCE(?, error_tipo),
                total_comprobante  = COALESCE(?, total_comprobante),
                total_impuesto     = COALESCE(?, total_impuesto),
                intentos           = intentos + 1,
                actualizado_en     = ?
            WHERE loyverse_receipt_id = ?
            """,
            (
                estado,
                clave,
                consecutivo,
                xml_path,
                json.dumps(respuesta, ensure_ascii=False) if respuesta else None,
                error,
                tipo_error,
                total_comprobante,
                total_impuesto,
                ahora,
                receipt_id,
            ),
        )
        conn.commit()
    logger.debug("Factura %s -> estado '%s'", receipt_id, estado)


def marcar_email_enviado(receipt_id: str) -> None:
    """Registra que el email de la factura fue enviado exitosamente."""
    ahora = datetime.now(timezone.utc).isoformat()
    with _get_connection() as conn:
        conn.execute(
            """
            UPDATE facturas
            SET email_enviado    = 1,
                email_enviado_en = ?,
                actualizado_en   = ?
            WHERE loyverse_receipt_id = ?
            """,
            (ahora, ahora, receipt_id),
        )
        conn.commit()


def obtener_para_reintento(max_intentos: int) -> list[dict]:
    """
    Facturas en estado ERROR con menos intentos que el máximo.
    Las RECHAZADAS no se reintentan (son definitivas).
    Las de error tipo VALIDACION tampoco (los datos son incorrectos).
    """
    with _get_connection() as conn:
        rows = conn.execute(
            """
            SELECT loyverse_receipt_id, loyverse_receipt_number,
                   intentos, clave_hacienda, consecutivo,
                   receptor_cedula, receptor_nombre, receptor_email,
                   tipo_comprobante, total_comprobante
            FROM   facturas
            WHERE  estado = 'error'
            AND    (error_tipo IS NULL OR error_tipo != 'validacion')
            AND    intentos < ?
            ORDER  BY creado_en
            """,
            (max_intentos,),
        ).fetchall()
    return [dict(r) for r in rows]


def resumen_del_dia() -> dict[str, int]:
    """Conteo de facturas por estado para el reporte final."""
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT estado, COUNT(*) AS total FROM facturas GROUP BY estado"
        ).fetchall()
    return {r["estado"]: r["total"] for r in rows}
