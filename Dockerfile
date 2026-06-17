# ── Imagen base ───────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Evitar archivos .pyc y buffering en logs
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ── Directorio de trabajo ─────────────────────────────────────────────────────
WORKDIR /app

# ── Dependencias del sistema ──────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Dependencias Python ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Código fuente ─────────────────────────────────────────────────────────────
COPY . .

# ── Directorios de datos persistentes ────────────────────────────────────────
# En DigitalOcean, montar un volumen en /data con docker-compose o App Platform
RUN mkdir -p /data/xml_output /data/logs /data/database

# ── Usuario sin privilegios (seguridad) ──────────────────────────────────────
RUN adduser --disabled-password --gecos "" appuser \
    && chown -R appuser:appuser /app /data
USER appuser

# ── Variables de entorno por defecto para Docker ────────────────────────────
ENV DATABASE_PATH=/data/database/facturas.db
ENV XML_OUTPUT_DIR=/data/xml_output
ENV LOG_DIR=/data/logs
ENV PORT=8000

# ── Healthcheck ───────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT}/ || exit 1

# ── Arrancar servidor ─────────────────────────────────────────────────────────
CMD ["sh", "-c", "uvicorn api.app:app --host 0.0.0.0 --port ${PORT}"]
