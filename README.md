# Airbnb → Google Calendar

Lee los correos de Airbnb **directamente de Yahoo Mail** y mantiene un calendario de Google con cada reserva: alojamiento, fechas, personas, horas de llegada y salida, sofá cama, cuna, peticiones del huésped, locker y limpieza entre reservas. Cuando un huésped pide algo (entrar antes, salir más tarde, cuna, sofá cama…), el anfitrión recibe un aviso por correo.

Se ejecuta solo en **GitHub Actions** (cada 20 minutos de día y cada hora de noche), con coste 0 €. Además prepara
**borradores de respuesta a los huéspedes** que apruebas en Telegram antes de que se envíen (ver la sección 6).

> ⚠️ **Mejor un repositorio privado** (*Settings → General → Danger Zone → Change visibility*, solo lo puede hacer el
> propietario). Mientras sea público: las contraseñas del wifi **no** están en el repositorio (van en el secreto
> `BASE_PRIVADA`) y lo privado del registro va cifrado, pero `config.toml` lleva los enlaces de los lockers y **los
> registros de cada ejecución de Actions son visibles para cualquiera** (nombres de huéspedes y asuntos de los correos).
> En privado, el uso de Actions (~1 750 min/mes) cabe en los 2 000 min gratuitos.
---

## 1. Para el anfitrión: cómo leer los eventos

Cada reserva son **dos eventos cortos**: la entrada, el día que llega el huésped a las 15:00, y la salida, el día que se va a las 11:00. En la vista de mes se ve qué piso entra y cuál sale cada día sin abrir nada.

Los eventos están siempre a la hora estándar, para que el equipo los encuentre siempre en el mismo sitio. La hora que dice el huésped va en el título y en la descripción, y no mueve el evento. Solo lo mueven una hora acordada (`#entrada` o `#salida`, ver abajo) o el anfitrión, arrastrándolo en Calendar.

```
🟢 ENTRADA Torrecilla · [LIMPIEZA OK] · 3 pers · llega 18:30 · ⏰ pide entrar 12:00 · 🍼 cuna
🔴 SALIDA Torrecilla · sale 11:00
```

- La **entrada** (verde) lleva la limpieza, las personas, la hora de llegada, la petición de entrar antes, el sofá cama y la cuna.
- La **salida** (rojo) dice a qué hora sale el huésped. Sin acuerdo con el anfitrión, nunca después de las 11:00, aunque el huésped diga que se va más tarde. Si sale antes, la suya, pero el evento sigue a las 11:00. Si se va un día antes («nos vamos esta noche al aeropuerto», «ya nos hemos ido»), el día y la hora: `sale 20/11 20:00`, y eso no es pedir salir más tarde. Si hay acuerdo, la acordada con ✅ (`sale 13:00 ✅`). La petición de salir más tarde va en su descripción.
- Los dos eventos llevan la **dirección del piso** como ubicación, con el mapa en Calendar.

La descripción de la salida dice la hora de salida y el locker. Si el huésped pide salir más tarde y aún no hay acuerdo, la petición va encima:

```
—— Airbnb · automático (escribe tus notas encima de esta línea) ——
⌛ PIDE SALIR DESPUÉS (a las 13:00). Si lo aceptas, escribe arriba: #salida 13:00

🕒 Salida: dom 08/11 a las 11:00

🔑 Locker: https://access.keynest.com/…
```

La descripción de la entrada tiene todo lo demás, de lo más urgente a lo menos:

```
—— Airbnb · automático (escribe tus notas encima de esta línea) ——
⏰ PIDE ENTRAR ANTES (a las 12:00). Si lo aceptas, escribe arriba: #entrada 12:00

🕒 Llegada: jue 05/11 a las 18:30   ·   Salida: dom 08/11 a las 11:00
🛋️ Sofá cama: no
🍼 Cuna: SÍ (lo indica el huésped)
🧹 Limpieza: 05/11 de 11:00 a 15:00 (el huésped anterior sale ese día a las 11:00)

📝 Peticiones:
   • Guardar maletas por la mañana

🔑 Locker: https://access.keynest.com/…

👤 Laura Garcia · Código HMAAAA0002
```

