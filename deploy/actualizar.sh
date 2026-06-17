#!/bin/bash
# =============================================================================
# actualizar.sh — Actualiza el servicio sin perder datos ni tiempo de inactividad
#
# Ejecutar en el servidor cada vez que haya cambios en el código:
#   cd /opt/facturacion-jovi && bash deploy/actualizar.sh
# =============================================================================

set -euo pipefail

GREEN='\033[0;32m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC} $*"; }

APP_DIR="/opt/facturacion-jovi"
cd "$APP_DIR"

info "Descargando cambios de GitHub..."
git pull origin main

info "Reconstruyendo imagen Docker..."
docker compose build --no-cache

info "Reiniciando servicio (sin perder datos)..."
docker compose up -d --force-recreate

info "Esperando arranque..."
sleep 10

STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ || echo "000")
if [ "$STATUS" = "200" ]; then
    info "Actualizacion completada. Servicio activo."
else
    echo "ADVERTENCIA: El servicio respondio HTTP $STATUS"
    echo "Revisa los logs: docker compose logs --tail=50"
fi
