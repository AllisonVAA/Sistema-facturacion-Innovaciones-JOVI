"""
facturacion/email_sender.py — Envío de factura electrónica por email al cliente.

Usa SMTP con TLS (Gmail por defecto).
Requiere configurar SMTP_PASSWORD en el .env con una
"Contraseña de aplicación" de Google (no la contraseña de Gmail normal).

Cómo generar la contraseña de aplicación:
  1. Ir a myaccount.google.com
  2. Seguridad → Verificación en 2 pasos (debe estar activa)
  3. Contraseñas de aplicaciones → Seleccionar app "Correo", dispositivo "Otro"
  4. Copiar la contraseña de 16 caracteres al .env como SMTP_PASSWORD
"""
import logging
import smtplib
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

from config import settings

logger = logging.getLogger(__name__)

TZ_CR = ZoneInfo("America/Costa_Rica")


def _html_cuerpo(resultado: dict, recibo: dict) -> str:
    """Genera el cuerpo HTML del email de la factura."""
    clave        = resultado.get("clave", "")
    consecutivo  = resultado.get("consecutivo", "")
    total        = resultado.get("total", 0.0)
    impuesto     = resultado.get("total_impuesto", 0.0)
    receptor     = resultado.get("receptor_nombre", "Cliente")
    fecha        = datetime.now(TZ_CR).strftime("%d/%m/%Y %H:%M")
    receipt_num  = recibo.get("receipt_number", "")

    # Formatear montos con separador de miles
    def fmt(n: float) -> str:
        return f"&#8353;{n:,.2f}"   # ₡ en HTML

    items_html = ""
    for item in recibo.get("line_items", []):
        nombre   = item.get("item_name", "Artículo")
        cant     = item.get("quantity", 1)
        total_ln = item.get("total_money", 0.0)
        items_html += f"""
        <tr>
            <td style="padding:8px;border-bottom:1px solid #eee;">{nombre}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;text-align:center;">{cant}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;text-align:right;">{fmt(total_ln)}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;color:#333;max-width:600px;margin:auto;padding:20px;">

    <div style="background:#1a6b3c;padding:20px;border-radius:8px 8px 0 0;text-align:center;">
        <h2 style="color:white;margin:0;">Innovaciones JOVI</h2>
        <p style="color:#c8e6c9;margin:4px 0 0;">Factura Electrónica</p>
    </div>

    <div style="background:#f9f9f9;border:1px solid #ddd;border-top:none;padding:24px;border-radius:0 0 8px 8px;">

        <p>Estimado/a <strong>{receptor}</strong>,</p>
        <p>Adjunto encontrará su comprobante electrónico emitido ante el Ministerio de Hacienda de Costa Rica.</p>

        <table style="width:100%;border-collapse:collapse;margin:16px 0;">
            <tr style="background:#e8f5e9;">
                <td style="padding:8px;font-weight:bold;">N° de Venta</td>
                <td style="padding:8px;">{receipt_num}</td>
            </tr>
            <tr>
                <td style="padding:8px;font-weight:bold;">Fecha</td>
                <td style="padding:8px;">{fecha} (hora Costa Rica)</td>
            </tr>
            <tr style="background:#e8f5e9;">
                <td style="padding:8px;font-weight:bold;">Consecutivo</td>
                <td style="padding:8px;font-family:monospace;">{consecutivo}</td>
            </tr>
            <tr>
                <td style="padding:8px;font-weight:bold;">Clave Hacienda</td>
                <td style="padding:8px;font-family:monospace;font-size:11px;">{clave}</td>
            </tr>
        </table>

        <h4 style="border-bottom:2px solid #1a6b3c;padding-bottom:4px;">Detalle de la compra</h4>
        <table style="width:100%;border-collapse:collapse;">
            <thead>
                <tr style="background:#1a6b3c;color:white;">
                    <th style="padding:8px;text-align:left;">Descripción</th>
                    <th style="padding:8px;text-align:center;">Cant.</th>
                    <th style="padding:8px;text-align:right;">Total</th>
                </tr>
            </thead>
            <tbody>{items_html}
            </tbody>
        </table>

        <table style="width:100%;margin-top:12px;">
            <tr>
                <td style="text-align:right;padding:4px;">IVA incluido:</td>
                <td style="text-align:right;padding:4px;width:130px;">{fmt(impuesto)}</td>
            </tr>
            <tr style="font-size:18px;font-weight:bold;color:#1a6b3c;">
                <td style="text-align:right;padding:4px;">TOTAL:</td>
                <td style="text-align:right;padding:4px;">{fmt(total)}</td>
            </tr>
        </table>

        <hr style="margin:24px 0;border:none;border-top:1px solid #ddd;">

        <p style="font-size:12px;color:#666;">
            Este comprobante fue generado y enviado a Hacienda de forma electrónica.<br>
            Puede verificarlo en <a href="https://www.hacienda.go.cr" style="color:#1a6b3c;">hacienda.go.cr</a>
            con la clave indicada arriba.<br><br>
            <strong>Innovaciones JOVI</strong><br>
            Cédula: {settings.EMISOR_CEDULA} | Tel: {settings.EMISOR_TELEFONO}<br>
            {settings.EMISOR_OTRAS_SENAS}
        </p>
    </div>
</body>
</html>"""


