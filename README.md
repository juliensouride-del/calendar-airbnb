# Airbnb (Yahoo Mail) → Google Calendar

Script en Python que lee los correos de Airbnb **directamente de Yahoo Mail** y mantiene el calendario de Google del anfitrión con las entradas y salidas de cada reserva: alojamiento, fechas, número de personas, horas de llegada y salida, sofá cama y cuna. Si un huésped pide algo especial, envía un aviso a su Yahoo.

Se ejecuta solo, cada 30 minutos, en **GitHub Actions**. Coste 0 € y sin servidores.

```
Yahoo Mail ──IMAP/TLS, solo lectura──▶ sincronizar.py (GitHub Actions, cada 30 min)
                                        ├─ parser.py       código, huésped, fechas, horas, personas
                                        ├─ gemini.py       mensajes del huésped: hora de llegada, cuna, sofá…
                                        ├─ calendario.py   crea, actualiza o cancela los eventos (API de Google)
                                        ├─ yahoo.py        avisos por SMTP de Yahoo
                                        └─ estado.json     correos ya procesados (rama `estado`)
```

## Cómo funciona

- **No toca el buzón.** Abre las carpetas en solo lectura y descarga con `BODY.PEEK[]`, así que nada se marca como leído. El anfitrión sigue viendo sus correos como siempre, y el script no depende de si los ha abierto o no.
- **No duplica nada.** Cada correo se identifica por `carpeta:UIDVALIDITY:UID` (único en IMAP) y queda registrado en `estado.json`. Cada reserva se encuentra en el calendario por su código, guardado como propiedad privada del evento.
- **Una reserva = un evento**, de la hora de entrada a la de salida: `🏠 Tirso · 3 pers · llega 18:00 · 🍼 cuna`. Horario estándar **entrada 15:00 / salida 11:00**: si el huésped pide entrar antes o salir más tarde, el calendario mantiene el estándar (salvo acuerdo expreso); si llega más tarde o sale antes, se usa su hora. Si se cancela: `❌ CANCELADA`, en gris. Las reservas del formato anterior (check-in y check-out por separado) se unifican al llegar el siguiente correo de la reserva.
- **Limpieza.** Si el huésped anterior del mismo piso sale ese día (o el anterior), el evento del que entra empieza por `[LIMPIEZA OK]` o, en naranja, `[ALERTA LIMPIEZA / AJUSTE HORARIO]`: se comprueba que caben 2 h de limpieza entre la salida y la entrada, dentro del horario del equipo (08:00–15:00, sección `[limpieza]` de `config.toml`). La descripción indica la ventana y la hora de entrega de llaves (la de entrada). Si cambia la hora de salida del anterior, se recalcula; además, cada ejecución revisa las limpiezas de los próximos 30 días, así que también se corrigen tras cambios de fechas o cancelaciones.
- **Mensajes de los huéspedes.** Gemini (gratis) extrae la hora de llegada y salida, la cuna, el sofá cama y otras peticiones. Si Gemini falla, se usan palabras clave.
- **Errores.** Un correo que falla se reintenta 3 veces y después se avisa. Si falla todo el script (Yahoo o Google caídos, contraseña revocada), la ejecución NO se pone en rojo (para que GitHub no envíe emails al propietario del repositorio): se marca con una anotación de error y se envía una notificación push solo al desarrollador por [ntfy.sh](https://ntfy.sh), al tema del secreto `ALERTA_NTFY_TOPIC`. La siguiente ejecución recupera lo pendiente (revisa los últimos 3 días).

| Pieza | Plan gratuito | Uso |
| --- | --- | --- |
| GitHub Actions (repositorio **privado**) | 2 000 min/mes | ≈ 1 450 min/mes (48 ejecuciones al día de < 1 min) |
| Google Calendar API | 1 000 000 peticiones/día | unas decenas al día |
| Gemini API | plan gratuito de AI Studio | un análisis por mensaje de huésped |

> El cron de GitHub no es exacto: en horas de mucha carga una ejecución puede retrasarse unos minutos. Para un calendario de limpiezas y llegadas es suficiente.

## Instalación (una vez, unos 20 minutos)

### 1. Contraseña de aplicación de Yahoo

En la cuenta de Yahoo del anfitrión: **Información de la cuenta → Seguridad de la cuenta → Generar contraseña de aplicación** (nombre: «Airbnb Calendar»). Copia las 16 letras. Se puede revocar cuando se quiera sin cambiar la contraseña principal.

### 2. Cuenta de servicio de Google (gratis)

1. https://console.cloud.google.com → crea un proyecto («airbnb-calendar»).
2. **APIs y servicios → Biblioteca → Google Calendar API → Habilitar**.
3. **IAM y administración → Cuentas de servicio → Crear** (sin roles). Abre la cuenta → **Claves → Agregar clave → JSON**. Se descarga un archivo `.json`: es el secreto `GOOGLE_SERVICE_ACCOUNT_JSON`. No lo subas al repositorio.
4. En Google Calendar, abre la configuración del calendario del anfitrión → **Compartir con determinadas personas** → añade el email de la cuenta de servicio (`…@….iam.gserviceaccount.com`) con permiso **«Hacer cambios en eventos»**.
5. En esa misma página, copia el **ID del calendario** (sección «Integrar el calendario»).

### 3. Clave de Gemini

https://aistudio.google.com → **Get API key**. Es opcional: sin ella se usan palabras clave y no se detectan horas.

### 4. Configuración

En `config.toml`:
- `[calendario] id` → el ID del paso 2.5.
- `[[alojamientos]]` → nombre, alias e `id_anuncio` de cada piso (el número de `airbnb.com/rooms/<nº>`).

### 5. Repositorio de GitHub (privado)

```powershell
git init; git add .; git commit -m "Airbnb → Calendar"
gh repo create airbnb-calendar --private --source . --push
```

En **Settings → Secrets and variables → Actions → New repository secret**:

| Secreto | Valor |
| --- | --- |
| `YAHOO_USUARIO` | correo de Yahoo del anfitrión |
| `YAHOO_CLAVE_APP` | contraseña de aplicación (paso 1) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | contenido completo del `.json` (paso 2.3) |
| `GEMINI_API_KEY` | clave de Gemini (paso 3) |
| `ALERTA_NTFY_TOPIC` | (opcional) tema de ntfy.sh para los avisos de fallo al desarrollador: un nombre largo y aleatorio. Suscríbete a él en la app ntfy |
| `CALENDAR_ID` | ID del calendario (Configuración del calendario → Integrar el calendario). Alternativa: ponerlo en `[calendario] id` de `config.toml` |

### 6. Puesta en marcha

En **Actions → Airbnb → Calendar → Run workflow**:
1. `comprobar`: verifica Yahoo, Google Calendar y Gemini sin escribir nada.
2. `diagnosticar`: muestra lo que se extrae de los correos de Airbnb de los últimos 7 días, sin tocar el calendario.
3. `sincronizar`: la primera ejecución anota la fecha de inicio. Desde ese momento se procesa cada correo nuevo. Después sigue sola cada 30 minutos.

Para cargar también reservas recientes, pon `procesar_desde = "2026-10-01"` en `config.toml` **antes** de la primera ejecución (con un máximo de 3 días hacia atrás, por `ventana_dias`).

## Desarrollo local

```powershell
py -m venv .venv; .venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q          # 36 pruebas, sin red
```

Las pruebas usan los correos reales de `ejemplos/` e incluyen:
- un servidor IMAP falso para probar el cliente de Yahoo;
- una imitación de la API REST de Google Calendar;
- la simulación completa de varias ejecuciones seguidas, con el registro persistente.

Para ejecutarlo contra los servicios reales, define las mismas variables de entorno que los secretos y lanza `python sincronizar.py --comprobar`.

## Mantenimiento

- **Pasar las reservas antiguas al formato nuevo** (un evento por reserva, con limpieza): en Actions, modo `migrar-simulacion` y luego `migrar`. En local: `python sincronizar.py --migrar [--dry-run]`.
- **Resumen de cada ejecución** (correos, eventos, errores y alertas de limpieza): en la página de la ejecución en Actions.
- **Avisos técnicos** (fallo de la ejecución, Gemini caído) llegan solo al desarrollador por ntfy (`ALERTA_NTFY_TOPIC`); el modo `comprobar` avisa si falta ese secreto. Al anfitrión solo le llegan las peticiones de huéspedes y los correos que hay que revisar a mano.
- **Borrar todos los eventos del script** (solo los que llevan sus marcas privadas; los eventos personales no se tocan): en Actions, modo `purgar-simulacion` para ver la lista y `purgar` (hay que escribir BORRAR en el campo `confirmar`) para borrar. En local: `python sincronizar.py --purgar --dry-run` y `python sincronizar.py --purgar` (pide escribir BORRAR). Para volver a cargarlos: modo `sincronizar` con `desde`.

- **Contraseña de Yahoo revocada o cambiada:** la ejecución falla y llega el aviso de ntfy. Genera otra contraseña de aplicación y actualiza el secreto `YAHOO_CLAVE_APP`.
- **Correos en Spam o archivados a mano:** añade `"Bulk"` o `"Archive"` a `[yahoo] carpetas`.
- **Airbnb cambia una plantilla:** llega el aviso «no se pudo procesar un correo». Los patrones están en `airbnb_calendar/parser.py` (`PATRONES`). Añade el correo nuevo a `ejemplos/`, escribe su prueba y ajusta el patrón.
- **Registro:** la rama `estado` guarda `estado.json`, con un commit solo cuando cambia. Se puede consultar o borrar. Si se borra, la siguiente ejecución vuelve a empezar desde ese momento.
- **Plantillas en inglés:** el parser espera los correos de Airbnb en inglés. No cambies el idioma de la cuenta de Airbnb.
