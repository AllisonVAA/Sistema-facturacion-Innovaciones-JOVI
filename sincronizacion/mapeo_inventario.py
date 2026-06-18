"""
mapeo_inventario.py — Cruza productos de Loyverse con productos de WooCommerce.

Uso:
    python mapeo_inventario.py \
        --loyverse  "../export_items (4).csv" \
        --woo       "woo_products.csv" \
        --salida    "mapeo_resultado.csv"

Columnas esperadas del CSV de Loyverse (exportado desde el panel de Loyverse):
    REF, Nombre, Codigo de barras, En inventario [Innovaciones JOVI], Precio [Innovaciones JOVI]

Columnas esperadas del CSV de WooCommerce (exportado desde WooCommerce > Productos > Exportar):
    ID, Name, SKU, Stock, Regular price
"""

import argparse
import csv
import unicodedata
import re
import sys
from pathlib import Path


# ── Normalización de texto para comparar nombres ────────────────────────────

def _normalizar(texto: str) -> str:
    """Minúsculas, sin tildes, sin caracteres especiales, espacios simples."""
    texto = texto.lower().strip()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def _similitud(a: str, b: str) -> float:
    """
    Similitud simple por palabras en común (Jaccard sobre tokens).
    Retorna un valor entre 0.0 y 1.0.
    """
    tokens_a = set(_normalizar(a).split())
    tokens_b = set(_normalizar(b).split())
    if not tokens_a or not tokens_b:
        return 0.0
    interseccion = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(interseccion) / len(union)


# ── Lectura de archivos ──────────────────────────────────────────────────────

def leer_loyverse(ruta: Path) -> list[dict]:
    productos = []
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for fila in reader:
            nombre = fila.get("Nombre", "").strip()
            ref = fila.get("REF", "").strip()
            barcode = fila.get("Codigo de barras", "").strip()
            stock_raw = fila.get("En inventario [Innovaciones JOVI]", "").strip()
            precio_raw = fila.get("Precio [Innovaciones JOVI]", "").strip()

            # Ignorar filas sin nombre o sin seguimiento de inventario
            if not nombre:
                continue
            if fila.get("Seguir el Inventario", "").strip().upper() != "Y":
                continue

            try:
                stock = float(stock_raw) if stock_raw else 0.0
            except ValueError:
                stock = 0.0

            productos.append({
                "loyverse_ref": ref,
                "loyverse_nombre": nombre,
                "loyverse_barcode": barcode,
                "loyverse_stock": int(stock),
                "loyverse_precio": precio_raw,
            })
    return productos


def leer_woocommerce(ruta: Path) -> list[dict]:
    productos = []
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for fila in reader:
            # WooCommerce puede exportar con distintos nombres de columna
            woo_id = (fila.get("ID") or fila.get("id") or "").strip()
            nombre = (fila.get("Name") or fila.get("name") or fila.get("Nombre") or "").strip()
            sku = (fila.get("SKU") or fila.get("sku") or "").strip()
            stock = (fila.get("Stock") or fila.get("stock") or fila.get("In stock?") or "").strip()
            precio = (fila.get("Regular price") or fila.get("regular_price") or fila.get("Precio") or "").strip()

            if not nombre:
                continue

            productos.append({
                "woo_id": woo_id,
                "woo_nombre": nombre,
                "woo_sku": sku,
                "woo_stock": stock,
                "woo_precio": precio,
            })
    return productos


# ── Algoritmo de mapeo ───────────────────────────────────────────────────────

UMBRAL_NOMBRE = 0.55   # Similitud mínima para considerar un match por nombre


