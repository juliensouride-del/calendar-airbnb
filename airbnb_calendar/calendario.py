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

Cambios a mano: el script recuerda (airbnb_escrito) lo último que escribió en cada
campo. Si el anfitrión cambia el título, el color o la hora de inicio o de fin, ese
campo ya no se toca (mover el evento cuenta como horario acordado). La descripción
tiene dos partes separadas por SEPARADOR: lo de encima es del anfitrión y se conserva
siempre; lo de debajo es automático (si lo edita, la descripción pasa a ser suya).
Para fijar una hora acordada también vale escribir '#entrada HH:MM' o '#salida HH:MM'.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from html import unescape
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
P_ESCRITO = "airbnb_escrito"          # huella de lo último que escribió el script (detecta cambios a mano)
# La descripción tiene dos partes: lo que escribe el anfitrión (encima) y lo automático (debajo)
SEPARADOR = "—— Airbnb · automático (escribe tus notas encima de esta línea) ——"
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
        cuerpo = self._cuerpo(ev, estado, limpieza)
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
            log.info("Evento actualizado: %s", eventos["evento"].get("summary") or ev.get("summary"))
        for sobrante in eventos.get("sobrantes") or []:
            self.borrar_evento(sobrante)
            log.info("Evento del formato anterior eliminado (reserva unificada): %s", sobrante.get("summary"))
        eventos["sobrantes"] = []
        self._actualizar_siguientes(estado)
        self._actualizar_lockers_compartidos(estado)
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

    def _cuerpo(self, ev: dict | None, estado: dict, limpieza: dict | None, reservas: list[dict] | None = None) -> dict:
        """
        Lo que hay que escribir en el evento (para PATCH o POST). Lo que el anfitrión haya cambiado a mano
        desde la última escritura del script (título, color, hora de inicio o de fin, o la parte automática
        de la descripción) NO se toca; su parte de la descripción (encima del separador) se conserva siempre.
        """
        self._preparar(estado, reservas)
        inicio, fin = self._horario(estado)
        color = ("cancelada" if estado["cancelada"] else
                 "alerta_limpieza" if limpieza and not limpieza["ok"] else "reserva")
        nuevo = {"summary": reglas.titulo_evento(estado, limpieza), "colorId": self.cfg.colores[color],
                 "inicio": _local(inicio), "fin": _local(fin),
                 "auto": reglas.descripcion_evento(estado, limpieza, self.cfg)}
        previo = _escrito(ev)
        actual, manual = self._leer_evento(ev)
        escrito, cuerpo = {}, {}
        for k, v in nuevo.items():
            if previo and k in previo and _h(actual.get(k)) != previo[k]:
                escrito[k] = previo[k]              # cambiado a mano: se respeta (y lo seguirá siendo)
                continue
            escrito[k] = _h(v)
            if k in ("inicio", "fin"):
                d = datetime.fromisoformat(v)
                cuerpo["start" if k == "inicio" else "end"] = {"dateTime": d.isoformat(), "timeZone": self.cfg.zona_horaria}
            elif k == "auto":
                cuerpo["description"] = (manual + "\n\n" if manual else "") + SEPARADOR + "\n" + v
            else:
                cuerpo[k] = v
        props = self._propiedades(estado, limpieza, ev)
        props[P_ESCRITO] = json.dumps(escrito, separators=(",", ":"))
        cuerpo["extendedProperties"] = {"private": props}
        return cuerpo

    def _leer_evento(self, ev: dict | None) -> tuple[dict, str]:
        """(valores actuales comparables con los que escribe el script, parte manual de la descripción)."""
        if not ev:
            return {}, ""
        desc = _texto_plano(ev.get("description") or "")
        if SEPARADOR in desc:
            manual, auto = desc.split(SEPARADOR, 1)
            manual, auto = manual.strip(), auto.lstrip("\n")
        elif _escrito(ev):
            manual, auto = "", None          # el anfitrión quitó el separador: la descripción es suya
        else:
            # Evento del formato anterior: todo era automático salvo las etiquetas de acuerdo
            texto = re.sub(r"<[^>]+>", "\n", desc)
            manual = "\n".join(m.group(0) for m in reglas.ETIQUETA_ACUERDO.finditer(texto))
            auto = desc
        actual = {"summary": ev.get("summary"), "colorId": ev.get("colorId"), "auto": auto,
                  "inicio": _local_evento(ev.get("start"), self.tz), "fin": _local_evento(ev.get("end"), self.tz)}
        return actual, manual

    def _preparar(self, estado: dict, reservas: list[dict] | None = None) -> None:
        """Datos que dependen de config.toml y de otras reservas: nombre corto y locker."""
        alojamiento = self.cfg.alojamiento_por_nombre(estado["alojamiento"])
        if not alojamiento:
            return
        # El nombre corto siempre sale de config.toml (si se cambia allí, se corrigen todos los eventos)
        estado["alias"] = alojamiento.alias or alojamiento.nombre
        estado["locker"] = alojamiento.locker or None
        otro = self.cfg.alojamiento_por_nombre(alojamiento.locker_coincide_con)
        if otro and alojamiento.locker_si_coincide and estado["checkin"]:
            if reservas is None:
                reservas = [e for _, e in self._reservas(otro.nombre, P_CHECKIN, date.fromisoformat(estado["checkin"]))]
            if any(_hash(o["alojamiento"]) == _hash(otro.nombre) and o.get("checkin") == estado["checkin"]
                   and not o.get("cancelada") for o in reservas):
                estado["locker"] = alojamiento.locker_si_coincide

    def _desactualizado(self, ev: dict, estado: dict, limpieza: dict | None, reservas: list[dict] | None = None) -> bool:
        """¿Hay algo que escribir en el evento (sin contar lo cambiado a mano)?"""
        cuerpo = self._cuerpo(ev, dict(estado), limpieza, reservas)
        p_nuevas, p_ev = cuerpo["extendedProperties"]["private"], _props(ev)
        if any(p_ev.get(k) != v for k, v in p_nuevas.items() if not k.startswith(P_ESTADO)):
            return True
        actual, _ = self._leer_evento(ev)
        for k, v in cuerpo.items():
            if k in ("start", "end"):
                if actual["inicio" if k == "start" else "fin"] != _local(datetime.fromisoformat(v["dateTime"])):
                    return True
            elif k != "extendedProperties" and (ev.get(k) or "") != v:
                return True
        return False

    def borrar_evento(self, ev: dict) -> None:
        self._pedir("DELETE", f"{self.url}/{ev['id']}", ignorar=(404, 410))

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
                if not self._desactualizado(ev, sig, limpieza):
                    continue
                self._escribir_limpieza(ev, sig, limpieza)

    def _escribir_limpieza(self, ev: dict, estado: dict, limpieza: dict | None, reservas: list[dict] | None = None) -> None:
        """Reescribe el evento con las reglas actuales (sin tocar lo cambiado a mano)."""
        cuerpo = self._cuerpo(ev, estado, limpieza, reservas)
        self._pedir("PATCH", f"{self.url}/{ev['id']}", json=cuerpo)
        self.stats["limpiezas_recalculadas"] += 1
        log.info("Evento puesto al día: %s", cuerpo.get("summary") or ev.get("summary"))

    def _actualizar_lockers_compartidos(self, estado: dict) -> None:
        """Si otro alojamiento comparte locker con este (p. ej. Torrecilla con Buenavista), su locker
        depende de que este tenga entrada el mismo día: se ponen al día sus reservas de ese día."""
        if not estado["alojamiento"] or not estado["checkin"]:
            return
        este = self.cfg.alojamiento_por_nombre(estado["alojamiento"])
        for a in self.cfg.alojamientos:
            if not este or self.cfg.alojamiento_por_nombre(a.locker_coincide_con) is not este:
                continue
            for ev, otra in self._reservas(a.nombre, P_CHECKIN, date.fromisoformat(estado["checkin"])):
                limpieza = reglas.evaluar_limpieza(self._reserva_anterior(otra), otra, self.cfg)
                if self._desactualizado(ev, otra, limpieza):
                    self._escribir_limpieza(ev, otra, limpieza)

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
        self.stats["reservas_revisadas"] = 0
        for ev, e in reservas:
            if e["checkin"] < hoy.isoformat() or not e["alojamiento"]:
                continue
            self.stats["reservas_revisadas"] += 1
            mismas = [o for _, o in reservas if _hash(o["alojamiento"]) == _hash(e["alojamiento"])]
            limpieza = reglas.evaluar_limpieza(_anterior_en(mismas, e, self.cfg), e, self.cfg)
            todas = [o for _, o in reservas]
            if self._desactualizado(ev, e, limpieza, todas):
                if simulacion:
                    inicio, fin = self._horario(e)
                    log.info("Se actualizaría: %s → %s (%s → %s)", ev.get("summary"),
                             reglas.titulo_evento(e, limpieza), inicio, fin)
                    self.stats["limpiezas_recalculadas"] += 1
                else:
                    self._escribir_limpieza(ev, e, limpieza, todas)
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
        principal = eventos.get("evento") or ev
        # Solo la parte del anfitrión (encima del separador): la automática sugiere "#entrada 12:30" y no es un acuerdo
        desc = _texto_plano(principal.get("description") or "")
        estado.update(reglas.leer_acuerdos(desc.split(SEPARADOR, 1)[0]))
        # Mover el evento a mano en Calendar también es un horario acordado (salvo que haya etiqueta)
        escrito = _escrito(principal)
        for campo, clave, dia in (("start", "inicio", estado.get("checkin")), ("end", "fin", estado.get("checkout"))):
            local = _local_evento(principal.get(campo))
            tipo = "entrada" if campo == "start" else "salida"
            if (escrito.get(clave) and local and _h(local) != escrito[clave] and local[:10] == dia
                    and not estado.get(f"acuerdo_{tipo}")):
                estado[f"acuerdo_{tipo}"] = local[11:16]
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