- **Avisos arriba** (solo si los hay): limpieza que no cabe, petición de entrar antes pendiente, mensajes que la IA no pudo analizar.
- **Valores por defecto:** si el huésped no dice nada, llegada 15:00, salida 11:00, sin sofá cama ni cuna. Si pide llegar antes o salir más tarde, las horas siguen siendo las estándar. La petición va arriba en su evento: entrar antes en la entrada, salir más tarde en la salida.
- **🧹 Limpieza:** si el huésped anterior del mismo piso sale ese día (o el anterior), la ventana para limpiar dentro del horario del equipo (08:00–15:00). Si no caben las 2 h, el título de la entrada lleva `[ALERTA LIMPIEZA / AJUSTE HORARIO]` y el evento sale en naranja.
- **🔑 Locker:** en los dos eventos. La salida lleva el mismo locker que la entrada, para dejar las llaves. Tirso tiene el suyo. Torrecilla y Buenavista comparten uno. Si los dos tienen entrada el mismo día, Torrecilla usa el alternativo, y su línea lo dice: `🔑 Locker: https://… (Buenavista también entra ese día)` en la entrada y `(Buenavista también entró el día de entrada)` en la salida.
- **Cancelada:** los dos eventos empiezan por `❌ CANCELADA` y salen en gris. Un relevo nunca está cancelado, porque antes se separa.
- **Creado desde un mensaje:** si llega un mensaje de una reserva cuya confirmación no se ha visto, se crean los eventos con lo que haya y una nota al final; la nota desaparece cuando llega la confirmación.

### El relevo

Si un huésped sale el mismo día que entra el siguiente en el mismo piso, su salida y esa entrada son **un solo evento**, el relevo:

```
🔄 SALIDA Y ENTRADA Torrecilla · [LIMPIEZA OK] · sale 11:00 · 3 pers · llega 18:30 · ⏰ pide entrar 12:00 · 🍼 cuna
```

- Empieza a las 11:00, o a la `#salida` del que se va, y dura 30 minutos, como todos los eventos, para que el calendario no se llene de bloques largos. La hora de llegada del que entra está en el título y en la descripción.
- Sale en azul (Pavo real), o en naranja si no cabe la limpieza.
- Después de `sale 11:00` vienen los mismos datos que en la entrada.
- Mientras hay relevo, el huésped que sale no tiene evento de salida. Su entrada sigue igual.
- Si una de las dos reservas se cancela o cambia de fechas, el relevo se separa. Vuelve la salida del que se iba y el evento pasa a ser una entrada normal.
- Lo que se añade al título pasa al relevo. Lleva lo de la salida del que se va y lo de la entrada del que llega, sin repetir: `(Caro H)🔄 SALIDA Y ENTRADA Torrecilla · …`. Al separarse, va a los dos eventos.
- Un título cambiado entero no se pierde. Al juntarse, pasa como una línea a las notas del relevo, encima de la línea automática. Al separarse, a las de los dos eventos. La línea pierde sus `#entrada` y `#salida`, porque en un título no son un acuerdo. El título vuelve a ser el automático.
- Salir el día anterior no es un relevo. Son dos eventos, como siempre.

La descripción tiene los avisos de los dos huéspedes, quién sale, la limpieza y quién entra:

```
—— Airbnb · automático (escribe tus notas encima de esta línea) ——
⌛ PIDE SALIR DESPUÉS (a las 13:00). Si lo aceptas, escribe arriba: #salida 13:00
⏰ PIDE ENTRAR ANTES (a las 12:00). Si lo aceptas, escribe arriba: #entrada 12:00

🔴 SALE: Marc Pérez · Código HMAAAA0001
🕒 Salida: jue 05/11 a las 11:00

🧹 Limpieza: 05/11 de 11:00 a 15:00 (el huésped anterior sale ese día a las 11:00)

🟢 ENTRA: Laura Garcia · Código HMAAAA0002
🕒 Llegada: jue 05/11 a las 18:30   ·   Salida: dom 08/11 a las 11:00
🛋️ Sofá cama: no
🍼 Cuna: SÍ (lo indica el huésped)

📝 Peticiones:
   • Guardar maletas por la mañana

🔑 Locker: https://access.keynest.com/…
```

El relevo lleva un solo locker, el del huésped que entra. El del que sale está en su evento de entrada.