def mapear(loyverse: list[dict], woocommerce: list[dict]) -> list[dict]:
    resultados = []

    # Índice de WooCommerce por SKU (barcode) para match exacto rápido
    woo_por_sku: dict[str, dict] = {}
    for w in woocommerce:
        if w["woo_sku"]:
            woo_por_sku[w["woo_sku"].upper()] = w

    for loy in loyverse:
        match_woo = None
        metodo = ""
        confianza = ""

        # 1️⃣ Match exacto por código de barras == SKU de WooCommerce
        if loy["loyverse_barcode"]:
            key = loy["loyverse_barcode"].upper()
            if key in woo_por_sku:
                match_woo = woo_por_sku[key]
                metodo = "barcode=SKU"
                confianza = "EXACTO"

        # 2️⃣ Match exacto por REF de Loyverse == SKU de WooCommerce
        if not match_woo and loy["loyverse_ref"]:
            key = loy["loyverse_ref"].upper()
            if key in woo_por_sku:
                match_woo = woo_por_sku[key]
                metodo = "REF=SKU"
                confianza = "EXACTO"

        # 3️⃣ Match por similitud de nombre
        if not match_woo:
            mejor_score = 0.0
            mejor_candidato = None
            for w in woocommerce:
                score = _similitud(loy["loyverse_nombre"], w["woo_nombre"])
                if score > mejor_score:
                    mejor_score = score
                    mejor_candidato = w
            if mejor_candidato and mejor_score >= UMBRAL_NOMBRE:
                match_woo = mejor_candidato
                metodo = "nombre"
                confianza = f"{'ALTO' if mejor_score >= 0.75 else 'MEDIO'} ({mejor_score:.0%})"

        fila = {
            # Lado Loyverse
            "loyverse_ref": loy["loyverse_ref"],
            "loyverse_nombre": loy["loyverse_nombre"],
            "loyverse_barcode": loy["loyverse_barcode"],
            "loyverse_stock": loy["loyverse_stock"],
            "loyverse_precio": loy["loyverse_precio"],
            # Lado WooCommerce
            "woo_id": match_woo["woo_id"] if match_woo else "",
            "woo_nombre": match_woo["woo_nombre"] if match_woo else "",
            "woo_sku": match_woo["woo_sku"] if match_woo else "",
            "woo_stock": match_woo["woo_stock"] if match_woo else "",
            "woo_precio": match_woo["woo_precio"] if match_woo else "",
            # Resultado del mapeo
            "metodo_match": metodo,
            "confianza": confianza,
            "accion_requerida": "" if match_woo else "SIN MATCH — revisar manualmente",
        }
        resultados.append(fila)

    return resultados


# ── Escritura del resultado ──────────────────────────────────────────────────

COLUMNAS = [
    "loyverse_ref", "loyverse_nombre", "loyverse_barcode",
    "loyverse_stock", "loyverse_precio",
    "woo_id", "woo_nombre", "woo_sku",
    "woo_stock", "woo_precio",
    "metodo_match", "confianza", "accion_requerida",
]


def guardar(resultados: list[dict], ruta: Path) -> None:
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNAS)
        writer.writeheader()
        writer.writerows(resultados)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Mapeo de inventario Loyverse ↔ WooCommerce")
    parser.add_argument("--loyverse", required=True, help="CSV exportado de Loyverse")
    parser.add_argument("--woo", required=True, help="CSV exportado de WooCommerce")
    parser.add_argument("--salida", default="mapeo_resultado.csv", help="Archivo de salida")
    args = parser.parse_args()

    ruta_loy = Path(args.loyverse)
    ruta_woo = Path(args.woo)
    ruta_sal = Path(args.salida)

    if not ruta_loy.exists():
        print(f"ERROR: No se encuentra el archivo de Loyverse: {ruta_loy}")
        sys.exit(1)
    if not ruta_woo.exists():
        print(f"ERROR: No se encuentra el archivo de WooCommerce: {ruta_woo}")
        sys.exit(1)

    print(f"Leyendo Loyverse: {ruta_loy}")
    loyverse = leer_loyverse(ruta_loy)
    print(f"  → {len(loyverse)} productos con seguimiento de inventario")

    print(f"Leyendo WooCommerce: {ruta_woo}")
    woocommerce = leer_woocommerce(ruta_woo)
    print(f"  → {len(woocommerce)} productos")

    print("Generando mapeo...")
    resultados = mapear(loyverse, woocommerce)

    exactos = sum(1 for r in resultados if r["confianza"] == "EXACTO")
    altos = sum(1 for r in resultados if r["confianza"].startswith("ALTO"))
    medios = sum(1 for r in resultados if r["confianza"].startswith("MEDIO"))
    sin_match = sum(1 for r in resultados if not r["woo_id"])

    guardar(resultados, ruta_sal)

    print("\n── Resumen ─────────────────────────────────────────────")
    print(f"  Productos Loyverse procesados : {len(resultados)}")
    print(f"  Match exacto (barcode/REF)    : {exactos}")
    print(f"  Match alto por nombre         : {altos}")
    print(f"  Match medio por nombre        : {medios}")
    print(f"  Sin match — revisar manual    : {sin_match}")
    print(f"\n  Resultado guardado en: {ruta_sal}")
    print("────────────────────────────────────────────────────────")
    print("\nPróximo paso: abre el CSV en Excel, revisa los 'SIN MATCH'")
    print("y los matches 'MEDIO', luego corre sync_inventario.py")


if __name__ == "__main__":
    main()
