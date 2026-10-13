#!/usr/bin/env python3
"""
Airbnb (Yahoo Mail) → Google Calendar.

  python sincronizar.py                 procesa los correos nuevos (lo que ejecuta GitHub Actions)
  python sincronizar.py --comprobar     comprueba Yahoo, Google Calendar y Gemini sin escribir nada
  python sincronizar.py --diagnosticar  muestra lo que se extrae de los correos de Airbnb de los
                                        últimos 7 días, SIN tocar el calendario ni el registro
  python sincronizar.py --purgar --dry-run   lista los eventos creados por este script
  python sincronizar.py --purgar             los borra (pide confirmación; --si para no preguntar).
                                        Solo toca eventos con las marcas privadas del script.
                                        Para volver a cargarlos: --desde yyyy-mm-dd
  python sincronizar.py --migrar [--dry-run] convierte las reservas del formato anterior (check-in y
                                        check-out por separado) en un único evento y recalcula limpiezas
  python sincronizar.py --recalcular [--dry-run] aplica las reglas actuales (horario estándar, limpieza)
                                        a todas las reservas que entran desde hoy, sin leer correos

Secretos (variables de entorno): YAHOO_USUARIO, YAHOO_CLAVE_APP,
GOOGLE_SERVICE_ACCOUNT_JSON y, opcionales, GEMINI_API_KEY y ALERTA_NTFY_TOPIC
(avisos técnicos solo al desarrollador). En GitHub Actions se escribe un resumen
de cada ejecución en GITHUB_STEP_SUMMARY.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from airbnb_calendar.calendario import Calendario, sesion_cuenta_servicio
from airbnb_calendar.config import Secretos, cargar_config
from airbnb_calendar.gemini import AnalizadorMensajes
from airbnb_calendar.ntfy import NotificadorNtfy
from airbnb_calendar.parser import parsear_correo
from airbnb_calendar.registro import Registro
from airbnb_calendar.sincronizador import Sincronizador
from airbnb_calendar.yahoo import BuzonYahoo, NotificadorYahoo, es_remitente

log = logging.getLogger("sincronizar")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--estado", default="estado/estado.json", help="registro de correos procesados")
    ap.add_argument("--desde", help="yyyy-mm-dd: procesa los correos desde esa fecha (carga manual)")
    ap.add_argument("--hasta", help="yyyy-mm-dd (excluida): límite de la carga manual")
    modo = ap.add_mutually_exclusive_group()
    modo.add_argument("--comprobar", action="store_true")
    modo.add_argument("--diagnosticar", action="store_true")
    modo.add_argument("--purgar", action="store_true", help="borra los eventos creados por este script")
    modo.add_argument("--migrar", action="store_true", help="unifica las reservas del formato anterior")
    modo.add_argument("--recalcular", action="store_true", help="aplica las reglas actuales a las reservas futuras")
    ap.add_argument("--dry-run", action="store_true", help="con --purgar, --migrar o --recalcular: solo lista, no cambia nada")
    ap.add_argument("--si", action="store_true", help="con --purgar: no pide confirmación")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    cfg = cargar_config(args.config)
    sec = None if args.purgar or args.migrar or args.recalcular else Secretos.desde_entorno()

    if args.diagnosticar:
        return diagnosticar(cfg, sec)

    if args.purgar:
        return purgar(Calendario(cfg, sesion_cuenta_servicio(Secretos.google_desde_entorno())),
                      simulacion=args.dry_run, confirmado=args.si)
    if args.migrar:
        return migrar(Calendario(cfg, sesion_cuenta_servicio(Secretos.google_desde_entorno())), args.dry_run)
    if args.recalcular:
        return recalcular(Calendario(cfg, sesion_cuenta_servicio(Secretos.google_desde_entorno())), args.dry_run)

    calendario = Calendario(cfg, sesion_cuenta_servicio(sec.google_service_account_json))
    analizador = AnalizadorMensajes(cfg, sec.gemini_api_key)
    if args.comprobar:
        return comprobar(cfg, sec, calendario, analizador)

    registro = Registro(args.estado)
    notificador = NotificadorYahoo(sec.yahoo_usuario, sec.yahoo_clave_app, cfg.email_avisos)
    try:
        with BuzonYahoo(sec.yahoo_usuario, sec.yahoo_clave_app) as buzon:
            fecha = lambda t: datetime.fromisoformat(t).replace(tzinfo=timezone.utc) if t else None   # noqa: E731
            if args.desde:
                notificador = NotificadorSilencioso()   # carga manual: no avisar de correos antiguos
            sincro = Sincronizador(cfg, buzon, calendario, analizador, notificador, registro,
                                   desde=fecha(args.desde), hasta=fecha(args.hasta),
                                   notificador_tecnico=NotificadorNtfy(os.environ.get("ALERTA_NTFY_TOPIC", "")))
            sincro.ejecutar()
            escribir_resumen(sincro.resumen, calendario.stats)
    finally:
        registro.guardar()      # lo procesado hasta un posible fallo queda registrado
    return 0


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


def migrar(calendario: Calendario, simulacion: bool) -> int:
    n = calendario.migrar(simulacion)
    if not simulacion:
        calendario.recalcular_limpiezas(date.today())
    log.info("✔ %d reserva(s) del formato anterior %s", n, "por migrar (simulación)" if simulacion else "migradas")
    return 0


def recalcular(calendario: Calendario, simulacion: bool) -> int:
    alertas = calendario.recalcular_limpiezas(date.today(), dias=None, simulacion=simulacion)
    log.info("✔ %d reserva(s) futura(s) %s · %d con alerta de limpieza", calendario.stats["limpiezas_recalculadas"],
             "por actualizar (simulación)" if simulacion else "actualizadas", len(alertas))
    return 0


class NotificadorSilencioso:
    def enviar(self, asunto: str, cuerpo: str) -> None:
        log.info("Aviso omitido (carga manual): %s", asunto)


def purgar(calendario: Calendario, simulacion: bool, confirmado: bool) -> int:
    eventos = calendario.eventos_propios()
    for ev in eventos:
        inicio = ((ev.get("start") or {}).get("dateTime") or "")[:16].replace("T", " ")
        print(f"  {inicio}  {ev.get('summary', '')}")
    log.info("%d evento(s) creados por Airbnb → Calendar", len(eventos))
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
        correos = buzon.correos_desde(datetime.now(timezone.utc) - timedelta(days=7), cfg.carpetas, cfg.remitentes)
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
        correos = buzon.correos_desde(datetime.now(timezone.utc) - timedelta(days=7), cfg.carpetas, cfg.remitentes)
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
    except Exception:
        log.exception("La ejecución ha fallado")
        sys.exit(1)
