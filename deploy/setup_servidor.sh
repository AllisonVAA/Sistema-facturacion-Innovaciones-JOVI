#!/bin/bash
# =============================================================================
# setup_servidor.sh — Configuración inicial del Droplet de DigitalOcean
#
# Ejecutar UNA SOLA VEZ en el servidor, como root o con sudo:
#   bash setup_servidor.sh
#
# Qué hace:
#   1. Actualiza el sistema
#   2. Instala Docker y Docker Compose
#   3. Configura el firewall (UFW)
#   4. Crea el usuario "jovi" sin privilegios para correr la app
#   5. Clona el repositorio de GitHub
#   6. Crea el archivo .env desde la plantilla
#   7. Arranca el servicio
# =============================================================================

set -euo pipefail   # Detener ante cualquier error

# ── Colores para los mensajes ─────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC} $*"; }
warning() { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Verificaciones previas ─────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || error "Ejecuta este script como root: sudo bash setup_servidor.sh"

info "======================================================"
info " Innovaciones JOVI — Configuración del servidor"
info "======================================================"

# ── Solicitar datos de configuración ──────────────────────────────────────────
echo ""
read -rp "URL de tu repositorio GitHub (privado): " REPO_URL
read -rp "Dominio para el servicio (ej: facturacion.miempresa.com): " DOMINIO
read -rp "Token de Loyverse: " LOYVERSE_TOKEN
read -rp "API Key para proteger los endpoints (escribe una clave larga): " API_KEY
read -rp "Correo Gmail para SMTP (innovacionesjovi.lib@gmail.com): " SMTP_USER
read -rsp "Contraseña de aplicación Gmail (16 caracteres sin espacios): " SMTP_PASSWORD
echo ""

# ── 1. Actualizar sistema ──────────────────────────────────────────────────────
info "Actualizando paquetes del sistema..."
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq curl git ufw

# ── 2. Instalar Docker ─────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    info "Instalando Docker..."
    curl -fsSL https://get.docker.com | bash
    systemctl enable docker
    systemctl start docker
    info "Docker instalado: $(docker --version)"
else
    info "Docker ya instalado: $(docker --version)"
fi

# ── 3. Configurar firewall UFW ────────────────────────────────────────────────
info "Configurando firewall..."
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh          # Puerto 22 — acceso SSH
ufw allow 80/tcp       # HTTP — necesario para Let's Encrypt
ufw allow 443/tcp      # HTTPS
ufw allow 443/udp      # HTTP/3
ufw --force enable
info "Firewall activo. Puertos abiertos: 22 (SSH), 80 (HTTP), 443 (HTTPS)"

# ── 4. Crear usuario sin privilegios ──────────────────────────────────────────
APP_USER="jovi"
APP_DIR="/opt/facturacion-jovi"

if ! id "$APP_USER" &>/dev/null; then
    info "Creando usuario '$APP_USER'..."
    useradd -m -s /bin/bash "$APP_USER"
    usermod -aG docker "$APP_USER"
fi

# ── 5. Clonar repositorio ─────────────────────────────────────────────────────
info "Clonando repositorio en $APP_DIR..."
if [ -d "$APP_DIR" ]; then
    warning "El directorio $APP_DIR ya existe. Actualizando..."
    cd "$APP_DIR" && git pull
else
    git clone "$REPO_URL" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ── 6. Crear archivo .env ─────────────────────────────────────────────────────
info "Creando archivo .env..."
ENV_FILE="$APP_DIR/.env"

cat > "$ENV_FILE" << EOF
# Generado automáticamente por setup_servidor.sh
# Modificar con: nano $ENV_FILE

# ── Loyverse ──────────────────────────────────────────────────────────────────
LOYVERSE_TOKEN=${LOYVERSE_TOKEN}
LOYVERSE_BASE_URL=https://api.loyverse.com/v1.0
LOYVERSE_CEDULA_FIELD=customer_code

# ── CRlibre / Hacienda (completar cuando se tengan credenciales) ───────────────
HACIENDA_USER=
HACIENDA_PASSWORD=
HACIENDA_PIN=
CERT_BASE64=
AMBIENTE=sandbox
USE_MOCK=true

# ── API del servicio ───────────────────────────────────────────────────────────
API_KEY=${API_KEY}
PORT=8000
HORA_EJECUCION=17

# ── Datos del emisor ───────────────────────────────────────────────────────────
EMISOR_TIPO_CEDULA=01
EMISOR_CEDULA=503910760
EMISOR_NOMBRE=Innovaciones JOVI
EMISOR_ACTIVIDAD=523406
EMISOR_EMAIL=innovacionesjovi.lib@gmail.com
EMISOR_TELEFONO=83586183
EMISOR_PROVINCIA=5
EMISOR_CANTON=01
EMISOR_DISTRITO=01
EMISOR_OTRAS_SENAS=75mts sur de tienda La Nueva, contiguo a la Copa de Oro

# ── Email SMTP ─────────────────────────────────────────────────────────────────
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=${SMTP_USER}
SMTP_PASSWORD=${SMTP_PASSWORD}
SMTP_FROM_NAME=Innovaciones JOVI

# ── Configuración operativa ────────────────────────────────────────────────────
DEFAULT_SUCURSAL=001
DEFAULT_TERMINAL=00001
TIPO_COMPROBANTE_CON_CEDULA=01
MONEDA=CRC
MAX_RETRIES=3
RETRY_DELAY_SECONDS=15
LOG_LEVEL=INFO
EOF

# Permisos seguros — solo el usuario de la app puede leer el .env
chmod 600 "$ENV_FILE"
chown "$APP_USER:$APP_USER" "$ENV_FILE"
info ".env creado con permisos seguros (600)"

# ── 7. Configurar el Caddyfile con el dominio ─────────────────────────────────
info "Configurando Caddyfile para el dominio: $DOMINIO"
sed -i "s/tudominio\.com/${DOMINIO}/g" "$APP_DIR/Caddyfile"

# ── 8. Construir imagen Docker e iniciar servicio ─────────────────────────────
info "Construyendo imagen Docker (puede tardar 2-3 minutos)..."
cd "$APP_DIR"
docker compose build --no-cache

info "Iniciando el servicio..."
docker compose up -d

# Esperar que el servicio arranque
info "Esperando que el servicio inicie..."
sleep 15

# ── 9. Verificar que funciona ─────────────────────────────────────────────────
info "Verificando el servicio..."
STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ || echo "000")

if [ "$STATUS" = "200" ]; then
    info "======================================================"
    info " Servicio funcionando correctamente!"
    info "======================================================"
    echo ""
    echo "  URL: https://${DOMINIO}"
    echo "  API Key: ${API_KEY}"
    echo ""
    echo "  Prueba el servicio:"
    echo "  curl https://${DOMINIO}/"
    echo ""
    echo "  Trigger manual de facturacion:"
    echo "  curl -X POST https://${DOMINIO}/api/facturar/hoy \\"
    echo "       -H 'X-API-Key: ${API_KEY}'"
    echo ""
    warning "IMPORTANTE: Guarda la API Key en un lugar seguro."
    warning "Para activar Hacienda real, editar: nano ${ENV_FILE}"
else
    error "El servicio no respondio (HTTP $STATUS). Revisa los logs: docker compose logs"
fi

info "Logs en tiempo real: cd $APP_DIR && docker compose logs -f"
