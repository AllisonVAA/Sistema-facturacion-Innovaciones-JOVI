import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from sincronizacion.woo_client import WooClient

woo = WooClient()
print("Descargando productos...")
productos = woo.get_all_products()
outlets = [p for p in productos if "outlet" in p.get("name", "").lower()]
print(f"Outlets encontrados: {len(outlets)}")

for i, p in enumerate(outlets, 1):
    try:
        woo._request("PUT", f"products/{p['id']}", json={"catalog_visibility": "hidden"})
        print(f"✓ [{i}/{len(outlets)}] {p['name'][:55]}")
    except Exception as e:
        print(f"✗ [{i}/{len(outlets)}] Error {p['id']}: {e}")
    if i % 10 == 0:
        time.sleep(0.5)

print("Listo.")
