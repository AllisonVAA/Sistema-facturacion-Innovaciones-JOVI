"""
api/app.py — Aplicación FastAPI principal.

Arrancar el servidor:
  Desarrollo local:
    uvicorn api.app:app --reload --port 8000

  Producción (Render / DigitalOcean):
    uvicorn api.app:app --host 0.0.0.0 --port $PORT

Documentación automática:
  http://localhost:8000/docs   (Swagger UI)
  http://localhost:8000/redoc  (ReDoc)
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routes import router
from api.scheduler import detener_scheduler, iniciar_scheduler
from config import settings
from database.storage import init_db

logger = logging.getLogger(__name__)


# ── Ciclo de vida de la aplicación ────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicialización al arrancar y limpieza al apagar."""
    # Startup
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    )
    logger.info("Iniciando Servicio de Facturacion — Innovaciones JOVI")
    logger.info("Ambiente: %s | Mock: %s", settings.AMBIENTE, settings.USE_MOCK)

    if not settings.api_key_configurada():
        logger.warning(
            "API_KEY no configurada. Los endpoints no estan protegidos. "
            "Define API_KEY en las variables de entorno del servidor."
        )

    init_db()
    scheduler = iniciar_scheduler()

    yield  # La app corre aquí

    # Shutdown
    detener_scheduler()
    logger.info("Servicio detenido.")


# ── App FastAPI ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Facturación Electrónica CR — Innovaciones JOVI",
    description=(
        "Servicio que conecta Loyverse con Hacienda Costa Rica "
        "para generar facturas electrónicas automáticamente."
    ),
    version="2.0.0",
    lifespan=lifespan,
    # Deshabilitar docs en producción si se prefiere:
    # docs_url=None, redoc_url=None,
)


# ── Middlewares ───────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # En producción, restringir al dominio propio
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log de cada petición entrante (método, ruta, status)."""
    response = await call_next(request)
    logger.info(
        "%s %s -> %d",
        request.method,
        request.url.path,
        response.status_code,
    )
    return response


# ── Manejo global de errores ─────────────────────────────────────────────────

@app.exception_handler(Exception)
async def error_handler(request: Request, exc: Exception):
    logger.error("Error no manejado en %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "Error interno del servidor", "detalle": str(exc)},
    )


# ── Registrar rutas ───────────────────────────────────────────────────────────

app.include_router(router)
