"""
facturacion/crlibre_adapter.py — Adaptador CRlibre para Innovaciones JOVI.

Responsabilidades:
  1. Convertir un recibo de Loyverse al formato de Factura Electrónica v4.3 CR.
  2. Calcular IVA inclusivo correctamente (precios ya incluyen 13%).
  3. Generar el XML según el esquema de Hacienda.
  4. Guardar el XML en disco con nombre estandarizado.
  5. Enviar a Hacienda:
       - Modo PRODUCCION: vía CRlibre (stub documentado, listo para conectar).
       - Modo MOCK:       respuesta simulada para desarrollo.

NOTA SOBRE CRLIBRE:
  Para activar la integración real, instala la librería y completa
  la función `_enviar_crlibre()` con las credenciales del .env.
  El stub está en la sección marcada con ── INTEGRACION CRLIBRE REAL ──
"""
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import settings
from database.storage import obtener_siguiente_consecutivo

logger = logging.getLogger(__name__)

TZ_CR = ZoneInfo("America/Costa_Rica")

# ── Intentar importar CRlibre ────────────────────────────────────────────────
try:
    import crlibre  # type: ignore
    CRLIBRE_DISPONIBLE = True
    logger.info("CRlibre importado correctamente")
except ImportError:
    CRLIBRE_DISPONIBLE = False
    logger.warning(
        "CRlibre no instalado. Modo mock activo. "
        "Instala con: pip install crlibre"
    )


# ── Tablas de codigos Hacienda ────────────────────────────────────────────────

# Tasa IVA -> CodigoTarifa Hacienda
# Tasa de IVA aplicada a todos los productos de Innovaciones JOVI.
# Decisión de negocio: los precios en Loyverse incluyen el 13% (IVA-inclusivo).
_IVA_RATE: float = 13.0

_TARIFA_MAP: dict[float, str] = {
    0.0:  "01",   # Exento
    1.0:  "02",   # Reducida 1%
    2.0:  "03",   # Reducida 2%
    4.0:  "04",   # Reducida 4%
    8.0:  "07",   # Reducida 8%
    13.0: "08",   # General 13%
}

# Loyverse payment type -> codigo Hacienda
# 01=Efectivo, 02=Tarjeta, 03=Cheque, 04=Transferencia, 99=Otros
_MEDIO_PAGO_MAP: dict[str, str] = {
    "CASH":              "01",
    "CARD":              "02",
    "NONINTEGRATEDCARD": "02",   # Tarjeta no integrada (datafono externo)
    "IZETTLECARD":       "02",
    "CREDITCARD":        "02",
    "CHECK":             "03",
    "TRANSFER":          "04",
    "GIFT_CARD":         "99",
    "OTHER":             "99",
}

# Métodos de pago personalizados de Loyverse llegan como type "OTHER" con un
# nombre propio. Se clasifican por palabra clave en el nombre.
# Códigos Hacienda: 01=Efectivo 02=Tarjeta 03=Cheque 04=Transferencia
#                   06=SINPE Móvil 99=Otros
_NOMBRE_PAGO_MAP: dict[str, str] = {
    "sinpe":         "06",
    "transferencia": "04",
    "transfer":      "04",
    "deposito":      "04",
    "depósito":      "04",
    "efectivo":      "01",
    "cash":          "01",
    "tarjeta":       "02",
    "credito":       "02",
    "crédito":       "02",
    "debito":        "02",
    "débito":        "02",
    "cheque":        "03",
}


def _codigo_medio_pago(pago: dict) -> str:
    """
    Determina el código Hacienda de un pago de Loyverse.

    Los tipos estándar (CASH, CARD, etc.) se mapean por `type`. Los métodos
    personalizados llegan como `type: OTHER` y se clasifican por su `name`
    (ej. "Transferencia" -> 04, "SINPE Móvil" -> 06).
    """
    tipo = (pago.get("type") or "OTHER").upper()
    if tipo in _MEDIO_PAGO_MAP and tipo != "OTHER":
        return _MEDIO_PAGO_MAP[tipo]

    nombre = (pago.get("name") or "").lower()
    for clave, codigo in _NOMBRE_PAGO_MAP.items():
        if clave in nombre:
            return codigo
    return "99"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tipo_cedula(cedula_num: str) -> str:
    """Determina el tipo de cédula por longitud."""
    n = len(cedula_num)
    if n == 9:
        return "01"   # Física
    if n == 10:
        return "02"   # Jurídica
    if n in (11, 12):
        return "03"   # DIMEX
    return "01"       # Fallback: física


