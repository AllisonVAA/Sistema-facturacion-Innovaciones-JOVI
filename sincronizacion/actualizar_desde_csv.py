"""
sincronizacion/actualizar_desde_csv.py — Actualiza precios y stock en WooCommerce
desde un CSV exportado de Loyverse.

- Productos con stock > 0: actualiza precio y stock, los pone visibles
- Productos con stock = 0: los marca como agotados (outofstock) — se ocultan
  automáticamente si en WooCommerce → Ajustes → Inventario está activado
  "Ocultar artículos sin existencias del catálogo"
- Omite productos de categoría "reparac" y productos sin precio

Uso:
    python sincronizacion/actualizar_desde_csv.py --csv "ruta/al/export_items.csv"
    python sincronizacion/actualizar_desde_csv.py --csv "ruta/al/export_items.csv" --prueba
"""

import argparse
import csv
import json
import logging
import re
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from sincronizacion.woo_client import WooClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

MAPEO_PATH = Path(__file__).parent / "mapeo.json"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalizar(texto: str) -> str:
    texto = texto.lower().strip()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9\s]", " ", texto).strip()


def _similitud(a: str, b: str) -> float:
    ta = set(_normalizar(a).split())
    tb = set(_normalizar(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _limpiar_nombre(nombre: str) -> str:
    return re.sub(r"[\.\s]*\bCOD[:\s]+\S+.*$", "", nombre, flags=re.IGNORECASE).strip()


def cargar_mapeo() -> dict:
    if MAPEO_PATH.exists():
        with open(MAPEO_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


# ── Leer CSV ──────────────────────────────────────────────────────────────────

def leer_csv(ruta: Path) -> list[dict]:
    """Lee el CSV de Loyverse y retorna lista de productos normalizados."""
    productos = []
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for fila in reader:
            nombre    = fila.get("Nombre", "").strip()
            categoria = fila.get("Categoria", "").strip()
            ref       = fila.get("REF", "").strip()
            barcode   = fila.get("Codigo de barras", "").strip()
            precio_raw = fila.get("Precio [Innovaciones JOVI]", "").strip()
            stock_raw  = fila.get("En inventario [Innovaciones JOVI]", "").strip()
            seguir     = fila.get("Seguir el Inventario", "N").strip().upper()

            if not nombre:
                continue

            # Omitir servicios de reparación
            if "reparac" in categoria.lower():
                continue

            # Omitir precio variable o vacío
            if not precio_raw or precio_raw.lower() == "variable":
                continue

            try:
                precio = int(float(precio_raw))
            except ValueError:
                continue

            try:
                stock = int(float(stock_raw)) if stock_raw else 0
            except ValueError:
                stock = 0

            productos.append({
                "nombre":    _limpiar_nombre(nombre),
                "categoria": categoria,
                "ref":       ref,
                "barcode":   barcode,
                "precio":    precio,
                "stock":     stock,
                "manage_stock": seguir == "Y",
            })

    logger.info("Productos leídos del CSV: %d", len(productos))
    return productos


# ── Buscar en WooCommerce ─────────────────────────────────────────────────────

def buscar_woo_id(prod: dict, woo_products: list, woo: WooClient) -> str | None:
    """Busca el woo_id por barcode, REF o similitud de nombre."""
    for codigo in [prod["barcode"], prod["ref"]]:
        if codigo:
            encontrado = woo.find_by_sku(codigo)
            if encontrado:
                return str(encontrado["id"])

    mejor_score = 0.0
    mejor_id = None
    for wp in woo_products:
        score = _similitud(prod["nombre"], wp.get("name", ""))
        if score > mejor_score:
            mejor_score = score
            mejor_id = str(wp["id"])

    if mejor_score >= 0.70:
        return mejor_id

    return None


# ── Actualizar en WooCommerce ─────────────────────────────────────────────────

def actualizar_producto(woo: WooClient, woo_id: str, prod: dict, prueba: bool) -> tuple[bool, str]:
    """Actualiza precio, stock y visibilidad en WooCommerce."""
    stock = prod["stock"]
    precio = str(prod["precio"]) if prod["precio"] > 0 else ""

    if prueba:
        accion = "OCULTAR (stock=0)" if stock == 0 else f"ACTUALIZAR stock={stock} precio=₡{prod['precio']:,}"
        return True, accion

    payload: dict = {
        "regular_price": precio,
        "manage_stock":  prod["manage_stock"],
        "stock_quantity": max(stock, 0),
    }

    if stock == 0 and prod["manage_stock"]:
        # Marcar como agotado — se oculta si WooCommerce tiene configurado
        # "Ocultar artículos sin existencias del catálogo"
        payload["stock_status"] = "outofstock"
        payload["catalog_visibility"] = "hidden"
    else:
        payload["stock_status"] = "instock"
        payload["catalog_visibility"] = "visible"

    return woo.update_product(woo_id, stock, precio)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Actualiza WooCommerce desde CSV de Loyverse")
    parser.add_argument("--csv", required=True, help="Ruta al CSV exportado de Loyverse")
    parser.add_argument("--prueba", action="store_true", help="Simula sin modificar nada")
    args = parser.parse_args()

    ruta = Path(args.csv)
    if not ruta.exists():
        logger.error("Archivo no encontrado: %s", ruta)
        sys.exit(1)

    modo = "PRUEBA" if args.prueba else "REAL"
    logger.info("=== Actualizar desde CSV [%s] ===", modo)

    woo = WooClient()
    productos_csv = leer_csv(ruta)

    logger.info("Descargando productos de WooCommerce para matching...")
    woo_products = woo.get_all_products()

    mapeo = cargar_mapeo()
    # Invertir mapeo para búsqueda rápida por woo_id → nombre
    woo_id_set = {v["woo_id"] for v in mapeo.values()}

    actualizados = ocultados = sin_match = errores = 0

    for i, prod in enumerate(productos_csv, 1):
        # Buscar woo_id en mapeo primero (más rápido)
        woo_id = None
        for vid, datos in mapeo.items():
            if (prod["barcode"] and prod["barcode"] == datos.get("sku")) or \
               (prod["ref"] and prod["ref"] == datos.get("sku")):
                woo_id = datos["woo_id"]
                break

        # Si no está en mapeo, buscar en WooCommerce
        if not woo_id:
            woo_id = buscar_woo_id(prod, woo_products, woo)

        if not woo_id:
            sin_match += 1
            logger.debug("Sin match: %s", prod["nombre"][:50])
            continue

        ok, msg = actualizar_producto(woo, woo_id, prod, args.prueba)

        if ok:
            if prod["stock"] == 0:
                ocultados += 1
                logger.info("⊘ [%d/%d] OCULTO '%s' stock=0", i, len(productos_csv), prod["nombre"][:45])
            else:
                actualizados += 1
                logger.info("✓ [%d/%d] '%s' stock=%d precio=₡%s",
                            i, len(productos_csv), prod["nombre"][:40], prod["stock"], f"{prod['precio']:,}")
        else:
            errores += 1
            logger.error("✗ [%d/%d] '%s' → %s", i, len(productos_csv), prod["nombre"][:40], msg)

        if i % 20 == 0:
            time.sleep(0.5)

    logger.info("=" * 55)
    logger.info("RESUMEN [%s]", modo)
    logger.info("  CSV procesado        : %d productos", len(productos_csv))
    logger.info("  Actualizados         : %d", actualizados)
    logger.info("  Ocultados (stock=0)  : %d", ocultados)
    logger.info("  Sin match en WooC.   : %d", sin_match)
    logger.info("  Errores              : %d", errores)
    logger.info("=" * 55)


if __name__ == "__main__":
    main()