### Acordar otra hora con el huésped

El horario estándar es **entrada 15:00 / salida 11:00**: si el huésped pide entrar antes o salir más tarde, se avisa, pero el calendario no cambia hasta que el anfitrión lo acuerde. La hora acordada **solo** se fija escribiendo en la descripción de cualquiera de los dos eventos, **encima de la línea automática**, `#entrada 12:00` o `#salida 13:30` (también vale `12h` o `13.30`). Para quitar el acuerdo, se borra la línea.

**En el relevo**, `#salida` es la hora del huésped que sale y `#entrada` la del que entra. El relevo empieza a la hora de `#salida`.

- **Al juntarse**, las notas de la salida del que se va pasan al relevo. Si había una `#entrada` en esa salida, pasa a su entrada. Si había una `#salida` en la entrada del que llega, pasa a su salida, o al relevo en el que sale si también sale en uno. Si alguien borró la línea automática de la salida del que se va, todo su texto pasa al relevo como notas.
- **Al separarse**, la salida del que se iba vuelve con las notas del relevo y su `#salida`. La entrada del que llega se queda con su `#entrada` y pierde la `#salida`, que no era suya. Las notas sin etiqueta quedan en los dos eventos.

**Un evento movido en Calendar se queda donde lo dejaron.** El script no lo devuelve a su hora, tampoco cuando llega un mensaje nuevo del huésped. Solo lo mueve en estos casos: la reserva cambia de día, se escribe, se cambia o se borra una etiqueta `#entrada` o `#salida`, o el relevo se junta o se separa. Entonces va a su hora calculada. Mover el evento no es un acuerdo. La limpieza y el título siguen la hora de la etiqueta o la estándar.

La hora acordada manda sobre cualquier regla (si no deja tiempo de limpieza, la entrada sale con alerta).

### Los cambios a mano se respetan

- Lo que se escriba **encima** de la línea `—— Airbnb · automático ——` se conserva siempre.
- Lo que se **añada al título**, delante o detrás, se conserva, por ejemplo un nombre: `(Caro H)🔴 SALIDA Torrecilla · sale 11:00`. El resto del título se sigue actualizando. Con `#salida 12:00` pasa a ser `(Caro H)🔴 SALIDA Torrecilla · sale 12:00 ✅`. Si se borra lo añadido, el título vuelve a ser el automático. También vale para los títulos cambiados antes de esta versión.
- Si se cambia el propio título automático, el título es del anfitrión y el script ya no lo toca.
- Si se cambia a mano el **color o la ubicación** de un evento, el script ya no toca ese campo de ese evento.
  Una ubicación puesta a mano antes de que el script escribiera la dirección también se respeta.
- Si se mueve un evento o se cambia su duración, se queda así hasta que cambie su hora calculada (ver arriba). Un evento pasado a día completo se queda así siempre.
- Si se edita la parte **automática** de la descripción, la descripción pasa a ser del anfitrión y deja de actualizarse.

### Avisos por correo

Al anfitrión solo le llegan las **peticiones de los huéspedes** (entrar antes, salir más tarde, sofá cama, cuna, otras peticiones) y las **modificaciones de reserva**, que se revisan a mano. Nunca se avisa de reservas que ya han terminado. Los errores técnicos van al desarrollador.

---

## 2. Cómo funciona

