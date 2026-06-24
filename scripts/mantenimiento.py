#!/usr/bin/env python3
"""
scripts/mantenimiento.py — Reporte semanal de mantenimiento Innovaciones JOVI.

Corre cada domingo a las 8:03 AM (Costa Rica) vía cron en el droplet de facturación.
Envía el reporte a allialvarez27@gmail.com.

Cron entry:
  3 14 * * 0 cd /opt/facturacion-jovi && python3 scripts/mantenimiento.py >> logs/mantenimiento.log 2>&1
"""
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

WOO_URL    = os.getenv("WOO_URL", "https://innovacionesjovi.com").rstrip("/")
WOO_KEY    = os.getenv("WOO_CONSUMER_KEY", "")
WOO_SECRET = os.getenv("WOO_CONSUMER_SECRET", "")
TZ_CR = ZoneInfo("America/Costa_Rica")

HEADERS_WOO = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}
AUTH_WOO = {"consumer_key": WOO_KEY, "consumer_secret": WOO_SECRET}

WP_HOST   = "root@138.197.47.70"
WP_PATH   = "/var/www/html"
DOC_HOST  = "root@161.35.113.80"


# ── Helpers ───────────────────────────────────────────────────────────────────

def ahora_cr() -> str:
    return datetime.now(TZ_CR).strftime("%d/%m/%Y %H:%M")


def ssh(host: str, cmd: str) -> tuple[str, str]:
    """Ejecuta un comando SSH y retorna (stdout, stderr)."""
    result = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15", host, cmd],
        capture_output=True, text=True, timeout=60
    )
    return result.stdout.strip(), result.stderr.strip()


def woo_get(endpoint: str, params: dict = {}) -> list:
    """Descarga todos los registros de un endpoint WooCommerce con paginación."""
    todos = []
    page = 1
    while True:
        p = {**AUTH_WOO, "per_page": 100, "page": page, **params}
        try:
            r = requests.get(
                f"{WOO_URL}/wp-json/wc/v3/{endpoint}",
                params=p, headers=HEADERS_WOO, timeout=45, allow_redirects=False
            )
            lote = r.json() if r.ok else []
        except Exception:
            break
        if not lote:
            break
        todos.extend(lote)
        if len(lote) < 100:
            break
        page += 1
    return todos


# ── Verificaciones ────────────────────────────────────────────────────────────

