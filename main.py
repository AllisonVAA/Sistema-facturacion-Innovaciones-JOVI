"""
main.py — Orquestador de facturación electrónica de Innovaciones JOVI.

Flujo diario (5:00 PM):
  1. Conecta a Loyverse y obtiene TODOS los recibos del día (con paginación).
  2. Enriquece cada recibo con datos del cliente y de la tienda.
  3. Aplica las reglas de negocio:
       - Solo se factura si el cliente tiene cédula registrada en Loyverse.
       - Sin cédula = el cliente no requirió factura electrónica (se omite).
  4. Evita duplicados consultando la BD local.
  5. Genera el XML, lo firma (o simula) y lo envía a Hacienda.
  6. Guarda el resultado en SQLite.
  7. Envía la factura por email al cliente.
  8. Reintenta facturas que fallaron por error de conexión.
  9. Imprime un resumen del día.

Cron sugerido (5:00 PM todos los días):
  0 17 * * * /ruta/al/venv/bin/python /ruta/al/proyecto/main.py >> /ruta/logs/cron.log 2>&1

Windows Task Scheduler:
  Programa: C:\\ruta\\venv\\Scripts\\python.exe
  Argumentos: C:\\ruta\\proyecto\\main.py
"""
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import settings
from database.storage import (
    EstadoFactura,
    TipoError,
    actualizar_estado,
    init_db,
    marcar_email_enviado,
    obtener_para_reintento,
    registrar_factura,
    resumen_del_dia,
    ya_fue_procesada,
)
from facturacion.crlibre_adapter import procesar_factura
from facturacion.email_sender import enviar_factura_por_email
from loyverse.client import get_loyverse_client

TZ_CR = ZoneInfo("America/Costa_Rica")


# ── Logging ───────────────────────────────────────────────────────────────────

def _configurar_logging() -> None:
    log_dir = Path(settings.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    fecha_str = datetime.now(TZ_CR).strftime("%Y-%m-%d")
    log_file  = log_dir / f"facturacion_{fecha_str}.log"

    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    fmt   = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"

    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(log_file), encoding="utf-8"),
        ],
    )


logger = logging.getLogger(__name__)


# ── Reglas de negocio ─────────────────────────────────────────────────────────

def _tiene_cedula(recibo: dict) -> bool:
    """Devuelve True si el cliente tiene cédula registrada en Loyverse."""
    cliente = recibo.get("customer_data") or {}
    campo   = settings.LOYVERSE_CEDULA_FIELD
    valor   = cliente.get(campo, "") or ""
    cedula  = "".join(c for c in valor if c.isdigit())
    return bool(cedula)


def _determinar_tipo_comprobante(recibo: dict) -> str:
    """
    Determina el tipo de comprobante según los datos del cliente.
    Con cédula -> TIPO_COMPROBANTE_CON_CEDULA (01 por defecto).
    """
    return settings.TIPO_COMPROBANTE_CON_CEDULA


def _clasificar_error(exc: Exception) -> TipoError:
    """
    Determina si un error es de validación (no reintenta) o de conexión (reintenta).
    """
    msg = str(exc).lower()
    if isinstance(exc, (ValueError, AssertionError)):
        return TipoError.VALIDACION
    if any(k in msg for k in ("connection", "timeout", "network", "refused", "unreachable")):
        return TipoError.CONEXION
    return TipoError.DESCONOCIDO


# ── Procesamiento de una venta ────────────────────────────────────────────────

