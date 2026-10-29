"""
Google Calendar (API REST v3) con una cuenta de servicio.

Cada reserva = DOS eventos cortos con hora (PARTES): la ENTRADA, el día de llegada a la
hora de entrada, y la SALIDA, el día de salida a la hora de salida. Así Calendar no los
dibuja en la franja de todo el día, como hace con los eventos de más de 24 h. Si el
huésped anterior del mismo alojamiento sale ese día (o el anterior), el título de la
entrada lleva [LIMPIEZA OK] o [ALERTA LIMPIEZA / AJUSTE HORARIO].

Los dos eventos llevan las mismas propiedades privadas (no visibles en el calendario)
para encontrarlos después, el estado completo de la reserva en JSON (si se borra uno, se
reconstruye desde el otro), su parte (airbnb_parte) y una marca de origen (airbnb_origen)
que permite borrarlos todos sin tocar otros eventos. Un evento sin parte es del formato
anterior (uno por estancia): hace de entrada y se convierte al escribirlo.

Cambios a mano: el script recuerda (airbnb_escrito) lo último que escribió en cada
campo. Si el anfitrión cambia el título, el color o la ubicación, ese campo ya no se toca.
La hora no: el equipo de limpieza arrastra eventos sin querer, así que un evento movido a
mano vuelve a su hora en la siguiente pasada. Una hora acordada solo se fija escribiendo
'#entrada HH:MM' o '#salida HH:MM' en cualquiera de los dos eventos. La descripción tiene
dos partes separadas por SEPARADOR: lo de encima es del anfitrión y se conserva siempre;
lo de debajo es automático (si lo edita, la descripción pasa a ser suya).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import unescape
from urllib.parse import quote
from zoneinfo import ZoneInfo

from . import formato, reglas
from .config import Alojamiento, Config
from .parser import clave_normalizada

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/calendar/v3/calendars/{cal}/events"
ALCANCE = "https://www.googleapis.com/auth/calendar.events"

P_ORIGEN = "airbnb_origen"            # marca de los eventos creados por este script
ORIGEN = "airbnb-calendar"
P_TIPO = "airbnb_tipo"                # 'reserva'
P_PARTE = "airbnb_parte"              # 'entrada' | 'salida'
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


@dataclass(frozen=True)
class Parte:
    fecha: str                                      # campo del estado con el día del evento
    hora: Callable[[dict, Config], str]
    titulo: Callable[[dict, dict | None, Config], str]
    descripcion: Callable[[dict, dict | None, Config], str]
    color: str                                      # clave de [calendario.colores]
    con_limpieza: bool                              # la limpieza solo importa al huésped que entra


PARTES = {
    "entrada": Parte("checkin", reglas.hora_entrada, formato.titulo_entrada, formato.descripcion_entrada, "reserva", True),
    "salida": Parte("checkout", reglas.hora_salida, lambda e, _, cfg: formato.titulo_salida(e, cfg),
                    lambda e, _, cfg: formato.descripcion_salida(e, cfg), "salida", False),
}


class CalendarioInaccesible(RuntimeError):
    """El calendario entero no es accesible (ID incorrecto o no compartido con la cuenta de servicio).
    No es culpa de ningún correo: la ejecución se detiene sin contar fallos."""


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
    def __init__(self, cfg: Config, sesion, dormir=time.sleep, creador: str | None = None):
        self.cfg = cfg
        self.sesion = sesion
        self.dormir = dormir
        self.creador = creador          # correo de la cuenta de servicio: reconoce los eventos sin marca de origen
        self.url = API.format(cal=quote(cfg.calendar_id, safe=""))
        self.tz = ZoneInfo(cfg.zona_horaria)
        self.stats = {"creados": 0, "actualizados": 0, "limpiezas_recalculadas": 0}

    # ------------------------------------------------------------------ lectura

    def comprobar_acceso(self) -> None:
        self._pedir("GET", self.url, params={"maxResults": 1})

    def buscar_eventos_reserva(self, codigo, alojamiento, checkin, checkout) -> dict:
        """
        {'entrada': ev|None, 'salida': ev|None, 'sobrantes': [...]}. Por código y por alojamiento + entrada, con los
        del código primero. 'sobrantes' son eventos duplicados de la misma reserva: se borran al guardarla.
        """
        candidatos = self._listar({P_CODIGO: codigo}) if codigo else []
        # También sin código: eventos creados desde un mensaje, o una parte que se quedó sin él si se cortó la
        # ejecución al ponérselo
        if alojamiento and checkin:
            ids = {ev["id"] for ev in candidatos}
            candidatos += [ev for ev in self._listar({P_ALOJAMIENTO: _hash(alojamiento), P_CHECKIN: checkin})
                           if ev["id"] not in ids]
        propios = [ev for ev in candidatos if _props(ev).get(P_TIPO) == "reserva"
                   and not (codigo and _props(ev).get(P_CODIGO) not in (None, codigo))]   # misma fecha, otra reserva
        return _por_parte(propios)

    def eventos_propios(self, hasta: date | None = None) -> list[dict]:
        """
        Los eventos de reservas creados por este script: con su marca de origen o, los de las primeras versiones (sin
        marca), creados por la cuenta de servicio. Con `hasta`, solo los de reservas que terminaron antes de ese día.
        """
        # Los eventos sin marca no se pueden filtrar por propiedad: se lista todo y se filtra aquí
        rango = {"timeMax": datetime.combine(hasta, datetime.min.time(), self.tz).isoformat()} if hasta else {}
        evs = [ev for ev in self._listar({}, **rango)
               if self._es_propio(ev) and (hasta is None or _terminado(ev, hasta, self.tz))]
        return sorted(evs, key=_inicio)

    def _es_propio(self, ev: dict) -> bool:
        if _props(ev).get(P_ORIGEN) == ORIGEN:
            return _props(ev).get(P_TIPO) == "reserva"          # doble comprobación antes de borrar nada
        return self.creador is not None and (ev.get("creator") or {}).get("email") == self.creador

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
        """Crea o actualiza los dos eventos de la reserva y recalcula la limpieza de la reserva siguiente."""
        if not estado["checkin"]:
            return eventos
        limpieza = reglas.evaluar_limpieza(self._reserva_anterior(estado), estado, self.cfg)
        for parte in _partes_de(estado):
            self._escribir(eventos, parte, estado, limpieza)
        self._borrar_sobrantes(eventos)
        self._actualizar_siguientes(estado)
        self._actualizar_lockers_compartidos(estado)
        return eventos

    def _borrar_sobrantes(self, eventos: dict) -> None:
        for sobrante in eventos["sobrantes"]:
            self.borrar_evento(sobrante)
            log.info("Evento duplicado de la reserva eliminado: %s", sobrante.get("summary"))
        eventos["sobrantes"] = []

    def _escribir(self, eventos: dict, parte: str, estado: dict, limpieza: dict | None,
                  reservas: list[dict] | None = None, contador: str = "actualizados") -> None:
        """Crea el evento de esa parte de la reserva o lo reescribe (sin tocar lo cambiado a mano)."""
        ev = eventos[parte]
        cuerpo = self._cuerpo(ev, parte, estado, limpieza, reservas)
        self._avisar_hora_movida(ev, parte, cuerpo)
        if ev is None:
            eventos[parte] = self._pedir("POST", self.url, json=cuerpo)
            self.stats["creados"] += 1
            log.info("Evento creado: %s (%s)", cuerpo["summary"], cuerpo["start"]["dateTime"][:16])
        else:
            eventos[parte] = self._pedir("PATCH", f"{self.url}/{ev['id']}", json=cuerpo)
            self.stats[contador] += 1
            log.info("Evento actualizado: %s", eventos[parte].get("summary") or ev.get("summary"))

    def _cuerpo(self, ev: dict | None, parte: str, estado: dict, limpieza: dict | None,
                reservas: list[dict] | None = None) -> dict:
        """
        Lo que hay que escribir en el evento de esa parte (para PATCH o POST). Lo que el anfitrión haya cambiado a
        mano desde la última escritura del script (título, color, ubicación o la parte automática de la descripción)
        NO se toca; su parte de la descripción (encima del separador) se conserva siempre. La hora siempre es la
        calculada (con los acuerdos #entrada / #salida), salvo en un evento pasado a día completo.
        """
        p = PARTES[parte]
        limpieza = limpieza if p.con_limpieza else None
        self._preparar(estado, reservas)
        previo = _escrito(ev)
        actual, manual = self._leer_evento(ev)
        if actual.get("location") and "location" not in previo:
            previo["location"] = _h(None)          # antes el script no escribía la ubicación: la que haya es del anfitrión

        dia_completo = bool(ev and (ev.get("start") or {}).get("date"))

        def respetado(k: str) -> bool:
            if k in ("inicio", "fin"):
                # Pasado a día completo a mano: Calendar no admite mezclar fecha con fecha y hora. Si no, la hora solo
                # la cambia una etiqueta: un evento arrastrado sin querer vuelve a la suya
                return dia_completo
            return k in previo and _h(actual.get(k)) != previo[k]     # cambiado a mano desde la última escritura

        inicio = datetime.fromisoformat(f"{estado[p.fecha]}T{p.hora(estado, self.cfg)}")
        color = ("cancelada" if estado["cancelada"] else
                 "alerta_limpieza" if limpieza and not limpieza["ok"] else p.color)
        alojamiento = self.cfg.alojamiento_por_nombre(estado["alojamiento"])
        nuevo = {"summary": p.titulo(estado, limpieza, self.cfg), "colorId": self.cfg.colores[color],
                 "location": alojamiento.direccion if alojamiento and alojamiento.direccion else None,
                 "inicio": _local(inicio), "fin": _local(inicio + timedelta(minutes=self.cfg.duracion_evento_min)),
                 "auto": p.descripcion(estado, limpieza, self.cfg)}
        escrito, cuerpo = {}, {}
        for k, v in nuevo.items():
            if v is None:
                continue
            if respetado(k):
                escrito[k] = previo.get(k, _h(actual.get(k)))   # se respeta (y lo seguirá siendo)
                continue
            escrito[k] = _h(v)
            if k in ("inicio", "fin"):
                d = datetime.fromisoformat(v)
                cuerpo["start" if k == "inicio" else "end"] = {"dateTime": d.isoformat(), "timeZone": self.cfg.zona_horaria}
            elif k == "auto":
                cuerpo["description"] = (manual + "\n\n" if manual else "") + SEPARADOR + "\n" + v
            else:
                cuerpo[k] = v
        props = self._propiedades(estado, parte, limpieza, ev)
        props[P_ESCRITO] = json.dumps(escrito, separators=(",", ":"))
        cuerpo["extendedProperties"] = {"private": props}
        return cuerpo

    def _avisar_hora_movida(self, ev: dict | None, parte: str, cuerpo: dict) -> None:
        """Deja en el log cada evento movido a mano que se devuelve a su hora calculada."""
        antes, escrito = _local_evento((ev or {}).get("start"), self.tz), _escrito(ev).get("inicio")
        if not antes or not escrito or _h(antes) == escrito:
            return
        ahora = cuerpo["start"]["dateTime"][:16]
        if antes == ahora:
            return
        desde = 11 if antes[:10] == ahora[:10] else 0       # movido a otro día: con la fecha
        log.info("Hora movida a mano, se vuelve a la calculada (%s → %s): %s. Para pactar otra hora: #%s HH:MM",
                 antes[desde:].replace("T", " "), ahora[desde:].replace("T", " "), ev.get("summary"), parte)

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
        actual = {"summary": ev.get("summary"), "colorId": ev.get("colorId"), "location": ev.get("location"), "auto": auto,
                  "inicio": _local_evento(ev.get("start"), self.tz), "fin": _local_evento(ev.get("end"), self.tz)}
        return actual, manual

    def _preparar(self, estado: dict, reservas: list[dict] | None = None) -> None:
        """Datos que dependen de config.toml y de otras reservas: nombre corto y locker."""
        alojamiento = self.cfg.alojamiento_por_nombre(estado["alojamiento"])
        if not alojamiento:
            return
        # El nombre corto siempre sale de config.toml (si se cambia allí, se corrigen todos los eventos)
        estado["alias"] = alojamiento.alias or alojamiento.nombre
        estado["locker"], estado["locker_coincide"] = alojamiento.locker or None, None
        otro = self.cfg.alojamiento_por_nombre(alojamiento.locker_coincide_con)
        if otro and alojamiento.locker_si_coincide and estado["checkin"]:
            if reservas is None:
                reservas = [e for _, e in self._reservas(otro.nombre, P_CHECKIN, date.fromisoformat(estado["checkin"]))]
            if any(_hash(o["alojamiento"]) == _hash(otro.nombre) and o.get("checkin") == estado["checkin"]
                   and not o.get("cancelada") for o in reservas):
                estado["locker"], estado["locker_coincide"] = alojamiento.locker_si_coincide, otro.alias

    def _desactualizado(self, ev: dict, parte: str, estado: dict, limpieza: dict | None,
                        reservas: list[dict] | None = None) -> bool:
        """¿Hay algo que escribir en el evento de esa parte (sin contar lo cambiado a mano)?"""
        cuerpo = self._cuerpo(ev, parte, dict(estado), limpieza, reservas)
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

    def _poner_al_dia(self, eventos: dict, estado: dict, limpieza: dict | None,
                      reservas: list[dict] | None = None, simulacion: bool = False) -> None:
        """Reescribe las partes de la reserva que estén desactualizadas, crea las que falten y borra los duplicados."""
        for parte in _partes_de(estado):
            ev = eventos[parte]
            if ev is not None and not self._desactualizado(ev, parte, estado, limpieza, reservas):
                continue
            if simulacion:
                cuerpo = self._cuerpo(ev, parte, dict(estado), limpieza, reservas)
                self._avisar_hora_movida(ev, parte, cuerpo)
                log.info("Se %s: %s", "crearía" if ev is None else "actualizaría",
                         cuerpo.get("summary") or ev.get("summary"))
                self.stats["creados" if ev is None else "limpiezas_recalculadas"] += 1
            else:
                self._escribir(eventos, parte, estado, limpieza, reservas, contador="limpiezas_recalculadas")
        if not simulacion:
            self._borrar_sobrantes(eventos)

    def borrar_evento(self, ev: dict) -> None:
        self._pedir("DELETE", f"{self.url}/{ev['id']}", ignorar=(404, 410))

    def _reservas(self, alojamiento: str, propiedad: str, dia: date) -> list[tuple[dict, dict]]:
        """(eventos, estado) de las reservas del alojamiento con esa fecha de entrada o salida."""
        evs = self._listar({P_TIPO: "reserva", P_ALOJAMIENTO: _hash(alojamiento), propiedad: dia.isoformat()})
        return [(eventos, e) for eventos in _agrupar(evs) if (e := leer_estado(eventos))]

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
            for eventos, sig in self._reservas(estado["alojamiento"], P_CHECKIN, dia):
                if _misma_reserva(sig, estado):
                    continue
                limpieza = reglas.evaluar_limpieza(self._reserva_anterior(sig), sig, self.cfg)
                self._poner_al_dia(eventos, sig, limpieza)

    def _actualizar_lockers_compartidos(self, estado: dict) -> None:
        """Si otro alojamiento comparte locker con este (p. ej. Torrecilla con Buenavista), su locker
        depende de que este tenga entrada el mismo día: se ponen al día sus reservas de ese día."""
        if not estado["alojamiento"] or not estado["checkin"]:
            return
        este = self.cfg.alojamiento_por_nombre(estado["alojamiento"])
        for a in self.cfg.alojamientos:
            if not este or self.cfg.alojamiento_por_nombre(a.locker_coincide_con) is not este:
                continue
            for eventos, otra in self._reservas(a.nombre, P_CHECKIN, date.fromisoformat(estado["checkin"])):
                limpieza = reglas.evaluar_limpieza(self._reserva_anterior(otra), otra, self.cfg)
                self._poner_al_dia(eventos, otra, limpieza)

    # ------------------------------------------------------- tareas periódicas

    def recalcular_limpiezas(self, hoy: date, dias: int | None = 30, simulacion: bool = False) -> list[dict]:
        """
        Pone al día los eventos de las reservas en curso y de las que entran entre hoy y dentro de `dias`
        (None = todas), sin depender de que llegue un correo (cambios de fechas o de reglas, cancelaciones,
        eventos movidos a mano, que vuelven a su hora, eventos antiguos de toda la estancia, que pasan a
        entrada + salida).
        Devuelve los estados de las reservas que entran desde hoy con alerta de limpieza.
        """
        rango = {"timeMin": f"{hoy - timedelta(days=2)}T00:00:00Z"}
        if dias is not None:
            rango["timeMax"] = f"{hoy + timedelta(days=dias)}T00:00:00Z"
        reservas = []
        for eventos in _agrupar(self._listar({P_TIPO: "reserva"}, **rango)):
            e = leer_estado(eventos)
            if e and e["checkin"] and any(eventos[p] is None for p in _partes_de(e)):
                # La otra parte puede quedar fuera del rango de fechas, y sus acuerdos cuentan: se busca aparte
                eventos = self.buscar_eventos_reserva(e["codigo"], e["alojamiento"], e["checkin"], e["checkout"])
                e = leer_estado(eventos)
            if e and e["checkin"]:
                reservas.append((eventos, e))
        todas = [e for _, e in reservas]
        alertas, dia = [], hoy.isoformat()
        self.stats["reservas_revisadas"] = 0
        for eventos, e in reservas:
            if (e["checkout"] or e["checkin"]) < dia or not e["alojamiento"]:
                continue
            self.stats["reservas_revisadas"] += 1
            if e["checkin"] >= dia:
                vecinas = todas
                anterior = _anterior_en([o for o in todas if _hash(o["alojamiento"]) == _hash(e["alojamiento"])], e, self.cfg)
            else:
                # Estancia en curso: el huésped anterior y las vecinas pueden quedar fuera del rango consultado
                vecinas, anterior = None, self._reserva_anterior(e)
            limpieza = reglas.evaluar_limpieza(anterior, e, self.cfg)
            self._poner_al_dia(eventos, e, limpieza, vecinas, simulacion)
            if limpieza and not limpieza["ok"] and e["checkin"] >= dia:
                alertas.append(e)
        return alertas

    def _propiedades(self, estado: dict, parte: str, limpieza: dict | None, anterior: dict | None) -> dict:
        datos = json.dumps(estado, ensure_ascii=False, separators=(",", ":"))
        trozos = [datos[i:i + TAM_TROZO] for i in range(0, len(datos), TAM_TROZO)] or [""]
        p = {
            P_ORIGEN: ORIGEN,
            P_TIPO: "reserva",
            P_PARTE: parte,
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
            if resp.status_code in (401, 403, 404) and metodo == "GET" and url == self.url:
                raise CalendarioInaccesible(f"Google Calendar no permite acceder al calendario (HTTP {resp.status_code})"
                                            f"{ayuda}")
            raise RuntimeError(f"Google Calendar {metodo} → HTTP {resp.status_code}: {resp.text[:300]}{ayuda}")
        return resp.json() if resp.content else {}


def leer_estado(eventos: dict) -> dict | None:
    """
    Estado JSON de la reserva: el del evento de entrada (si no, el de la salida o el de un duplicado), con los
    horarios acordados por el anfitrión (#entrada / #salida) en cualquiera de los dos eventos.
    """
    estado = next(filter(None, map(_estado_guardado, (eventos["entrada"], eventos["salida"], *eventos["sobrantes"]))), None)
    if estado is None:
        return None
    partes = [ev for ev in (eventos["entrada"], eventos["salida"]) if ev]
    # Los acuerdos de horario los escribe el anfitrión en la descripción: lo que pone ahí manda
    # (si borra la etiqueta, el acuerdo desaparece)
    estado.update(acuerdo_entrada=None, acuerdo_salida=None)
    for ev in partes:
        # Solo la parte del anfitrión (encima del separador): la automática sugiere "#entrada 12:30" y no es un acuerdo
        desc = _texto_plano(ev.get("description") or "")
        for k, v in reglas.leer_acuerdos(desc.split(SEPARADOR, 1)[0]).items():
            estado[k] = estado[k] or v
    return estado


def _estado_guardado(ev: dict | None) -> dict | None:
    p = _props(ev)
    n = int(p.get(P_ESTADO_N) or 0)
    if not n:
        return None
    try:
        return json.loads("".join(p.get(f"{P_ESTADO}{i}") or "" for i in range(n)))
    except json.JSONDecodeError as e:
        log.warning("Estado ilegible en el evento %s: %s", ev.get("id"), e)
        return None


def _por_parte(evs: list[dict]) -> dict:
    """
    {'entrada': ev|None, 'salida': ev|None, 'sobrantes': [...]} con los eventos de una reserva. Un evento sin parte
    es del formato anterior (uno por estancia) y hace de entrada. Si hay varios de una parte, vale el primero.
    """
    eventos = {**dict.fromkeys(PARTES), "sobrantes": []}
    for ev in evs:
        parte = _props(ev).get(P_PARTE) or "entrada"
        if parte in PARTES and eventos[parte] is None:
            eventos[parte] = ev
        else:
            eventos["sobrantes"].append(ev)
    return eventos


def _agrupar(evs: list[dict]) -> list[dict]:
    """Los eventos de cada reserva juntos (ver _por_parte): por código o, si no lo hay, por alojamiento y fechas."""
    grupos: dict = {}
    for ev in evs:
        p = _props(ev)
        grupos.setdefault(p.get(P_CODIGO) or (p.get(P_ALOJAMIENTO), p.get(P_CHECKIN), p.get(P_CHECKOUT)), []).append(ev)
    return [_por_parte(g) for g in grupos.values()]


def _partes_de(estado: dict) -> list[str]:
    """Las partes que debe tener la reserva (sin fecha de salida, no hay evento de salida)."""
    return [nombre for nombre, p in PARTES.items() if estado[p.fecha]]


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


def _local_evento(campo: dict | None, tz: ZoneInfo) -> str | None:
    """'yyyy-mm-ddTHH:MM' en la zona del calendario, a partir de start/end de la API de Calendar."""
    t = (campo or {}).get("dateTime")
    if not t:
        return None
    d = datetime.fromisoformat(t.replace("Z", "+00:00"))
    return _local(d.astimezone(tz) if d.tzinfo else d)


def _terminado(ev: dict, hasta: date, tz: ZoneInfo) -> bool:
    """
    ¿Terminó antes del día `hasta`? Con marca cuenta la salida de la reserva, así una reserva que cruza ese día se
    conserva entera (también su entrada); sin marca, el final del evento.
    """
    p = _props(ev)
    if p.get(P_ORIGEN) == ORIGEN:
        return (p.get(P_CHECKOUT) or p.get(P_CHECKIN, "")) < hasta.isoformat()
    fin = ev.get("end") or {}
    # Un evento de día completo termina al empezar su end.date (que no se incluye)
    return (_local_evento(fin, tz) or fin.get("date", "") + "T00:00") <= f"{hasta}T00:00"


def _inicio(ev: dict) -> str:
    """Inicio tal como lo da la API: dateTime o, si es de día completo, date."""
    inicio = ev.get("start") or {}
    return inicio.get("dateTime") or inicio.get("date") or ""


def _huella(limpieza: dict | None) -> str:
    return json.dumps(limpieza, sort_keys=True, separators=(",", ":")) if limpieza else ""


def _props(ev: dict | None) -> dict:
    return ((ev or {}).get("extendedProperties") or {}).get("private") or {}


def _hash(nombre: str | None) -> str:
    return hashlib.sha1(clave_normalizada(nombre).encode()).hexdigest()[:16]