def _escrito(ev: dict | None) -> dict:
    try:
        return json.loads(_props(ev).get(P_ESCRITO) or "{}")
    except json.JSONDecodeError:
        return {}


def _h(valor) -> str:
    """Huella de un valor; los textos se comparan sin espacios sobrantes al final de cada línea."""
    texto = "\n".join(l.rstrip() for l in str(valor if valor is not None else "").splitlines()).strip()
    return hashlib.sha1(texto.encode()).hexdigest()[:12]


def _texto_plano(desc: str) -> str:
    """Al editar la descripción en la web, Calendar la guarda en HTML: se pasa a texto para compararla."""
    if not re.search(r"<(br|p|div|span|b|i|u|a|ul|li)\b[^>]*>|&[a-z]+;|&#\d+;", desc, re.I):
        return desc
    t = re.sub(r"<br\s*/?>", "\n", desc, flags=re.I)
    t = re.sub(r"</(p|div|li)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return unescape(t).replace("\xa0", " ")


def _local(d: datetime) -> str:
    """Hora local sin zona, 'yyyy-mm-ddTHH:MM'."""
    return d.strftime("%Y-%m-%dT%H:%M")


def _local_evento(campo: dict | None, tz: ZoneInfo | None = None) -> str | None:
    """'yyyy-mm-ddTHH:MM' en la zona del evento, a partir de start/end de la API de Calendar."""
    t = (campo or {}).get("dateTime")
    if not t:
        return None
    zona = tz or ZoneInfo((campo or {}).get("timeZone") or "Europe/Madrid")
    d = datetime.fromisoformat(t.replace("Z", "+00:00"))
    return _local(d.astimezone(zona) if d.tzinfo else d)


def _huella(limpieza: dict | None) -> str:
    return json.dumps(limpieza, sort_keys=True, separators=(",", ":")) if limpieza else ""


def _props(ev: dict | None) -> dict:
    return ((ev or {}).get("extendedProperties") or {}).get("private") or {}


def _hash(nombre: str | None) -> str:
    return hashlib.sha1(clave_normalizada(nombre).encode()).hexdigest()[:16]