def _procesar_venta(recibo: dict) -> bool:
    """
    Intenta facturar un recibo con reintentos ante errores de conexión.
    Retorna True si la factura fue ACEPTADA por Hacienda.
    """
    receipt_id  = recibo["id"]
    receipt_num = str(recibo.get("receipt_number", ""))
    fecha       = recibo.get("receipt_date", datetime.now(TZ_CR).isoformat())

    store      = recibo.get("store_data", {})
    sucursal   = store.get("sucursal", settings.DEFAULT_SUCURSAL)
    terminal   = store.get("terminal", settings.DEFAULT_TERMINAL)
    tipo       = _determinar_tipo_comprobante(recibo)
    cliente    = recibo.get("customer_data") or {}

    campo_cedula = settings.LOYVERSE_CEDULA_FIELD
    cedula_raw   = "".join(
        c for c in (cliente.get(campo_cedula) or "") if c.isdigit()
    )

    registrar_factura(
        receipt_id      = receipt_id,
        receipt_number  = receipt_num,
        fecha_emision   = fecha,
        tipo_comprobante = tipo,
        receptor_cedula = cedula_raw or None,
        receptor_nombre = cliente.get("name"),
        receptor_email  = cliente.get("email"),
        total_comprobante = float(recibo.get("total_money", 0)),
        total_impuesto    = float(recibo.get("total_tax", 0)),
    )

    for intento in range(1, settings.MAX_RETRIES + 1):
        try:
            logger.info(
                "Procesando recibo #%s [ID: %s] (intento %d/%d)",
                receipt_num, receipt_id, intento, settings.MAX_RETRIES,
            )

            resultado = procesar_factura(
                recibo           = recibo,
                tipo_comprobante = tipo,
                sucursal         = sucursal,
                terminal         = terminal,
            )

            respuesta = resultado["respuesta"]
            estado    = (
                EstadoFactura.ACEPTADA
                if resultado["aceptada"]
                else EstadoFactura.RECHAZADA
            )

            actualizar_estado(
                receipt_id,
                estado,
                clave       = resultado["clave"],
                consecutivo = resultado["consecutivo"],
                xml_path    = resultado["xml_path"],
                respuesta   = respuesta,
            )

            if resultado["aceptada"]:
                logger.info(
                    "Recibo #%s ACEPTADO | Clave: ...%s | Total: CRC %.2f",
                    receipt_num,
                    resultado["clave"][-8:],
                    resultado["total"],
                )
                # Enviar email al cliente
                email_ok = enviar_factura_por_email(recibo, resultado)
                if email_ok:
                    marcar_email_enviado(receipt_id)
                return True

            else:
                # Rechazo de Hacienda = definitivo, no reintenta
                logger.warning(
                    "Recibo #%s RECHAZADO por Hacienda: %s",
                    receipt_num,
                    respuesta.get("detalle-mensaje", "sin detalle"),
                )
                return False

        except ValueError as exc:
            # Error de validación: datos incorrectos, no tiene sentido reintentar
            tipo_err = TipoError.VALIDACION
            logger.error(
                "Error de validacion en recibo #%s (no se reintenta): %s",
                receipt_num, exc,
            )
            actualizar_estado(
                receipt_id, EstadoFactura.ERROR,
                error=str(exc), tipo_error=tipo_err,
            )
            return False

        except Exception as exc:
            tipo_err = _clasificar_error(exc)
            logger.error(
                "Error en recibo #%s (intento %d, tipo=%s): %s",
                receipt_num, intento, tipo_err, exc,
                exc_info=(intento == settings.MAX_RETRIES),
            )
            actualizar_estado(
                receipt_id, EstadoFactura.ERROR,
                error=str(exc), tipo_error=tipo_err,
            )

            if tipo_err == TipoError.VALIDACION:
                return False

            if intento < settings.MAX_RETRIES:
                espera = settings.RETRY_DELAY_SECONDS * intento
                logger.info("Reintento en %ds...", espera)
                time.sleep(espera)

    logger.error("Recibo #%s agoto todos los reintentos.", receipt_num)
    return False


# ── Reintentos de ejecuciones anteriores ──────────────────────────────────────

def _reintentar_errores_previos(mapa_recibos: dict[str, dict]) -> None:
    """
    Reintenta facturas en estado ERROR de ejecuciones anteriores
    (errores de conexión que quedaron sin resolver).
    """
    pendientes = obtener_para_reintento(settings.MAX_RETRIES)
    if not pendientes:
        logger.info("Sin errores pendientes de ejecuciones anteriores.")
        return

    logger.info("Reintentando %d factura(s) de ejecuciones anteriores...", len(pendientes))
    for reg in pendientes:
        rid = reg["loyverse_receipt_id"]
        recibo = mapa_recibos.get(rid)
        if recibo is None:
            logger.warning(
                "Recibo %s no encontrado en el lote de hoy. "
                "Solo se pueden reintentar ventas del dia actual.",
                rid,
            )
            continue
        _procesar_venta(recibo)


# ── Orquestador principal ─────────────────────────────────────────────────────

