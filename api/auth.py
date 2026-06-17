"""
api/auth.py — Autenticación por API Key para los endpoints del servicio.

Todos los endpoints de acción requieren el header:
  X-API-Key: <valor de API_KEY en .env>

Si API_KEY está vacía (desarrollo local), se omite la validación con una advertencia.
"""
import logging

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from config import settings

logger = logging.getLogger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verificar_api_key(api_key: str | None = Security(_api_key_header)) -> str:
    """
    Dependencia de FastAPI que valida el header X-API-Key.

    Uso en un endpoint:
        @router.post("/ruta")
        async def mi_endpoint(key: str = Depends(verificar_api_key)):
            ...
    """
    if not settings.api_key_configurada():
        logger.warning(
            "API_KEY no configurada. Cualquier peticion es aceptada. "
            "Define API_KEY en produccion."
        )
        return "sin-autenticacion"

    if not api_key or api_key != settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API Key invalida o ausente. Incluye el header X-API-Key.",
        )

    return api_key
