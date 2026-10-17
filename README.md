# Airbnb → Google Calendar

Lee los correos de Airbnb **directamente de Yahoo Mail** y mantiene un calendario de Google con cada reserva: alojamiento, fechas, personas, horas de llegada y salida, sofá cama, cuna, peticiones del huésped, locker y limpieza entre reservas. Cuando un huésped pide algo (entrar antes, salir más tarde, cuna, sofá cama…), el anfitrión recibe un aviso por correo.

Se ejecuta solo en **GitHub Actions** cada 2 horas de día (8:00–22:00 en Madrid), sin servidores y con coste 0 €.

> ⚠️ **El repositorio es público.** No contiene secretos (están en los Secrets de GitHub) y los correos de `ejemplos/` están anonimizados, pero `config.toml` incluye los enlaces de los lockers y el historial de git conserva versiones anteriores de los ejemplos. Conviene hacerlo **privado** (Settings → General → Danger Zone → Change visibility): el uso de Actions (~250 min/mes) cabe en los 2 000 min gratuitos de un repositorio privado.

---

## 1. Para el anfitrión: cómo leer un evento

Cada reserva es **un evento** que va de la hora de entrada a la de salida:

```
[LIMPIEZA OK] 🏠 Torrecilla · 3 pers · llega 18:30 · ⏰ pide entrar 12:00 · 🍼 cuna
```

La descripción va de lo más urgente a lo menos:

```
—— Airbnb · automático (escribe tus notas encima de esta línea) ——
⏰ PIDE ENTRAR ANTES (a las 12:00). Si lo aceptas, escribe arriba: #entrada 12:00

🕒 Llegada: 18:30   ·   Salida: 11:00
🛋️ Sofá cama: no
🍼 Cuna: SÍ (lo indica el huésped)
🧹 Limpieza: 05/11 de 11:00 a 15:00 (el huésped anterior sale ese día a las 11:00)

📝 Peticiones:
   • Guardar maletas por la mañana

🔑 Locker: https://access.keynest.com/…

👤 Laura Garcia · Código HMAAAA0002
```

- **Avisos arriba** (solo si los hay): limpieza que no cabe, petición de horario pendiente, mensajes que la IA no pudo analizar.
- **Valores por defecto:** si el huésped no dice nada, llegada 15:00, salida 11:00, sin sofá cama ni cuna.
- **🧹 Limpieza:** si el huésped anterior del mismo piso sale ese día (o el anterior), la ventana para limpiar dentro del horario del equipo (08:00–15:00). Si no caben las 2 h, el título empieza por `[ALERTA LIMPIEZA / AJUSTE HORARIO]` y el evento sale en naranja.
- **🔑 Locker:** el de cada piso. Torrecilla comparte locker con Buenavista: si los dos tienen entrada el mismo día, Torrecilla usa el alternativo.
- **Cancelada:** `❌ CANCELADA`, en gris.
- **Creado desde un mensaje:** si llega un mensaje de una reserva cuya confirmación no se ha visto, se crea el evento con lo que haya y una nota al final; la nota desaparece cuando llega la confirmación.

### Acordar otra hora con el huésped

El horario estándar es **entrada 15:00 / salida 11:00**: si el huésped pide entrar antes o salir más tarde, se avisa, pero el calendario no cambia hasta que el anfitrión lo acuerde. Para fijar la hora acordada:

- **mover el evento** en Calendar, o
- escribir en la descripción, **encima de la línea automática**, `#entrada 12:00` o `#salida 13:30` (también vale `12h` o `13.30`). Para quitar el acuerdo, se borra la línea.

La hora acordada manda sobre cualquier regla (si no deja tiempo de limpieza, el evento sale con alerta).

### Los cambios a mano se respetan

- Lo que se escriba **encima** de la línea `—— Airbnb · automático ——` se conserva siempre.
- Si se cambia a mano el **título, el color, la ubicación o las horas**, el script ya no toca ese campo.
- Si se edita la parte **automática** de la descripción, la descripción pasa a ser del anfitrión y deja de actualizarse.

### Avisos por correo

Al anfitrión solo le llegan las **peticiones de los huéspedes** (entrar antes, salir más tarde, sofá cama, cuna, otras peticiones) y las **modificaciones de reserva**, que se revisan a mano. Nunca se avisa de reservas que ya han terminado. Los errores técnicos van al desarrollador.

---

## 2. Cómo funciona

