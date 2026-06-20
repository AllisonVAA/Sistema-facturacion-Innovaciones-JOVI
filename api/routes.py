"""
api/routes.py — Endpoints del servicio de facturación electrónica.

Endpoints públicos (sin autenticación):
  GET  /             → health check y estado del servicio
  GET  /api/status   → estado detallado con resumen de BD

Endpoints protegidos (requieren header X-API-Key):
  POST /api/facturar/hoy              → factura todas las ventas del día
  POST /api/facturar/recibo/{id}      → factura un recibo específico de Loyverse
  GET  /api/facturas                  → lista paginada de facturas
  GET  /api/facturas/{receipt_id}     → detalle de una factura

Webhook Loyverse (sin auth — validado por secret en query param):
  POST /api/webhook/loyverse          → recibe eventos de Loyverse y actualiza WooCommerce
"""
import logging
import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from api.auth import verificar_api_key
from config import settings
from database.storage import init_db, resumen_del_dia

logger = logging.getLogger(__name__)
router = APIRouter()
TZ_CR  = ZoneInfo("America/Costa_Rica")


# ── Modelos de respuesta (sin Pydantic complejo, dicts simples) ───────────────

def _ahora_cr() -> str:
    return datetime.now(TZ_CR).strftime("%Y-%m-%d %H:%M:%S")


# ── Endpoints públicos ────────────────────────────────────────────────────────

@router.get("/", tags=["Estado"])
async def health_check() -> dict:
    """Estado del servicio. No requiere autenticación."""
    return {
        "servicio": "Facturación Electrónica — Innovaciones JOVI",
        "estado":   "activo",
        "hora_cr":  _ahora_cr(),
        "ambiente": settings.AMBIENTE,
        "mock":     settings.USE_MOCK,
        "mock_loyverse": settings.USE_MOCK_LOYVERSE,
        "mock_hacienda": settings.USE_MOCK_HACIENDA,
        "hacienda_lista": settings.hacienda_lista(),
        "email_configurado": settings.email_configurado(),
    }


@router.get("/api/status", tags=["Estado"])
async def status_detallado() -> dict:
    """Estado detallado con resumen de la base de datos."""
    init_db()
    resumen = resumen_del_dia()
    return {
        "hora_cr":  _ahora_cr(),
        "ambiente": settings.AMBIENTE,
        "mock":     settings.USE_MOCK,
        "mock_loyverse": settings.USE_MOCK_LOYVERSE,
        "mock_hacienda": settings.USE_MOCK_HACIENDA,
        "hacienda_lista": settings.hacienda_lista(),
        "resumen_facturas_hoy": resumen,
        "hora_ejecucion_diaria": f"{settings.HORA_EJECUCION:02d}:00 (hora Costa Rica)",
    }


# ── Endpoints protegidos ──────────────────────────────────────────────────────

@router.post("/api/facturar/hoy", tags=["Facturación"])
async def facturar_hoy(
    _key: str = Depends(verificar_api_key),
) -> dict[str, Any]:
    """
    Obtiene todas las ventas del día en Loyverse y las factura.
    Este endpoint también es llamado automáticamente por el scheduler a las 5 PM CR.
    """
    # Importar aquí para no crear dependencias circulares al cargar la app
    from main import run as ejecutar_proceso

    logger.info("Facturacion manual iniciada via API")
    try:
        resultado = ejecutar_proceso()
        return {
            "mensaje": "Proceso completado",
            "hora_cr": _ahora_cr(),
            "resultado": resultado,
        }
    except SystemExit:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo conectar a Loyverse. Verifica el token.",
        )
    except Exception as exc:
        logger.error("Error en facturar_hoy: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.post("/api/facturar/recibo/{loyverse_id}", tags=["Facturación"])
async def facturar_recibo(
    loyverse_id: str,
    _key: str = Depends(verificar_api_key),
) -> dict[str, Any]:
    """
    Factura un recibo específico de Loyverse por su ID (UUID).
    Útil para reintentos manuales o pruebas puntuales.
    """
    from database.storage import ya_fue_procesada
    from facturacion.crlibre_adapter import procesar_factura
    from facturacion.email_sender import enviar_factura_por_email
    from database.storage import (
        EstadoFactura, actualizar_estado, marcar_email_enviado, registrar_factura
    )
    from loyverse.client import get_loyverse_client

    if ya_fue_procesada(loyverse_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"El recibo {loyverse_id} ya fue facturado exitosamente.",
        )

    cliente_lv = get_loyverse_client()

    # En modo real se necesita obtener el recibo específico de Loyverse
    if not settings.USE_MOCK_LOYVERSE:
        try:
            recibo_raw = cliente_lv._get(f"receipts/{loyverse_id}")
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recibo {loyverse_id} no encontrado en Loyverse: {exc}",
            )
        recibo = cliente_lv.enriquecer_recibo(recibo_raw)
    else:
        # En mock, buscar en los datos de prueba
        recibos = cliente_lv.get_receipts_today()
        recibo = next((r for r in recibos if r["id"] == loyverse_id), None)
        if recibo is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recibo {loyverse_id} no encontrado en los datos mock.",
            )

    # Verificar cédula
    cliente    = recibo.get("customer_data") or {}
    campo      = settings.LOYVERSE_CEDULA_FIELD
    cedula_raw = "".join(c for c in (cliente.get(campo) or "") if c.isdigit())
    if not cedula_raw:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El recibo no tiene cédula de cliente. No se puede facturar.",
        )

    store    = recibo.get("store_data", {})
    sucursal = store.get("sucursal", settings.DEFAULT_SUCURSAL)
    terminal = store.get("terminal", settings.DEFAULT_TERMINAL)
    tipo     = settings.TIPO_COMPROBANTE_CON_CEDULA

    registrar_factura(
        receipt_id        = loyverse_id,
        receipt_number    = str(recibo.get("receipt_number", "")),
        fecha_emision     = recibo.get("receipt_date", _ahora_cr()),
        tipo_comprobante  = tipo,
        receptor_cedula   = cedula_raw,
        receptor_nombre   = cliente.get("name"),
        receptor_email    = cliente.get("email"),
        total_comprobante = float(recibo.get("total_money", 0)),
        total_impuesto    = float(recibo.get("total_tax", 0)),
    )

    try:
        resultado = procesar_factura(recibo, tipo, sucursal, terminal)
        estado    = EstadoFactura.ACEPTADA if resultado["aceptada"] else EstadoFactura.RECHAZADA
        actualizar_estado(
            loyverse_id, estado,
            clave=resultado["clave"],
            consecutivo=resultado["consecutivo"],
            xml_path=resultado["xml_path"],
            respuesta=resultado["respuesta"],
        )
        if resultado["aceptada"]:
            email_ok = enviar_factura_por_email(recibo, resultado)
            if email_ok:
                marcar_email_enviado(loyverse_id)
        return {
            "recibo_id": loyverse_id,
            "estado":    estado,
            "clave":     resultado["clave"],
            "consecutivo": resultado["consecutivo"],
            "total":     resultado["total"],
            "email_enviado": resultado["aceptada"] and settings.email_configurado(),
        }
    except Exception as exc:
        logger.error("Error facturando recibo %s: %s", loyverse_id, exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )


@router.get("/api/facturas", tags=["Consulta"])
async def listar_facturas(
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(20, ge=1, le=100),
    estado: str | None = Query(None, description="pendiente|aceptada|rechazada|error"),
    _key: str = Depends(verificar_api_key),
) -> dict[str, Any]:
    """Lista paginada de facturas registradas en la base de datos."""
    from pathlib import Path
    db_path = Path(settings.DATABASE_PATH)
    if not db_path.exists():
        return {"total": 0, "pagina": pagina, "facturas": []}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    where  = "WHERE estado = ?" if estado else ""
    params = [estado] if estado else []

    total  = conn.execute(f"SELECT COUNT(*) FROM facturas {where}", params).fetchone()[0]
    offset = (pagina - 1) * por_pagina
    rows   = conn.execute(
        f"""
        SELECT loyverse_receipt_id, loyverse_receipt_number, estado,
               consecutivo, clave_hacienda, receptor_nombre, receptor_cedula,
               receptor_email, total_comprobante, total_impuesto,
               email_enviado, fecha_emision, creado_en
        FROM   facturas
        {where}
        ORDER  BY creado_en DESC
        LIMIT  ? OFFSET ?
        """,
        params + [por_pagina, offset],
    ).fetchall()
    conn.close()

    return {
        "total":     total,
        "pagina":    pagina,
        "por_pagina": por_pagina,
        "facturas":  [dict(r) for r in rows],
    }


@router.get("/api/facturas/{receipt_id}", tags=["Consulta"])
async def detalle_factura(
    receipt_id: str,
    _key: str = Depends(verificar_api_key),
) -> dict[str, Any]:
    """Detalle completo de una factura por su ID de Loyverse."""
    from pathlib import Path
    import json

    db_path = Path(settings.DATABASE_PATH)
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="Base de datos no inicializada.")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM facturas WHERE loyverse_receipt_id = ?", (receipt_id,)
    ).fetchone()
    conn.close()

    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Factura con receipt_id '{receipt_id}' no encontrada.",
        )

    data = dict(row)
    # Deserializar JSON de respuesta Hacienda
    if data.get("respuesta_hacienda"):
        try:
            data["respuesta_hacienda"] = json.loads(data["respuesta_hacienda"])
        except Exception:
            pass

    return data


# ── Webhook Loyverse → WooCommerce ────────────────────────────────────────────

@router.post("/api/webhook/loyverse", tags=["Sincronización"])
async def webhook_loyverse(request: Request) -> dict[str, Any]:
    """
    Recibe eventos de Loyverse (items.create, items.update, inventory_levels.update)
    y actualiza WooCommerce en tiempo real. No requiere X-API-Key ya que Loyverse
    no soporta headers personalizados, pero puedes agregar ?secret=TOKEN si querés
    validación extra.

    Configurar en Loyverse: Ajustes → Webhooks → URL:
      https://facturacion.innovacionesjovi.com/api/webhook/loyverse
    """
    import sys
    from pathlib import Path

    # Agregar la raíz del proyecto al path para importar sincronizacion/
    raiz = Path(__file__).parent.parent
    if str(raiz) not in sys.path:
        sys.path.insert(0, str(raiz))

    from sincronizacion.webhook_handler import procesar_evento

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload JSON inválido")

    # Loyverse envía el tipo de evento en el campo "type" del payload raíz
    evento  = body.get("type", "")
    payload = body.get("data", body)  # algunos eventos ponen los datos en "data"

    logger.info("Webhook Loyverse recibido: evento=%s", evento)

    resultado = procesar_evento(evento, payload)

    # Siempre retornar 200 a Loyverse para que no reintente
    return {"recibido": True, "evento": evento, "resultado": resultado}