```
Yahoo Mail ──IMAP, solo lectura──▶ sincronizar.py (GitHub Actions, cada 20 min de día)
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
- **No duplica nada.** Cada correo se identifica por su UID de IMAP y su `Message-ID` (el mismo aunque se archive) y queda registrado. Cada reserva se encuentra en el calendario por su código, guardado como propiedad privada de sus dos eventos, junto con su estado completo (JSON). Si se borra uno de los dos, se vuelve a crear desde el otro. El relevo guarda además una copia del estado del huésped que sale, así que su reserva se encuentra aunque se borren sus otros eventos.
- **Yahoo por semanas.** Yahoo devuelve como mucho 1 000 resultados por búsqueda y descarta el resto sin avisar: se busca por tramos de una semana.
- **Manda lo último que dice el huésped.** Los correos se procesan en orden: un mensaje nuevo sustituye la hora de llegada o salida y puede pedir, cambiar o retirar la petición de horario. Llegar entre las 00:00 y las 06:00 cuenta como llegar tarde.
- **Gemini, con cabeza.** No se envían a la IA los mensajes de reservas que ya han terminado (no hay nada que preparar ni que avisar) ni los que son solo cortesía («Thanks!», «Vale, perfecto», «👍»): todas sus palabras deben ser de cortesía, sin números ni preguntas. Si Gemini falla en el día a día, se usan palabras clave (detectan sofá, cuna y peticiones de horario, sin horas) y el desarrollador recibe un aviso al día (que distingue la cuota diaria agotada de un error de clave o de modelo). En una recarga, si Gemini no responde ni esperando, la recarga se detiene en lugar de guardar un análisis peor.
- **Errores.** Un correo que falla se reintenta en las siguientes ejecuciones y, tras 3 intentos, se avisa al desarrollador. Si falla el envío de un aviso al anfitrión, el calendario se actualiza igual y el aviso se reintenta. Si el calendario no es accesible (ID o permisos), la ejecución se detiene sin contar fallos. Si la automatización está caída varios días, al volver recupera los correos desde la última ejecución que terminó bien. La ejecución nunca se pone en rojo en GitHub (para que GitHub no envíe correos al propietario del repositorio): el aviso llega por correo (`ALERTA_EMAIL`) y por [ntfy.sh](https://ntfy.sh).

| Pieza | Plan gratuito | Uso |
| --- | --- | --- |
| GitHub Actions | 2 000 min/mes en privado (ilimitado en público) | ≈58 ejecuciones al día de < 1 min (≈1 750 min/mes) |
| Google Calendar API | 1 000 000 peticiones/día | unas decenas al día |
| Gemini API | cuota diaria gratuita de AI Studio | un análisis por mensaje de huésped (no los de cortesía ni los de reservas terminadas) |

---

## 3. Instalación (una vez, unos 20 minutos)

1. **Contraseña de aplicación de Yahoo.** En la cuenta del anfitrión: Información de la cuenta → Seguridad → *Generar contraseña de aplicación*. Se puede revocar sin cambiar la contraseña principal.
2. **Cuenta de servicio de Google.**
   1. https://console.cloud.google.com → nuevo proyecto → *APIs y servicios → Biblioteca → Google Calendar API → Habilitar*.
   2. *IAM y administración → Cuentas de servicio → Crear* (sin roles) → *Claves → Agregar clave → JSON*. Ese archivo es el secreto `GOOGLE_SERVICE_ACCOUNT_JSON` (no lo subas al repositorio).
   3. En Google Calendar, configuración del calendario → *Compartir con determinadas personas* → el email de la cuenta de servicio (`…@….iam.gserviceaccount.com`) con permiso **«Hacer cambios en eventos»**.
   4. En la misma página, *Integrar el calendario* → **ID del calendario**.
3. **Clave de Gemini:** https://aistudio.google.com → *Get API key*. Opcional: sin ella se usan palabras clave y no se detectan horas.
4. **`config.toml`:** los `[[alojamientos]]` (nombre exacto del anuncio, `alias`, `id_anuncio`, `direccion`, `capacidad_sin_sofa`, `locker`), el horario estándar y el de limpieza.
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
| `BANDEJA_URL`, `BANDEJA_SECRETO` | (respuestas) dirección del bot en Vercel y el secreto compartido con él (sección 6) |
| `BASE_PRIVADA` | (respuestas) datos privados de la base de conocimiento: contraseñas de wifi (sección 6) |

6. **Puesta en marcha** (*Actions → Airbnb → Calendar → Run workflow*): `comprobar` (verifica Yahoo, Calendar y Gemini sin escribir nada) y después `recargar` con `desde` = la fecha desde la que cargar reservas. A partir de ahí sigue sola.

---

## 4. Operación

Todo se lanza desde *Actions → Airbnb → Calendar → Run workflow*:

| Modo | Qué hace |
| --- | --- |
| `sincronizar` | lo mismo que las ejecuciones automáticas: procesa los correos nuevos |
| `recargar` | con `desde` (y opcionalmente `hasta`): vuelve a procesar todos los correos del tramo en orden, sin avisos al anfitrión, y al final pone al día todas las reservas futuras. Cada reserva se reconstruye desde su confirmación; los mensajes de reservas ya terminadas no se analizan con la IA. Hasta ~5,5 h por ejecución |
| `comprobar` | verifica Yahoo, Google Calendar, Gemini y los avisos, sin escribir nada |
| `diagnosticar` | muestra lo que se extrae de los correos de los últimos 7 días, sin tocar el calendario |
| `listar` | con `desde` (y opcionalmente `hasta`, por defecto 31 días después): lista todos los eventos del calendario de ese tramo, también los borrados y los puestos a mano, con quién los creó y cuándo cambiaron. No escribe nada |
| `recalcular-simulacion` / `recalcular` | aplica las reglas actuales (horario, limpieza, formato, lockers) a todas las reservas en curso y futuras, sin leer correos. Con `desde`, revisa las reservas que salen desde esa fecha, también las ya terminadas |
| `purgar-simulacion` / `purgar` | lista / borra todos los eventos creados por el script (escribiendo `BORRAR` en `confirmar`), también los de las primeras versiones, que no llevan marca; los eventos personales no se tocan. Con `hasta`, solo los de reservas que terminaron antes de esa fecha. Una reserva que la cruza se conserva entera |

**Entorno de pruebas.** Con `entorno = pruebas` se usa el calendario de pruebas (`CALENDAR_ID_PRUEBAS` y `GOOGLE_SERVICE_ACCOUNT_JSON_PRUEBAS`; si faltan, no se ejecuta y nunca toca el calendario real), el registro va en la rama `estado-pruebas` y los avisos de peticiones van solo a `ALERTA_EMAIL` con `[PRUEBAS]`. Las ejecuciones programadas son siempre de producción.

**Qué hacer si…**

- **Llega «la ejecución ha fallado»:** abre el enlace del aviso. Si es la contraseña de Yahoo, genera otra y actualiza `YAHOO_CLAVE_APP`; si es un 404 de Calendar, revisa `CALENDAR_ID` y que el calendario esté compartido con la cuenta de servicio.
- **Llega «no se pudo procesar un correo»:** probablemente Airbnb ha cambiado una plantilla. Los patrones están en `airbnb_calendar/parser.py` (`PATRONES`): añade el correo (anonimizado) a `ejemplos/`, escribe su prueba y ajusta el patrón.
- **Una recarga se detiene por Gemini:** se ha agotado la cuota diaria gratuita (se renueva hacia las 9:00 de Madrid). Vuelve a lanzarla con `desde` = la fecha que indica el error; es idempotente.
- **Correos de Airbnb en Spam:** añade `"Bulk"` a `[yahoo] carpetas` en `config.toml`.
- **Acabas de actualizar a los eventos de entrada y salida:** lanza `recalcular-simulacion` para ver cuántos eventos se actualizarían y cuántos se crearían, y después `recalcular`. Cada evento antiguo que ocupa toda la estancia pasa a ser el evento de entrada, y se crea el de salida. Las notas del anfitrión y sus etiquetas `#entrada` / `#salida` se conservan. Las horas movidas a mano no: si una era un acuerdo, escríbelo como etiqueta. Sin `recalcular`, las ejecuciones automáticas convierten igual las estancias en curso y las que entran en los próximos 30 días.
- **Acabas de actualizar al relevo:** lanza `recalcular-simulacion`. Dice `Se juntaría: …` por cada salida y entrada del mismo día. Después, `recalcular` las junta, con las notas y etiquetas de cada una en su sitio. Sin `recalcular`, las ejecuciones automáticas juntan igual las de los próximos 30 días.
- **Cambiar el nombre corto de un piso, el horario o un locker:** edita `config.toml`; las reservas futuras se corrigen en la siguiente ejecución (todas, con `recalcular`).
- **Registro:** la rama `estado` guarda `estado.json` (correos procesados de los últimos días). Si se borra, la siguiente ejecución empieza desde ese momento.
- **Plantillas en inglés:** el parser espera los correos de Airbnb en inglés; no cambies el idioma de la cuenta de Airbnb.

