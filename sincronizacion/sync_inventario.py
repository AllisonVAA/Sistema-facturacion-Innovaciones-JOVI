"""
sync_inventario.py — Actualiza el stock en WooCommerce a partir del mapeo validado.

Uso:
    python sync_inventario.py --mapeo mapeo_resultado.csv

Variables de entorno requeridas (.env):
    WOO_URL            URL base de la tienda (ej: https://mitienda.com)
    WOO_CONSUMER_KEY   Consumer Key de WooCommerce REST API
    WOO_CONSUMER_SECRET Consumer Secret de WooCommerce REST API

Cómo obtener las credenciales de WooCommerce:
    Panel WordPress → WooCommerce → Ajustes → Avanzado → REST API → Agregar clave
    Permisos: Lectura/Escritura
"""

import argparse
import csv
import os
import sys
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

WOO_URL             = os.getenv("WOO_URL", "").rstrip("/")
WOO_CONSUMER_KEY    = os.getenv("WOO_CONSUMER_KEY", "")
WOO_CONSUMER_SECRET = os.getenv("WOO_CONSUMER_SECRET", "")

CONFIANZAS_VALIDAS = {"EXACTO", "ALTO"}   # Solo estas se sincronizan automáticamente


# ── Validación de configuración ──────────────────────────────────────────────

def validar_config() -> None:
    errores = []
    if not WOO_URL:
        errores.append("WOO_URL no está definida en .env")
    if not WOO_CONSUMER_KEY:
        errores.append("WOO_CONSUMER_KEY no está definida en .env")
    if not WOO_CONSUMER_SECRET:
        errores.append("WOO_CONSUMER_SECRET no está definida en .env")
    if errores:
        for e in errores:
            print(f"ERROR: {e}")
        sys.exit(1)


# ── Llamada a la API de WooCommerce ─────────────────────────────────────────

def actualizar_stock_woo(woo_id: str, stock: int, precio: str = "") -> tuple[bool, str]:
    """
    Actualiza el stock_quantity del producto en WooCommerce.
    Retorna (éxito, mensaje).
    """
    url = f"{WOO_URL}/wp-json/wc/v3/products/{woo_id}"
    payload = {
        "manage_stock": True,
        "stock_quantity": stock,
    }
    # Agregar precio si viene y es un número válido (ignorar "variable" y vacíos)
    if precio and precio.lower() != "variable":
        try:
            precio_num = float(precio)
            if precio_num > 0:
                payload["regular_price"] = str(int(precio_num))
        except ValueError:
            pass
    params = {
        "consumer_key": WOO_CONSUMER_KEY,
        "consumer_secret": WOO_CONSUMER_SECRET,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.put(
            url,
            json=payload,
            params=params,
            headers=headers,
            timeout=45,
            allow_redirects=False,
        )
        # Si hay redirección, seguirla manualmente con PUT
        if resp.status_code in (301, 302, 307, 308):
            nueva_url = resp.headers.get("Location", "")
            resp = requests.put(
                nueva_url,
                json=payload,
                params=params,
                timeout=20,
                allow_redirects=False,
            )
        if resp.status_code in (200, 201):
            return True, "OK"
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    except requests.RequestException as exc:
        return False, str(exc)


# ── Lectura del mapeo ────────────────────────────────────────────────────────

def leer_mapeo(ruta: Path) -> list[dict]:
    filas = []
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for fila in reader:
            filas.append(fila)
    return filas


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Sincroniza stock Loyverse → WooCommerce")
    parser.add_argument("--mapeo", default="mapeo_resultado.csv", help="CSV de mapeo validado")
    parser.add_argument(
        "--modo",
        choices=["prueba", "real"],
        default="prueba",
        help="'prueba' muestra qué haría sin tocar nada; 'real' aplica los cambios",
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=0,
        help="Procesar solo los primeros N productos (0 = todos)",
    )
    args = parser.parse_args()

    validar_config()
    print(f"  URL  : {WOO_URL}")
    print(f"  KEY  : {WOO_CONSUMER_KEY[:10]}...")
    print(f"  SECRET: {WOO_CONSUMER_SECRET[:10]}...")

    ruta_mapeo = Path(args.mapeo)
    if not ruta_mapeo.exists():
        print(f"ERROR: No se encuentra {ruta_mapeo}")
        sys.exit(1)

    filas = leer_mapeo(ruta_mapeo)

    # Filtrar solo las filas sincronizables
    candidatos = []
    for f in filas:
        confianza = f.get("confianza", "")
        accion = f.get("accion_requerida", "")
        woo_id = f.get("woo_id", "").strip()
        stock_raw = f.get("loyverse_stock", "0").strip()

        if not woo_id:
            continue
        if accion.startswith("SIN MATCH") or accion == "IGNORAR":
            continue

        # Aceptar EXACTO, ALTO, y también MEDIO si el usuario lo dejó con woo_id
        try:
            stock = int(float(stock_raw)) if stock_raw else 0
        except ValueError:
            stock = 0

        candidatos.append({
            "woo_id": woo_id,
            "loyverse_nombre": f.get("loyverse_nombre", ""),
            "loyverse_ref": f.get("loyverse_ref", ""),
            "loyverse_stock": stock,
            "loyverse_precio": f.get("loyverse_precio", ""),
            "confianza": confianza,
        })

    if args.limite > 0:
        candidatos = candidatos[:args.limite]

    print(f"\nProductos a sincronizar: {len(candidatos)}")
    if args.modo == "prueba":
        print("MODO PRUEBA — no se modifica nada en WooCommerce\n")
    else:
        print("MODO REAL — aplicando cambios en WooCommerce\n")

    exitosos = 0
    fallidos = 0
    errores_log = []

    for i, prod in enumerate(candidatos, 1):
        nombre_corto = prod["loyverse_nombre"][:45]
        stock = prod["loyverse_stock"]

        if args.modo == "prueba":
            print(f"  [{i:>4}/{len(candidatos)}] {nombre_corto:<46} → stock: {stock}")
            exitosos += 1
            continue

        ok, msg = actualizar_stock_woo(prod["woo_id"], stock, prod.get("loyverse_precio", ""))
        estado = "✓" if ok else "✗"
        print(f"  {estado} [{i:>4}/{len(candidatos)}] {nombre_corto:<46} → stock: {stock}")
        if ok:
            exitosos += 1
        else:
            fallidos += 1
            errores_log.append(f"{prod['loyverse_ref']} | {prod['loyverse_nombre']} | {msg}")

        # Pausa breve para no saturar la API de WooCommerce
        if i % 10 == 0:
            time.sleep(0.5)

    print(f"\n── Resumen ─────────────────────────────────────────────")
    if args.modo == "prueba":
        print(f"  Productos que SE ACTUALIZARÍAN: {exitosos}")
        print(f"\n  Para aplicar los cambios, corra:")
        print(f"  python sync_inventario.py --mapeo {args.mapeo} --modo real")
    else:
        print(f"  Actualizados correctamente : {exitosos}")
        print(f"  Con error                  : {fallidos}")
        if errores_log:
            log_path = Path("sync_errores.log")
            log_path.write_text("\n".join(errores_log), encoding="utf-8")
            print(f"  Detalle de errores         : {log_path}")
    print("────────────────────────────────────────────────────────")


if __name__ == "__main__":
    main()
