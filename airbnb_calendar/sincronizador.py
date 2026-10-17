"""
Flujo principal: correos de Yahoo → parser → reglas / Gemini → Google Calendar → avisos.

Las dependencias externas (buzón, calendario, analizador, notificador) se
inyectan, así que el flujo completo se prueba en local con imitaciones.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import reglas
from .calendario import CalendarioInaccesible, leer_estado
from .config import Config
from .gemini import GeminiAgotado
from .parser import DatosCorreo, parsear_correo
from .registro import Registro
from .yahoo import Correo, es_remitente

log = logging.getLogger(__name__)


class ErrorDefinitivo(Exception):
    """Error que no se arregla reintentando (p. ej. plantilla de Airbnb desconocida)."""


class Sincronizador:
    def __init__(self, cfg: Config, buzon, calendario, analizador, notificador, registro: Registro,
                 ahora: datetime | None = None, desde: datetime | None = None,
                 hasta: datetime | None = None, notificador_tecnico=None):
        self.cfg = cfg
        self.buzon = buzon
        self.cal = calendario
        self.analizador = analizador
        self.notificador = notificador                      # al anfitrión (Yahoo)
        self.tecnico = notificador_tecnico or _SoloLog()    # al desarrollador (ntfy); nunca al cliente
        self.resumen = {"correos": 0, "errores": [], "alertas_limpieza": []}
        self.registro = registro
        self.ahora = ahora or datetime.now(timezone.utc)
        self.desde = desde      # carga manual: ignora la ventana y el inicio del registro
        self.hasta = hasta
        self.tz = ZoneInfo(cfg.zona_horaria)

    def ejecutar(self) -> int:
        """Procesa los correos pendientes. Devuelve cuántos ha procesado."""
        # Si el calendario no es accesible se para aquí: si no, cada correo contaría un fallo y,
        # tras varios intentos, se daría por perdido aunque el problema sea la configuración
        self.cal.comprobar_acceso()
        inicio = self.registro.fecha_inicio(self.cfg.procesar_desde, self.ahora)
        desde = self.desde or max(inicio, self.ahora - timedelta(days=self.cfg.ventana_dias))
        # Carga manual (--desde): se reprocesa todo el tramo (p. ej. tras una purga); es idempotente
        excluir = set() if self.desde else set(self.registro.procesados)
        correos = (self.buzon.correos_desde(desde, self.cfg.carpetas, self.cfg.remitentes, excluir, hasta=self.hasta)
                   if self.hasta else self.buzon.correos_desde(desde, self.cfg.carpetas, self.cfg.remitentes, excluir))
        log.info("%d correo(s) nuevo(s) desde %s", len(correos), desde.isoformat(timespec="minutes"))
        for i, c in enumerate(correos, 1):
            self.procesar_correo(c)
            if i % 20 == 0:
                self.registro.guardar()     # si la ejecución se corta, no se pierde lo avanzado
        self.registro.limpiar(self.ahora - timedelta(days=self.cfg.ventana_dias + 7))
        # Limpiezas de los próximos días, aunque no haya llegado ningún correo (cambios de fechas, migración…)
        self.resumen["alertas_limpieza"] = self.cal.recalcular_limpiezas(self.ahora.astimezone(self.tz).date())
        self.resumen["correos"] = len(correos)
        return len(correos)

    # ------------------------------------------------------------------ correo

    def procesar_correo(self, c: Correo) -> None:
        if not es_remitente(c.remitente, self.cfg.remitentes):
            log.info("Ignorado (remitente %s): %s", c.remitente, c.asunto)
            self._marcar_procesado(c)
            return
        local = c.fecha.astimezone(self.tz)
        ctx = {
            "fecha_hora": local.strftime("%Y-%m-%d %H:%M"),
            "fecha_corta": local.strftime("%d/%m"),
            "ref": f"«{c.asunto or 'sin asunto'}» · recibido el {local:%d/%m/%Y %H:%M} · https://mail.yahoo.com",
        }
        try:
            datos = parsear_correo(c.asunto, c.texto, local.date().isoformat(), self.cfg.alojamientos)
            log.info("[%s] %s", datos.tipo, c.asunto)
            for a in datos.avisos:
                log.warning("  ⚠ %s", a)
            self._procesar_datos(datos, ctx)
            self._marcar_procesado(c)
        except CalendarioInaccesible:
            raise                                        # problema de configuración: no es culpa del correo
        except GeminiAgotado as err:
            # Recarga manual: se para aquí (este correo queda sin procesar) para no empeorar los datos
            raise RuntimeError(f'Gemini no responde ({err}). Recarga detenida en "{c.asunto}" '
                               f"(recibido el {local:%Y-%m-%d}): vuelve a lanzarla más tarde con desde = esa fecha.") from None
        except Exception as err:                         # noqa: BLE001 — un correo malo no para los demás
            definitivo = isinstance(err, ErrorDefinitivo) or self.registro.sumar_fallo(c.id) >= self.cfg.max_reintentos
            log.error('Error en "%s": %s (%s)', c.asunto, err, "definitivo" if definitivo else "se reintentará")
            self.resumen["errores"].append(f"{c.asunto}: {err}")
            if definitivo:
                self._marcar_procesado(c)
                if self.cfg.avisar_errores:
                    # Errores técnicos: al desarrollador (ntfy / ALERTA_EMAIL), nunca al anfitrión
                    self.tecnico.enviar(
                        "⚠️ Airbnb → Calendar: no se pudo procesar un correo",
                        f"Correo: {c.asunto}\n\nError: {err}\n\n"
                        "Revísalo a mano y actualiza el calendario si hace falta. No se volverá a intentar.\n\n"
                        f"Correo en Yahoo Mail: {ctx['ref']}")

    def _marcar_procesado(self, c: Correo) -> None:
        # También por Message-ID: si después se archiva (otra carpeta, otro UID) no se vuelve a procesar
        for id_ in filter(None, (c.id, getattr(c, "id_mensaje", ""))):
            self.registro.marcar_procesado(id_, c.fecha)

    def _procesar_datos(self, d: DatosCorreo, ctx: dict) -> None:
        if d.faltan:
            raise ErrorDefinitivo(f"No se pudieron extraer: {', '.join(d.faltan)}. ¿Ha cambiado la plantilla de Airbnb?")
        if d.tipo == "confirmacion":
            self._confirmacion_o_mensaje(d, ctx)
        elif d.tipo == "cancelacion":
            self._cancelacion(d)
        elif d.tipo == "mensaje":
            self._mensaje(d, ctx)
        elif d.tipo == "modificacion":
            if self.cfg.avisar_modificaciones:
                self.notificador.enviar("✏️ Airbnb: modificación de reserva (revisar el calendario)",
                                        "Ha llegado una modificación de reserva, que no se procesa automáticamente:\n\n"
                                        f"Correo en Yahoo Mail: {ctx['ref']}\n\nRevisa las fechas y las personas en el calendario.")
        else:
            log.info("  (ignorado: %s)", d.tipo)

    def _buscar(self, d: DatosCorreo) -> dict:
        return self.cal.buscar_eventos_reserva(d.codigo, d.alojamiento, d.checkin, d.checkout)

    def _confirmacion_o_mensaje(self, d: DatosCorreo, ctx: dict, eventos: dict | None = None) -> None:
        eventos = eventos if eventos is not None else self._buscar(d)
        estado = leer_estado(eventos)
        if estado and d.tipo == "confirmacion":
            # La confirmación es el primer correo de la reserva: si ya existe el evento es que se están
            # re-procesando correos (--desde). Se reconstruye todo lo deducido de los mensajes, en orden,
            # para que mande lo último que dijo el huésped y no queden interpretaciones antiguas.
            reglas.resetear_mensajes(estado)
        estado = estado or reglas.crear_estado()
        reglas.actualizar_con_datos(estado, d)
        self._analizar_texto_huesped(estado, d, ctx)
        alojamiento = self.cfg.alojamiento_por_nombre(estado["alojamiento"])
        reglas.aplicar_reglas(estado, alojamiento, self.cfg)
        novedades = reglas.avisos_pendientes(estado, self.cfg)
        # Primero el aviso: si después fallara el guardado se reintenta (mejor un aviso repetido que uno perdido)
        if novedades and not self._terminada(estado):
            self._avisar_peticiones(estado, novedades, d, ctx, en_calendario=True)
        reglas.marcar_avisado(estado)
        self.cal.guardar_reserva(estado, eventos, alojamiento)

    def _cancelacion(self, d: DatosCorreo) -> None:
        eventos = self._buscar(d)
        if not eventos["evento"]:
            log.info("  Cancelación de una reserva que no está en el calendario (%s).", d.codigo)
            return
        estado = leer_estado(eventos) or reglas.crear_estado(d)
        reglas.actualizar_con_datos(estado, d)
        estado["cancelada"] = True
        self.cal.guardar_reserva(estado, eventos, self.cfg.alojamiento_por_nombre(estado["alojamiento"]))

    def _mensaje(self, d: DatosCorreo, ctx: dict) -> None:
        eventos = self._buscar(d)
        if eventos["evento"] or self.cfg.crear_evento_desde_mensaje:
            return self._confirmacion_o_mensaje(d, ctx, eventos)
        # Reserva anterior a la instalación: no se crea, pero si el huésped pide algo se avisa.
        # (Sin reglas deterministas, para no repetir el aviso con cada mensaje.)
        estado = reglas.crear_estado(d)
        self._analizar_texto_huesped(estado, d, ctx)
        novedades = reglas.avisos_pendientes(estado, self.cfg)
        if novedades and not self._terminada(estado):
            self._avisar_peticiones(estado, novedades, d, ctx, en_calendario=False)
        log.info("  Mensaje de una reserva que no está en el calendario: no se crean eventos.")

    # ------------------------------------------------------------ huésped y avisos

    def _analizar_texto_huesped(self, estado: dict, d: DatosCorreo, ctx: dict) -> None:
        texto = _texto_huesped(d)
        if not texto:
            return
        r = self.analizador.analizar(texto, {
            "alojamiento": estado["alojamiento"], "checkin": estado["checkin"], "checkout": estado["checkout"],
            "hora_checkin": estado["hora_checkin"], "hora_checkout": estado["hora_checkout"],
            "personas": reglas.texto_personas(estado), "enviado_el": ctx["fecha_hora"],
            "peticiones_previas": estado["peticiones"],
        })
        log.info("  Análisis (%s): %s", r.fuente, r.analisis)
        reglas.fusionar_analisis(estado, r.analisis, r.fuente, ctx["fecha_corta"], self.cfg)
        if r.error:
            estado["notas"] = (estado["notas"] + [{"f": ctx["fecha_corta"], "t": "⚠️ Mensaje sin analizar por la IA; revísalo en Yahoo Mail."}])[-5:]
            if self.registro.puede_avisar("gemini", self.ahora):
                self.tecnico.enviar("⚠️ Airbnb → Calendar: Gemini no responde",
                                        "Los mensajes se están analizando solo con palabras clave (sin horas de llegada/salida).\n\n"
                                        f"Error: {r.error}\n\nComprueba el secreto GEMINI_API_KEY y [gemini] modelo en config.toml.")

    def _terminada(self, estado: dict) -> bool:
        """Reserva que ya ha terminado: no tiene sentido avisar al anfitrión de sus peticiones."""
        fin = estado.get("checkout") or estado.get("checkin")
        return bool(fin) and fin < self.ahora.astimezone(self.tz).date().isoformat()

    def _avisar_peticiones(self, estado: dict, novedades: list[str], d: DatosCorreo, ctx: dict, en_calendario: bool) -> None:
        etiquetas = [n for p, n in (("⏰", "entrar antes"), ("⌛", "salir después"), ("🛋️", "sofá cama"),
                                    ("🍼", "cuna"), ("📝", "petición"), ("🕒", "horario"))
                     if any(x.startswith(p) for x in novedades)]
        asunto = (f"🔔 Airbnb · {estado['alias'] or estado['alojamiento']} · "
                  f"{reglas.fecha_corta_es(estado['checkin'])}–{reglas.fecha_corta_es(estado['checkout'])} · {', '.join(etiquetas)}")
        l = ["Novedades detectadas en la reserva:", ""]
        l += ["  • " + n for n in novedades]
        l += ["", "—— Reserva ——",
              "🏠 " + (estado["alojamiento"] or "?"),
              f"📅 Check-in: {reglas.fecha_larga_es(estado['checkin'])}   Check-out: {reglas.fecha_larga_es(estado['checkout'])}",
              f"👥 {reglas.texto_personas(estado)}   ·   Huésped: {estado['huesped'] or '?'}",
              f"🕒 Llegada prevista: {estado['hora_llegada'] or 'no indicada'}   ·   Salida prevista: {estado['hora_salida'] or 'no indicada'}"]
        if not en_calendario:
            l += ["", "⚠️ Esta reserva NO está en el calendario (es anterior a la instalación)."]
        texto = _texto_huesped(d)
        if texto:
            l += ["", "—— Mensaje del huésped ——", texto[:2000]]
        l += ["", "Correo en Yahoo Mail: " + ctx["ref"]]
        self.notificador.enviar(asunto, "\n".join(l))


class _SoloLog:
    def enviar(self, asunto: str, cuerpo: str) -> None:
        log.warning("Aviso técnico: %s", asunto)


def _texto_huesped(d: DatosCorreo) -> str:
    return "\n\n".join(m.texto for m in d.mensajes).strip()
