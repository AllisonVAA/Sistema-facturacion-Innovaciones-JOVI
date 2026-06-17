#!/bin/bash
# =============================================================================
# agregar_certificado.sh — Agrega el certificado .p12 de Hacienda al servidor
#
# Ejecutar cuando tengas el certificado digital del BCCR:
#   bash deploy/agregar_certificado.sh /ruta/local/certificado.p12
#
# El certificado se convierte a Base64 y se agrega al .env del servidor.
# El archivo .p12 original NUNCA se sube al repositorio.
# =============================================================================

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC} $*"; }
warning() { echo -e "${YELLOW}[WARN]${NC} $*"; }

CERT_LOCAL="${1:-}"
ENV_FILE="/opt/facturacion-jovi/.env"

if [ -z "$CERT_LOCAL" ]; then
    echo "Uso: bash deploy/agregar_certificado.sh /ruta/al/certificado.p12"
    exit 1
fi

if [ ! -f "$CERT_LOCAL" ]; then
    echo "Error: No se encontró el archivo $CERT_LOCAL"
    exit 1
fi

info "Convirtiendo certificado a Base64..."
CERT_B64=$(base64 -w 0 "$CERT_LOCAL")

info "Actualizando CERT_BASE64 en el servidor..."
# Reemplazar o agregar la variable en el .env
if grep -q "^CERT_BASE64=" "$ENV_FILE"; then
    sed -i "s|^CERT_BASE64=.*|CERT_BASE64=${CERT_B64}|" "$ENV_FILE"
else
    echo "CERT_BASE64=${CERT_B64}" >> "$ENV_FILE"
fi

# También actualizar credenciales de Hacienda
echo ""
read -rp "Usuario Hacienda (ATV): " H_USER
read -rsp "Contraseña Hacienda: " H_PASS; echo ""
read -rsp "PIN del certificado: " H_PIN; echo ""

sed -i "s|^HACIENDA_USER=.*|HACIENDA_USER=${H_USER}|" "$ENV_FILE"
sed -i "s|^HACIENDA_PASSWORD=.*|HACIENDA_PASSWORD=${H_PASS}|" "$ENV_FILE"
sed -i "s|^HACIENDA_PIN=.*|HACIENDA_PIN=${H_PIN}|" "$ENV_FILE"
sed -i "s|^USE_MOCK=.*|USE_MOCK=false|" "$ENV_FILE"
sed -i "s|^AMBIENTE=.*|AMBIENTE=sandbox|" "$ENV_FILE"

info "Reiniciando servicio con nuevas credenciales..."
cd /opt/facturacion-jovi
docker compose up -d --force-recreate

sleep 8
STATUS=$(curl -s http://localhost:8000/api/status | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('hacienda_lista'))" 2>/dev/null || echo "error")

if [ "$STATUS" = "True" ]; then
    info "Credenciales de Hacienda verificadas. Sistema listo para produccion."
    warning "Cambia AMBIENTE=produccion cuando Hacienda confirme el ambiente real."
else
    echo "ADVERTENCIA: hacienda_lista=$STATUS — Verifica las credenciales."
    echo "  Revisar logs: docker compose logs facturacion --tail=30"
fi