def _generar_clave_50(consecutivo: str, fecha: datetime) -> str:
    """
    Clave numérica de 50 dígitos según Hacienda CR (Resolución DGT-R-48-2016).

    Posiciones:
      1-3   País (506)
      4-5   Día (DD)
      6-7   Mes (MM)
      8-9   Año (AA, 2 dígitos)
      10-21 Cédula emisor (12 dígitos, relleno izquierdo con 0)
      22-41 Consecutivo (20 dígitos completos)
      42    Situación (1=Normal, 2=Contingencia, 3=Sin internet)
      43-50 Código de seguridad (8 dígitos aleatorios)

    Total: 3+2+2+2+12+20+1+8 = 50 dígitos
    """
    cedula = settings.EMISOR_CEDULA.replace("-", "").replace(" ", "").zfill(12)
    consec = consecutivo.zfill(20)
    seguridad = str(random.randint(10_000_000, 99_999_999))

    clave = (
        "506"
        + fecha.strftime("%d")
        + fecha.strftime("%m")
        + fecha.strftime("%y")
        + cedula
        + consec
        + "1"
        + seguridad
    )

    if len(clave) != 50:
        raise ValueError(f"Clave invalida ({len(clave)} digitos): {clave}")
    return clave


def _limpiar_xml_str(valor: str) -> str:
    """Escapa caracteres especiales XML."""
    return (
        str(valor)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _ts_cr(fecha: datetime) -> str:
    """Timestamp en formato Hacienda: 2026-06-16T10:30:00-06:00"""
    local = fecha.astimezone(TZ_CR)
    return local.strftime("%Y-%m-%dT%H:%M:%S") + settings.UTC_OFFSET


# ── Conversión Loyverse -> Factura ────────────────────────────────────────────

def _convertir_lineas(line_items: list[dict]) -> tuple[list[dict], dict]:
    """
    Convierte los line_items de Loyverse al formato de DetalleServicio.

    Decisión de negocio (Innovaciones JOVI): los precios YA incluyen el IVA 13%.
    Loyverse no tiene el impuesto configurado (reporta total_tax=0), así que el
    13% se desglosa aquí a partir del monto bruto de cada línea:

      bruto_con_iva   = gross_total_money   (precio final, antes de descuento)
      descuento_bruto = total_discount      (descuento con IVA incluido)
      neto            = bruto_con_iva / 1.13
      base_imponible  = (bruto_con_iva - descuento_bruto) / 1.13
      iva             = base_imponible * 0.13

    Todas las líneas se tratan como gravadas al 13% (CodigoTarifa 08).
    Retorna (lineas_hacienda, resumen).
    """
    lineas = []
    resumen = {
        "total_mercancias_gravadas": 0.0,
        "total_mercancias_exentas":  0.0,
        "total_descuentos":          0.0,
        "total_impuesto":            0.0,
        "total_comprobante":         0.0,
    }

    for idx, item in enumerate(line_items, start=1):
        cantidad        = float(item.get("quantity", 1)) or 1.0
        # gross_total_money = precio final con IVA antes de descuento.
        # Si no viene, se usa total_money (post-descuento) como respaldo.
        bruto_con_iva   = float(item.get("gross_total_money",
                                          item.get("total_money", 0)))
        descuento_bruto = float(item.get("total_discount", 0))

        # Desglose IVA-inclusivo 13%
        subtotal_neto   = round(bruto_con_iva / (1 + _IVA_RATE / 100), 5)
        descuento_neto  = round(descuento_bruto / (1 + _IVA_RATE / 100), 5)
        base_imponible  = round(subtotal_neto - descuento_neto, 5)
        iva             = round(base_imponible * _IVA_RATE / 100, 5)
        precio_unitario = round(subtotal_neto / cantidad, 5)
        monto_total_lin = round(base_imponible + iva, 5)

        linea: dict[str, Any] = {
            "NumeroLinea":    idx,
            "Codigo":         _limpiar_xml_str(item.get("sku") or f"ITEM{idx:03d}"),
            "Detalle":        _limpiar_xml_str(item.get("item_name", f"Articulo {idx}")),
            "Unidad":         "Unid",
            "Cantidad":       cantidad,
            "PrecioUnitario": precio_unitario,
            "SubTotal":       subtotal_neto,
            "Descuento":      descuento_neto,
            "SubTotalNeto":   base_imponible,
            "MontoTotalLinea": monto_total_lin,
            "Impuesto": {
                "Codigo":       "01",         # 01 = IVA
                "CodigoTarifa": _TARIFA_MAP.get(_IVA_RATE, "08"),
                "Tarifa":       _IVA_RATE,
                "Monto":        iva,
            },
        }

        lineas.append(linea)

        # Todas las líneas son gravadas al 13%
        resumen["total_mercancias_gravadas"] += base_imponible
        resumen["total_descuentos"]  += descuento_neto
        resumen["total_impuesto"]    += iva
        resumen["total_comprobante"] += monto_total_lin

    # Redondear totales del resumen
    for k in resumen:
        resumen[k] = round(resumen[k], 5)

    return lineas, resumen


def venta_a_factura(recibo: dict, consecutivo: str) -> dict[str, Any]:
    """
    Convierte un recibo enriquecido de Loyverse al dict de datos para el XML.

    El parámetro `consecutivo` ya viene generado y reservado desde la BD
    para garantizar unicidad.
    """
    fecha   = datetime.now(TZ_CR)
    clave   = _generar_clave_50(consecutivo, fecha)
    cliente = recibo.get("customer_data") or {}
    store   = recibo.get("store_data") or {}

    # ── Receptor ──────────────────────────────────────────────────────────────
    cedula_raw = "".join(
        c for c in (cliente.get(settings.LOYVERSE_CEDULA_FIELD) or "") if c.isdigit()
    )
    tipo_cedula_receptor = _tipo_cedula(cedula_raw) if cedula_raw else None

    receptor: dict[str, Any] = {"Nombre": _limpiar_xml_str(cliente.get("name", "Consumidor Final"))}
    if cedula_raw:
        receptor["Identificacion"] = {
            "Tipo":   tipo_cedula_receptor,
            "Numero": cedula_raw,
        }
    if cliente.get("email"):
        receptor["CorreoElectronico"] = cliente["email"]

    # ── Medio de pago ─────────────────────────────────────────────────────────
    medios_pago: list[str] = []
    for p in recibo.get("payments", []):
        codigo = _codigo_medio_pago(p)
        if codigo not in medios_pago:
            medios_pago.append(codigo)
    if not medios_pago:
        medios_pago = ["01"]

    # ── Líneas y resumen ──────────────────────────────────────────────────────
    lineas, resumen = _convertir_lineas(recibo.get("line_items", []))

    total_venta_neta = round(
        resumen["total_mercancias_gravadas"] + resumen["total_mercancias_exentas"],
        5,
    )

    return {
        "Clave":             clave,
        "NumeroConsecutivo": consecutivo,
        "FechaEmision":      _ts_cr(fecha),
        "Emisor": {
            "Nombre": _limpiar_xml_str(settings.EMISOR_NOMBRE),
            "Identificacion": {
                "Tipo":   settings.EMISOR_TIPO_CEDULA,
                "Numero": settings.EMISOR_CEDULA.replace("-", ""),
            },
            "ActividadEconomica": settings.EMISOR_ACTIVIDAD,
            "Ubicacion": {
                "Provincia":   settings.EMISOR_PROVINCIA,
                "Canton":      settings.EMISOR_CANTON,
                "Distrito":    settings.EMISOR_DISTRITO,
                "OtrasSenas":  _limpiar_xml_str(settings.EMISOR_OTRAS_SENAS),
            },
            "Telefono": {
                "CodigoPais":    "506",
                "NumTelefono":   settings.EMISOR_TELEFONO,
            },
            "CorreoElectronico": settings.EMISOR_EMAIL,
        },
        "Receptor":          receptor,
        "CondicionVenta":    "01",   # 01=Contado
        "MedioPago":         medios_pago,
        "DetalleServicio":   {"LineaDetalle": lineas},
        "ResumenFactura": {
            "CodigoTipoMoneda": {
                "CodigoMoneda": settings.MONEDA,
                "TipoCambio":   1.0,
            },
            "TotalServGravados":       0.0,
            "TotalServExentos":        0.0,
            "TotalMercanciasGravadas": resumen["total_mercancias_gravadas"],
            "TotalMercanciasExentas":  resumen["total_mercancias_exentas"],
            "TotalGravado":            resumen["total_mercancias_gravadas"],
            "TotalExento":             resumen["total_mercancias_exentas"],
            "TotalVenta":              round(total_venta_neta + resumen["total_descuentos"], 5),
            "TotalDescuentos":         resumen["total_descuentos"],
            "TotalVentaNeta":          total_venta_neta,
            "TotalImpuesto":           resumen["total_impuesto"],
            "TotalComprobante":        resumen["total_comprobante"],
        },
        # Metadatos auxiliares (no van al XML, los usa main.py)
        "_receptor_cedula":  cedula_raw,
        "_receptor_nombre":  cliente.get("name", ""),
        "_receptor_email":   cliente.get("email", ""),
        "_store":            store,
    }


# ── Generación de XML v4.3 ────────────────────────────────────────────────────

def _xml_lineas(lineas: list[dict]) -> str:
    partes = []
    for ln in lineas:
        imp = ln.get("Impuesto")
        impuesto_xml = ""
        if imp:
            impuesto_xml = f"""
            <Impuesto>
                <Codigo>{imp['Codigo']}</Codigo>
                <CodigoTarifa>{imp['CodigoTarifa']}</CodigoTarifa>
                <Tarifa>{imp['Tarifa']}</Tarifa>
                <Monto>{imp['Monto']:.5f}</Monto>
            </Impuesto>"""

        descuento_xml = ""
        if ln.get("Descuento", 0) > 0:
            descuento_xml = f"""
            <MontoDescuento>{ln['Descuento']:.5f}</MontoDescuento>
            <NaturalezaDescuento>Descuento aplicado en venta</NaturalezaDescuento>"""

        partes.append(f"""
        <LineaDetalle>
            <NumeroLinea>{ln['NumeroLinea']}</NumeroLinea>
            <Codigo tipo="04">{ln['Codigo']}</Codigo>
            <Detalle>{ln['Detalle']}</Detalle>
            <Unidad>{ln['Unidad']}</Unidad>
            <Cantidad>{ln['Cantidad']}</Cantidad>
            <PrecioUnitario>{ln['PrecioUnitario']:.5f}</PrecioUnitario>
            <SubTotal>{ln['SubTotal']:.5f}</SubTotal>{descuento_xml}{impuesto_xml}
            <MontoTotalLinea>{ln['MontoTotalLinea']:.5f}</MontoTotalLinea>
        </LineaDetalle>""")

    return "".join(partes)


def _xml_receptor(receptor: dict) -> str:
    partes = [f"<Nombre>{receptor['Nombre']}</Nombre>"]
    if "Identificacion" in receptor:
        i = receptor["Identificacion"]
        partes.append(
            f"<Identificacion>"
            f"<Tipo>{i['Tipo']}</Tipo>"
            f"<Numero>{i['Numero']}</Numero>"
            f"</Identificacion>"
        )
    if receptor.get("CorreoElectronico"):
        partes.append(f"<CorreoElectronico>{receptor['CorreoElectronico']}</CorreoElectronico>")
    return "".join(partes)


def generar_xml(datos: dict) -> str:
    """
    Genera el XML de Factura/Tiquete Electrónico según el esquema v4.3 de Hacienda.
    En producción con CRlibre, esta función puede ser reemplazada por la del SDK.
    """
    em  = datos["Emisor"]
    res = datos["ResumenFactura"]
    mon = res["CodigoTipoMoneda"]
    ub  = em["Ubicacion"]
    tel = em["Telefono"]

    medios_pago_xml = "".join(
        f"<MedioPago>{m}</MedioPago>" for m in datos["MedioPago"]
    )

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<FacturaElectronica
    xmlns="https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.3/facturaElectronica"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.3/facturaElectronica">
    <Clave>{datos['Clave']}</Clave>
    <CodigoActividad>{em['ActividadEconomica']}</CodigoActividad>
    <NumeroConsecutivo>{datos['NumeroConsecutivo']}</NumeroConsecutivo>
    <FechaEmision>{datos['FechaEmision']}</FechaEmision>
    <Emisor>
        <Nombre>{em['Nombre']}</Nombre>
        <Identificacion>
            <Tipo>{em['Identificacion']['Tipo']}</Tipo>
            <Numero>{em['Identificacion']['Numero']}</Numero>
        </Identificacion>
        <NombreComercial>{em['Nombre']}</NombreComercial>
        <Ubicacion>
            <Provincia>{ub['Provincia']}</Provincia>
            <Canton>{ub['Canton']}</Canton>
            <Distrito>{ub['Distrito']}</Distrito>
            <OtrasSenas>{ub['OtrasSenas']}</OtrasSenas>
        </Ubicacion>
        <Telefono>
            <CodigoPais>{tel['CodigoPais']}</CodigoPais>
            <NumTelefono>{tel['NumTelefono']}</NumTelefono>
        </Telefono>
        <CorreoElectronico>{em['CorreoElectronico']}</CorreoElectronico>
    </Emisor>
    <Receptor>
        {_xml_receptor(datos['Receptor'])}
    </Receptor>
    <CondicionVenta>{datos['CondicionVenta']}</CondicionVenta>
    {medios_pago_xml}
    <DetalleServicio>
        {_xml_lineas(datos['DetalleServicio']['LineaDetalle'])}
    </DetalleServicio>
    <ResumenFactura>
        <CodigoTipoMoneda>
            <CodigoMoneda>{mon['CodigoMoneda']}</CodigoMoneda>
            <TipoCambio>{mon['TipoCambio']}</TipoCambio>
        </CodigoTipoMoneda>
        <TotalServGravados>{res['TotalServGravados']:.5f}</TotalServGravados>
        <TotalServExentos>{res['TotalServExentos']:.5f}</TotalServExentos>
        <TotalMercanciasGravadas>{res['TotalMercanciasGravadas']:.5f}</TotalMercanciasGravadas>
        <TotalMercanciasExentas>{res['TotalMercanciasExentas']:.5f}</TotalMercanciasExentas>
        <TotalGravado>{res['TotalGravado']:.5f}</TotalGravado>
        <TotalExento>{res['TotalExento']:.5f}</TotalExento>
        <TotalVenta>{res['TotalVenta']:.5f}</TotalVenta>
        <TotalDescuentos>{res['TotalDescuentos']:.5f}</TotalDescuentos>
        <TotalVentaNeta>{res['TotalVentaNeta']:.5f}</TotalVentaNeta>
        <TotalImpuesto>{res['TotalImpuesto']:.5f}</TotalImpuesto>
        <TotalComprobante>{res['TotalComprobante']:.5f}</TotalComprobante>
    </ResumenFactura>
</FacturaElectronica>"""
    return xml


def guardar_xml(clave: str, xml_content: str) -> str:
    """Persiste el XML en disco. Retorna la ruta del archivo."""
    output_dir = Path(settings.XML_OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    fecha_str = datetime.now(TZ_CR).strftime("%Y%m%d")
    path = output_dir / f"{fecha_str}_{clave}.xml"
    path.write_text(xml_content, encoding="utf-8")
    logger.debug("XML guardado: %s", path)
    return str(path)


# ── INTEGRACION CRLIBRE REAL ──────────────────────────────────────────────────
#
# Esta sección se activa cuando:
#   1. USE_MOCK=false en el .env
#   2. Las credenciales de Hacienda están configuradas
#   3. CRlibre está instalado (pip install crlibre)
#
# Para completar la integración, rellena _enviar_crlibre() con los
# métodos exactos de la versión de CRlibre que instales.
# Consulta su documentación: https://github.com/CRLibre/CRLibre

def _enviar_crlibre(xml_content: str, datos: dict) -> dict[str, Any]:
    """
    Firma y envía el XML a Hacienda usando CRlibre.

    Pasos que CRlibre realiza internamente:
      1. Carga el certificado .p12 con el PIN
      2. Firma el XML con el algoritmo exigido por Hacienda (XMLDSig)
      3. Codifica el XML firmado en Base64
      4. Obtiene o renueva el token OAuth2 del ATV de Hacienda
      5. Envía el comprobante al endpoint correspondiente (staging o producción)
      6. Retorna el estado: aceptado | procesando | rechazado

    Adapta los nombres de clases/métodos según la versión instalada.
    """
    # ── Ejemplo de integración (ajustar según la API de CRlibre instalada) ──
    #
    # from crlibre.firma import FirmaDigital
    # from crlibre.hacienda import ClienteHacienda
    #
    # firma = FirmaDigital(
    #     cert_path = settings.CERT_PATH,
    #     pin       = settings.HACIENDA_PIN,
    # )
    # xml_firmado = firma.firmar(xml_content)
    #
    # hacienda = ClienteHacienda(
    #     usuario   = settings.HACIENDA_USER,
    #     password  = settings.HACIENDA_PASSWORD,
    #     ambiente  = settings.AMBIENTE,   # "sandbox" | "produccion"
    # )
    # respuesta = hacienda.enviar_comprobante(
    #     xml_firmado  = xml_firmado,
    #     clave        = datos["Clave"],
    #     consecutivo  = datos["NumeroConsecutivo"],
    # )
    # return respuesta   # dict con "ind-estado", "mensaje", etc.
    # ────────────────────────────────────────────────────────────────────────

    raise NotImplementedError(
        "Integración CRlibre pendiente. "
        "Completa _enviar_crlibre() en facturacion/crlibre_adapter.py "
        "con las credenciales de Hacienda y el SDK de CRlibre instalado."
    )


def _enviar_mock(datos: dict) -> dict[str, Any]:
    """Simula la respuesta de Hacienda para desarrollo y pruebas."""
    aceptado = random.random() > 0.05   # 95% de aceptación simulada
    return {
        "ind-estado": "aceptado" if aceptado else "rechazado",
        "clave": datos["Clave"],
        "fecha": datetime.now(TZ_CR).isoformat(),
        "xml-respuesta": f"<RespuestaXML><Estado>{'aceptado' if aceptado else 'rechazado'}</Estado></RespuestaXML>",
        "detalle-mensaje": (
            "Comprobante recibido y procesado correctamente."
            if aceptado
            else "[MOCK] Error en estructura del comprobante."
        ),
    }


# ── Punto de entrada público ──────────────────────────────────────────────────

def procesar_factura(
    recibo: dict,
    tipo_comprobante: str,
    sucursal: str,
    terminal: str,
) -> dict[str, Any]:
    """
    Orquesta el procesamiento completo de una factura:
      1. Reserva el consecutivo (atómico, irrepetible)
      2. Convierte el recibo al formato de Hacienda
      3. Genera el XML v4.3
      4. Guarda el XML en disco
      5. Envía a Hacienda (real o mock)
      6. Retorna resultado con clave, consecutivo, xml_path y respuesta

    Raises:
      ValueError: si los datos de entrada son inválidos (no reintenta)
      RuntimeError: si falla la conexión/envío (puede reintentar)
    """
    # 1. Consecutivo — se reserva ANTES de generar el XML para no desperdiciar
    consecutivo = obtener_siguiente_consecutivo(tipo_comprobante, sucursal, terminal)

    # 2. Convertir venta -> datos de factura
    datos = venta_a_factura(recibo, consecutivo)

    # 3. Generar XML
    xml_content = generar_xml(datos)

    # 4. Guardar XML
    xml_path = guardar_xml(datos["Clave"], xml_content)

    # 5. Enviar a Hacienda
    usar_mock = settings.USE_MOCK_HACIENDA or not CRLIBRE_DISPONIBLE
    if not usar_mock and not settings.hacienda_lista():
        logger.warning(
            "Credenciales de Hacienda incompletas. Usando mock aunque USE_MOCK=false."
        )
        usar_mock = True

    if usar_mock:
        respuesta = _enviar_mock(datos)
    else:
        respuesta = _enviar_crlibre(xml_content, datos)

    aceptada = respuesta.get("ind-estado") == "aceptado"

    return {
        "clave":          datos["Clave"],
        "consecutivo":    consecutivo,
        "xml_path":       xml_path,
        "respuesta":      respuesta,
        "aceptada":       aceptada,
        "receptor_cedula": datos.get("_receptor_cedula", ""),
        "receptor_nombre": datos.get("_receptor_nombre", ""),
        "receptor_email":  datos.get("_receptor_email", ""),
        "total":           datos["ResumenFactura"]["TotalComprobante"],
        "total_impuesto":  datos["ResumenFactura"]["TotalImpuesto"],
    }