```
Yahoo Mail ──IMAP, solo lectura──▶ sincronizar.py (GitHub Actions, cada 2 h de día)
                                   └─ airbnb_calendar/
                                      ├─ yahoo.py        lee los correos (IMAP) y envía los avisos (SMTP)
                                      ├─ parser.py       código, huésped, fechas, horas y personas de cada correo
                                      ├─ gemini.py       mensajes del huésped: horas, sofá, cuna, peticiones (IA);
                                      │                  sin IA: mensajes de cortesía y respaldo por palabras clave
                                      ├─ reglas.py       estado de la reserva, fusión de mensajes, horario y limpieza
                                      ├─ formato.py      título y descripción de los eventos
                                      ├─ avisos.py       qué novedades hay que avisar al anfitrión
                                      ├─ calendario.py   Google Calendar: crea, actualiza, respeta cambios a mano
                                      ├─ sincronizador.py  el flujo completo de cada correo
                                      ├─ registro.py     correos ya procesados (estado.json, rama `estado`)
                                      ├─ config.py       config.toml + secretos
                                      └─ ntfy.py         avisos técnicos (ntfy y correo al desarrollador)
```

- **No toca el buzón.** Abre las carpetas en solo lectura (`INBOX` y `Archive`) y descarga con `BODY.PEEK[]`: nada se marca como leído.
- **No duplica nada.** Cada correo se identifica por su UID de IMAP y su `Message-ID` (el mismo aunque se archive) y queda registrado. Cada reserva se encuentra en el calendario por su código, guardado como propiedad privada del evento, junto con su estado completo (JSON).
- **Yahoo por semanas.** Yahoo devuelve como mucho 1 000 resultados por búsqueda y descarta el resto sin avisar: se busca por tramos de una semana.
- **Manda lo último que dice el huésped.** Los correos se procesan en orden: un mensaje nuevo sustituye la hora de llegada o salida y puede pedir, cambiar o retirar la petición de horario. Llegar entre las 00:00 y las 06:00 cuenta como llegar tarde.
- **Gemini, con cabeza.** Los mensajes que son solo cortesía («Thanks!», «Vale, perfecto», «👍») no se envían a la IA: todas sus palabras deben ser de cortesía, sin números ni preguntas. Si Gemini falla en el día a día, se usan palabras clave (detectan sofá, cuna y peticiones de horario, sin horas). En una recarga, si Gemini no responde ni esperando, la recarga se detiene en lugar de guardar un análisis peor.
- **Errores.** Un correo que falla se reintenta en las siguientes ejecuciones y, tras 3 intentos, se avisa al desarrollador. Si el calendario no es accesible (ID o permisos), la ejecución se detiene sin contar fallos. La ejecución nunca se pone en rojo en GitHub (para que GitHub no envíe correos al propietario del repositorio): el aviso llega por correo (`ALERTA_EMAIL`) y por [ntfy.sh](https://ntfy.sh).

| Pieza | Plan gratuito | Uso |
| --- | --- | --- |
| GitHub Actions | 2 000 min/mes en privado (ilimitado en público) | ~8 ejecuciones al día de < 1 min |
| Google Calendar API | 1 000 000 peticiones/día | unas decenas al día |
| Gemini API | cuota diaria gratuita de AI Studio | un análisis por mensaje de huésped (no los de cortesía) |

---

## 3. Instalación (una vez, unos 20 minutos)

1. **Contraseña de aplicación de Yahoo.** En la cuenta del anfitrión: Información de la cuenta → Seguridad → *Generar contraseña de aplicación*. Se puede revocar sin cambiar la contraseña principal.
2. **Cuenta de servicio de Google.**
   1. https://console.cloud.google.com → nuevo proyecto → *APIs y servicios → Biblioteca → Google Calendar API → Habilitar*.
   2. *IAM y administración → Cuentas de servicio → Crear* (sin roles) → *Claves → Agregar clave → JSON*. Ese archivo es el secreto `GOOGLE_SERVICE_ACCOUNT_JSON` (no lo subas al repositorio).
   3. En Google Calendar, configuración del calendario → *Compartir con determinadas personas* → el email de la cuenta de servicio (`…@….iam.gserviceaccount.com`) con permiso **«Hacer cambios en eventos»**.
   4. En la misma página, *Integrar el calendario* → **ID del calendario**.
3. **Clave de Gemini:** https://aistudio.google.com → *Get API key*. Opcional: sin ella se usan palabras clave y no se detectan horas.
4. **`config.toml`:** los `[[alojamientos]]` (nombre exacto del anuncio, `alias`, `id_anuncio`, `capacidad_sin_sofa`, `locker`, `direccion`), el horario estándar y el de limpieza.
5. **Secrets del repositorio** (*Settings → Secrets and variables → Actions*):

| Secreto | Valor |
| --- | --- |
| `YAHOO_USUARIO` | correo de Yahoo del anfitrión |
| `YAHOO_CLAVE_APP` | contraseña de aplicación (paso 1) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | contenido completo del `.json` (paso 2.2) |
| `CALENDAR_ID` | ID del calendario (paso 2.4) |
| `GEMINI_API_KEY` | clave de Gemini (paso 3) |
| `ALERTA_EMAIL` | correo del desarrollador para los avisos técnicos |
| `ALERTA_NTFY_TOPIC` | (opcional) tema de ntfy.sh para notificaciones push de fallos: un nombre largo y aleatorio |
| `CALENDAR_ID_PRUEBAS` | (opcional) ID del calendario de pruebas |
| `GOOGLE_SERVICE_ACCOUNT_JSON_PRUEBAS` | (opcional) `.json` de la cuenta de servicio con acceso al calendario de pruebas |

6. **Puesta en marcha** (*Actions → Airbnb → Calendar → Run workflow*): `comprobar` (verifica Yahoo, Calendar y Gemini sin escribir nada) y después `recargar` con `desde` = la fecha desde la que cargar reservas. A partir de ahí sigue sola.

---

## 4. Operación

Todo se lanza desde *Actions → Airbnb → Calendar → Run workflow*:

| Modo | Qué hace |
| --- | --- |
| `sincronizar` | lo mismo que las ejecuciones automáticas: procesa los correos nuevos |
| `recargar` | con `desde` (y opcionalmente `hasta`): vuelve a procesar todos los correos del tramo en orden, sin avisos al anfitrión, y al final pone al día todas las reservas futuras. Cada reserva se reconstruye desde su confirmación. Hasta ~5,5 h por ejecución |
| `comprobar` | verifica Yahoo, Google Calendar, Gemini y los avisos, sin escribir nada |
| `diagnosticar` | muestra lo que se extrae de los correos de los últimos 7 días, sin tocar el calendario |
| `recalcular-simulacion` / `recalcular` | aplica las reglas actuales (horario, limpieza, formato, lockers) a todas las reservas futuras, sin leer correos |
| `purgar-simulacion` / `purgar` | lista / borra todos los eventos creados por el script (escribiendo `BORRAR` en `confirmar`); los eventos personales no se tocan |

**Entorno de pruebas.** Con `entorno = pruebas` se usa el calendario de pruebas (`CALENDAR_ID_PRUEBAS` y `GOOGLE_SERVICE_ACCOUNT_JSON_PRUEBAS`; si faltan, no se ejecuta y nunca toca el calendario real), el registro va en la rama `estado-pruebas` y los avisos de peticiones van solo a `ALERTA_EMAIL` con `[PRUEBAS]`. Las ejecuciones programadas son siempre de producción.

**Qué hacer si…**

- **Llega «la ejecución ha fallado»:** abre el enlace del aviso. Si es la contraseña de Yahoo, genera otra y actualiza `YAHOO_CLAVE_APP`; si es un 404 de Calendar, revisa `CALENDAR_ID` y que el calendario esté compartido con la cuenta de servicio.
- **Llega «no se pudo procesar un correo»:** probablemente Airbnb ha cambiado una plantilla. Los patrones están en `airbnb_calendar/parser.py` (`PATRONES`): añade el correo (anonimizado) a `ejemplos/`, escribe su prueba y ajusta el patrón.
- **Una recarga se detiene por Gemini:** se ha agotado la cuota diaria gratuita (se renueva hacia las 9:00 de Madrid). Vuelve a lanzarla con `desde` = la fecha que indica el error; es idempotente.
- **Correos de Airbnb en Spam:** añade `"Bulk"` a `[yahoo] carpetas` en `config.toml`.
- **Cambiar el nombre corto de un piso, el horario o un locker:** edita `config.toml`; las reservas futuras se corrigen en la siguiente ejecución (todas, con `recalcular`).
- **Registro:** la rama `estado` guarda `estado.json` (correos procesados de los últimos días). Si se borra, la siguiente ejecución empieza desde ese momento.
- **Plantillas en inglés:** el parser espera los correos de Airbnb en inglés; no cambies el idioma de la cuenta de Airbnb.

---

## 5. Desarrollo

```powershell
py -m venv .venv; .venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q      # 92 pruebas, sin red
.venv\Scripts\ruff check .             # estilo del código
```

Las pruebas usan los correos (anonimizados) de `ejemplos/` e incluyen un servidor IMAP falso, una imitación de la API de Google Calendar y de Gemini, y la simulación completa de varias ejecuciones seguidas con el registro persistente. En cada push, GitHub Actions ejecuta `ruff` y las pruebas (`.github/workflows/pruebas.yml`).

Para ejecutarlo contra los servicios reales, define las mismas variables de entorno que los secretos y lanza `python sincronizar.py --comprobar`.