---

## 5. Desarrollo

```powershell
py -m venv .venv; .venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q      # pruebas del script y del bot (bot/tests), sin red
.venv\Scripts\ruff check .             # estilo del código
```

Las pruebas usan los correos (anonimizados) de `ejemplos/` e incluyen un servidor IMAP falso, una imitación de la API de Google Calendar y de Gemini, y la simulación completa de varias ejecuciones seguidas con el registro persistente. En cada push, GitHub Actions ejecuta `ruff` y las pruebas (`.github/workflows/pruebas.yml`).

Para ejecutarlo contra los servicios reales, define las mismas variables de entorno que los secretos y lanza `python sincronizar.py --comprobar`.

---

## 6. Respuestas a los huéspedes (Telegram)

Cuando un huésped escribe, el script prepara un **borrador** de respuesta con la base de conocimiento
(`base_conocimiento.toml`) y los datos de la reserva, y lo manda a **Telegram**. Ahí decides tú:

```
🏠 Torrecilla · Laura Garcia · 05/12 → 08/12 · 2 adultos
Huésped · 25/11 17:30
│ Hi! Can we check in at 12?
🔎 Para ti
• 🧹 Entrada antes: el huésped anterior sale ese mismo día a las 11:00; la limpieza puede ir de 11:00 a 15:00 (cabe). El piso estaría listo hacia las 13:00.
✍️ Borrador (EN · confianza media)
  Hi Laura! We always try to be flexible…
[✅ Enviar] [✏️ Editar]
[❌ Descartar]
```

