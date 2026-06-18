"""
crear_productos.py — Crea en WooCommerce los productos marcados como AGREGAR en el mapeo.

Uso:
    python crear_productos.py --mapeo mapeo_resultado.csv --modo prueba
    python crear_productos.py --mapeo mapeo_resultado.csv --modo real
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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
}


# ── Validación ───────────────────────────────────────────────────────────────

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


# ── Crear producto en WooCommerce ────────────────────────────────────────────

def crear_producto_woo(nombre: str, stock: int, precio: str, categoria: str) -> tuple[bool, str]:
    url    = f"{WOO_URL}/wp-json/wc/v3/products"
    params = {"consumer_key": WOO_CONSUMER_KEY, "consumer_secret": WOO_CONSUMER_SECRET}

    payload: dict = {
        "name":         nombre,
        "status":       "publish",
        "manage_stock": True,
        "stock_quantity": max(stock, 0),  # WooCommerce no acepta negativos al crear
    }

    # Precio
    if precio and precio.lower() != "variable":
        try:
            precio_num = float(precio)
            if precio_num > 0:
                payload["regular_price"] = str(int(precio_num))
        except ValueError:
            pass

    # Categoría
    if categoria:
        payload["categories"] = [{"name": categoria}]

    try:
        resp = requests.post(
            url,
            json=payload,
            params=params,
            headers=HEADERS,
            timeout=45,
            allow_redirects=False,
        )
        if resp.status_code in (200, 201):
            woo_id = resp.json().get("id", "")
            return True, str(woo_id)
        return False, f"HTTP {resp.status_code}: {resp.text[:300]}"
    except requests.RequestException as exc:
        return False, str(exc)


# ── Lectura del mapeo ────────────────────────────────────────────────────────

def leer_candidatos(ruta: Path) -> list[dict]:
    candidatos = []
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for fila in reader:
            accion = fila.get("accion_requerida", "").strip().upper()
            if accion != "AGREGAR":
                continue
            nombre   = fila.get("loyverse_nombre", "").strip()
            precio   = fila.get("loyverse_precio", "").strip()
            categoria = fila.get("loyverse_categoria", fila.get("categoria", "")).strip()
            stock_raw = fila.get("loyverse_stock", "0").strip()
            ref      = fila.get("loyverse_ref", "").strip()
            try:
                stock = int(float(stock_raw)) if stock_raw else 0
            except ValueError:
                stock = 0
            if not nombre:
                continue
            candidatos.append({
                "ref":      ref,
                "nombre":   nombre,
                "stock":    stock,
                "precio":   precio,
                "categoria": categoria,
            })
    return candidatos


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Crea productos nuevos en WooCommerce desde el mapeo")
    parser.add_argument("--mapeo", default="mapeo_resultado.csv")
    parser.add_argument("--modo", choices=["prueba", "real"], default="prueba")
    parser.add_argument("--limite", type=int, default=0, help="Procesar solo los primeros N (0 = todos)")
    args = parser.parse_args()

    validar_config()

    ruta = Path(args.mapeo)
    if not ruta.exists():
        print(f"ERROR: No se encuentra {ruta}")
        sys.exit(1)

    candidatos = leer_candidatos(ruta)

    if args.limite > 0:
        candidatos = candidatos[:args.limite]

    print(f"\nProductos a crear: {len(candidatos)}")
    if args.modo == "prueba":
        print("MODO PRUEBA — no se crea nada en WooCommerce\n")
    else:
        print("MODO REAL — creando productos en WooCommerce\n")

    exitosos = 0
    fallidos = 0
    errores_log = []

    for i, prod in enumerate(candidatos, 1):
        nombre_corto = prod["nombre"][:45]

        if args.modo == "prueba":
            precio_str = f"₡{int(float(prod['precio'])):,}" if prod["precio"] and prod["precio"].lower() != "variable" else prod["precio"] or "sin precio"
            print(f"  [{i:>4}/{len(candidatos)}] {nombre_corto:<46} stock: {prod['stock']}  precio: {precio_str}")
            exitosos += 1
            continue

        ok, resultado = crear_producto_woo(prod["nombre"], prod["stock"], prod["precio"], prod["categoria"])
        estado = "✓" if ok else "✗"
        woo_id_str = f"→ ID {resultado}" if ok else f"→ ERROR"
        print(f"  {estado} [{i:>4}/{len(candidatos)}] {nombre_corto:<46} {woo_id_str}")

        if ok:
            exitosos += 1
        else:
            fallidos += 1
            errores_log.append(f"{prod['ref']} | {prod['nombre']} | {resultado}")

        if i % 10 == 0:
            time.sleep(0.5)

    print(f"\n── Resumen ─────────────────────────────────────────────")
    if args.modo == "prueba":
        print(f"  Productos que SE CREARÍAN: {exitosos}")
        print(f"\n  Para crear en WooCommerce:")
        print(f"  python crear_productos.py --mapeo {args.mapeo} --modo real")
    else:
        print(f"  Creados correctamente : {exitosos}")
        print(f"  Con error             : {fallidos}")
        if errores_log:
            log_path = Path("crear_errores.log")
            log_path.write_text("\n".join(errores_log), encoding="utf-8")
            print(f"  Detalle de errores    : {log_path}")
    print("────────────────────────────────────────────────────────")


if __name__ == "__main__":
    main()