def enviar_factura_por_email(
    recibo: dict,
    resultado: dict,
) -> bool:
    """
    Envía la factura electrónica al correo del cliente.

    Args:
        recibo:    Recibo enriquecido de Loyverse (con customer_data).
        resultado: Dict retornado por procesar_factura() en crlibre_adapter.

    Returns:
        True si el email fue enviado exitosamente, False en caso contrario.
    """
    if not settings.email_configurado():
        logger.warning(
            "SMTP no configurado (SMTP_USER o SMTP_PASSWORD vacios). "
            "Agrega una contrasena de aplicacion de Google al .env."
        )
        return False

    email_receptor = resultado.get("receptor_email", "")
    if not email_receptor:
        logger.info(
            "Cliente sin email registrado en Loyverse. No se envia correo."
        )
        return False

    xml_path = resultado.get("xml_path", "")
    if not xml_path or not Path(xml_path).exists():
        logger.warning("XML no encontrado en %s. No se adjunta al email.", xml_path)

    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = (
            f"Factura Electronica - Innovaciones JOVI - "
            f"#{recibo.get('receipt_number', '')}"
        )
        msg["From"]    = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_USER}>"
        msg["To"]      = email_receptor

        # Cuerpo HTML
        cuerpo = MIMEText(_html_cuerpo(resultado, recibo), "html", "utf-8")
        msg.attach(cuerpo)

        # Adjuntar XML
        if xml_path and Path(xml_path).exists():
            with open(xml_path, "rb") as f:
                adjunto = MIMEBase("application", "xml")
                adjunto.set_payload(f.read())
            encoders.encode_base64(adjunto)
            nombre_xml = Path(xml_path).name
            adjunto.add_header(
                "Content-Disposition",
                "attachment",
                filename=nombre_xml,
            )
            msg.attach(adjunto)

        # Enviar
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(settings.SMTP_USER, email_receptor, msg.as_string())

        logger.info(
            "Email enviado a %s (factura %s)",
            email_receptor,
            resultado.get("consecutivo", ""),
        )
        return True

    except smtplib.SMTPAuthenticationError:
        logger.error(
            "Error de autenticacion SMTP. Verifica que SMTP_PASSWORD sea "
            "una 'Contrasena de aplicacion' de Google, no tu contrasena normal."
        )
        return False
    except smtplib.SMTPException as exc:
        logger.error("Error SMTP al enviar email a %s: %s", email_receptor, exc)
        return False
    except Exception as exc:
        logger.error("Error inesperado al enviar email: %s", exc, exc_info=True)
        return False
