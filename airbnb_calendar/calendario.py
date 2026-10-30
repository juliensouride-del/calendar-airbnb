"""
Google Calendar (API REST v3) con una cuenta de servicio.

Cada reserva = DOS eventos cortos con hora (PARTES): la ENTRADA, el día de llegada a la
hora de entrada, y la SALIDA, el día de salida a la hora de salida. Así Calendar no los
dibuja en la franja de todo el día, como hace con los eventos de más de 24 h. Si el
huésped anterior del mismo alojamiento sale ese día (o el anterior), el título de la
entrada lleva [LIMPIEZA OK] o [ALERTA LIMPIEZA / AJUSTE HORARIO].

Relevo: si el huésped anterior sale el mismo día que entra este, en el mismo alojamiento, su
salida y esta entrada son un solo evento, de la hora de salida a la de entrada. Es de la
reserva que entra (su parte es 'relevo' y ocupa el hueco de la entrada) y lleva una copia
del estado de la que sale (airbnb_saliente_*), que no tiene evento de salida mientras dure:
la encuentra por alojamiento y fecha ('sale_en') y solo lo lee. Únicamente la reserva que
entra junta y separa el relevo (_juntar_o_separar).

Los eventos llevan las mismas propiedades privadas (no visibles en el calendario)
para encontrarlos después, el estado completo de la reserva en JSON (si se borra uno, se
reconstruye desde el otro), su parte (airbnb_parte) y una marca de origen (airbnb_origen)
que permite borrarlos todos sin tocar otros eventos. Un evento sin parte es del formato
anterior (uno por estancia): hace de entrada y se convierte al escribirlo.

Cambios a mano: el script recuerda (airbnb_escrito) lo último que escribió en cada
campo. Si el anfitrión cambia el título, el color o la ubicación, ese campo ya no se toca.
La hora no: el equipo de limpieza arrastra eventos sin querer, así que un evento movido a
mano vuelve a su hora en la siguiente pasada. Una hora acordada solo se fija escribiendo
'#entrada HH:MM' o '#salida HH:MM' en los eventos de la reserva (en el relevo, #salida es
del huésped que sale y #entrada del que entra: Parte.etiquetas). La descripción tiene
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
from dataclasses import dataclass, replace
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
P_PARTE = "airbnb_parte"              # 'entrada' | 'salida' | 'relevo'
P_CODIGO = "airbnb_codigo"
P_ALOJAMIENTO = "airbnb_alojamiento"  # hash del nombre normalizado (los valores de filtro deben ser simples)
P_CHECKIN = "airbnb_checkin"
P_CHECKOUT = "airbnb_checkout"
P_LIMPIEZA = "airbnb_limpieza"        # última evaluación de la limpieza (para no reescribir si no cambia)
P_ESTADO = "airbnb_estado_"           # JSON del estado en trozos: airbnb_estado_n (cuántos), airbnb_estado_0, _1, ...
P_SALIENTE = "airbnb_saliente_"       # en el relevo, el de la reserva que sale (igual, en trozos)
P_ESCRITO = "airbnb_escrito"          # huella de lo último que escribió el script (detecta cambios a mano)
# La descripción tiene dos partes: lo que escribe el anfitrión (encima) y lo automático (debajo)
SEPARADOR = "—— Airbnb · automático (escribe tus notas encima de esta línea) ——"
TAM_TROZO = 900                       # los valores de una propiedad admiten hasta 1024 caracteres
REINTENTABLES = {429, 500, 502, 503, 504}
ESPERAS_S = (2, 5, 15)                # backoff ante cortes momentáneos de Google


@dataclass(frozen=True)
class Vista:
    """Lo que hace falta para escribir los eventos de una reserva: su estado, la que sale del alojamiento el día que
    entra (o el anterior) y la limpieza entre las dos."""
    e: dict
    sale: dict | None = None
    limpieza: dict | None = None

    @classmethod
    def de(cls, e: dict, sale: dict | None, cfg: Config) -> Vista:
        return cls(e, sale, reglas.evaluar_limpieza(sale, e, cfg))

    @property
    def relevo(self) -> bool:
        return reglas.es_relevo(self.sale, self.e)


@dataclass(frozen=True)
class Parte:
    hueco: str                                      # dónde va en los eventos de la reserva: 'entrada' | 'salida'
    fecha: str                                      # campo del estado con el día del evento
    horas: Callable[[Vista, Config], tuple[str, str | None]]   # inicio y fin (None: inicio + duracion_evento_min)
    titulo: Callable[[Vista, Config], str]
    descripcion: Callable[[Vista, Config], str]
    color: str                                      # clave de [calendario.colores]
    con_limpieza: bool                              # la limpieza solo importa al huésped que entra
    etiquetas: tuple[str, ...]                      # #entrada / #salida que son de su reserva; la 1.ª, la de su hora
    etiqueta_inicio: str                            # la que fija su hora de inicio


PARTES = {
    "entrada": Parte("entrada", "checkin", lambda v, cfg: (reglas.hora_entrada(v.e, cfg), None),
                     lambda v, cfg: formato.titulo_entrada(v.e, v.limpieza, cfg),
                     lambda v, cfg: formato.descripcion_entrada(v.e, v.limpieza, cfg), "reserva", True,
                     ("entrada", "salida"), "entrada"),
    "salida": Parte("salida", "checkout", lambda v, cfg: (reglas.hora_salida(v.e, cfg), None),
                    lambda v, cfg: formato.titulo_salida(v.e, cfg),
                    lambda v, cfg: formato.descripcion_salida(v.e, cfg), "salida", False,
                    ("salida", "entrada"), "salida"),
    "relevo": Parte("entrada", "checkin", lambda v, cfg: (reglas.hora_salida(v.sale, cfg), reglas.hora_entrada(v.e, cfg)),
                    lambda v, cfg: formato.titulo_relevo(v.sale, v.e, v.limpieza, cfg),
                    lambda v, cfg: formato.descripcion_relevo(v.sale, v.e, v.limpieza, cfg), "relevo", True,
                    ("entrada",), "salida"),
}
# Etiquetas #entrada / #salida: cada evento cuenta para la reserva que lo lee solo las suyas (Parte.etiquetas). En el
# relevo, #salida es del huésped que sale: la reserva que entra no la lee y la que sale solo lee esa
ETIQUETAS_SALE_EN = ("salida",)


@dataclass(frozen=True)
class Notas:
    """
    Cambios en lo que escribió el anfitrión en un evento al juntar o separar un relevo: fuera las etiquetas de un tipo
    (#entrada / #salida, que pasan a ser de otra reserva) y dentro las líneas que lleguen de otro evento y aún no estén.
    """
    quitar: str | None = None
    anadir: str = ""

    def aplicar(self, texto: str) -> str:
        lineas = _sin_etiquetas(texto, self.quitar).splitlines() if self.quitar else texto.splitlines()
        tiene = {l.strip() for l in lineas}
        for l in self.anadir.splitlines():
            if l.strip() and l.strip() not in tiene:
                lineas.append(l)
                tiene.add(l.strip())
        return "\n".join(lineas).strip()

    def __add__(self, otras: Notas) -> Notas:
        return Notas(self.quitar or otras.quitar, "\n".join(filter(None, (self.anadir, otras.anadir))))


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
        self.stats = {"creados": 0, "actualizados": 0, "limpiezas_recalculadas": 0, "juntados": 0, "separados": 0}

    # ------------------------------------------------------------------ lectura

    def comprobar_acceso(self) -> None:
        self._pedir("GET", self.url, params={"maxResults": 1})

    def buscar_eventos_reserva(self, codigo, alojamiento, checkin, checkout) -> dict:
        """Los eventos de la reserva (ver _eventos), por código y por alojamiento + entrada, con los del código primero."""
        candidatos = self._listar({P_CODIGO: codigo}) if codigo else []
        # También sin código: eventos creados desde un mensaje, o una parte que se quedó sin él si se cortó la
        # ejecución al ponérselo
        if alojamiento and checkin:
            ids = {ev["id"] for ev in candidatos}
            candidatos += [ev for ev in self._listar({P_ALOJAMIENTO: _hash(alojamiento), P_CHECKIN: checkin})
                           if ev["id"] not in ids]
        propios = [ev for ev in candidatos if _props(ev).get(P_TIPO) == "reserva"
                   and not (codigo and _props(ev).get(P_CODIGO) not in (None, codigo))]   # misma fecha, otra reserva
        return self._eventos(propios, {"codigo": codigo, "alojamiento": alojamiento, "checkin": checkin, "checkout": checkout})

    def _eventos(self, propios: list[dict], yo: dict | None = None, relevos: list[dict] | None = None) -> dict:
        """
        {'entrada': ev|None, 'salida': ev|None, 'sale_en': ev|None, 'sobrantes': [...]}: los eventos propios de una
        reserva, cada uno en el hueco de su parte (el relevo, en el de la entrada). Si hay varios para un hueco, vale el
        primero y los demás sobran: se borran al guardarla. 'sale_en' es el relevo en el que sale, que es de la reserva
        siguiente y solo se lee: el del alojamiento el día de su salida con su copia (si ahora sale otro día, un relevo
        antiguo ya no es suyo). Se busca entre `relevos` si ya están listados los de ese día; si no, en el calendario.
        yo: la reserva, si no tiene eventos propios.
        """
        eventos = {"entrada": None, "salida": None, "sale_en": None, "sobrantes": []}
        for ev in propios:
            parte = PARTES.get(_parte(ev))
            if parte and eventos[parte.hueco] is None:
                eventos[parte.hueco] = ev
            else:
                eventos["sobrantes"].append(ev)
        yo = _estado_propio(eventos) or yo
        if yo and yo["alojamiento"] and yo["checkout"]:
            if relevos is None:
                relevos = self._listar({P_ALOJAMIENTO: _hash(yo["alojamiento"]), P_CHECKIN: yo["checkout"], P_PARTE: "relevo"})
            eventos["sale_en"] = next((ev for ev in relevos if _sale_en(ev, yo)), None)
        return eventos

    def _agrupar(self, evs: list[dict], relevos: list[dict] | None = None) -> list[dict]:
        """
        Los eventos de cada reserva juntos (ver _eventos): por código o, si no lo hay, por alojamiento y fechas.
        relevos: los ya listados de los días en que salen. Una reserva que solo está en la copia de uno de ellos (se
        borraron sus eventos, o no están en el listado) también cuenta.
        """
        grupos: dict = {}
        for ev in evs:
            p = _props(ev)
            grupos.setdefault(p.get(P_CODIGO) or (p.get(P_ALOJAMIENTO), p.get(P_CHECKIN), p.get(P_CHECKOUT)), []).append(ev)
        reservas = [self._eventos(g, relevos=relevos) for g in grupos.values()]
        con_relevo = {id(eventos["sale_en"]) for eventos in reservas}
        return reservas + [self._eventos([], sale, [ev]) for ev in relevos or []
                           if id(ev) not in con_relevo and (sale := _estado_guardado(ev, P_SALIENTE))]

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
        """Crea o actualiza los eventos de la reserva y pone al día las reservas que dependen de ella."""
        if not estado["checkin"]:
            return eventos
        antes = leer_estado(eventos)
        self._poner_al_dia(eventos, self._vista(estado), forzar=True)
        self._actualizar_siguientes(estado, antes)
        self._actualizar_lockers_compartidos(estado)
        return eventos

    def _vista(self, estado: dict) -> Vista:
        return Vista.de(estado, self._reserva_anterior(estado), self.cfg)

    def _borrar_sobrantes(self, eventos: dict) -> None:
        for sobrante in eventos["sobrantes"]:
            self.borrar_evento(sobrante)
            log.info("Evento duplicado de la reserva eliminado: %s", sobrante.get("summary"))
        eventos["sobrantes"] = []

    def _escribir(self, eventos: dict, parte: str, v: Vista, reservas: list[dict] | None = None,
                  contador: str = "actualizados", notas: Notas | None = None) -> None:
        """Crea el evento de esa parte de la reserva o lo reescribe (sin tocar lo cambiado a mano)."""
        hueco = PARTES[parte].hueco
        ev = eventos[hueco]
        cuerpo = self._cuerpo(ev, parte, v, reservas, notas)
        self._avisar_hora_movida(ev, parte, cuerpo)
        if ev is None:
            eventos[hueco] = self._pedir("POST", self.url, json=cuerpo)
            self.stats["creados"] += 1
            log.info("Evento creado: %s (%s)", cuerpo["summary"], cuerpo["start"]["dateTime"][:16])
        else:
            eventos[hueco] = self._pedir("PATCH", f"{self.url}/{ev['id']}", json=cuerpo)
            self.stats[contador] += 1
            log.info("Evento actualizado: %s", eventos[hueco].get("summary") or ev.get("summary"))

    def _cuerpo(self, ev: dict | None, parte: str, v: Vista, reservas: list[dict] | None = None,
                notas: Notas | None = None) -> dict:
        """
        Lo que hay que escribir en el evento de esa parte (para PATCH o POST). Lo que el anfitrión haya cambiado a
        mano desde la última escritura del script (título, color, ubicación o la parte automática de la descripción)
        NO se toca; su parte de la descripción (encima del separador) se conserva siempre, salvo los cambios de
        `notas`. La hora siempre es la calculada (con los acuerdos #entrada / #salida), salvo en un evento pasado a
        día completo.
        """
        p = PARTES[parte]
        limpieza = v.limpieza if p.con_limpieza else None
        # El locker del que sale en el relevo es el de su estado guardado: depende de quién entró el día que entró él,
        # que puede quedar fuera de `reservas`, y lo pone al día _actualizar_lockers_compartidos
        self._preparar(v.e, reservas)
        previo = _escrito(ev)
        actual, manual = self._leer_evento(ev)
        if actual.get("location") and "location" not in previo:
            previo["location"] = _h(None)          # antes el script no escribía la ubicación: la que haya es del anfitrión
        nuevo_manual = notas.aplicar(manual) if notas else manual

        dia_completo = bool(ev and (ev.get("start") or {}).get("date"))

        def respetado(k: str) -> bool:
            if k in ("inicio", "fin"):
                # Pasado a día completo a mano: Calendar no admite mezclar fecha con fecha y hora. Si no, la hora solo
                # la cambia una etiqueta: un evento arrastrado sin querer vuelve a la suya
                return dia_completo
            return k in previo and _h(actual.get(k)) != previo[k]     # cambiado a mano desde la última escritura

        desde, hasta = p.horas(v, self.cfg)
        inicio = datetime.fromisoformat(f"{v.e[p.fecha]}T{desde}")
        fin = datetime.fromisoformat(f"{v.e[p.fecha]}T{hasta}") if hasta else inicio
        if fin <= inicio:
            fin = inicio + timedelta(minutes=self.cfg.duracion_evento_min)
        color = ("cancelada" if v.e["cancelada"] else
                 "alerta_limpieza" if limpieza and not limpieza["ok"] else p.color)
        alojamiento = self.cfg.alojamiento_por_nombre(v.e["alojamiento"])
        nuevo = {"summary": p.titulo(v, self.cfg), "colorId": self.cfg.colores[color],
                 "location": alojamiento.direccion if alojamiento and alojamiento.direccion else None,
                 "inicio": _local(inicio), "fin": _local(fin), "auto": p.descripcion(v, self.cfg)}
        escrito, cuerpo = {}, {}
        for k, val in nuevo.items():
            if val is None:
                continue
            if respetado(k):
                escrito[k] = previo.get(k, _h(actual.get(k)))   # se respeta (y lo seguirá siendo)
                if k == "auto" and nuevo_manual != manual:
                    # La descripción es del anfitrión, pero una etiqueta que pasa a ser de otra reserva no se queda
                    cuerpo["description"] = _descripcion(nuevo_manual, actual["auto"])
                continue
            escrito[k] = _h(val)
            if k in ("inicio", "fin"):
                d = datetime.fromisoformat(val)
                cuerpo["start" if k == "inicio" else "end"] = {"dateTime": d.isoformat(), "timeZone": self.cfg.zona_horaria}
            elif k == "auto":
                cuerpo["description"] = _descripcion(nuevo_manual, val)
            else:
                cuerpo[k] = val
        props = self._propiedades(v, parte, limpieza, ev)
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
                 antes[desde:].replace("T", " "), ahora[desde:].replace("T", " "), ev.get("summary"),
                 PARTES[parte].etiqueta_inicio)

    def _leer_evento(self, ev: dict | None) -> tuple[dict, str]:
        """(valores actuales comparables con los que escribe el script, parte manual de la descripción)."""
        if not ev:
            return {}, ""
        desc = _texto_plano(ev.get("description") or "")
        if SEPARADOR in desc:
            manual, auto = desc.split(SEPARADOR, 1)
            manual, auto = manual.strip(), auto.lstrip("\n")
        elif _escrito(ev):
            manual, auto = desc.strip(), None      # el anfitrión quitó el separador: la descripción es suya
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
                reservas = [e for _, e in self._reservas(otro.nombre, "checkin", date.fromisoformat(estado["checkin"]))]
            if any(_hash(o["alojamiento"]) == _hash(otro.nombre) and o.get("checkin") == estado["checkin"]
                   and not o.get("cancelada") for o in reservas):
                estado["locker"], estado["locker_coincide"] = alojamiento.locker_si_coincide, otro.alias

    def _desactualizado(self, ev: dict, parte: str, v: Vista, reservas: list[dict] | None = None,
                        notas: Notas | None = None) -> bool:
        """¿Hay algo que escribir en el evento de esa parte (sin contar lo cambiado a mano)?"""
        cuerpo = self._cuerpo(ev, parte, _copia(v), reservas, notas)
        p_nuevas, p_ev = cuerpo["extendedProperties"]["private"], _props(ev)
        if any(p_ev.get(k) != val for k, val in p_nuevas.items() if not k.startswith((P_ESTADO, P_SALIENTE))):
            return True
        actual, _ = self._leer_evento(ev)
        for k, val in cuerpo.items():
            if k in ("start", "end"):
                if actual["inicio" if k == "start" else "fin"] != _local(datetime.fromisoformat(val["dateTime"])):
                    return True
            elif k != "extendedProperties" and (ev.get(k) or "") != val:
                return True
        return False

    def _poner_al_dia(self, eventos: dict, v: Vista, reservas: list[dict] | None = None, simulacion: bool = False,
                      forzar: bool = False, notas: dict[str, Notas] | None = None) -> None:
        """
        Reescribe las partes de la reserva que estén desactualizadas (todas, con `forzar`: ha llegado un correo y el
        estado guardado cambia aunque no se vea), crea las que falten, junta o separa el relevo y borra los duplicados.
        notas: cambios por hueco que pide otra reserva al juntar o separar su relevo.
        """
        cambios, borrar = self._juntar_o_separar(eventos, v, simulacion)
        for hueco, n in (notas or {}).items():
            cambios[hueco] = cambios[hueco] + n if hueco in cambios else n
        for parte in _partes_de(v, eventos):
            hueco = PARTES[parte].hueco
            ev, n = eventos[hueco], cambios.get(hueco)
            if not forzar and ev is not None and not self._desactualizado(ev, parte, v, reservas, n):
                continue
            if simulacion:
                cuerpo = self._cuerpo(ev, parte, _copia(v), reservas, n)
                self._avisar_hora_movida(ev, parte, cuerpo)
                log.info("Se %s: %s", "crearía" if ev is None else "actualizaría",
                         cuerpo.get("summary") or ev.get("summary"))
                self.stats["creados" if ev is None else "limpiezas_recalculadas"] += 1
            else:
                self._escribir(eventos, parte, v, reservas, "actualizados" if forzar else "limpiezas_recalculadas", n)
        if not simulacion:
            for ev in borrar:
                self.borrar_evento(ev)
            self._borrar_sobrantes(eventos)

    def _juntar_o_separar(self, eventos: dict, v: Vista, simulacion: bool) -> tuple[dict[str, Notas], list[dict]]:
        """
        Junta en un relevo la salida de la reserva anterior y la entrada de esta, o lo separa si ya no lo es
        (cancelada, otras fechas). Solo lo hace la reserva que entra; los eventos de la que sale se escriben con su
        estado (_poner_al_dia). Lo que pasa a otros eventos se escribe antes, así que si se corta a medias, cada
        etiqueta sigue en un evento donde es de la misma reserva, y la siguiente pasada termina el trabajo.
        Devuelve los cambios en las notas de los eventos de esta reserva (por hueco) y los eventos de la que sale que
        hay que borrar después de escribirlos.
        """
        hueco = eventos["entrada"]
        _, manual = self._leer_evento(hueco)
        antes = _estado_guardado(hueco, P_SALIENTE) if _parte(hueco) == "relevo" else None
        ahora = v.sale if v.relevo else None
        sigue = bool(antes and ahora and reglas.misma_reserva(antes, ahora))
        cambios: dict[str, Notas] = {}
        borrar: list[dict] = []
        if antes and not sigue:
            self._contar("separados", "separaría" if simulacion else "separan", antes, v.e)
            if not simulacion:
                sale = self.buscar_eventos_reserva(antes["codigo"], antes["alojamiento"], antes["checkin"], antes["checkout"])
                # Vuelve su evento de salida con lo escrito en el relevo, que cuenta ya aunque ahora salga otro día
                sale["sale_en"] = hueco
                estado_sale = leer_estado(sale)
                sale["sale_en"] = None
                self._poner_al_dia(sale, self._vista(estado_sale),
                                   notas={"salida": Notas(anadir=_sin_etiquetas(manual, "entrada"))})
        if hueco and not sigue and (antes or ahora):
            # Las #salida que hay ahora en la entrada son de otra reserva o, si aún no es el relevo, de esta, que pasan
            # a su salida (por eso el relevo se escribe después que la salida: _partes_de)
            cambios["entrada"] = Notas(quitar="salida")
            if not antes:
                cambios["salida"] = Notas(anadir=_lineas_con(manual, "salida"))
        if ahora:
            sale = self.buscar_eventos_reserva(ahora["codigo"], ahora["alojamiento"], ahora["checkin"], ahora["checkout"])
            if sale["salida"] or (hueco and not sigue):
                self._contar("juntados", "juntaría" if simulacion else "juntan", ahora, v.e)
            if sale["salida"]:
                # El relevo se queda con lo escrito en la salida, salvo las #entrada, que pasan a la entrada de esa reserva
                _, manual_sale = self._leer_evento(sale["salida"])
                if (lineas := _lineas_con(manual_sale, "entrada")) and not simulacion:
                    self._poner_al_dia(sale, self._vista(leer_estado(sale)), notas={"entrada": Notas(anadir=lineas)})
                cambios["entrada"] = cambios.get("entrada", Notas()) + Notas(anadir=_sin_etiquetas(manual_sale, "entrada"))
                borrar.append(sale["salida"])
        return cambios, borrar

    def _contar(self, clave: str, verbo: str, sale: dict, entra: dict) -> None:
        self.stats[clave] += 1
        log.info("Se %s: %s %s · sale %s · entra %s", verbo, entra["alias"] or entra["alojamiento"],
                 formato.fecha_corta_es(entra["checkin"]), sale["codigo"] or "?", entra["codigo"] or "?")

    def borrar_evento(self, ev: dict) -> None:
        self._pedir("DELETE", f"{self.url}/{ev['id']}", ignorar=(404, 410))

    def _reservas(self, alojamiento: str, campo: str, dia: date) -> list[tuple[dict, dict]]:
        """(eventos, estado) de las reservas del alojamiento que entran (campo 'checkin') o salen ('checkout') ese día."""
        filtros = {P_TIPO: "reserva", P_ALOJAMIENTO: _hash(alojamiento)}
        evs = self._listar({**filtros, P_CHECKIN if campo == "checkin" else P_CHECKOUT: dia.isoformat()})
        # Las que salen ese día pueden salir en un relevo: se lista una vez para todas
        relevos = self._listar({**filtros, P_CHECKIN: dia.isoformat(), P_PARTE: "relevo"}) if campo == "checkout" else None
        reservas = []
        for eventos in self._agrupar(evs, relevos):
            e = leer_estado(eventos)
            if e and e[campo] == dia.isoformat() and not _propios(eventos):
                # Solo está la copia del relevo: si aún tiene eventos, pueden decir otra fecha
                eventos = self.buscar_eventos_reserva(e["codigo"], e["alojamiento"], e["checkin"], e["checkout"])
                e = leer_estado(eventos)
            if e and e[campo] == dia.isoformat():
                reservas.append((eventos, e))
        return reservas

    def _reserva_anterior(self, estado: dict) -> dict | None:
        """Reserva del mismo alojamiento que sale el día de entrada (o, si no hay, el día anterior)."""
        if not estado["alojamiento"]:
            return None
        d = date.fromisoformat(estado["checkin"])
        for dia in (d, d - timedelta(days=1)):
            if anterior := _anterior_en([e for _, e in self._reservas(estado["alojamiento"], "checkout", dia)], estado, self.cfg):
                return anterior
        return None

    def _actualizar_siguientes(self, estado: dict, antes: dict | None = None) -> None:
        """Las reservas que entran el día de salida (o el siguiente) dependen de esta salida: se recalculan (y, si
        entran el mismo día, juntan o separan el relevo). Si la salida ha cambiado de día (`antes`, el estado guardado),
        también las de su día anterior: el relevo en el que salía se separa en esta misma pasada."""
        dias = {(e["alojamiento"], date.fromisoformat(e["checkout"]) + timedelta(days=n))
                for e in (estado, antes) if e and e["alojamiento"] and e["checkout"] for n in (0, 1)}
        for alojamiento, dia in sorted(dias):
            for eventos, sig in self._reservas(alojamiento, "checkin", dia):
                if reglas.misma_reserva(sig, estado):
                    continue
                self._poner_al_dia(eventos, self._vista(sig))

    def _actualizar_lockers_compartidos(self, estado: dict) -> None:
        """Si otro alojamiento comparte locker con este (p. ej. Torrecilla con Buenavista), su locker
        depende de que este tenga entrada el mismo día: se ponen al día sus reservas de ese día y el relevo
        en el que salen, que también lo muestra."""
        if not estado["alojamiento"] or not estado["checkin"]:
            return
        este = self.cfg.alojamiento_por_nombre(estado["alojamiento"])
        for a in self.cfg.alojamientos:
            if not este or self.cfg.alojamiento_por_nombre(a.locker_coincide_con) is not este:
                continue
            for eventos, otra in self._reservas(a.nombre, "checkin", date.fromisoformat(estado["checkin"])):
                self._poner_al_dia(eventos, self._vista(otra))
                self._actualizar_siguientes(otra)

    # ------------------------------------------------------- tareas periódicas

    def recalcular_limpiezas(self, hoy: date, dias: int | None = 30, simulacion: bool = False) -> list[dict]:
        """
        Pone al día los eventos de las reservas en curso y de las que entran entre hoy y dentro de `dias`
        (None = todas), sin depender de que llegue un correo (cambios de fechas o de reglas, cancelaciones,
        eventos movidos a mano, que vuelven a su hora, eventos antiguos de toda la estancia, que pasan a
        entrada + salida, salidas y entradas del mismo día, que se juntan en un relevo).
        Devuelve los estados de las reservas que entran desde hoy con alerta de limpieza.
        """
        rango = {"timeMin": f"{hoy - timedelta(days=2)}T00:00:00Z"}
        if dias is not None:
            rango["timeMax"] = f"{hoy + timedelta(days=dias)}T00:00:00Z"
        reservas = []
        evs = self._listar({P_TIPO: "reserva"}, **rango)
        for eventos in self._agrupar(evs, [ev for ev in evs if _parte(ev) == "relevo"]):
            e = leer_estado(eventos)
            if e and e["checkin"] and any(eventos[PARTES[p].hueco] is None for p in _partes_de(Vista(e), eventos)):
                # La otra parte puede quedar fuera del rango de fechas, y sus acuerdos cuentan: se busca aparte. Así
                # también el relevo en el que sale, que es del mismo día que su salida
                eventos = self.buscar_eventos_reserva(e["codigo"], e["alojamiento"], e["checkin"], e["checkout"])
                e = leer_estado(eventos)
            if e and e["checkin"]:
                reservas.append((eventos, e))
        # Por orden de entrada: al juntar o separar un relevo, la reserva que entra escribe en los eventos de la que
        # sale, que así ya se ha revisado y no los vuelve a escribir con lo que leyó antes
        reservas.sort(key=lambda r: r[1]["checkin"])
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
            v = Vista.de(e, anterior, self.cfg)
            self._poner_al_dia(eventos, v, vecinas, simulacion)
            if v.limpieza and not v.limpieza["ok"] and e["checkin"] >= dia:
                alertas.append(e)
        return alertas

    def _propiedades(self, v: Vista, parte: str, limpieza: dict | None, anterior: dict | None) -> dict:
        estado = v.e
        p = {
            P_ORIGEN: ORIGEN,
            P_TIPO: "reserva",
            P_PARTE: parte,
            P_ALOJAMIENTO: _hash(estado["alojamiento"]),
            P_CHECKIN: estado["checkin"] or "",
            P_CHECKOUT: estado["checkout"] or "",
            P_LIMPIEZA: _huella(limpieza),
            **_trozos(P_ESTADO, estado, anterior),
            # Solo lo escribe la reserva que entra, cada vez que escribe el relevo; en otra parte, se borra
            **_trozos(P_SALIENTE, v.sale if parte == "relevo" else None, anterior),
        }
        if estado["codigo"]:
            p[P_CODIGO] = estado["codigo"]
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
    Estado JSON de la reserva: el del evento de entrada (si no, el de la salida, el de un duplicado o la copia del
    relevo en el que sale), con los horarios acordados por el anfitrión (#entrada / #salida) en sus eventos.
    """
    estado = _estado_propio(eventos) or _estado_guardado(eventos["sale_en"], P_SALIENTE)
    if estado is None:
        return None
    # Los acuerdos de horario los escribe el anfitrión en la descripción: lo que pone ahí manda (si borra la etiqueta,
    # el acuerdo desaparece). Cada evento cuenta solo sus etiquetas y, si dos dicen otra hora, manda el que la muestra
    fuentes = [(ev, PARTES[_parte(ev)].etiquetas) for ev in (eventos["entrada"], eventos["salida"]) if ev]
    if eventos["sale_en"]:
        fuentes.append((eventos["sale_en"], ETIQUETAS_SALE_EN))
    horas = sorted((etiquetas.index(tipo), n, tipo, hora) for n, (ev, etiquetas) in enumerate(fuentes)
                   for tipo, hora in _acuerdos(ev).items() if hora and tipo in etiquetas)
    estado.update(acuerdo_entrada=None, acuerdo_salida=None)
    for *_, tipo, hora in horas:
        estado[f"acuerdo_{tipo}"] = estado[f"acuerdo_{tipo}"] or hora
    return estado


def en_calendario(eventos: dict) -> bool:
    """¿Tiene la reserva algún evento en el calendario (también el relevo en el que sale)?"""
    return any(eventos[k] for k in ("entrada", "salida", "sale_en"))


def _propios(eventos: dict) -> list[dict]:
    return [ev for ev in (eventos["entrada"], eventos["salida"], *eventos["sobrantes"]) if ev]


def _estado_propio(eventos: dict) -> dict | None:
    """El estado guardado en sus eventos: el de la entrada, si no el de la salida o el de un duplicado."""
    return next(filter(None, map(_estado_guardado, _propios(eventos))), None)


def _sale_en(relevo: dict, yo: dict) -> bool:
    """¿Es el relevo en el que sale la reserva `yo` (con su fecha de salida de ahora)?"""
    p = _props(relevo)
    return (p.get(P_PARTE) == "relevo" and p.get(P_ALOJAMIENTO) == _hash(yo["alojamiento"]) and p.get(P_CHECKIN) == yo["checkout"]
            and (sale := _estado_guardado(relevo, P_SALIENTE)) is not None and reglas.misma_reserva(sale, yo))


def _acuerdos(ev: dict) -> dict:
    """{'entrada': 'HH:MM' | None, 'salida': ...}. Solo de la parte del anfitrión (encima del separador): la automática
    sugiere '#entrada 12:30' y no es un acuerdo."""
    desc = _texto_plano(ev.get("description") or "").split(SEPARADOR, 1)[0]
    return {k.removeprefix("acuerdo_"): v for k, v in reglas.leer_acuerdos(desc).items()}


def _estado_guardado(ev: dict | None, prefijo: str = P_ESTADO) -> dict | None:
    p = _props(ev)
    n = int(p.get(f"{prefijo}n") or 0)
    if not n:
        return None
    try:
        return json.loads("".join(p.get(f"{prefijo}{i}") or "" for i in range(n)))
    except json.JSONDecodeError as e:
        log.warning("Estado ilegible en el evento %s: %s", ev.get("id"), e)
        return None


def _trozos(prefijo: str, estado: dict | None, anterior: dict | None) -> dict:
    """
    El estado en JSON, en trozos ({prefijo}n y {prefijo}0, 1...). PATCH fusiona las propiedades: los trozos que sobran
    de un estado anterior más largo (todos, sin estado) se borran con null.
    """
    datos = json.dumps(estado, ensure_ascii=False, separators=(",", ":")) if estado is not None else ""
    trozos = [datos[i:i + TAM_TROZO] for i in range(0, len(datos), TAM_TROZO)]
    n_anterior = int(_props(anterior).get(f"{prefijo}n") or 0)
    p = {f"{prefijo}{i}": t for i, t in enumerate(trozos)}
    p.update({f"{prefijo}{i}": None for i in range(len(trozos), n_anterior)})
    if trozos or n_anterior:
        p[f"{prefijo}n"] = str(len(trozos)) if trozos else None
    return p


def _parte(ev: dict | None) -> str:
    """Un evento sin parte es del formato anterior (uno por estancia) y hace de entrada."""
    return _props(ev).get(P_PARTE) or "entrada"


def _partes_de(v: Vista, eventos: dict) -> list[str]:
    """
    Las partes que debe tener la reserva: sin fecha de salida, o si sale ese día en el relevo de la siguiente, no tiene
    evento de salida. El relevo se escribe después de la salida: al juntarlos, las #salida de esta reserva pasan antes
    a su salida.
    """
    partes = ["salida", "relevo"] if v.relevo else ["entrada", "salida"]
    sale_en = eventos["sale_en"] and _sale_en(eventos["sale_en"], v.e)
    return [p for p in partes if v.e[PARTES[p].fecha] and not (p == "salida" and sale_en)]


def _anterior_en(candidatas: list[dict], estado: dict, cfg: Config) -> dict | None:
    """De las reservas candidatas, la que sale el día de entrada de `estado` (o, si no hay, el día anterior)."""
    d = date.fromisoformat(estado["checkin"])
    for dia in (d, d - timedelta(days=1)):
        otras = [e for e in candidatas if e.get("checkout") == dia.isoformat()
                 and not e["cancelada"] and not reglas.misma_reserva(e, estado)]
        if otras:
            return max(otras, key=lambda e: reglas.hora_salida(e, cfg))
    return None


def _copia(v: Vista) -> Vista:
    """Para calcular sin tocar los estados (_preparar los completa)."""
    return replace(v, e=dict(v.e), sale=dict(v.sale) if v.sale else None)


def _descripcion(manual: str, auto: str | None) -> str:
    """Lo del anfitrión encima del separador y lo automático debajo (sin `auto`, la descripción es toda suya)."""
    if auto is None:
        return manual
    return (manual + "\n\n" if manual else "") + SEPARADOR + "\n" + auto


def _sin_etiquetas(texto: str, tipo: str) -> str:
    """El texto sin las etiquetas #tipo (#entrada o #salida); una línea que solo tenía etiquetas desaparece."""
    lineas = []
    for l in texto.splitlines():
        sin = reglas.ETIQUETA_ACUERDO.sub(lambda m: "" if m.group(1).lower() == tipo else m.group(0), l)
        if sin == l:
            lineas.append(l)
        elif sin.strip():
            lineas.append(re.sub(r"\s{2,}", " ", sin).strip())
    return "\n".join(lineas).strip()


def _lineas_con(texto: str, tipo: str) -> str:
    """Las líneas con alguna etiqueta #tipo."""
    return "\n".join(l for l in texto.splitlines()
                     if any(m.group(1).lower() == tipo for m in reglas.ETIQUETA_ACUERDO.finditer(l)))


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
