"""
sincronizacion/webhook_handler.py — Procesa eventos webhook de Loyverse.

Loyverse envía un POST a /api/webhook/loyverse cada vez que cambia el inventario,
se crea o modifica un ítem. Este módulo recibe el evento y actualiza WooCommerce.

Eventos que manejamos:
  - items.create      → crear producto en WooCommerce
  - items.update      → actualizar precio en WooCommerce
  - inventory_levels.update → actualizar stock en WooCommerce (requiere plan Advanced)

En plan gratuito solo llegan items.create e items.update (sin stock).
El stock se actualiza cuando Loyverse lo envía en el payload del ítem.
"""
import json
import logging
from pathlib import Path

from sincronizacion.woo_client import WooClient

import re

logger = logging.getLogger(__name__)


def _limpiar_nombre(nombre: str) -> str:
    return re.sub(r"[\.\s]*\bCOD[:\s]+\S+.*$", "", nombre, flags=re.IGNORECASE).strip()

MAPEO_PATH = Path(__file__).parent / "mapeo.json"


# ── Mapeo persistente ─────────────────────────────────────────────────────────

def cargar_mapeo() -> dict:
    if MAPEO_PATH.exists():
        with open(MAPEO_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def guardar_mapeo(mapeo: dict) -> None:
    with open(MAPEO_PATH, "w", encoding="utf-8") as f:
        json.dump(mapeo, f, ensure_ascii=False, indent=2)


# ── Extraer datos de la variante ──────────────────────────────────────────────

def _extraer_variante(item: dict) -> dict:
    """Extrae los datos relevantes de la primera variante del ítem."""
    variantes = item.get("variants", [])
    variante  = variantes[0] if variantes else {}
    stores    = variante.get("stores", [])
    store     = stores[0] if stores else {}

    return {
        "variant_id": variante.get("variant_id", ""),
        "sku":        (variante.get("sku") or "").strip(),
        "barcode":    (variante.get("barcode") or "").strip(),
        "precio":     str(int(variante.get("default_price") or 0)),
        "stock":      int(store.get("in_stock") or 0),
    }


# ── Handlers por tipo de evento ───────────────────────────────────────────────

def handle_item_create(item: dict, woo: WooClient) -> dict:
    """Crea el producto en WooCommerce y lo agrega al mapeo."""
    nombre   = _limpiar_nombre(item.get("item_name", "").strip())
    variante = _extraer_variante(item)
    vid      = variante["variant_id"]

    if not nombre or not vid:
        return {"accion": "ignorado", "razon": "item sin nombre o variant_id"}

    # No crear productos sin stock o de reparación
    if variante["stock"] == 0:
        return {"accion": "ignorado", "razon": "stock 0"}
    categoria = (item.get("category_name") or "").lower()
    if "reparac" in categoria:
        return {"accion": "ignorado", "razon": "categoria reparacion"}

    mapeo = cargar_mapeo()

    # Si ya existe en el mapeo solo actualizamos
    if vid in mapeo:
        woo_id = mapeo[vid]["woo_id"]
        ok, msg = woo.update_product(woo_id, variante["stock"], variante["precio"])
        logger.info("item.create (ya existe) '%s' woo_id=%s → %s", nombre, woo_id, msg)
        return {"accion": "actualizado", "woo_id": woo_id, "ok": ok}

    # Buscar primero por barcode/SKU
    woo_id = None
    for codigo in [variante["barcode"], variante["sku"]]:
        if codigo:
            encontrado = woo.find_by_sku(codigo)
            if encontrado:
                woo_id = str(encontrado["id"])
                break

    if woo_id:
        mapeo[vid] = {"woo_id": woo_id, "nombre": nombre, "sku": variante["sku"]}
        guardar_mapeo(mapeo)
        ok, msg = woo.update_product(woo_id, variante["stock"], variante["precio"])
        logger.info("item.create match '%s' woo_id=%s → %s", nombre, woo_id, msg)
        return {"accion": "match_y_actualizado", "woo_id": woo_id, "ok": ok}

    # Crear en WooCommerce
    sku_crear = variante["barcode"] or variante["sku"] or vid[:8]
    ok, resultado = woo.create_product(
        nombre, sku_crear, variante["stock"], variante["precio"]
    )
    if ok:
        mapeo[vid] = {"woo_id": resultado, "nombre": nombre, "sku": sku_crear}
        guardar_mapeo(mapeo)
        logger.info("item.create CREADO '%s' woo_id=%s", nombre, resultado)
        return {"accion": "creado", "woo_id": resultado, "ok": True}

    logger.error("item.create FALLO crear '%s': %s", nombre, resultado)
    return {"accion": "error", "detalle": resultado, "ok": False}


def handle_item_update(item: dict, woo: WooClient) -> dict:
    """Actualiza precio y stock en WooCommerce si el ítem está en el mapeo."""
    nombre   = _limpiar_nombre(item.get("item_name", "").strip())
    variante = _extraer_variante(item)
    vid      = variante["variant_id"]

    if not vid:
        return {"accion": "ignorado", "razon": "sin variant_id"}

    mapeo = cargar_mapeo()

    if vid not in mapeo:
        # No está en el mapeo — intentar crear
        logger.info("item.update '%s' no está en mapeo, intentando crear", nombre)
        return handle_item_create(item, woo)

    woo_id = mapeo[vid]["woo_id"]
    ok, msg = woo.update_product(woo_id, variante["stock"], variante["precio"])
    logger.info("item.update '%s' woo_id=%s stock=%d → %s",
                nombre, woo_id, variante["stock"], msg)
    return {"accion": "actualizado", "woo_id": woo_id, "ok": ok, "msg": msg}


def handle_inventory_update(payload: dict, woo: WooClient) -> dict:
    """
    Actualiza el stock cuando Loyverse envía un evento inventory_levels.update.
    Solo disponible en plan Advanced de Loyverse.
    """
    variant_id = payload.get("variant_id", "")
    in_stock   = int(payload.get("in_stock") or 0)

    if not variant_id:
        return {"accion": "ignorado", "razon": "sin variant_id"}

    mapeo = cargar_mapeo()

    if variant_id not in mapeo:
        logger.warning("inventory.update variant_id=%s no está en mapeo", variant_id)
        return {"accion": "ignorado", "razon": "variant_id no mapeado"}

    woo_id = mapeo[variant_id]["woo_id"]
    ok, msg = woo.update_product(woo_id, in_stock)
    logger.info("inventory.update woo_id=%s stock=%d → %s", woo_id, in_stock, msg)
    return {"accion": "stock_actualizado", "woo_id": woo_id, "stock": in_stock, "ok": ok}


# ── Dispatcher principal ──────────────────────────────────────────────────────

def procesar_evento(evento: str, payload: dict) -> dict:
    """
    Recibe el tipo de evento y el payload de Loyverse y ejecuta la acción correcta.
    """
    logger.info("Webhook Loyverse recibido: %s", evento)

    try:
        woo = WooClient()
    except ValueError as exc:
        logger.error("WooClient no configurado: %s", exc)
        return {"ok": False, "error": str(exc)}

    if evento in ("items.create",):
        return handle_item_create(payload, woo)

    if evento in ("items.update",):
        return handle_item_update(payload, woo)

    if evento in ("inventory_levels.update",):
        return handle_inventory_update(payload, woo)

    logger.debug("Evento no manejado: %s", evento)
    return {"accion": "ignorado", "evento": evento}