- **✅ Enviar** → **✅ Sí, enviar**: se publica en el hilo de Airbnb y la tarjeta pasa a «✅ Enviado».
- **✏️ Editar**: abre una pantalla dentro de Telegram con el texto editable; «Enviar al huésped» (con confirmación)
  o «Guardar sin enviar». También vale **responder a la tarjeta** con el texto nuevo.
- **❌ Descartar**: no se envía nada (contestas tú en Airbnb si hace falta).
- `/pendientes` vuelve a mostrar los borradores sin decidir.

**Nada se envía al huésped sin tu confirmación.** Además:
- solo se escribe a la dirección de respuesta del hilo de Airbnb (`…@reply.airbnb.com`), nunca a otra;
- un borrador se envía como mucho una vez;
- con huecos `[[COMPLETAR: …]]` (datos que no están en la base) no se puede enviar hasta rellenarlos;
- la IA solo puede usar datos de la base: si pone un enlace que no está en la base, la tarjeta lo avisa;
- los borradores del entorno de **pruebas** van marcados 🧪 y «enviar» solo lo simula.

**Cómo se contesta.** Hay dos formas, según tenga o no el bot una cuenta de correo en Vercel:

- **Copiar y pegar** (sin cuenta de correo en Vercel): la tarjeta trae el texto listo. Lo copias (toca el texto),
  «🔗 Abrir Airbnb», lo pegas en la conversación y tocas «✅ Ya lo pegué». No hace falta ninguna contraseña de correo.
- **Envío desde el bot** (con `YAHOO_USUARIO` / `YAHOO_CLAVE_APP` del anfitrión en Vercel): cada aviso de Airbnb lleva un
  `Reply-To` (`…@reply.airbnb.com`) y responder a esa dirección publica el mensaje en la conversación. Airbnb solo la acepta
  desde la cuenta que recibió el aviso, así que el bot solo envía un hilo desde esa misma cuenta; si no la tiene, ese
  borrador pasa a copiar y pegar. Si una reserva aún no tiene hilo (el huésped no ha escrito), también.

**Idioma.** Los borradores se escriben siempre en **español** (`[respuestas] idioma = "es"` en `config.toml`): Airbnb
traduce el mensaje al idioma del huésped. Con `idioma = ""`, se escriben en el idioma en que escribe el huésped.

**Agenda.** Además de las respuestas, se proponen los mensajes de cada momento de la estancia (`[[momentos]]` de la base:
bienvenida, día antes de salir, despedida…), el día y a partir de la hora que indica cada uno, una sola vez por reserva.
En `config.toml`, `[respuestas] momentos` dice cuáles: quita los que el anfitrión ya envía con los mensajes programados de
Airbnb. Un mensaje con el texto `PENDIENTE` en la base no se propone.

**La base de conocimiento** (`base_conocimiento.toml`) es la única fuente de datos de los borradores. `PENDIENTE` = dato que
falta: el borrador lo marca para que lo completes y nunca lo inventa. Para cambiar una respuesta, edita la base (los pisos
se llaman como los `alias` de `config.toml`).

