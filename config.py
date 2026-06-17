"""
config.py — Configuración centralizada de Innovaciones JOVI.

En producción (Render / DigitalOcean) todas las variables se definen
como Environment Variables en el panel del proveedor, nunca en archivos.
"""
import base64
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


class Config:
    # ── Loyverse ─────────────────────────────────────────────────────────────
    LOYVERSE_TOKEN: str       = os.getenv("LOYVERSE_TOKEN", "")
    LOYVERSE_BASE_URL: str    = os.getenv("LOYVERSE_BASE_URL", "https://api.loyverse.com/v1.0")
    LOYVERSE_CEDULA_FIELD: str = os.getenv("LOYVERSE_CEDULA_FIELD", "customer_code")

    # ── CRlibre / Hacienda ───────────────────────────────────────────────────
    HACIENDA_USER: str     = os.getenv("HACIENDA_USER", "")
    HACIENDA_PASSWORD: str = os.getenv("HACIENDA_PASSWORD", "")
    HACIENDA_PIN: str      = os.getenv("HACIENDA_PIN", "")
    AMBIENTE: str          = os.getenv("AMBIENTE", "sandbox")

    # El certificado .p12 se almacena como Base64 en la variable CERT_BASE64.
    # Al arrancar el servicio se escribe en un archivo temporal seguro.
    # Nunca subir el .p12 al repositorio.
    CERT_BASE64: str = os.getenv("CERT_BASE64", "")
    # Ruta local para desarrollo (solo si CERT_BASE64 está vacío)
    _CERT_PATH_LOCAL: str = os.getenv("CERT_PATH", str(BASE_DIR / "certificado.p12"))

    # ── Datos del emisor ─────────────────────────────────────────────────────
    EMISOR_TIPO_CEDULA: str  = os.getenv("EMISOR_TIPO_CEDULA", "01")
    EMISOR_CEDULA: str       = os.getenv("EMISOR_CEDULA", "503910760")
    EMISOR_NOMBRE: str       = os.getenv("EMISOR_NOMBRE", "Innovaciones JOVI")
    EMISOR_ACTIVIDAD: str    = os.getenv("EMISOR_ACTIVIDAD", "523406")
    EMISOR_EMAIL: str        = os.getenv("EMISOR_EMAIL", "innovacionesjovi.lib@gmail.com")
    EMISOR_TELEFONO: str     = os.getenv("EMISOR_TELEFONO", "83586183")
    EMISOR_PROVINCIA: str    = os.getenv("EMISOR_PROVINCIA", "5")
    EMISOR_CANTON: str       = os.getenv("EMISOR_CANTON", "01")
    EMISOR_DISTRITO: str     = os.getenv("EMISOR_DISTRITO", "01")
    EMISOR_OTRAS_SENAS: str  = os.getenv(
        "EMISOR_OTRAS_SENAS",
        "75mts sur de tienda La Nueva, contiguo a la Copa de Oro",
    )

    # ── Sucursal / Terminal ───────────────────────────────────────────────────
    DEFAULT_SUCURSAL: str = os.getenv("DEFAULT_SUCURSAL", "001")
    DEFAULT_TERMINAL: str = os.getenv("DEFAULT_TERMINAL", "00001")

    # ── Comprobante ───────────────────────────────────────────────────────────
    TIPO_COMPROBANTE_CON_CEDULA: str = os.getenv("TIPO_COMPROBANTE_CON_CEDULA", "01")

    # ── Email SMTP ────────────────────────────────────────────────────────────
    SMTP_HOST: str      = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT: int      = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER: str      = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD: str  = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM_NAME: str = os.getenv("SMTP_FROM_NAME", "Innovaciones JOVI")

    # ── API del servicio web ──────────────────────────────────────────────────
    # Clave que deben enviar los clientes en el header X-API-Key
    API_KEY: str = os.getenv("API_KEY", "")
    # Puerto del servidor (Render inyecta PORT automáticamente)
    PORT: int    = int(os.getenv("PORT", "8000"))
    # Hora (CR) en que se ejecuta el proceso diario: "17" = 5:00 PM
    HORA_EJECUCION: int = int(os.getenv("HORA_EJECUCION", "17"))

    # ── Rutas de datos ────────────────────────────────────────────────────────
    # En Render con disco persistente, montar en /data
    # En DigitalOcean con Docker, mapear volumen a /data
    DATABASE_PATH: str  = os.getenv("DATABASE_PATH",  str(BASE_DIR / "database" / "facturas.db"))
    XML_OUTPUT_DIR: str = os.getenv("XML_OUTPUT_DIR", str(BASE_DIR / "xml_output"))
    LOG_DIR: str        = os.getenv("LOG_DIR",        str(BASE_DIR / "logs"))

    # ── Sistema ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str         = os.getenv("LOG_LEVEL", "INFO")
    MAX_RETRIES: int       = int(os.getenv("MAX_RETRIES", "3"))
    RETRY_DELAY_SECONDS: int = int(os.getenv("RETRY_DELAY_SECONDS", "15"))
    USE_MOCK: bool         = os.getenv("USE_MOCK", "true").lower() == "true"
    MONEDA: str            = os.getenv("MONEDA", "CRC")
    TIMEZONE              = "America/Costa_Rica"
    UTC_OFFSET            = "-06:00"

    # ── Métodos de utilidad ───────────────────────────────────────────────────

    def get_cert_path(self) -> str:
        """
        Retorna la ruta al certificado .p12.

        Si CERT_BASE64 está definido (producción), decodifica el Base64
        y escribe el archivo en un directorio temporal seguro del SO.
        Si no, usa la ruta local CERT_PATH (desarrollo).

        Para convertir tu .p12 a Base64:
          Linux/Mac: base64 -w 0 certificado.p12
          Windows:   [Convert]::ToBase64String([IO.File]::ReadAllBytes('certificado.p12'))
        """
        if self.CERT_BASE64:
            tmp_dir = Path(tempfile.gettempdir()) / "jovi_certs"
            tmp_dir.mkdir(mode=0o700, exist_ok=True)
            cert_path = tmp_dir / "certificado.p12"
            if not cert_path.exists():
                cert_path.write_bytes(base64.b64decode(self.CERT_BASE64))
                cert_path.chmod(0o600)
            return str(cert_path)
        return self._CERT_PATH_LOCAL

    def hacienda_lista(self) -> bool:
        """True cuando las credenciales de Hacienda están completas."""
        return bool(
            self.HACIENDA_USER
            and self.HACIENDA_PASSWORD
            and self.HACIENDA_PIN
            and (self.CERT_BASE64 or Path(self._CERT_PATH_LOCAL).exists())
        )

    def email_configurado(self) -> bool:
        """True cuando el SMTP está listo para enviar."""
        return bool(self.SMTP_USER and self.SMTP_PASSWORD)

    def api_key_configurada(self) -> bool:
        """True cuando la API key del servicio está definida."""
        return bool(self.API_KEY)


settings = Config()
