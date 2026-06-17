"""
api/scheduler.py — Ejecución automática diaria con APScheduler.

Dispara el proceso de facturación a las 5:00 PM hora Costa Rica,
todos los días, sin necesidad de cron externo.

APScheduler corre en un hilo de fondo dentro del mismo proceso FastAPI,
por lo que solo necesitas un servicio corriendo en Render/DigitalOcean.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings

logger = logging.getLogger(__name__)
TZ_CR  = ZoneInfo("America/Costa_Rica")

_scheduler: BackgroundScheduler | None = None


def _ejecutar_proceso_diario() -> None:
    """Callback que ejecuta el proceso de facturación diario."""
    logger.info(
        "=== Scheduler: iniciando proceso diario (%s) ===",
        datetime.now(TZ_CR).strftime("%Y-%m-%d %H:%M:%S"),
    )
    try:
        # Importar aquí para evitar importaciones circulares al iniciar la app
        from main import run as ejecutar
        ejecutar()
    except Exception as exc:
        logger.error("Error en proceso diario programado: %s", exc, exc_info=True)


def iniciar_scheduler() -> BackgroundScheduler:
    """
    Inicia el scheduler en background. Llamar al arranque de la app FastAPI.
    Retorna la instancia para poder detenerla al apagar.
    """
    global _scheduler

    hora = settings.HORA_EJECUCION

    _scheduler = BackgroundScheduler(timezone=str(TZ_CR))
    _scheduler.add_job(
        _ejecutar_proceso_diario,
        trigger=CronTrigger(
            hour=hora,
            minute=0,
            timezone=TZ_CR,
        ),
        id="facturacion_diaria",
        name=f"Facturación diaria a las {hora:02d}:00 CR",
        replace_existing=True,
        misfire_grace_time=3600,  # Si el server estuvo caído, ejecutar hasta 1h después
    )

    _scheduler.start()

    proxima = _scheduler.get_job("facturacion_diaria").next_run_time
    logger.info(
        "Scheduler activo. Proxima ejecucion: %s (hora Costa Rica)",
        proxima.astimezone(TZ_CR).strftime("%Y-%m-%d %H:%M:%S"),
    )
    return _scheduler


def detener_scheduler() -> None:
    """Apaga el scheduler limpiamente. Llamar en el shutdown de FastAPI."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler detenido.")
