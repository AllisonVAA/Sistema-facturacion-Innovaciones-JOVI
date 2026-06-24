"""
sincronizacion/woo_client.py — Cliente para la API REST de WooCommerce.

Lee WOO_URL, WOO_CONSUMER_KEY y WOO_CONSUMER_SECRET desde el .env del proyecto.
"""
import logging
import os
import time
import random
from typing import Any

import requests
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)

WOO_URL             = os.getenv("WOO_URL", "").rstrip("/")
WOO_CONSUMER_KEY    = os.getenv("WOO_CONSUMER_KEY", "")
WOO_CONSUMER_SECRET = os.getenv("WOO_CONSUMER_SECRET", "")

# Cloudflare bloquea el User-Agent por defecto de python-requests
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
}


class WooClient:
    """Encapsula las llamadas a la API de WooCommerce v3."""

    def __init__(self) -> None:
        if not WOO_URL or not WOO_CONSUMER_KEY or not WOO_CONSUMER_SECRET:
            raise ValueError(
                "Faltan WOO_URL / WOO_CONSUMER_KEY / WOO_CONSUMER_SECRET en el .env"
            )
        self._auth_params = {
            "consumer_key": WOO_CONSUMER_KEY,
            "consumer_secret": WOO_CONSUMER_SECRET,
        }

    def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        url = f"{WOO_URL}/wp-json/wc/v3/{endpoint.lstrip('/')}"
        params = {**self._auth_params, **kwargs.pop("params", {})}
        for intento in range(4):
            try:
                resp = requests.request(
                    method, url,
                    params=params,
                    headers=_HEADERS,
                    timeout=45,
                    allow_redirects=False,
                    **kwargs,
                )
                resp.raise_for_status()
                return resp.json()
            except (requests.ConnectionError, requests.Timeout) as exc:
                if intento == 3:
                    raise
                espera = 5 + intento * 5 + random.uniform(0, 3)
                logger.warning("Conexión perdida (intento %d/4), reintentando en %.1fs: %s", intento + 1, espera, exc)
                time.sleep(espera)

    # ── Lectura ───────────────────────────────────────────────────────────────

    def get_all_products(self) -> list[dict]:
        """Descarga todos los productos de WooCommerce con paginación."""
        logger.info("Descargando productos de WooCommerce...")
        todos: list[dict] = []
        page = 1
        while True:
            lote = self._request("GET", "products", params={"per_page": 100, "page": page})
            if not lote:
                break
            todos.extend(lote)
            logger.debug("Página %d: %d productos WooCommerce", page, len(lote))
            if len(lote) < 100:
                break
            page += 1
        logger.info("Total productos en WooCommerce: %d", len(todos))
        return todos

    def find_by_sku(self, sku: str) -> dict | None:
        """Busca un producto en WooCommerce por SKU exacto."""
        try:
            results = self._request("GET", "products", params={"sku": sku, "per_page": 1})
            return results[0] if results else None
        except requests.HTTPError:
            return None

    # ── Escritura ─────────────────────────────────────────────────────────────

    def update_product(self, woo_id: str | int, stock: int, precio: str = "") -> tuple[bool, str]:
        """Actualiza stock y precio de un producto existente."""
        payload: dict = {
            "manage_stock": True,
            "stock_quantity": max(stock, 0),
        }
        if precio and precio.lower() not in ("variable", ""):
            try:
                precio_num = float(precio)
                if precio_num > 0:
                    payload["regular_price"] = str(int(precio_num))
            except ValueError:
                pass
        try:
            self._request("PUT", f"products/{woo_id}", json=payload)
            return True, "OK"
        except requests.HTTPError as exc:
            return False, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
        except requests.RequestException as exc:
            return False, str(exc)

    def create_product(
        self,
        nombre: str,
        sku: str,
        stock: int,
        precio: str = "",
        categoria: str = "",
    ) -> tuple[bool, str]:
        """Crea un producto nuevo en WooCommerce. Retorna (ok, woo_id_o_error)."""
        payload: dict = {
            "name": nombre,
            "sku": sku,
            "status": "publish",
            "manage_stock": True,
            "stock_quantity": max(stock, 0),
        }
        if precio and precio.lower() not in ("variable", ""):
            try:
                precio_num = float(precio)
                if precio_num > 0:
                    payload["regular_price"] = str(int(precio_num))
            except ValueError:
                pass
        if categoria:
            payload["categories"] = [{"name": categoria}]

        try:
            result = self._request("POST", "products", json=payload)
            return True, str(result.get("id", ""))
        except requests.HTTPError as exc:
            return False, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
        except requests.RequestException as exc:
            return False, str(exc)
