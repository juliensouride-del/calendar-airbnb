#!/usr/bin/env python3
"""
Airbnb (Yahoo Mail) → Google Calendar.

  python sincronizar.py                 procesa los correos nuevos (lo que ejecuta GitHub Actions)
  python sincronizar.py --desde yyyy-mm-dd [--hasta yyyy-mm-dd]
                                        recarga: vuelve a procesar los correos del tramo (en orden,
                                        sin avisos al anfitrión) y al final pone al día todas las
                                        reservas futuras
  python sincronizar.py --comprobar     comprueba Yahoo, Google Calendar y Gemini sin escribir nada
  python sincronizar.py --diagnosticar  muestra lo que se extrae de los correos de Airbnb de los
                                        últimos 7 días, SIN tocar el calendario ni el registro
  python sincronizar.py --purgar --dry-run   lista los eventos creados por este script
  python sincronizar.py --purgar             los borra (pide confirmación; --si para no preguntar).
                                        Solo toca eventos con las marcas privadas del script o
                                        creados por su cuenta de servicio (primeras versiones).
                                        Con --hasta yyyy-mm-dd, solo los de reservas que terminaron
                                        antes de ese día. Para volver a cargarlos: --desde yyyy-mm-dd
  python sincronizar.py --recalcular [--dry-run] aplica las reglas actuales (horario estándar, limpieza)
                                        a todas las reservas que entran desde hoy, sin leer correos.
                                        Con --desde yyyy-mm-dd, a las que salen desde ese día
                                        (también las ya terminadas)

Secretos (variables de entorno): YAHOO_USUARIO, YAHOO_CLAVE_APP, GOOGLE_SERVICE_ACCOUNT_JSON,
CALENDAR_ID y, opcionales, GEMINI_API_KEY, ALERTA_EMAIL y ALERTA_NTFY_TOPIC (avisos técnicos
solo al desarrollador). Con PRUEBAS=1 los avisos de peticiones van solo a ALERTA_EMAIL. En GitHub Actions se escribe un resumen
de cada ejecución en GITHUB_STEP_SUMMARY.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

from airbnb_calendar.calendario import P_ORIGEN, Calendario, sesion_cuenta_servicio
from airbnb_calendar.config import Secretos, cargar_config
from airbnb_calendar.gemini import AnalizadorMensajes
from airbnb_calendar.ntfy import NotificadorNtfy, Varios
from airbnb_calendar.parser import parsear_correo
from airbnb_calendar.registro import Registro
from airbnb_calendar.sincronizador import Sincronizador
from airbnb_calendar.yahoo import BuzonYahoo, NotificadorYahoo, es_remitente

log = logging.getLogger("sincronizar")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--estado", default="estado/estado.json", help="registro de correos procesados")
    ap.add_argument("--desde", help="yyyy-mm-dd: procesa los correos desde esa fecha (carga manual); con --recalcular, "
                                    "revisa las reservas que salen desde ese día, aunque ya hayan terminado")
    ap.add_argument("--hasta", help="yyyy-mm-dd (excluida): límite de la carga manual; con --purgar, borra solo "
                                    "las reservas que terminaron antes de ese día")
    modo = ap.add_mutually_exclusive_group()
    modo.add_argument("--comprobar", action="store_true")
    modo.add_argument("--diagnosticar", action="store_true")
    modo.add_argument("--purgar", action="store_true", help="borra los eventos creados por este script")
    modo.add_argument("--recalcular", action="store_true", help="aplica las reglas actuales a las reservas futuras")
    ap.add_argument("--dry-run", action="store_true", help="con --purgar o --recalcular: solo lista, no cambia nada")
    ap.add_argument("--si", action="store_true", help="con --purgar: no pide confirmación")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    cfg = cargar_config(args.config)
    sec = None if args.purgar or args.recalcular else Secretos.desde_entorno()

    if args.diagnosticar:
        return diagnosticar(cfg, sec)

    if args.purgar:
        sesion = sesion_cuenta_servicio(Secretos.google_desde_entorno())
        # Los eventos de las primeras versiones no llevan marca: se reconocen por la cuenta que los creó
        calendario = Calendario(cfg, sesion, creador=sesion.credentials.service_account_email)
        return purgar(calendario, simulacion=args.dry_run, confirmado=args.si,
                      hasta=date.fromisoformat(args.hasta) if args.hasta else None)
    if args.recalcular:
        return recalcular(Calendario(cfg, sesion_cuenta_servicio(Secretos.google_desde_entorno())), args.dry_run,
                          desde=date.fromisoformat(args.desde) if args.desde else None)

    calendario = Calendario(cfg, sesion_cuenta_servicio(sec.google_service_account_json))
    # Carga manual (--desde): Gemini espera al límite de peticiones y, si aun así falla, la carga se detiene
    analizador = AnalizadorMensajes(cfg, sec.gemini_api_key, paciente=bool(args.desde))
    if args.comprobar:
        return comprobar(cfg, sec, calendario, analizador)

    registro = Registro(args.estado)
    notificador = NotificadorYahoo(sec.yahoo_usuario, sec.yahoo_clave_app, cfg.email_avisos)
    if os.environ.get("PRUEBAS"):
        # Entorno de pruebas: los avisos de peticiones nunca llegan al anfitrión, solo al desarrollador
        email = os.environ.get("ALERTA_EMAIL", "").strip()
        notificador = (ConPrefijo(NotificadorYahoo(sec.yahoo_usuario, sec.yahoo_clave_app, email), "[PRUEBAS] ")
                       if email else NotificadorSilencioso())
    try:
        with BuzonYahoo(sec.yahoo_usuario, sec.yahoo_clave_app) as buzon:
            fecha = lambda t: datetime.fromisoformat(t).replace(tzinfo=UTC) if t else None   # noqa: E731
            if args.desde:
                notificador = NotificadorSilencioso()   # carga manual: no avisar de correos antiguos
            sincro = Sincronizador(cfg, buzon, calendario, analizador, notificador, registro,
                                   desde=fecha(args.desde), hasta=fecha(args.hasta),
                                   notificador_tecnico=notificador_tecnico(sec))
            sincro.ejecutar()
            if args.desde:
                # Recarga: además de los próximos 30 días, todas las reservas futuras con las reglas actuales
                calendario.recalcular_limpiezas(date.today(), dias=None)
            escribir_resumen(sincro.resumen, calendario.stats)
    finally:
        registro.guardar()      # lo procesado hasta un posible fallo queda registrado
    return 0


def notificador_tecnico(sec):
    """Avisos de errores SOLO al desarrollador: ntfy (ALERTA_NTFY_TOPIC) y correo (ALERTA_EMAIL)."""
    email = os.environ.get("ALERTA_EMAIL", "").strip()
    return Varios(NotificadorNtfy(os.environ.get("ALERTA_NTFY_TOPIC", "")),
                  NotificadorYahoo(sec.yahoo_usuario, sec.yahoo_clave_app, email) if email and sec else None)


def escribir_resumen(resumen: dict, stats: dict) -> None:
    """Resumen de la ejecución en la página de GitHub Actions (si no, en el log)."""
    l = ["### Airbnb → Calendar", "",
         f"- Correos procesados: **{resumen['correos']}**",
         f"- Eventos creados: {stats['creados']} · actualizados: {stats['actualizados']}"
         f" · limpiezas recalculadas: {stats['limpiezas_recalculadas']}",
         f"- Errores: **{len(resumen['errores'])}**"]
    l += [f"  - {e}" for e in resumen["errores"]]
    l.append(f"- Alertas de limpieza (próximos 30 días): **{len(resumen['alertas_limpieza'])}**")
    l += [f"  - {e['alias'] or e['alojamiento']} · entrada {e['checkin']} · {e['codigo'] or '?'}"
          for e in resumen["alertas_limpieza"]]
    ruta = os.environ.get("GITHUB_STEP_SUMMARY")
    if ruta:
        with open(ruta, "a", encoding="utf-8") as f:
            f.write("\n".join(l) + "\n")
    else:
        log.info("\n".join(l))


def recalcular(calendario: Calendario, simulacion: bool, desde: date | None = None) -> int:
    alertas = calendario.recalcular_limpiezas(desde or date.today(), dias=None, simulacion=simulacion)
    st = calendario.stats
    cambios = ("%d evento(s) por actualizar y %d por crear (simulación)" if simulacion
               else "%d evento(s) actualizado(s) y %d creado(s)") % (st["limpiezas_recalculadas"], st["creados"])
    log.info("✔ %d reserva(s) en curso o futura(s) revisada(s) · %s · %d con alerta de limpieza",
             st["reservas_revisadas"], cambios, len(alertas))
    return 0


class ConPrefijo:
    def __init__(self, notificador, prefijo: str):
        self.notificador, self.prefijo = notificador, prefijo

    def enviar(self, asunto: str, cuerpo: str) -> None:
        self.notificador.enviar(self.prefijo + asunto, cuerpo)


class NotificadorSilencioso:
    def enviar(self, asunto: str, cuerpo: str) -> None:
        log.info("Aviso omitido (carga manual): %s", asunto)


def purgar(calendario: Calendario, simulacion: bool, confirmado: bool, hasta: date | None = None) -> int:
    eventos = calendario.eventos_propios(hasta)
    for ev in eventos:
        inicio = ev.get("start") or {}
        dia = (inicio.get("dateTime") or inicio.get("date") or "")[:16].replace("T", " ")
        marca = "" if (ev.get("extendedProperties") or {}).get("private", {}).get(P_ORIGEN) else "  (sin marca)"
        print(f"  {dia}  {ev.get('summary', '')}{marca}")
    log.info("%d evento(s) creados por Airbnb → Calendar%s", len(eventos),
             f" de reservas que terminaron antes del {hasta}" if hasta else "")
    if simulacion or not eventos:
        return 0
    if not confirmado:
        if not sys.stdin.isatty():
            log.error("Sin consola para confirmar: usa --si para borrar sin preguntar")
            return 1
        if input(f"Escribe BORRAR para eliminar estos {len(eventos)} eventos: ").strip() != "BORRAR":
            log.info("Cancelado: no se ha borrado nada.")
            return 0
    for ev in eventos:
        calendario.borrar_evento(ev)
    log.info("✔ %d evento(s) borrados", len(eventos))
    return 0


def comprobar(cfg, sec, calendario, analizador) -> int:
    with BuzonYahoo(sec.yahoo_usuario, sec.yahoo_clave_app) as buzon:
        correos = buzon.correos_desde(datetime.now(UTC) - timedelta(days=7), cfg.carpetas, cfg.remitentes)
    log.info("✔ Yahoo Mail: %d correo(s) de Airbnb en los últimos 7 días", len(correos))
    calendario.comprobar_acceso()
    log.info("✔ Google Calendar: acceso de escritura al calendario %s", cfg.calendar_id)
    if sec.gemini_api_key and cfg.usar_gemini:
        r = analizador.analizar("Hello! We will arrive around 6pm and we need a crib for our baby.", {"enviado_el": "2026-10-08 10:00"})
        if r.error:
            log.error("✘ Gemini: %s", r.error)
            return 1
        log.info("✔ Gemini (%s): %s", cfg.gemini_modelo, r.analisis)
    else:
        log.warning("⚠ Gemini desactivado o sin GEMINI_API_KEY: se usarán palabras clave")
    if os.environ.get("ALERTA_NTFY_TOPIC"):
        log.info("✔ ALERTA_NTFY_TOPIC configurado: los fallos se avisan al desarrollador")
    else:
        log.warning("⚠ Falta el secreto ALERTA_NTFY_TOPIC: si la sincronización falla, NADIE recibirá aviso")
    return 0


def diagnosticar(cfg, sec) -> int:
    with BuzonYahoo(sec.yahoo_usuario, sec.yahoo_clave_app) as buzon:
        correos = buzon.correos_desde(datetime.now(UTC) - timedelta(days=7), cfg.carpetas, cfg.remitentes)
    for c in correos:
        if not es_remitente(c.remitente, cfg.remitentes):
            continue
        d = asdict(parsear_correo(c.asunto, c.texto, c.fecha.date().isoformat(), cfg.alojamientos))
        d["mensajes"] = [{"autor": m["autor"], "texto": m["texto"][:120]} for m in d["mensajes"]]
        print(f"──────── {c.fecha:%Y-%m-%d %H:%M} · {c.remitente}\nAsunto: {c.asunto}")
        print("\n".join(f"  {k}: {v}" for k, v in d.items() if v not in (None, [], False)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:
        log.exception("La ejecución ha fallado")
        if os.environ.get("ALERTA_EMAIL") and os.environ.get("YAHOO_USUARIO"):
            try:   # además del aviso por ntfy del workflow
                NotificadorYahoo(os.environ["YAHOO_USUARIO"], os.environ.get("YAHOO_CLAVE_APP", "").replace(" ", ""),
                                 os.environ["ALERTA_EMAIL"]).enviar(
                    "⚠️ Airbnb → Calendar: la ejecución ha fallado",
                    f"{type(err).__name__}: {err}\n\nDetalles en el registro de GitHub Actions.")
            except Exception:                        # noqa: BLE001
                log.warning("No se pudo enviar el aviso de fallo por correo")
        sys.exit(1)
