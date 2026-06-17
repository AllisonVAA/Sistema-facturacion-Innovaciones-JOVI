# Facturación Electrónica CR — Loyverse + CRlibre

Sistema Python que automatiza la facturación electrónica en Costa Rica.
Obtiene las ventas del día desde **Loyverse**, las convierte al formato
requerido por **Hacienda** y las envía usando **CRlibre**.

---

## Estructura del proyecto

```
facturacion-cr/
├── loyverse/
│   └── client.py           # Cliente API Loyverse (real + mock)
├── facturacion/
│   └── crlibre_adapter.py  # Adaptador CRlibre: XML, firma, envío
├── database/
│   └── storage.py          # Control de facturas en SQLite
├── xml_output/             # XMLs generados (creado automáticamente)
├── logs/                   # Archivos de log diarios
├── main.py                 # Orquestador principal
├── config.py               # Variables de entorno
├── requirements.txt
└── .env.example
```

---

## Requisitos

- Python 3.10+
- pip

---

## Instalación

```bash
# 1. Clonar o descargar el proyecto
cd facturacion-cr

# 2. Crear entorno virtual
python -m venv venv
source venv/bin/activate        # Linux/Mac
venv\Scripts\activate           # Windows

# 3. Instalar dependencias
pip install -r requirements.txt

# 4. Configurar variables de entorno
cp .env.example .env
# Editar .env con tus credenciales reales
```

---

## Configuración

Copia `.env.example` a `.env` y llena los valores:

| Variable | Descripción |
|---|---|
| `LOYVERSE_TOKEN` | Token de API de tu cuenta Loyverse |
| `HACIENDA_USER` | Usuario del ATV de Hacienda (cédula@hacienda.go.cr) |
| `HACIENDA_PASSWORD` | Contraseña del ATV |
| `HACIENDA_PIN` | PIN del certificado p12 |
| `CERT_PATH` | Ruta al archivo `.p12` de firma digital |
| `EMISOR_CEDULA` | Cédula jurídica o física del emisor |
| `EMISOR_NOMBRE` | Nombre legal de la empresa |
| `EMISOR_ACTIVIDAD` | Código de actividad económica (CABYS) |
| `AMBIENTE` | `sandbox` (pruebas) o `produccion` |
| `USE_MOCK` | `true` para pruebas sin credenciales reales |
| `TIPO_COMPROBANTE` | `01`=Factura Electrónica, `04`=Tiquete |

---

## Ejecución

### Modo mock (sin credenciales — para desarrollo)

```bash
# Asegúrate de que USE_MOCK=true en el .env
python main.py
```

Salida esperada:

```
2026-04-27 17:00:00 [INFO] main: ============================================================
2026-04-27 17:00:00 [INFO] main: Iniciando facturación electrónica — 2026-04-27T17:00:00
2026-04-27 17:00:00 [INFO] main: Ambiente: sandbox | Mock: True
2026-04-27 17:00:00 [WARNING] loyverse.client: [MOCK] Generando ventas de ejemplo para hoy
2026-04-27 17:00:00 [INFO] main: Total de ventas obtenidas: 3
2026-04-27 17:00:00 [INFO] main: 3 venta(s) nuevas, 0 ya facturadas anteriormente
2026-04-27 17:00:00 [INFO] main: Procesando venta R-001 (intento 1/3)
2026-04-27 17:00:00 [INFO] main: Venta R-001 → ACEPTADA (clave: 50627042631011234560...)
...
2026-04-27 17:00:01 [INFO] main: RESUMEN DEL DÍA:
2026-04-27 17:00:01 [INFO] main:   Ventas procesadas hoy : 3 exitosas / 0 fallidas
```

### Modo producción

```bash
# 1. Configura las credenciales reales en .env
# 2. Cambia USE_MOCK=false y AMBIENTE=produccion
# 3. Instala CRlibre cuando esté disponible:
pip install crlibre

# 4. Ejecutar
python main.py
```

---

## Automatización con cron

El script está listo para ejecutarse con cron. **No implementa cron internamente.**

Agrega esta línea a tu crontab (`crontab -e`) para ejecutarlo a las 5:00 PM:

```cron
0 17 * * * /ruta/al/venv/bin/python /ruta/al/proyecto/main.py >> /ruta/logs/cron.log 2>&1
```

En Windows puedes usar el Programador de Tareas apuntando a:

```
C:\ruta\al\venv\Scripts\python.exe C:\ruta\al\proyecto\main.py
```

---

## Base de datos

Se crea automáticamente en `database/facturas.db`. Para consultar:

```bash
sqlite3 database/facturas.db "SELECT loyverse_receipt_id, estado, intentos, clave_hacienda FROM facturas;"
```

### Estados posibles

| Estado | Descripción |
|---|---|
| `pendiente` | Registro creado, aún no procesado |
| `enviada` | Enviada a Hacienda, esperando respuesta |
| `aceptada` | Hacienda aceptó el comprobante |
| `rechazada` | Hacienda rechazó el comprobante (no se reintenta) |
| `error` | Error técnico (se reintenta hasta MAX_RETRIES) |

---

## Integración con CRlibre

El archivo `facturacion/crlibre_adapter.py` integra CRlibre sin modificar
su código base. La función `enviar_a_hacienda_real()` contiene el esqueleto
con comentarios para conectar la librería cuando esté instalada.

```python
# En crlibre_adapter.py, función enviar_a_hacienda_real():
from crlibre.hacienda import Hacienda
from crlibre.firma import firmar_xml

xml_firmado = firmar_xml(xml, cert_path=settings.CERT_PATH, pin=settings.HACIENDA_PIN)
hacienda = Hacienda(usuario=settings.HACIENDA_USER, password=settings.HACIENDA_PASSWORD)
respuesta = hacienda.enviar(xml_firmado)
```

Ajusta los parámetros según la versión de CRlibre que instales.

---

## Pasar a producción — checklist

- [ ] Obtener certificado de firma digital en el Banco Central de Costa Rica
- [ ] Registrarse en el ATV de Hacienda: https://www.hacienda.go.cr/ATV/
- [ ] Configurar todas las variables en `.env` con valores reales
- [ ] Cambiar `USE_MOCK=false` y `AMBIENTE=produccion`
- [ ] Instalar y configurar CRlibre con credenciales reales
- [ ] Probar primero en el ambiente de **sandbox** de Hacienda
- [ ] Validar la clave numérica y el número consecutivo con Hacienda
- [ ] Configurar el cron en el servidor de producción

---

## Reintentos automáticos

Las facturas que fallen por error técnico se reintentan automáticamente
en la misma ejecución hasta `MAX_RETRIES` veces con espera exponencial.
Las facturas **rechazadas** por Hacienda no se reintentan (el rechazo es definitivo).