def check_velocidad() -> dict:
    try:
        url = (
            "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
            "?url=https://innovacionesjovi.com&strategy=mobile"
        )
        data = requests.get(url, timeout=30).json()
        cats  = data["lighthouseResult"]["categories"]
        score = int(cats["performance"]["score"] * 100)
        audits = data["lighthouseResult"]["audits"]
        lcp   = audits.get("largest-contentful-paint", {}).get("displayValue", "N/A")
        cls   = audits.get("cumulative-layout-shift", {}).get("displayValue", "N/A")
        emoji = "✅" if score >= 90 else ("⚠️" if score >= 50 else "❌")
        return {"score": score, "emoji": emoji, "lcp": lcp, "cls": cls, "ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_plugins() -> dict:
    try:
        cmd = f"wp --path={WP_PATH} plugin list --update=available --format=json --allow-root"
        out, err = ssh(WP_HOST, cmd)
        plugins = json.loads(out) if out else []
        return {"plugins": plugins, "ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_servidor() -> dict:
    try:
        out, _ = ssh(DOC_HOST, "docker ps --format '{{.Names}}\t{{.Status}}'")
        lineas = [l for l in out.splitlines() if "facturacion" in l]
        activo = any("Up" in l for l in lineas)
        return {"activo": activo, "ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_webhook() -> dict:
    try:
        r = requests.get("https://facturacion.innovacionesjovi.com/", timeout=15)
        return {"ok": r.status_code == 200, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_productos() -> dict:
    productos = woo_get("products", {"status": "publish"})
    sin_imagen = [
        p for p in productos
        if not p.get("images") or "placeholder" in (p["images"][0].get("src", ""))
    ]
    sin_precio = [
        p for p in productos
        if not p.get("regular_price") or p["regular_price"] == "0"
    ]
    stock_cero = [
        p for p in productos
        if p.get("manage_stock") and (p.get("stock_quantity") or 0) == 0
    ]
    return {
        "sin_imagen": sin_imagen,
        "sin_precio": sin_precio,
        "stock_cero": stock_cero,
        "ok": True
    }


# ── Reporte ───────────────────────────────────────────────────────────────────

def lista_productos(productos: list, n: int = 10) -> str:
    if not productos:
        return "   Ninguno ✅"
    lineas = [f"   - {p['name'][:55]} (ID: {p['id']})" for p in productos[:n]]
    if len(productos) > n:
        lineas.append(f"   ... y {len(productos) - n} más")
    return "\n".join(lineas)


def armar_reporte(v, pl, srv, wh, prod) -> tuple[str, bool]:
    fecha   = ahora_cr()
    urgente = False

    # Velocidad
    if v["ok"]:
        vel = f"   Score: {v['score']}/100 {v['emoji']}\n   LCP: {v['lcp']}  |  CLS: {v['cls']}"
    else:
        vel = f"   ⚠️ No disponible ({v.get('error','')})"

    # Plugins
    if pl["ok"]:
        if pl["plugins"]:
            pp = "\n".join(
                f"   - {p['name']} {p['version']} → {p['update_version']}"
                for p in pl["plugins"][:10]
            )
            plugins_txt = f"{len(pl['plugins'])} pendientes\n{pp}"
        else:
            plugins_txt = "0 — todo actualizado ✅"
    else:
        plugins_txt = f"⚠️ No disponible ({pl.get('error','')})"

    # Servidor
    if srv["ok"]:
        srv_txt = "✅ Activo" if srv["activo"] else "❌ CAÍDO — REVISAR URGENTE"
        if not srv["activo"]:
            urgente = True
    else:
        srv_txt = f"⚠️ No disponible ({srv.get('error','')})"

    wh_txt = "✅ OK" if wh["ok"] else f"❌ No responde ({wh.get('error','')})"

    # Productos
    si = prod.get("sin_imagen", [])
    sp = prod.get("sin_precio", [])
    sc = prod.get("stock_cero", [])

    # Acciones recomendadas
    acciones = []
    if urgente:
        acciones.append("🚨 URGENTE: Revisar contenedor Docker en el servidor de facturación")
    if not wh["ok"]:
        acciones.append("🚨 Webhook de Loyverse no responde — sincronización detenida")
    if pl["ok"] and pl["plugins"]:
        acciones.append(f"Actualizar {len(pl['plugins'])} plugin(s) de WordPress")
    if si:
        acciones.append(f"Agregar imágenes a {len(si)} productos")
    if sp:
        acciones.append(f"Definir precio en {len(sp)} productos")
    if sc:
        acciones.append(f"Revisar stock de {len(sc)} productos agotados publicados")
    if not acciones:
        acciones.append("Todo en orden — sin acciones urgentes esta semana 🎉")

    acciones_txt = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(acciones))

    reporte = f"""{'═'*55}
  REPORTE DE MANTENIMIENTO — Innovaciones JOVI
  📅 Domingo {fecha} (hora Costa Rica)
{'═'*55}

🚀 VELOCIDAD DEL SITIO (móvil)
{vel}

🔌 PLUGINS DESACTUALIZADOS: {plugins_txt}

🖥️  SERVIDOR FACTURACIÓN
   Contenedor: {srv_txt}
   Sync Loyverse: {wh_txt}

🖼️  SIN IMAGEN: {len(si)} productos
{lista_productos(si)}

💰 SIN PRECIO: {len(sp)} productos
{lista_productos(sp)}

📦 STOCK AGOTADO (publicados): {len(sc)} productos
{lista_productos(sc)}

{'─'*55}
✅ ACCIONES RECOMENDADAS ESTA SEMANA:
{acciones_txt}
{'═'*55}
Reporte generado automáticamente — Innovaciones JOVI
"""
    return reporte, urgente


def guardar_reporte(reporte: str) -> Path:
    logs_dir = Path(__file__).parent.parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    fecha = datetime.now(TZ_CR).strftime("%Y%m%d")
    ruta  = logs_dir / f"mantenimiento_{fecha}.txt"
    ruta.write_text(reporte, encoding="utf-8")
    return ruta


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*55}")
    print(f"Mantenimiento semanal — {ahora_cr()}")
    print(f"{'='*55}")

    print("Verificando velocidad...")
    v = check_velocidad()

    print("Verificando plugins WordPress...")
    pl = check_plugins()

    print("Verificando servidor DigitalOcean...")
    srv = check_servidor()

    print("Verificando webhook Loyverse...")
    wh = check_webhook()

    print("Descargando productos WooCommerce...")
    prod = check_productos()

    print("Armando reporte...")
    reporte, urgente = armar_reporte(v, pl, srv, wh, prod)

    print(reporte)

    ruta = guardar_reporte(reporte)
    print(f"Reporte guardado en: {ruta} ✅")


if __name__ == "__main__":
    main()