def run() -> dict:
    """
    Orquesta el proceso completo de facturación del día.
    Retorna un dict con el resumen del resultado (usado por el endpoint API).
    """
    _configurar_logging()
    inicio = datetime.now(TZ_CR)

    logger.info("=" * 65)
    logger.info("INNOVACIONES JOVI - Facturacion Electronica CR")
    logger.info("Inicio: %s | Ambiente: %s | Mock: %s",
                inicio.strftime("%Y-%m-%d %H:%M:%S"), settings.AMBIENTE, settings.USE_MOCK)
    logger.info("=" * 65)

    # 1. Inicializar BD
    init_db()

    # 2. Obtener ventas del día (con paginación completa)
    cliente_lv = get_loyverse_client()
    try:
        recibos_raw = cliente_lv.get_receipts_today()
    except Exception as exc:
        logger.critical("No se pudo conectar a Loyverse: %s", exc)
        sys.exit(1)

    if not recibos_raw:
        logger.info("No hay ventas registradas en Loyverse para hoy.")
        logger.info("Proceso finalizado sin facturas.")
        return {"recibos_totales": 0, "aceptadas": 0, "rechazadas": 0, "errores": 0}

    # 3. Enriquecer recibos (cliente + tienda)
    logger.info("Enriqueciendo %d recibo(s) con datos de clientes...", len(recibos_raw))
    recibos = [cliente_lv.enriquecer_recibo(r) for r in recibos_raw]

    # 4. Clasificar recibos
    con_cedula     = [r for r in recibos if _tiene_cedula(r)]
    sin_cedula     = [r for r in recibos if not _tiene_cedula(r)]
    ya_procesados  = [r for r in con_cedula if ya_fue_procesada(r["id"])]
    por_facturar   = [r for r in con_cedula if not ya_fue_procesada(r["id"])]

    logger.info(
        "Recibos: %d total | %d sin cedula (omitidos) | "
        "%d ya facturados | %d a procesar",
        len(recibos), len(sin_cedula), len(ya_procesados), len(por_facturar),
    )
    if sin_cedula:
        numeros = [str(r.get("receipt_number", r["id"])) for r in sin_cedula]
        logger.info("Recibos sin cedula (sin factura): #%s", ", #".join(numeros))

    # 5. Procesar facturas nuevas
    exitosas = 0
    rechazadas = 0
    errores = 0

    for recibo in por_facturar:
        ok = _procesar_venta(recibo)
        if ok:
            exitosas += 1
        elif ya_fue_procesada(recibo["id"]):
            pass  # fue rechazada definitivamente, ya contado
        else:
            errores += 1

    # Contar rechazadas (estado final en BD)
    resumen_bd = resumen_del_dia()
    rechazadas = resumen_bd.get("rechazada", 0)

    # 6. Reintentar errores de ejecuciones previas
    mapa_recibos = {r["id"]: r for r in recibos}
    _reintentar_errores_previos(mapa_recibos)

    # 7. Resumen final
    duracion = (datetime.now(TZ_CR) - inicio).total_seconds()
    resumen_bd = resumen_del_dia()

    resumen_final = {
        "fecha":             inicio.strftime("%Y-%m-%d"),
        "recibos_totales":   len(recibos),
        "sin_cedula":        len(sin_cedula),
        "ya_facturados":     len(ya_procesados),
        "aceptadas":         exitosas,
        "rechazadas":        rechazadas,
        "errores":           resumen_bd.get("error", 0),
        "duracion_segundos": round(duracion, 1),
    }

    logger.info("-" * 65)
    logger.info("RESUMEN DEL DIA - %s", inicio.strftime("%d/%m/%Y"))
    logger.info("  Recibos totales en Loyverse : %d", len(recibos))
    logger.info("  Sin cedula (omitidos)       : %d", len(sin_cedula))
    logger.info("  Facturas aceptadas hoy      : %d", exitosas)
    logger.info("  Facturas rechazadas         : %d", rechazadas)
    logger.info("  Errores pendientes          : %d", resumen_bd.get("error", 0))
    logger.info("  Estado acumulado en BD:")
    for estado, total in sorted(resumen_bd.items()):
        logger.info("    %-12s : %d", estado, total)
    logger.info("  Duracion total              : %.1fs", duracion)
    logger.info("=" * 65)

    return resumen_final


if __name__ == "__main__":
    run()
