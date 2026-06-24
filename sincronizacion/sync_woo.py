"""
sincronizacion/sync_woo.py — Sincronización automática Loyverse → WooCommerce.

Funcionamiento:
  1. Descarga todos los productos e inventario desde la API de Loyverse.
  2. Carga el mapeo persistido en mapeo.json (variant_id → woo_id).
  3. Para cada producto de Loyverse:
     - Si ya está en el mapeo → actualiza stock y precio en WooCommerce.
     - Si es nuevo → busca en WooCommerce por barcode/SKU; si lo encuentra
       lo agrega al mapeo; si no, lo crea automáticamente.
  4. Guarda el mapeo actualizado en mapeo.json.

Uso manual:
    python sync_woo.py               # modo real
    python sync_woo.py --prueba      # simula sin modificar nada

Cron recomendado (cada 6 horas):
    0 */6 * * * cd /opt/facturacion-jovi && python3 sincronizacion/sync_woo.py >> logs/sync_woo.log 2>&1
"""

import argparse
import json
import logging
import sys
import time
import unicodedata
import re
from pathlib import Path
from datetime import datetime

# Agregar el directorio raíz del proyecto al path para importar loyverse/
sys.path.insert(0, str(Path(__file__).parent.parent))

from loyverse.client import LoyverseClient
from sincronizacion.woo_client import WooClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

MAPEO_PATH = Path(__file__).parent / "mapeo.json"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _limpiar_nombre(nombre: str) -> str:
    """Elimina sufijos de código interno tipo '. COD: ABC123' del nombre."""
    import re
    return re.sub(r"[\.\s]*\bCOD[:\s]+\S+.*$", "", nombre, flags=re.IGNORECASE).strip()


def _normalizar(texto: str) -> str:
    texto = texto.lower().strip()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def _similitud(a: str, b: str) -> float:
    tokens_a = set(_normalizar(a).split())
    tokens_b = set(_normalizar(b).split())
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


# ── Mapeo persistente ─────────────────────────────────────────────────────────

