#!/usr/bin/env python3
"""
Airbnb (Yahoo Mail) → Google Calendar.

  python sincronizar.py                 procesa los correos nuevos (lo que ejecuta GitHub Actions)
  python sincronizar.py --comprobar     comprueba Yahoo, Google Calendar y Gemini sin escribir nada
  python sincronizar.py --diagnosticar  muestra lo que se extrae de los correos de Airbnb de los
                                        últimos 7 días, SIN tocar el calendario ni el registro

Secretos (variables de entorno): YAHOO_USUARIO, YAHOO_CLAVE_APP,
GOOGLE_SERVICE_ACCOUNT_JSON y, opcional, GEMINI_API_KEY.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from airbnb_calendar.calendario import Calendario, sesion_cuenta_servicio
from airbnb_calendar.config import Secretos, cargar_config
from airbnb_calendar.gemini import AnalizadorMensajes
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
    modo = ap.add_mutually_exclusive_group()
    modo.add_argument("--comprobar", action="store_true")
    modo.add_argument("--diagnosticar", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    cfg = cargar_config(args.config)
    sec = Secretos.desde_entorno()

    if args.diagnosticar:
        return diagnosticar(cfg, sec)

    calendario = Calendario(cfg, sesion_cuenta_servicio(sec.google_service_account_json))
    analizador = AnalizadorMensajes(cfg, sec.gemini_api_key)
    if args.comprobar:
        return comprobar(cfg, sec, calendario, analizador)

    registro = Registro(args.estado)
    notificador = NotificadorYahoo(sec.yahoo_usuario, sec.yahoo_clave_app, cfg.email_avisos)
    try:
        with BuzonYahoo(sec.yahoo_usuario, sec.yahoo_clave_app) as buzon:
            desde = datetime.fromisoformat(args.desde).replace(tzinfo=timezone.utc) if args.desde else None
            Sincronizador(cfg, buzon, calendario, analizador, notificador, registro, desde=desde).ejecutar()
    finally:
        registro.guardar()      # lo procesado hasta un posible fallo queda registrado
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
