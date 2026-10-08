"""
Google Calendar (API REST v3) con una cuenta de servicio.

Cada reserva = 2 eventos cortos (entrada y salida). Los eventos llevan
propiedades privadas (no visibles en el calendario) para encontrarlos después
y el estado completo de la reserva en JSON.

⚠️ Si se edita a mano el título o la descripción de un evento, se sobrescribirán
cuando llegue un nuevo correo de esa reserva.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .config import Alojamiento, Config
from .parser import clave_normalizada
from .reglas import descripcion_evento, titulo_evento

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/calendar/v3/calendars/{cal}/events"
ALCANCE = "https://www.googleapis.com/auth/calendar.events"

P_TIPO = "airbnb_tipo"               # 'entrada' | 'salida'
P_CODIGO = "airbnb_codigo"
P_ALOJAMIENTO = "airbnb_alojamiento"  # hash del nombre normalizado (los valores de filtro deben ser simples)
P_CHECKIN = "airbnb_checkin"
P_ESTADO_N = "airbnb_estado_n"        # nº de trozos del JSON
P_ESTADO = "airbnb_estado_"           # airbnb_estado_0, _1, ...
TAM_TROZO = 900                       # los valores de una propiedad admiten hasta 1024 caracteres
REINTENTABLES = {429, 500, 502, 503, 504}
ESPERAS_S = (2, 5, 15)                # backoff ante cortes momentáneos de Google


def sesion_cuenta_servicio(json_credenciales: str):
    """Sesión HTTP autenticada con la cuenta de servicio (JSON completo de la clave)."""
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account
    info = json.loads(json_credenciales)
    cred = service_account.Credentials.from_service_account_info(info, scopes=[ALCANCE])
    return AuthorizedSession(cred)


class Calendario:
    def __init__(self, cfg: Config, sesion, dormir=time.sleep):
        self.cfg = cfg
        self.sesion = sesion
        self.dormir = dormir
        self.url = API.format(cal=quote(cfg.calendar_id, safe=""))
        self.tz = ZoneInfo(cfg.zona_horaria)

    # ------------------------------------------------------------------ lectura

    def comprobar_acceso(self) -> None:
        self._pedir("GET", self.url, params={"maxResults": 1})

    def buscar_eventos_reserva(self, codigo, alojamiento, checkin, checkout) -> dict:
        """{'entrada': evento|None, 'salida': evento|None}. Por código si lo hay; si no, por alojamiento + entrada."""
        res = {"entrada": None, "salida": None}
        candidatos = []
        if codigo:
            candidatos += self._listar({P_CODIGO: codigo})
        if alojamiento and checkin:
            candidatos += self._listar({P_ALOJAMIENTO: _hash(alojamiento), P_CHECKIN: checkin})
        for ev in candidatos:
            p = _props(ev)
            if codigo and p.get(P_CODIGO) and p[P_CODIGO] != codigo:
                continue       # misma fecha y piso, pero otra reserva
            tipo = p.get(P_TIPO)
            if tipo in res and res[tipo] is None:
                res[tipo] = ev
        return res

    def _listar(self, filtros: dict) -> list[dict]:
        params = [("privateExtendedProperty", f"{k}={v}") for k, v in filtros.items()]
        params += [("singleEvents", "true"), ("showDeleted", "false"), ("maxResults", "50")]
        return self._pedir("GET", self.url, params=params).get("items", [])

    # ---------------------------------------------------------------- escritura

    def guardar_reserva(self, estado: dict, eventos: dict, alojamiento: Alojamiento | None) -> dict:
        """Crea o actualiza los dos eventos con el estado actual de la reserva."""
        actualizado = datetime.now(self.tz).strftime("%d/%m/%Y %H:%M")
        descripcion = descripcion_evento(estado, actualizado)
        for tipo in ("entrada", "salida"):
            fecha = estado["checkin"] if tipo == "entrada" else estado["checkout"]
            if not fecha:
                continue
            hora = (estado["hora_llegada"] or estado["hora_checkin"] or self.cfg.hora_entrada_por_defecto if tipo == "entrada"
                    else estado["hora_salida"] or estado["hora_checkout"] or self.cfg.hora_salida_por_defecto)
            inicio = datetime.fromisoformat(f"{fecha}T{hora}")
            fin = inicio + timedelta(minutes=self.cfg.duracion_evento_min)
            cuerpo = {
                "summary": titulo_evento(estado, tipo),
                "description": descripcion,
                "start": {"dateTime": inicio.isoformat(), "timeZone": self.cfg.zona_horaria},
                "end": {"dateTime": fin.isoformat(), "timeZone": self.cfg.zona_horaria},
                "colorId": self.cfg.colores["cancelada" if estado["cancelada"] else tipo],
                "extendedProperties": {"private": self._propiedades(estado, tipo, eventos.get(tipo))},
            }
            ev = eventos.get(tipo)
            if ev is None:
                if alojamiento and alojamiento.direccion:
                    cuerpo["location"] = alojamiento.direccion
                if self.cfg.recordatorios_minutos is not None:
                    cuerpo["reminders"] = {"useDefault": False, "overrides": [
                        {"method": "popup", "minutes": m} for m in self.cfg.recordatorios_minutos]}
                eventos[tipo] = self._pedir("POST", self.url, json=cuerpo)
                log.info("Evento creado: %s (%s %s)", cuerpo["summary"], fecha, hora)
            else:
                eventos[tipo] = self._pedir("PATCH", f"{self.url}/{ev['id']}", json=cuerpo)
                log.info("Evento actualizado: %s", cuerpo["summary"])
        return eventos

    def _propiedades(self, estado: dict, tipo: str, anterior: dict | None) -> dict:
        datos = json.dumps(estado, ensure_ascii=False, separators=(",", ":"))
        trozos = [datos[i:i + TAM_TROZO] for i in range(0, len(datos), TAM_TROZO)] or [""]
        p = {
            P_TIPO: tipo,
            P_ALOJAMIENTO: _hash(estado["alojamiento"]),
            P_CHECKIN: estado["checkin"] or "",
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

    def _pedir(self, metodo: str, url: str, **kw) -> dict:
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
        if resp.status_code >= 400:
            ayuda = ""
            if resp.status_code in (403, 404):
                ayuda = (" — ¿Has compartido el calendario con el email de la cuenta de servicio"
                         " («Hacer cambios en eventos») y es correcto [calendario] id?")
            raise RuntimeError(f"Google Calendar {metodo} → HTTP {resp.status_code}: {resp.text[:300]}{ayuda}")
        return resp.json() if resp.content else {}


def leer_estado(eventos: dict) -> dict | None:
    """Estado JSON guardado en el evento de entrada (o, si no, en el de salida)."""
    for ev in (eventos.get("entrada"), eventos.get("salida")):
        p = _props(ev)
        n = int(p.get(P_ESTADO_N) or 0)
        if not n:
            continue
        try:
            return json.loads("".join(p.get(f"{P_ESTADO}{i}") or "" for i in range(n)))
        except json.JSONDecodeError as e:
            log.warning("Estado ilegible en el evento %s: %s", ev.get("id"), e)
    return None


def _props(ev: dict | None) -> dict:
    return ((ev or {}).get("extendedProperties") or {}).get("private") or {}


def _hash(nombre: str | None) -> str:
    return hashlib.sha1(clave_normalizada(nombre).encode()).hexdigest()[:16]