def cargar_mapeo() -> dict:
    """
    Carga el mapeo desde mapeo.json.
    Estructura: {"variant_id": {"woo_id": "123", "nombre": "...", "sku": "..."}}
    """
    if MAPEO_PATH.exists():
        with open(MAPEO_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def guardar_mapeo(mapeo: dict) -> None:
    with open(MAPEO_PATH, "w", encoding="utf-8") as f:
        json.dump(mapeo, f, ensure_ascii=False, indent=2)
    logger.info("Mapeo guardado: %d entradas en %s", len(mapeo), MAPEO_PATH)


# ── Aplanar productos de Loyverse ─────────────────────────────────────────────

def aplanar_items(items: list[dict], categorias: dict, niveles: dict) -> list[dict]:
    """
    Convierte la estructura anidada de Loyverse (item → variantes) en una
    lista plana de productos listos para sincronizar.
    """
    planos = []
    for item in items:
        nombre    = item.get("item_name", "").strip()
        cat_id    = item.get("category_id") or ""
        categoria = categorias.get(cat_id, "")
        variantes = item.get("variants", [])

        nombre = _limpiar_nombre(nombre)

        # Omitir servicios de reparación
        if "reparac" in categoria.lower():
            continue

        for variante in variantes:
            vid    = variante.get("variant_id", "")
            sku    = (variante.get("sku") or "").strip()
            barcode = (variante.get("barcode") or "").strip()
            precio  = variante.get("default_price", 0) or 0
            stock   = int(niveles.get(vid, 0))

            if not vid:
                continue

            planos.append({
                "variant_id": vid,
                "nombre":     nombre,
                "sku":        sku,
                "barcode":    barcode,
                "precio":     str(int(precio)) if precio else "",
                "stock":      stock,
                "categoria":  categoria,
            })
    return planos


# ── Buscar match en WooCommerce ───────────────────────────────────────────────

def buscar_en_woo(prod: dict, woo_products: list[dict], woo_client: WooClient) -> str | None:
    """
    Intenta encontrar el producto en WooCommerce.
    Orden de búsqueda: barcode==SKU → sku==SKU → similitud de nombre.
    Retorna el woo_id como string, o None si no se encuentra.
    """
    # 1. Por barcode o SKU exacto vía API
    for codigo in [prod["barcode"], prod["sku"]]:
        if codigo:
            encontrado = woo_client.find_by_sku(codigo)
            if encontrado:
                return str(encontrado["id"])

    # 2. Por similitud de nombre contra los productos ya descargados
    mejor_score = 0.0
    mejor_id = None
    for wp in woo_products:
        score = _similitud(prod["nombre"], wp.get("name", ""))
        if score > mejor_score:
            mejor_score = score
            mejor_id = str(wp["id"])

    if mejor_score >= 0.70:
        logger.debug(
            "Match por nombre (%.0f%%): '%s' → WooID %s",
            mejor_score * 100, prod["nombre"], mejor_id,
        )
        return mejor_id

    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync automático Loyverse → WooCommerce")
    parser.add_argument("--prueba", action="store_true", help="Simula sin modificar nada")
    args = parser.parse_args()

    modo = "PRUEBA" if args.prueba else "REAL"
    logger.info("=== Sync Loyverse → WooCommerce [%s] — %s ===",
                modo, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # Clientes
    loy = LoyverseClient()
    woo = WooClient()

    # Descargar datos
    logger.info("Descargando datos de Loyverse...")
    categorias = loy.get_categories()
    items      = loy.get_items()
    niveles    = loy.get_inventory_levels(items)
    productos  = aplanar_items(items, categorias, niveles)
    logger.info("Productos Loyverse (variantes): %d", len(productos))

    # Descargar productos WooCommerce para match por nombre
    logger.info("Descargando productos de WooCommerce...")
    woo_products = woo.get_all_products()

    # Cargar mapeo persistido
    mapeo = cargar_mapeo()
    mapeo_modificado = False

    # Contadores
    actualizados = creados = sin_match = errores = 0
    errores_log: list[str] = []

    for i, prod in enumerate(productos, 1):
        vid    = prod["variant_id"]
        nombre = prod["nombre"]
        stock  = prod["stock"]
        precio = prod["precio"]

        # ── Caso 1: ya está en el mapeo ──────────────────────────────────────
        if vid in mapeo:
            woo_id = mapeo[vid]["woo_id"]
            if args.prueba:
                logger.info("[%d/%d] ACTUALIZAR woo_id=%s '%s' stock=%d precio=%s",
                            i, len(productos), woo_id, nombre[:40], stock, precio)
                actualizados += 1
                continue

            ok, msg = woo.update_product(woo_id, stock, precio)
            if ok:
                actualizados += 1
                logger.info("✓ [%d/%d] '%s' stock=%d", i, len(productos), nombre[:40], stock)
            else:
                errores += 1
                errores_log.append(f"{vid} | {nombre} | {msg}")
                logger.error("✗ [%d/%d] '%s' → %s", i, len(productos), nombre[:40], msg)

        # ── Caso 2: nuevo — buscar o crear en WooCommerce ────────────────────
        else:
            woo_id = buscar_en_woo(prod, woo_products, woo)

            if woo_id:
                # Encontrado: agregar al mapeo y actualizar
                mapeo[vid] = {"woo_id": woo_id, "nombre": nombre, "sku": prod["sku"]}
                mapeo_modificado = True

                if not args.prueba:
                    ok, msg = woo.update_product(woo_id, stock, precio)
                    if ok:
                        actualizados += 1
                        logger.info("✓ [%d/%d] NUEVO MATCH '%s' → woo_id=%s stock=%d",
                                    i, len(productos), nombre[:40], woo_id, stock)
                    else:
                        errores += 1
                        errores_log.append(f"{vid} | {nombre} | {msg}")
                else:
                    actualizados += 1
                    logger.info("[%d/%d] NUEVO MATCH '%s' → woo_id=%s", i, len(productos), nombre[:40], woo_id)

            else:
                # No encontrado: crear en WooCommerce
                sku_para_crear = prod["barcode"] or prod["sku"] or vid[:8]

                if args.prueba:
                    logger.info("[%d/%d] CREAR '%s' sku=%s stock=%d precio=%s",
                                i, len(productos), nombre[:40], sku_para_crear, stock, precio)
                    creados += 1
                    continue

                ok, resultado = woo.create_product(
                    nombre, sku_para_crear, stock, precio, prod["categoria"]
                )
                if ok:
                    mapeo[vid] = {"woo_id": resultado, "nombre": nombre, "sku": sku_para_crear}
                    mapeo_modificado = True
                    creados += 1
                    logger.info("✓ [%d/%d] CREADO '%s' → woo_id=%s",
                                i, len(productos), nombre[:40], resultado)
                else:
                    sin_match += 1
                    errores_log.append(f"{vid} | {nombre} | CREATE FAILED: {resultado}")
                    logger.error("✗ [%d/%d] No se pudo crear '%s': %s",
                                 i, len(productos), nombre[:40], resultado)

        # Pausa breve cada 20 productos para no saturar las APIs
        if i % 20 == 0:
            time.sleep(0.5)

    # Guardar mapeo actualizado
    if mapeo_modificado and not args.prueba:
        guardar_mapeo(mapeo)

    # Guardar log de errores
    if errores_log:
        log_path = Path(__file__).parent / "sync_errores.log"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(errores_log))

    logger.info("=" * 55)
    logger.info("RESUMEN [%s]", modo)
    logger.info("  Productos procesados : %d", len(productos))
    logger.info("  Actualizados         : %d", actualizados)
    logger.info("  Creados nuevos       : %d", creados)
    logger.info("  Sin match / error    : %d", sin_match + errores)
    if errores_log:
        logger.info("  Detalle errores      : sincronizacion/sync_errores.log")
    logger.info("=" * 55)


if __name__ == "__main__":
    main()
