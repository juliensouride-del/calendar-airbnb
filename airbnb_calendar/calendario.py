"""
Google Calendar (API REST v3) con una cuenta de servicio.

Cada reserva = UN evento que va de la hora de entrada a la de salida. Si el
huésped anterior del mismo alojamiento sale ese día (o el anterior), el título
lleva [LIMPIEZA OK] o [ALERTA LIMPIEZA / AJUSTE HORARIO].

Los eventos llevan propiedades privadas (no visibles en el calendario) para
encontrarlos después, el estado completo de la reserva en JSON y una marca de
origen (airbnb_origen) que permite borrarlos todos sin tocar otros eventos.

Las reservas del formato anterior (dos eventos: entrada y salida) se convierten
en un único evento la próxima vez que llega un correo suyo.

⚠️ Si se edita a mano el título, la descripción o la hora de un evento, se
sobrescribirán. Para fijar una hora acordada con el huésped se escribe en la
descripción '#entrada HH:MM' o '#salida HH:MM': esa hora manda y se conserva.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from . import reglas
from .config import Alojamiento, Config
from .parser import clave_normalizada

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/calendar/v3/calendars/{cal}/events"
ALCANCE = "https://www.googleapis.com/auth/calendar.events"

P_ORIGEN = "airbnb_origen"            # marca de los eventos creados por este script
ORIGEN = "airbnb-calendar"
P_TIPO = "airbnb_tipo"                # 'reserva' (formato anterior: 'entrada' | 'salida')
TIPOS_PROPIOS = ("reserva", "entrada", "salida")
P_CODIGO = "airbnb_codigo"
P_ALOJAMIENTO = "airbnb_alojamiento"  # hash del nombre normalizado (los valores de filtro deben ser simples)
P_CHECKIN = "airbnb_checkin"
P_CHECKOUT = "airbnb_checkout"
P_LIMPIEZA = "airbnb_limpieza"        # última evaluación de la limpieza (para no reescribir si no cambia)
P_ESTADO_N = "airbnb_estado_n"        # nº de trozos del JSON
P_ESTADO = "airbnb_estado_"           # airbnb_estado_0, _1, ...
TAM_TROZO = 900                       # los valores de una propiedad admiten hasta 1024 caracteres
REINTENTABLES = {429, 500, 502, 503, 504}
ESPERAS_S = (2, 5, 15)                # backoff ante cortes momentáneos de Google


def sesion_cuenta_servicio(json_credenciales: str):
    """Sesión HTTP autenticada con la cuenta de servicio (JSON completo de la clave)."""
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account
    try:
        info = json.loads(json_credenciales)
    except json.JSONDecodeError as e:
        raise SystemExit(f"GOOGLE_SERVICE_ACCOUNT_JSON no es un JSON válido ({e}). Pega en el secreto el "
                         "contenido COMPLETO del archivo .json de la clave, empezando por la llave de apertura.") from None
    cred = service_account.Credentials.from_service_account_info(info, scopes=[ALCANCE])
    return AuthorizedSession(cred)


class Calendario:
    def __init__(self, cfg: Config, sesion, dormir=time.sleep):
        self.cfg = cfg
        self.sesion = sesion
        self.dormir = dormir
        self.url = API.format(cal=quote(cfg.calendar_id, safe=""))
        self.tz = ZoneInfo(cfg.zona_horaria)
        self.stats = {"creados": 0, "actualizados": 0, "limpiezas_recalculadas": 0}

    # ------------------------------------------------------------------ lectura

    def comprobar_acceso(self) -> None:
        self._pedir("GET", self.url, params={"maxResults": 1})

    def buscar_eventos_reserva(self, codigo, alojamiento, checkin, checkout) -> dict:
        """
        {'evento': evento|None, 'sobrantes': [...]}. Por código si lo hay; si no, por alojamiento + entrada.
        'sobrantes' son eventos de más de la misma reserva (el check-out del formato anterior).
        """
        candidatos = self._listar({P_CODIGO: codigo}) if codigo else []
        # Fallback por alojamiento + entrada (p. ej. evento creado desde un mensaje, sin código)
        if not candidatos and alojamiento and checkin:
            candidatos = self._listar({P_ALOJAMIENTO: _hash(alojamiento), P_CHECKIN: checkin})
        propios = [ev for ev in candidatos if _props(ev).get(P_TIPO) in TIPOS_PROPIOS
                   and not (codigo and _props(ev).get(P_CODIGO) not in (None, codigo))]   # misma fecha, otra reserva
        propios.sort(key=lambda ev: TIPOS_PROPIOS.index(_props(ev)[P_TIPO]))
        return {"evento": propios[0] if propios else None, "sobrantes": propios[1:]}

    def eventos_propios(self) -> list[dict]:
        """Todos los eventos creados por este script (también los del formato anterior, sin marca de origen)."""
        vistos: dict[str, dict] = {}
        for filtros in ({P_ORIGEN: ORIGEN}, *({P_TIPO: t} for t in TIPOS_PROPIOS)):
            for ev in self._listar(filtros):
                if _props(ev).get(P_TIPO) in TIPOS_PROPIOS:      # doble comprobación antes de borrar nada
                    vistos[ev["id"]] = ev
        return sorted(vistos.values(), key=lambda ev: (ev.get("start") or {}).get("dateTime") or "")

    def _listar(self, filtros: dict, **extra) -> list[dict]:
        params = [("privateExtendedProperty", f"{k}={v}") for k, v in filtros.items()]
        params += [("singleEvents", "true"), ("showDeleted", "false"), ("maxResults", "250"), *extra.items()]
        items, token = [], None
        while True:
            r = self._pedir("GET", self.url, params=params + ([("pageToken", token)] if token else []))
            items += r.get("items", [])
            token = r.get("nextPageToken")
            if not token:
                return items

    # ---------------------------------------------------------------- escritura

    def guardar_reserva(self, estado: dict, eventos: dict, alojamiento: Alojamiento | None) -> dict:
        """Crea o actualiza el evento de la reserva y recalcula la limpieza de la reserva siguiente."""
        if not estado["checkin"]:
            return eventos
        limpieza = reglas.evaluar_limpieza(self._reserva_anterior(estado), estado, self.cfg)
        inicio, fin = self._horario(estado)
        ev = eventos.get("evento")
        cuerpo = {
            **self._contenido(estado, limpieza),
            **self._fechas(inicio, fin),
            "extendedProperties": {"private": self._propiedades(estado, limpieza, ev)},
        }
        if ev is None:
            if alojamiento and alojamiento.direccion:
                cuerpo["location"] = alojamiento.direccion
            if self.cfg.recordatorios_minutos is not None:
                cuerpo["reminders"] = {"useDefault": False, "overrides": [
                    {"method": "popup", "minutes": m} for m in self.cfg.recordatorios_minutos]}
            eventos["evento"] = self._pedir("POST", self.url, json=cuerpo)
            self.stats["creados"] += 1
            log.info("Evento creado: %s (%s → %s)", cuerpo["summary"], inicio, fin)
        else:
            eventos["evento"] = self._pedir("PATCH", f"{self.url}/{ev['id']}", json=cuerpo)
            self.stats["actualizados"] += 1
            log.info("Evento actualizado: %s", cuerpo["summary"])
        for sobrante in eventos.get("sobrantes") or []:
            self.borrar_evento(sobrante)
            log.info("Evento del formato anterior eliminado (reserva unificada): %s", sobrante.get("summary"))
        eventos["sobrantes"] = []
        self._actualizar_siguientes(estado)
        return eventos

    def _horario(self, estado: dict) -> tuple[datetime, datetime]:
        """Inicio y fin del evento según las reglas de horario actuales (hora local, sin zona)."""
        inicio = datetime.fromisoformat(f"{estado['checkin']}T{reglas.hora_entrada(estado, self.cfg)}")
        fin = (datetime.fromisoformat(f"{estado['checkout']}T{reglas.hora_salida(estado, self.cfg)}")
               if estado["checkout"] else inicio)
        if fin <= inicio:
            fin = inicio + timedelta(minutes=self.cfg.duracion_evento_min)
        return inicio, fin

    def _fechas(self, inicio: datetime, fin: datetime) -> dict:
        return {"start": {"dateTime": inicio.isoformat(), "timeZone": self.cfg.zona_horaria},
                "end": {"dateTime": fin.isoformat(), "timeZone": self.cfg.zona_horaria}}

    def _horario_distinto(self, ev: dict, estado: dict) -> bool:
        """¿La hora del evento no es la que le toca con las reglas actuales?"""
        def leer(campo):
            t = (ev.get(campo) or {}).get("dateTime")
            if not t:
                return None
            d = datetime.fromisoformat(t.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=self.tz)
        inicio, fin = (d.replace(tzinfo=self.tz) for d in self._horario(estado))
        return leer("start") != inicio or leer("end") != fin

    def borrar_evento(self, ev: dict) -> None:
        self._pedir("DELETE", f"{self.url}/{ev['id']}", ignorar=(404, 410))

    def _contenido(self, estado: dict, limpieza: dict | None) -> dict:
        actualizado = datetime.now(self.tz).strftime("%d/%m/%Y %H:%M")
        color = ("cancelada" if estado["cancelada"] else
                 "alerta_limpieza" if limpieza and not limpieza["ok"] else "reserva")
        return {
            "summary": reglas.titulo_evento(estado, limpieza),
            "description": reglas.descripcion_evento(estado, actualizado, limpieza, self.cfg),
            "colorId": self.cfg.colores[color],
        }

    def _reservas(self, alojamiento: str, propiedad: str, dia: date) -> list[tuple[dict, dict]]:
        """(evento, estado) de las reservas del alojamiento con esa fecha de entrada o salida."""
        evs = self._listar({P_TIPO: "reserva", P_ALOJAMIENTO: _hash(alojamiento), propiedad: dia.isoformat()})
        return [(ev, e) for ev in evs if (e := leer_estado({"evento": ev}))]

    def _reserva_anterior(self, estado: dict) -> dict | None:
        """Reserva del mismo alojamiento que sale el día de entrada (o, si no hay, el día anterior)."""
        if not estado["alojamiento"]:
            return None
        d = date.fromisoformat(estado["checkin"])
        candidatas = [e for dia in (d, d - timedelta(days=1))
                      for _, e in self._reservas(estado["alojamiento"], P_CHECKOUT, dia)]
        return _anterior_en(candidatas, estado, self.cfg)

    def _actualizar_siguientes(self, estado: dict) -> None:
        """Las reservas que entran el día de salida (o el siguiente) dependen de esta salida: se recalculan."""
        if not estado["alojamiento"] or not estado["checkout"]:
            return
        d = date.fromisoformat(estado["checkout"])
        for dia in (d, d + timedelta(days=1)):
            for ev, sig in self._reservas(estado["alojamiento"], P_CHECKIN, dia):
                if _misma_reserva(sig, estado):
                    continue
                limpieza = reglas.evaluar_limpieza(self._reserva_anterior(sig), sig, self.cfg)
                if _props(ev).get(P_LIMPIEZA, "") == _huella(limpieza) and not self._horario_distinto(ev, sig):
                    continue
                self._escribir_limpieza(ev, sig, limpieza)

    def _escribir_limpieza(self, ev: dict, estado: dict, limpieza: dict | None) -> None:
        """Reescribe texto, color y horario del evento (el horario siempre con las reglas actuales)."""
        cuerpo = {**self._contenido(estado, limpieza), **self._fechas(*self._horario(estado)),
                  "extendedProperties": {"private": {P_LIMPIEZA: _huella(limpieza)}}}
        self._pedir("PATCH", f"{self.url}/{ev['id']}", json=cuerpo)
        self.stats["limpiezas_recalculadas"] += 1
        log.info("Limpieza recalculada: %s", cuerpo["summary"])

    # ------------------------------------------------------- tareas periódicas

    def recalcular_limpiezas(self, hoy: date, dias: int | None = 30, simulacion: bool = False) -> list[dict]:
        """
        Recalcula limpieza y horario de las reservas que entran entre hoy y dentro de `dias` (None = todas),
        sin depender de que llegue un correo (cambios de fechas o de reglas, cancelaciones, reservas
        migradas). Una sola consulta. Devuelve los estados de las reservas con alerta de limpieza.
        """
        rango = {"timeMin": f"{hoy - timedelta(days=2)}T00:00:00Z"}
        if dias is not None:
            rango["timeMax"] = f"{hoy + timedelta(days=dias)}T00:00:00Z"
        evs = self._listar({P_TIPO: "reserva"}, **rango)
        reservas = [(ev, e) for ev in evs if (e := leer_estado({"evento": ev})) and e["checkin"]]
        alertas = []
        for ev, e in reservas:
            if e["checkin"] < hoy.isoformat() or not e["alojamiento"]:
                continue
            mismas = [o for _, o in reservas if _hash(o["alojamiento"]) == _hash(e["alojamiento"])]
            limpieza = reglas.evaluar_limpieza(_anterior_en(mismas, e, self.cfg), e, self.cfg)
            if _props(ev).get(P_LIMPIEZA, "") != _huella(limpieza) or self._horario_distinto(ev, e):
                if simulacion:
                    inicio, fin = self._horario(e)
                    log.info("Se actualizaría: %s → %s (%s → %s)", ev.get("summary"),
                             reglas.titulo_evento(e, limpieza), inicio, fin)
                    self.stats["limpiezas_recalculadas"] += 1
                else:
                    self._escribir_limpieza(ev, e, limpieza)
            if limpieza and not limpieza["ok"]:
                alertas.append(e)
        return alertas

    def migrar(self, simulacion: bool = False) -> int:
        """Convierte las reservas del formato anterior (entrada + salida) en un único evento."""
        grupos: dict[str, list[dict]] = {}
        for ev in self.eventos_propios():
            p = _props(ev)
            if p.get(P_TIPO) in ("entrada", "salida"):
                clave = p.get(P_CODIGO) or f"{p.get(P_ALOJAMIENTO)}|{p.get(P_CHECKIN)}"
                grupos.setdefault(clave, []).append(ev)
        for clave, evs in grupos.items():
            evs.sort(key=lambda ev: TIPOS_PROPIOS.index(_props(ev)[P_TIPO]))
            estado = leer_estado({"evento": evs[0], "sobrantes": evs[1:]})
            if not estado:
                log.warning("Reserva %s sin estado legible: no se migra", clave)
                continue
            log.info("%s %s", "Se migraría" if simulacion else "Migrando", evs[0].get("summary"))
            if not simulacion:
                self.guardar_reserva(estado, {"evento": evs[0], "sobrantes": evs[1:]},
                                     self.cfg.alojamiento_por_nombre(estado["alojamiento"]))
        return len(grupos)

    def _propiedades(self, estado: dict, limpieza: dict | None, anterior: dict | None) -> dict:
        datos = json.dumps(estado, ensure_ascii=False, separators=(",", ":"))
        trozos = [datos[i:i + TAM_TROZO] for i in range(0, len(datos), TAM_TROZO)] or [""]
        p = {
            P_ORIGEN: ORIGEN,
            P_TIPO: "reserva",
            P_ALOJAMIENTO: _hash(estado["alojamiento"]),
            P_CHECKIN: estado["checkin"] or "",
            P_CHECKOUT: estado["checkout"] or "",
            P_LIMPIEZA: _huella(limpieza),
            P_ESTADO_N: str(len(trozos)),
            **{f"{P_ESTADO}{i}": t for i, t in enumerate(trozos)},
        }
        if estado["codigo"]:
            p[P_CODIGO] = estado["codigo"]
        # PATCH fusiona las propiedades: los trozos sobrantes de un estado anterior más largo se borran con null
        n_anterior = int(_props(anterior).get(P_ESTADO_N) or 0) if anterior else 0
        p.update({f"{P_ESTADO}{i}": None for i in range(len(trozos), n_anterior)})
        return p

    # -------------------------------------------------------------------- HTTP

    def _pedir(self, metodo: str, url: str, ignorar: tuple = (), **kw) -> dict:
        for espera in (*ESPERAS_S, None):
            try:
                resp = self.sesion.request(metodo, url, timeout=30, **kw)
            except OSError as e:          # los errores de red de requests heredan de OSError
                if espera is None:
                    raise
                log.warning("Google Calendar %s: %s — reintento en %ss", metodo, e, espera)
            else:
                if resp.status_code not in REINTENTABLES or espera is None:
                    break
                log.warning("Google Calendar %s → HTTP %s — reintento en %ss", metodo, resp.status_code, espera)
            self.dormir(espera)
        if resp.status_code in ignorar:
            return {}
        if resp.status_code >= 400:
            ayuda = ""
            if resp.status_code in (403, 404):
                ayuda = (" — ¿Has compartido el calendario con el email de la cuenta de servicio"
                         " («Hacer cambios en eventos») y es correcto [calendario] id?")
            raise RuntimeError(f"Google Calendar {metodo} → HTTP {resp.status_code}: {resp.text[:300]}{ayuda}")
        return resp.json() if resp.content else {}


def leer_estado(eventos: dict) -> dict | None:
    """Estado JSON guardado en el evento de la reserva (o en los sobrantes del formato anterior)."""
    for ev in (eventos.get("evento"), *(eventos.get("sobrantes") or [])):
        p = _props(ev)
        n = int(p.get(P_ESTADO_N) or 0)
        if not n:
            continue
        try:
            estado = json.loads("".join(p.get(f"{P_ESTADO}{i}") or "" for i in range(n)))
        except json.JSONDecodeError as e:
            log.warning("Estado ilegible en el evento %s: %s", ev.get("id"), e)
            continue
        # Los acuerdos de horario los escribe el anfitrión en la descripción: lo que pone ahí manda
        # (si borra la etiqueta, el acuerdo desaparece)
        estado.update(reglas.leer_acuerdos((eventos.get("evento") or ev).get("description")))
        return estado
    return None


def _anterior_en(candidatas: list[dict], estado: dict, cfg: Config) -> dict | None:
    """De las reservas candidatas, la que sale el día de entrada de `estado` (o, si no hay, el día anterior)."""
    d = date.fromisoformat(estado["checkin"])
    for dia in (d, d - timedelta(days=1)):
        otras = [e for e in candidatas if e.get("checkout") == dia.isoformat()
                 and not e["cancelada"] and not _misma_reserva(e, estado)]
        if otras:
            return max(otras, key=lambda e: reglas.hora_salida(e, cfg))
    return None


def _misma_reserva(a: dict, b: dict) -> bool:
    if a.get("codigo") and b.get("codigo"):
        return a["codigo"] == b["codigo"]
    return a.get("checkin") == b.get("checkin") and a.get("checkout") == b.get("checkout")


def _huella(limpieza: dict | None) -> str:
    return json.dumps(limpieza, sort_keys=True, separators=(",", ":")) if limpieza else ""


def _props(ev: dict | None) -> dict:
    return ((ev or {}).get("extendedProperties") or {}).get("private") or {}


def _hash(nombre: str | None) -> str:
    return hashlib.sha1(clave_normalizada(nombre).encode()).hexdigest()[:16]