**El repositorio es público**, así que en él no va nada confidencial:
- Las contraseñas del wifi van en la base como `PRIVADO`, y su valor real en el secreto **`BASE_PRIVADA`** (un TOML con
  las mismas secciones; formato en `base_privada.ejemplo.toml`). Sin ese secreto, el wifi se trata como `PENDIENTE`.
  Para probar en local, crea `base_privada.toml` (está en `.gitignore`).
- En la rama `estado` solo van identificadores y fechas. Los borradores que esperan a que Vercel responda y las
  direcciones de respuesta van **cifrados** con una clave derivada de `BANDEJA_SECRETO`.

```
Yahoo ──▶ sincronizar.py (Actions, cada 20 min de día) ──▶ Gemini (borrador) ──▶ bot/ en Vercel ──▶ Telegram
                                                                                  │  tú apruebas
                                        conversación de Airbnb ◀── copiar y pegar ┤
                                                                ◀── SMTP de Yahoo ┘ (opcional)
```
- `airbnb_calendar/base.py` base de conocimiento · `redactor.py` borradores con Gemini · `respuestas.py` cuándo y qué
  se propone · `bandeja.py` cliente del bot.
- `bot/` (Vercel, Python/Flask): webhook de Telegram, pantalla de edición (Mini App), envío por SMTP (opcional).
  Guarda los borradores en Upstash Redis (90 días).

### Puesta en marcha (una vez)

La lista paso a paso está en [`docs/PUESTA_EN_MARCHA.md`](docs/PUESTA_EN_MARCHA.md). En resumen: bot de Telegram
(@BotFather) → proyecto de Vercel con carpeta `bot/` y Upstash Redis → secretos de GitHub `BANDEJA_URL`, `BANDEJA_SECRETO`
y `BASE_PRIVADA` → *Actions → Run workflow → `comprobar`* → `/start` en el bot → `APROBADORES` en Vercel → `comprobar`
otra vez (llega un borrador de demostración 🧪).

### Apagarlo o quitarlo

Los borradores son una capa aparte: **nunca cambian el calendario**, y un fallo suyo no para la sincronización.
- **Pausar sin tocar código:** borra el secreto `BANDEJA_URL` en GitHub. El calendario sigue igual y no se preparan borradores.
- **Desactivarlo:** `[respuestas] activar = false` en `config.toml`.
- **Que el bot envíe solo** (en vez de copiar y pegar): pon `YAHOO_USUARIO` y `YAHOO_CLAVE_APP` del anfitrión en Vercel
  (los mismos valores que en GitHub) y *Redeploy*.
- **Quitarlo del todo:** borra `bot/`, `base_conocimiento.toml`, `base_privada.ejemplo.toml`, los módulos `base.py`,
  `redactor.py`, `respuestas.py` y `bandeja.py`, la sección `[respuestas]` y los secretos `BANDEJA_*` y `BASE_PRIVADA`;
  y el proyecto de Vercel.

### Qué hacer si…
- **La tarjeta dice «No se pudo enviar»:** mira en Airbnb que no haya llegado y toca 🔁 Reintentar, o pégalo a mano y toca
  «✅ Ya lo pegué». Si es la contraseña de aplicación, actualiza `YAHOO_CLAVE_APP` en Vercel.
- **El primer envío real:** comprueba en Airbnb que aparece en la conversación. Si Airbnb lo rechazara, llegará un correo de
  rebote a la cuenta que envía: hasta resolverlo, copia el texto de la tarjeta, pégalo en Airbnb y descarta el borrador.
- **No llegan borradores:** `comprobar` dice qué falta. Los mensajes de cortesía («Thanks!») y los de más de 48 h no generan
  borrador; tampoco las recargas.
- **Vercel o Upstash caídos:** los borradores se guardan (cifrados) en el registro y se reintentan en cada ejecución; el
  desarrollador recibe un aviso.
- **Cuenta de Vercel:** el plan Hobby es para uso personal. Para dejarlo funcionando para un cliente, pasa el proyecto a su
  cuenta (plan Pro) o despliega `bot/` en otro servicio (es una app Flask normal).
