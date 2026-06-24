"""
exportar_woo.py — Descarga todos los productos de WooCommerce a CSV.

Uso:
    python sincronizacion/exportar_woo.py
"""
import csv
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
from sincronizacion.woo_client import WooClient

def main():
    woo = WooClient()
    print("Descargando productos de WooCommerce...")
    productos = woo.get_all_products()
    print(f"Total: {len(productos)} productos")

    fecha = datetime.now().strftime("%Y%m%d_%H%M")
    salida = Path(__file__).parent / f"woo_inventario_{fecha}.csv"

    campos = ["id", "name", "sku", "status", "stock_quantity", "manage_stock",
              "regular_price", "sale_price", "categories", "permalink"]

    with open(salida, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        writer.writeheader()
        for p in productos:
            cats = ", ".join(c["name"] for c in p.get("categories", []))
            writer.writerow({
                "id":             p.get("id", ""),
                "name":           p.get("name", ""),
                "sku":            p.get("sku", ""),
                "status":         p.get("status", ""),
                "stock_quantity": p.get("stock_quantity", ""),
                "manage_stock":   p.get("manage_stock", ""),
                "regular_price":  p.get("regular_price", ""),
                "sale_price":     p.get("sale_price", ""),
                "categories":     cats,
                "permalink":      p.get("permalink", ""),
            })

    print(f"Exportado: {salida}")

if __name__ == "__main__":
    main()
