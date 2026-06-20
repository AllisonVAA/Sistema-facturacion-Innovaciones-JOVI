"""
loyverse/client.py — Cliente de la API de Loyverse para Innovaciones JOVI.

Mejoras sobre la versión anterior:
  - Paginación completa con cursor (nunca se pierden ventas)
  - Zona horaria correcta: Costa Rica = UTC-6, sin horario de verano
  - ID único real: 'receipt_number' (la API de recibos no expone 'id')
  - Caché de clientes para no repetir llamadas a /customers/{id}
  - Filtro de recibos cancelados
  - Mapeo de métodos de pago a códigos Hacienda
  - Datos de sucursal/terminal desde la API o fallback a defaults
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from config import settings

logger = logging.getLogger(__name__)

# Costa Rica: UTC-6, sin horario de verano
TZ_CR = ZoneInfo("America/Costa_Rica")

# Mapa de tipos de pago Loyverse -> códigos Hacienda CR
# 01=Efectivo, 02=Tarjeta, 03=Cheque, 04=Transferencia, 99=Otros
_MEDIO_PAGO_MAP: dict[str, str] = {
    "CASH":       "01",
    "CARD":       "02",
    "CHECK":      "03",
    "TRANSFER":   "04",
    "GIFT_CARD":  "99",
    "OTHER":      "99",
}


# ── Helpers de fecha ──────────────────────────────────────────────────────────

def rango_dia_cr_en_utc(dia: datetime | None = None) -> tuple[str, str]:
    """
    Convierte el día laboral de Costa Rica a un rango UTC para la API.

    Si dia=None usa la fecha actual en CR.
    Retorna (inicio_utc_iso, fin_utc_iso) en formato ISO-8601 con Z.
    """
    if dia is None:
        ahora_cr = datetime.now(TZ_CR)
    else:
        ahora_cr = dia.astimezone(TZ_CR)

    inicio_cr = ahora_cr.replace(hour=0, minute=0, second=0, microsecond=0)
    fin_cr    = ahora_cr.replace(hour=23, minute=59, second=59, microsecond=999999)

    inicio_utc = inicio_cr.astimezone(timezone.utc)
    fin_utc    = fin_cr.astimezone(timezone.utc)

    return (
        inicio_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        fin_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def loyverse_ts_a_cr(ts: str) -> datetime:
    """Convierte un timestamp de Loyverse (UTC ISO-8601) a datetime en CR."""
    ts_clean = ts.replace("Z", "+00:00")
    dt_utc = datetime.fromisoformat(ts_clean)
    return dt_utc.astimezone(TZ_CR)


def medio_pago_hacienda(payments: list[dict]) -> list[str]:
    """Mapea los pagos de Loyverse a códigos de medio de pago de Hacienda."""
    codigos: list[str] = []
    for p in payments:
        tipo = p.get("type", "OTHER").upper()
        codigos.append(_MEDIO_PAGO_MAP.get(tipo, "99"))
    # Si no hay pagos, usar efectivo por defecto
    return codigos if codigos else ["01"]


# ── Cliente real ──────────────────────────────────────────────────────────────

class LoyverseClient:
    """
    Cliente para la API REST de Loyverse v1.0.

    Características:
      - Paginación automática con cursor
      - Reintentos ante errores 429 (rate limit) y 5xx
      - Caché de clientes para la sesión actual
      - Extrae sucursal/terminal del store de Loyverse
    """

    def __init__(self) -> None:
        if not settings.LOYVERSE_TOKEN:
            raise ValueError(
                "LOYVERSE_TOKEN no configurado. Agrega el token al archivo .env"
            )
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {settings.LOYVERSE_TOKEN}"}
        )
        self._base = settings.LOYVERSE_BASE_URL.rstrip("/")
        self._customer_cache: dict[str, dict] = {}
        self._store_cache: dict[str, dict] = {}

    def _get(self, endpoint: str, params: dict | None = None, reintentos: int = 3) -> dict:
        """GET con reintentos ante errores transitorios."""
        url = f"{self._base}/{endpoint.lstrip('/')}"
        for intento in range(1, reintentos + 1):
            try:
                resp = self._session.get(url, params=params, timeout=30)

                if resp.status_code == 429:
                    espera = int(resp.headers.get("Retry-After", "10"))
                    logger.warning("Rate limit Loyverse. Esperando %ds...", espera)
                    time.sleep(espera)
                    continue

                if resp.status_code in (500, 502, 503, 504):
                    logger.warning(
                        "Error %d Loyverse (intento %d/%d). Reintentando...",
                        resp.status_code, intento, reintentos,
                    )
                    time.sleep(5 * intento)
                    continue

                resp.raise_for_status()
                return resp.json()

            except requests.ConnectionError as exc:
                logger.error("Sin conexion a Loyverse (intento %d): %s", intento, exc)
                if intento < reintentos:
                    time.sleep(5 * intento)
                else:
                    raise

        raise RuntimeError(f"Loyverse no responde despues de {reintentos} intentos")

    # ── Recibos ───────────────────────────────────────────────────────────────

    def get_receipts_today(self) -> list[dict[str, Any]]:
        """
        Obtiene TODOS los recibos del día actual en Costa Rica.

        Usa paginación con cursor para garantizar que no se pierda
        ninguna venta, incluso si hay más de 250 transacciones.
        Solo retorna recibos NO cancelados (cancelled=false).
        """
        inicio, fin = rango_dia_cr_en_utc()
        logger.info(
            "Consultando ventas Loyverse: %s -> %s (hora CR)",
            inicio, fin,
        )

        todos: list[dict] = []
        params: dict[str, Any] = {
            "created_at_min": inicio,
            "created_at_max": fin,
            "limit": 250,
        }

        pagina = 1
        while True:
            logger.debug("Paginando recibos Loyverse, pagina %d", pagina)
            data = self._get("receipts", params=params)
            recibos = data.get("receipts", [])

            # Filtrar cancelados
            validos = [r for r in recibos if not r.get("cancelled", False)]
            todos.extend(validos)

            cancelados = len(recibos) - len(validos)
            if cancelados:
                logger.info("Pagina %d: %d recibos (%d cancelados omitidos)",
                            pagina, len(recibos), cancelados)

            cursor = data.get("cursor")
            if not cursor:
                break  # No hay más páginas

            params = {"cursor": cursor}
            pagina += 1

        logger.info(
            "Total recibos validos hoy: %d (en %d pagina(s))", len(todos), pagina
        )
        return todos

    # ── Clientes ──────────────────────────────────────────────────────────────

    def get_customer(self, customer_id: str) -> dict[str, Any] | None:
        """
        Obtiene los datos de un cliente por su ID.
        Usa caché en memoria para no repetir llamadas dentro de la misma ejecución.
        """
        if customer_id in self._customer_cache:
            return self._customer_cache[customer_id]

        try:
            data = self._get(f"customers/{customer_id}")
            self._customer_cache[customer_id] = data
            return data
        except requests.HTTPError as exc:
            if exc.response.status_code == 404:
                logger.warning("Cliente %s no encontrado en Loyverse", customer_id)
                self._customer_cache[customer_id] = {}
                return None
            raise

    def extraer_cedula_cliente(self, customer: dict) -> str:
        """
        Extrae la cédula del campo configurado en LOYVERSE_CEDULA_FIELD.

        Por defecto usa 'customer_code'. Solo retorna dígitos.
        """
        campo = settings.LOYVERSE_CEDULA_FIELD
        valor = customer.get(campo, "") or ""
        # Quitar guiones, espacios y otros caracteres no numéricos
        cedula = "".join(c for c in valor if c.isdigit())
        return cedula

    # ── Tienda / Sucursal ─────────────────────────────────────────────────────

    def get_store_info(self, store_id: str | None = None) -> dict[str, Any]:
        """
        Intenta obtener el store de Loyverse para extraer
        el código de sucursal. Retorna dict con 'sucursal' y 'terminal'.
        Si falla, usa los defaults de configuración.
        """
        if store_id and store_id in self._store_cache:
            return self._store_cache[store_id]

        try:
            data = self._get("stores")
            stores = data.get("stores", [])

            store = None
            if store_id:
                store = next((s for s in stores if s.get("id") == store_id), None)
            if not store and stores:
                store = stores[0]

            if store:
                # Loyverse no expone códigos de sucursal en formato Hacienda,
                # así que usamos los primeros 3 dígitos del ID como referencia
                # o directamente los defaults configurados.
                result = {
                    "sucursal": settings.DEFAULT_SUCURSAL,
                    "terminal": settings.DEFAULT_TERMINAL,
                    "nombre": store.get("name", ""),
                    "store_id": store.get("id", ""),
                }
                if store_id:
                    self._store_cache[store_id] = result
                return result

        except Exception as exc:
            logger.warning(
                "No se pudo obtener info del store de Loyverse (%s). "
                "Usando defaults: sucursal=%s terminal=%s",
                exc, settings.DEFAULT_SUCURSAL, settings.DEFAULT_TERMINAL,
            )

        return {
            "sucursal": settings.DEFAULT_SUCURSAL,
            "terminal": settings.DEFAULT_TERMINAL,
            "nombre":   "",
            "store_id": store_id or "",
        }

    # ── Items (productos) ─────────────────────────────────────────────────────

    def get_items(self) -> list[dict[str, Any]]:
        """
        Obtiene TODOS los ítems del catálogo de Loyverse con paginación.
        Cada ítem incluye sus variantes (sku, barcode, precio, costo).
        """
        logger.info("Descargando catálogo de productos desde Loyverse...")
        todos: list[dict] = []
        params: dict[str, Any] = {"limit": 250}
        pagina = 1

        while True:
            data = self._get("items", params=params)
            items = data.get("items", [])
            todos.extend(items)
            logger.debug("Página %d: %d ítems", pagina, len(items))
            cursor = data.get("cursor")
            if not cursor:
                break
            params = {"cursor": cursor}
            pagina += 1

        logger.info("Total ítems en Loyverse: %d", len(todos))
        return todos

    def get_inventory_levels(self, items: list[dict], store_id: str | None = None) -> dict[str, float]:
        """
        Obtiene el stock actual de todas las variantes.
        La API de Loyverse requiere variant_ids explícitos — los extraemos de los items.
        Retorna un dict {variant_id: in_stock}.
        """
        logger.info("Consultando niveles de inventario en Loyverse...")

        # Extraer todos los variant_ids de los items
        variant_ids = [
            v["variant_id"]
            for item in items
            for v in item.get("variants", [])
            if v.get("variant_id")
        ]

        if not variant_ids:
            logger.warning("No hay variant_ids para consultar inventario")
            return {}

        # Obtener store_id si no se proporcionó
        if not store_id:
            stores_data = self._get("stores")
            stores = stores_data.get("stores", [])
            if stores:
                store_id = stores[0]["id"]

        niveles: dict[str, float] = {}

        # La API acepta hasta 100 variant_ids por llamada
        CHUNK = 100
        for i in range(0, len(variant_ids), CHUNK):
            chunk = variant_ids[i:i + CHUNK]
            params: dict[str, Any] = {
                "variant_ids": ",".join(chunk),
                "limit": 250,
            }
            if store_id:
                params["store_id"] = store_id

            while True:
                data = self._get("inventory_levels", params=params)
                for nivel in data.get("inventory_levels", []):
                    vid = nivel.get("variant_id", "")
                    stock = nivel.get("in_stock", 0.0) or 0.0
                    if vid:
                        niveles[vid] = float(stock)
                cursor = data.get("cursor")
                if not cursor:
                    break
                params = {"cursor": cursor}

        logger.info("Niveles de inventario obtenidos: %d variantes", len(niveles))
        return niveles

    def get_categories(self) -> dict[str, str]:
        """Retorna un dict {category_id: nombre_categoria}."""
        data = self._get("categories")
        return {
            c["id"]: c.get("name", "")
            for c in data.get("categories", [])
            if c.get("id")
        }

    # ── Enriquecer recibo ─────────────────────────────────────────────────────

    def enriquecer_recibo(self, recibo: dict) -> dict:
        """
        Agrega al recibo los datos del cliente (si existe) y de la tienda.
        Retorna el recibo con los campos 'customer_data' y 'store_data' agregados.
        """
        recibo = dict(recibo)  # no mutar el original

        # La API real de Loyverse identifica los recibos por 'receipt_number'
        # (no expone un campo 'id'). Normalizamos para que el resto del sistema
        # use un identificador único estable independientemente de la fuente.
        if not recibo.get("id"):
            recibo["id"] = str(recibo.get("receipt_number", ""))

        customer_id = recibo.get("customer_id")
        if customer_id:
            recibo["customer_data"] = self.get_customer(customer_id) or {}
        else:
            recibo["customer_data"] = {}

        store_id = recibo.get("store_id")
        recibo["store_data"] = self.get_store_info(store_id)

        return recibo


# ── Cliente mock ──────────────────────────────────────────────────────────────

class LoyverseMockClient:
    """
    Simula la API de Loyverse con datos realistas de Innovaciones JOVI.
    Usar mientras no se tengan credenciales de Hacienda (USE_MOCK=true).
    """

    def get_receipts_today(self) -> list[dict[str, Any]]:
        logger.info("[MOCK] Generando ventas de ejemplo para Innovaciones JOVI")
        ahora = datetime.now(TZ_CR).isoformat()

        return [
            {
                "id": "lr-uuid-0001",
                "receipt_number": 1001,
                "receipt_date": ahora,
                "cancelled": False,
                "total_money": 25400.0,
                "total_tax": 2920.35,
                "customer_id": "cust-uuid-0001",
                "store_id": "store-uuid-001",
                "payments": [{"type": "CASH", "money_amount": 25400.0}],
                "line_items": [
                    {
                        "item_name": "Servicio de mantenimiento",
                        "sku": "MANT-001",
                        "quantity": 1.0,
                        "price": 22469.03,
                        "total_money": 22469.03,
                        "total_tax_money": 2920.97,
                        "total_discount": 0.0,
                        "taxes": [{"name": "IVA", "rate": 13.0, "inclusive": True,
                                   "money_amount": 2920.97}],
                    },
                    {
                        "item_name": "Repuesto generico",
                        "sku": "REP-099",
                        "quantity": 1.0,
                        "price": 2930.97,
                        "total_money": 2930.97,
                        "total_tax_money": 0.0,
                        "total_discount": 0.0,
                        "taxes": [],  # exento
                    },
                ],
                # Datos de cliente enriquecidos directamente en mock
                "customer_data": {
                    "id": "cust-uuid-0001",
                    "name": "Maria Gomez Rodriguez",
                    "email": "maria.gomez@ejemplo.com",
                    "customer_code": "206780123",   # cedula fisica
                    "phone_number": "8888-1234",
                },
                "store_data": {
                    "sucursal": "001",
                    "terminal": "00001",
                    "nombre": "Innovaciones JOVI",
                },
            },
            {
                "id": "lr-uuid-0002",
                "receipt_number": 1002,
                "receipt_date": ahora,
                "cancelled": False,
                "total_money": 11300.0,
                "total_tax": 1300.0,
                "customer_id": None,   # sin cliente = sin factura
                "store_id": "store-uuid-001",
                "payments": [{"type": "CARD", "money_amount": 11300.0}],
                "line_items": [
                    {
                        "item_name": "Accesorio electronico",
                        "sku": "ACC-005",
                        "quantity": 2.0,
                        "price": 5650.0,
                        "total_money": 11300.0,
                        "total_tax_money": 1300.0,
                        "total_discount": 0.0,
                        "taxes": [{"name": "IVA", "rate": 13.0, "inclusive": True,
                                   "money_amount": 1300.0}],
                    }
                ],
                "customer_data": {},
                "store_data": {
                    "sucursal": "001",
                    "terminal": "00001",
                },
            },
            {
                "id": "lr-uuid-0003",
                "receipt_number": 1003,
                "receipt_date": ahora,
                "cancelled": False,
                "total_money": 45200.0,
                "total_tax": 5196.46,
                "customer_id": "cust-uuid-0002",
                "store_id": "store-uuid-001",
                "payments": [
                    {"type": "CARD", "money_amount": 30000.0},
                    {"type": "CASH", "money_amount": 15200.0},
                ],
                "line_items": [
                    {
                        "item_name": "Equipo de computo",
                        "sku": "EQ-020",
                        "quantity": 1.0,
                        "price": 40003.54,
                        "total_money": 40003.54,
                        "total_tax_money": 4607.70,
                        "total_discount": 0.0,
                        "taxes": [{"name": "IVA", "rate": 13.0, "inclusive": True,
                                   "money_amount": 4607.70}],
                    },
                    {
                        "item_name": "Garantia extendida",
                        "sku": "GAR-001",
                        "quantity": 1.0,
                        "price": 5196.46,
                        "total_money": 5196.46,
                        "total_tax_money": 598.76,
                        "total_discount": 500.0,
                        "taxes": [{"name": "IVA", "rate": 13.0, "inclusive": True,
                                   "money_amount": 598.76}],
                    },
                ],
                "customer_data": {
                    "id": "cust-uuid-0002",
                    "name": "Tech Soluciones Ltda",
                    "email": "compras@techsoluciones.com",
                    "customer_code": "3102456789",  # cedula juridica
                    "phone_number": "2234-5678",
                },
                "store_data": {
                    "sucursal": "001",
                    "terminal": "00001",
                },
            },
        ]

    def enriquecer_recibo(self, recibo: dict) -> dict:
        """En el mock los recibos ya vienen enriquecidos."""
        return recibo

    def get_store_info(self, store_id: str | None = None) -> dict:
        return {
            "sucursal": settings.DEFAULT_SUCURSAL,
            "terminal": settings.DEFAULT_TERMINAL,
            "nombre": "Innovaciones JOVI",
        }


# ── Factory ───────────────────────────────────────────────────────────────────

def get_loyverse_client() -> LoyverseClient | LoyverseMockClient:
    if settings.USE_MOCK_LOYVERSE:
        logger.warning("[MOCK] Usando LoyverseMockClient. Cambia USE_MOCK_LOYVERSE=false para datos reales.")
        return LoyverseMockClient()
    logger.info("Usando Loyverse REAL (API en vivo).")
    return LoyverseClient()
