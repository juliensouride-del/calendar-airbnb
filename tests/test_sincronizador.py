"""
Simulación de extremo a extremo (sin red): el código real con imitaciones de la
API REST de Google Calendar, el buzón de Yahoo, Gemini y el SMTP.
"""
from __future__ import annotations

import copy
import itertools
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import requests

from airbnb_calendar.calendario import Calendario
from airbnb_calendar.gemini import AnalizadorMensajes
from airbnb_calendar.registro import Registro
from airbnb_calendar.sincronizador import Sincronizador
from airbnb_calendar.yahoo import Correo

from conftest import cargar_ejemplo, cargar_eml, config_prueba

# ---------------------------------------------------------------------------
#  Imitaciones
# ---------------------------------------------------------------------------


class Respuesta:
    def __init__(self, codigo, datos=None, texto=""):
        self.status_code = codigo
        self._datos = datos
        self.text = texto or (json.dumps(datos) if datos is not None else "")
        self.content = self.text.encode()

    def json(self):
        return self._datos


class CalendarFalso:
    """API REST v3 de Google Calendar en memoria (list con privateExtendedProperty, insert, patch, delete)."""

    def __init__(self):
        self.eventos: dict[str, dict] = {}
        self.ids = itertools.count(1)
        self.fallar_escrituras = 0

    def request(self, metodo, url, params=None, json=None, timeout=None):
        assert url.startswith("https://www.googleapis.com/calendar/v3/calendars/cal-prueba/events")
        if metodo == "GET":
            filtros = [v.split("=", 1) for k, v in (params or []) if k == "privateExtendedProperty"]
            items = [e for e in self.eventos.values()
                     if all(e["extendedProperties"]["private"].get(k) == v for k, v in filtros)]
            return Respuesta(200, {"items": copy.deepcopy(items)})
        if self.fallar_escrituras:
            self.fallar_escrituras -= 1
            return Respuesta(503, texto="backendError")
        if metodo == "POST":
            ev = copy.deepcopy(json)
            ev["id"] = f"ev{next(self.ids)}"
            ev["extendedProperties"]["private"] = {k: v for k, v in ev["extendedProperties"]["private"].items() if v is not None}
            self.eventos[ev["id"]] = ev
            return Respuesta(200, copy.deepcopy(ev))
        if metodo == "DELETE":
            return Respuesta(204) if self.eventos.pop(url.rsplit("/", 1)[1], None) else Respuesta(410, texto="deleted")
        if metodo == "PATCH":
            ev = self.eventos[url.rsplit("/", 1)[1]]
            for k, v in json.items():
                if k == "extendedProperties":       # PATCH fusiona los objetos; null borra la clave
                    priv = ev["extendedProperties"]["private"]
                    for pk, pv in v["private"].items():
                        priv.pop(pk, None) if pv is None else priv.__setitem__(pk, pv)
                else:
                    ev[k] = copy.deepcopy(v)
            return Respuesta(200, copy.deepcopy(ev))
        raise AssertionError(metodo)

    def ev(self, codigo_o_alias):
        for e in self.eventos.values():
            p = e["extendedProperties"]["private"]
            if p.get("airbnb_codigo") == codigo_o_alias or codigo_o_alias in e["summary"]:
                return e
        return None


class GeminiFalso:
    """Respuestas fijas que imitan lo que debería contestar el modelo."""

    def __init__(self, modo="ok", colgado=None):
        self.modo, self.colgado, self.llamadas = modo, colgado, []

    def post(self, url, json=None, headers=None, timeout=None):
        self.llamadas.append(url)
        if self.colgado and self.colgado in url:
            raise requests.ReadTimeout("timeout")
        if self.modo == "falla":
            return Respuesta(503, texto='{"error":"overloaded"}')
        txt = json["contents"][0]["parts"][0]["text"]
        huesped = txt.split('"""')[1]
        enviado = re.search(r"Mensaje enviado el: \d{4}-\d{2}-\d{2} (\d{2}):(\d{2})", txt)
        r = {"hora_llegada": None, "hora_salida": None, "sofa_cama": "sin_mencion", "cuna": "sin_mencion",
             "otras_peticiones": [], "resumen": ""}
        if re.search(r"crib|cot\b", huesped, re.I): r["cuna"] = "si"
        if re.search(r"sofa bed", huesped, re.I): r["sofa_cama"] = "si"
        if re.search(r"around 6pm", huesped, re.I): r["hora_llegada"] = "18:00"
        if re.search(r"arrive before 1500", huesped, re.I): r["hora_llegada"] = "15:00"
        if re.search(r"in one hour", huesped, re.I) and enviado:
            r["hora_llegada"] = f"{int(enviado.group(1)) + 1:02d}:{enviado.group(2)}"
        if re.search(r"around noon", huesped, re.I): r["otras_peticiones"].append("Pregunta si puede entrar hacia las 12:00")
        if re.search(r"out right before 11", huesped, re.I): r["hora_salida"] = "11:00"
        if r["cuna"] == "si" or r["sofa_cama"] == "si" or r["hora_llegada"] or r["hora_salida"]:
            r["resumen"] = "Resumen simulado."
        return Respuesta(200, {"candidates": [{"content": {"parts": [{"text": __import__("json").dumps(r)}]}}]})


class BuzonFalso:
    def __init__(self):
        self.correos: list[Correo] = []
        self.n = itertools.count(1)

    def recibir(self, e, remitente="Airbnb <automated@airbnb.com>") -> str:
        c = Correo(f"INBOX:1700000000:{next(self.n)}", e.fecha, remitente, e.asunto, e.cuerpo)
        self.correos.append(c)
        return c.id

    def correos_desde(self, desde, carpetas, remitentes, excluir=frozenset()):
        return sorted((c for c in self.correos if c.fecha >= desde and c.id not in excluir), key=lambda c: c.fecha)


class Entorno:
    def __init__(self, tmp_path, gemini="ok", colgado=None, **cfg):
        self.cfg = config_prueba(**{"procesar_desde": "2026-09-01", "ventana_dias": 400, **cfg})
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.cal = CalendarFalso()
        self.buzon = BuzonFalso()
        self.gemini = GeminiFalso(gemini, colgado)
        self.avisos: list[tuple[str, str]] = []          # al anfitrión
        self.tecnicos: list[tuple[str, str]] = []        # al desarrollador (ntfy)
        self.ruta_estado = tmp_path / "estado.json"
        self.ahora = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)

    def ejecutar(self):
        """Una ejecución completa, como en GitHub Actions: carga el registro, procesa y lo guarda."""
        registro = Registro(self.ruta_estado)
        notificador = type("N", (), {"enviar": lambda _, a, c: self.avisos.append((a, c))})()
        tecnico = type("T", (), {"enviar": lambda _, a, c: self.tecnicos.append((a, c))})()
        analizador = AnalizadorMensajes(self.cfg, "clave-falsa", sesion=self.gemini, dormir=lambda s: None)
        Sincronizador(self.cfg, self.buzon, Calendario(self.cfg, self.cal, dormir=lambda s: None), analizador, notificador,
                      registro, ahora=self.ahora, notificador_tecnico=tecnico).ejecutar()
        registro.guardar()
        return registro

    @property
    def asuntos(self):
        return [a for a, _ in self.avisos]


def hora(ev, campo="start"):
    return ev[campo]["dateTime"].replace("T", " ")[:16]

# ---------------------------------------------------------------------------
#  Correos sintéticos construidos a partir de las plantillas reales
# ---------------------------------------------------------------------------


CONF_IMANE = cargar_ejemplo("ej0.txt", "2026-10-01")[0]
CANC_SAM = cargar_ejemplo("ej1.txt", "2026-10-01")[0]
MENSAJE_IMANE = replace(CONF_IMANE, fecha_ref="2026-10-03", fecha_hora="2026-10-03 10:00",
                        asunto="RE: Reservation for Espacioso y luminoso apartamento Corazón de Madrid, Nov 26 – Dec 2",
                        cuerpo="\n".join([
                            "Reservation for Espacioso y luminoso apartamento Corazón de Madrid, Nov 26 – Dec 2",
                            "For your protection and safety, always communicate through Airbnb.", "",
                            "Imane", "Imane", "Booker", "",
                            "Hi! We will arrive around 6pm and we will need the sofa bed for my sister, thanks!", "",
                            "Reply", "You can also respond by replying directly to this email.", "",
                            "Espacioso y luminoso apartamento Corazón de Madrid", "Rental unit - Entire home/apt hosted by Julian", "",
                            "Julian", "Check-in", "", "Thursday", "November 26, 2026", "", "3:00 PM", "",
                            "Checkout", "", "Wednesday", "December 2, 2026", "", "11:00 AM", "", "Guests", "2 adults"]))
CANC_IMANE = replace(CANC_SAM, fecha_hora="2026-10-04 12:00", fecha_ref="2026-10-04",
                     cuerpo=re.sub(r"Feb 1\s*–\s*4, 2027", "Nov 26 – Dec 2, 2026",
                                   re.sub(r"Feb 1\s*–\s*4, 1 guest", "Nov 26 – Dec 2, 2 guests",
                                          CANC_SAM.cuerpo.replace("HMCJH8BNX4", "HMTSBCMZH9"))).replace("guest Sam", "guest Imane"))

# ---------------------------------------------------------------------------


def test_A_confirmacion_mensaje_reejecucion_idempotente_cancelacion(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_IMANE)
    s.ejecutar()
    assert len(s.cal.eventos) == 1, "un único evento por reserva"
    entrada = s.cal.ev("HMTSBCMZH9")
    assert entrada["summary"] == "🏠 Corazón de Madrid · 2 pers"
    assert hora(entrada) == "2026-11-26 15:00" and hora(entrada, "end") == "2026-12-02 11:00", "de la entrada a la salida"
    assert entrada["start"]["timeZone"] == "Europe/Madrid"
    assert entrada["colorId"] == "10"
    assert entrada["extendedProperties"]["private"]["airbnb_origen"] == "airbnb-calendar"
    assert len(s.gemini.llamadas) == 1, "Gemini analiza el mensaje inicial (francés)"
    assert s.avisos == [], "sin peticiones → sin aviso"

    s.buzon.recibir(MENSAJE_IMANE)
    s.ejecutar()
    assert len(s.cal.eventos) == 1, "no se duplican eventos"
    entrada = s.cal.ev("HMTSBCMZH9")
    assert entrada["summary"] == "🏠 Corazón de Madrid · 2 pers · llega 18:00 · 🛋️ sofá cama"
    assert hora(entrada) == "2026-11-26 18:00", "el evento se mueve a la hora de llegada"
    assert "🕒 Hora prevista de llegada: 18:00" in entrada["description"]
    assert len(s.avisos) == 1 and "sofá cama" in s.asuntos[0]
    assert "we will need the sofa bed" in s.avisos[0][1]
    assert "Correo en Yahoo Mail: «RE: Reservation for" in s.avisos[0][1]

    llamadas = len(s.gemini.llamadas)
    s.ejecutar()
    assert len(s.gemini.llamadas) == llamadas, "re-ejecutar no reprocesa (registro persistente)"
    assert len(s.avisos) == 1, "re-ejecutar no repite avisos"

    s.buzon.recibir(CANC_IMANE)
    s.ejecutar()
    entrada = s.cal.ev("HMTSBCMZH9")
    assert entrada["summary"].startswith("❌ CANCELADA · 🏠 Corazón de Madrid")
    assert entrada["colorId"] == "8"
    assert entrada["description"].startswith("❌ RESERVA CANCELADA")
    assert "Huésped: Imane Bensalah" in entrada["description"]
    assert len(s.cal.eventos) == 1 and len(s.avisos) == 1


def test_B_mensajes_de_reserva_que_no_esta_en_el_calendario(tmp_path):
    s = Entorno(tmp_path)
    ids = [s.buzon.recibir(c) for c in cargar_ejemplo("ej3.txt", "2026-09-22")]
    registro = s.ejecutar()
    assert s.cal.eventos == {}
    assert len(s.avisos) == 1, s.asuntos
    asunto, cuerpo = s.avisos[0]
    assert "Antón Martín" in asunto and "cuna" in asunto
    assert "NO está en el calendario" in cuerpo
    assert "Pregunta si puede entrar hacia las 12:00" in cuerpo
    assert all(registro.ya_procesado(i) for i in ids)


def test_C_crear_desde_mensaje_ej3_y_ej2_con_horas_cuna_y_avisos(tmp_path):
    s = Entorno(tmp_path, crear_evento_desde_mensaje=True)
    for c in cargar_ejemplo("ej3.txt", "2026-09-22") + cargar_ejemplo("ej2.txt", "2026-10-01"):
        s.buzon.recibir(c)
    s.ejecutar()
    assert len(s.cal.eventos) == 2
    e3 = s.cal.ev("Antón Martín")
    assert e3["summary"] == "🏠 Antón Martín · 3 pers · llega 15:05 · sale 11:00 · 🍼 cuna"
    assert hora(e3) == "2026-09-23 15:05"
    assert "🍼 Cuna: SÍ — lo indica el huésped" in e3["description"]
    assert "Pregunta si puede entrar hacia las 12:00" in e3["description"]
    e2 = s.cal.ev("Lavapiés")
    assert e2["summary"] == "🏠 Lavapiés · 3 pers · llega 15:00"
    assert e2["start"]["dateTime"].startswith("2026-10-09")
    # Un único aviso (cuna + petición, en el primer mensaje); las horas no avisan (avisar_horas = false)
    assert len(s.avisos) == 1 and "cuna" in s.asuntos[0] and "petición" in s.asuntos[0]


def test_D_gemini_caido_palabras_clave_nota_y_un_solo_aviso_al_dia(tmp_path):
    s = Entorno(tmp_path, gemini="falla", crear_evento_desde_mensaje=True)
    for c in cargar_ejemplo("ej3.txt", "2026-09-22")[:2]:
        s.buzon.recibir(c)
    s.ejecutar()
    e = s.cal.ev("Antón Martín")
    assert "🍼 cuna" in e["summary"]
    assert "Mensaje sin analizar por la IA" in e["description"]
    assert sum("Gemini no responde" in a for a, _ in s.tecnicos) == 1
    assert not any("Gemini" in a for a in s.asuntos), "los avisos técnicos no llegan al cliente"
    assert sum("cuna" in a for a in s.asuntos) == 1
    s.buzon.recibir(cargar_ejemplo("ej3.txt", "2026-09-22")[2])
    s.ejecutar()
    assert sum("Gemini no responde" in a for a, _ in s.tecnicos) == 1, "el aviso de Gemini se limita a uno al día"


def test_E_plantilla_irreconocible_aviso_y_sin_reintentos(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(replace(CONF_IMANE, asunto="New booking confirmed!", cuerpo="New booking confirmed! Bob arrives soon.\nThis template changed."))
    s.ejecutar()
    assert len(s.avisos) == 1 and "No se pudieron extraer: codigo" in s.avisos[0][1]
    s.ejecutar()
    assert len(s.avisos) == 1, "no se vuelve a intentar"


def test_F_correos_de_airbnb_que_no_son_reservas_se_ignoran(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(replace(CONF_IMANE, asunto="Your monthly earnings report", cuerpo="Here is how you did this month."))
    s.ejecutar()
    assert s.cal.eventos == {} and s.avisos == []


def test_G_eml_reales_confirmacion_y_mensaje_con_hora_sofa_y_cuna(tmp_path):
    s = Entorno(tmp_path)
    conf = cargar_eml("confirmed.eml")
    s.buzon.recibir(conf)
    s.ejecutar()
    assert len(s.cal.eventos) == 1
    entrada = s.cal.ev("HMMW9HP8JC")
    assert entrada["summary"] == "🏠 Antón Martín · 2 pers"
    assert hora(entrada) == "2026-10-20 15:00"
    assert entrada["end"]["dateTime"].startswith("2026-10-23")

    # Mensaje con la plantilla real de cunasolicitud.eml, adaptado a la reserva de Jennifer
    base = cargar_eml("cunasolicitud.eml")
    cuerpo = base.cuerpo
    for viejo, nuevo in [(r"SEP 16\s*–\s*19", "OCT 20 – 23"), (r"WEDNESDAY(\s+)SATURDAY", r"TUESDAY\1FRIDAY"),
                         (r"September 16, 2026(\s+)September 19, 2026", r"October 20, 2026\1October 23, 2026"),
                         (r"2 adults, 1 infant", "2 adults"), (r"AGUSTIN", "JENNIFER"),
                         (r'Reacted ❤️ to "Hi Agustin! Yes no problem! "',
                          "Hi! We will arrive around 6pm, we will need the sofa bed for my son and a crib, thanks!")]:
        cuerpo = re.sub(viejo, nuevo, cuerpo)
    s.buzon.recibir(replace(base, fecha_hora="2026-10-07 09:00", fecha_ref="2026-10-07", cuerpo=cuerpo,
                            asunto="RE: Reservation for Apartamentos de Julian, Anton Martin - Apartame..., Oct 20 – 23"))
    s.ejecutar()
    assert len(s.cal.eventos) == 1, "no se duplican eventos"
    entrada = s.cal.ev("HMMW9HP8JC")
    assert entrada["summary"].startswith("🏠 Antón Martín · 2 pers · llega 18:00"), entrada["summary"]
    assert "🛋️ sofá cama" in entrada["summary"] and "🍼 cuna" in entrada["summary"]
    assert "🏠 Alojamiento: Anton Martin Agradable" in entrada["description"]
    assert len(s.avisos) == 1


def test_H_modelo_gemini_colgado_timeout_y_respaldo(tmp_path):
    s = Entorno(tmp_path, colgado="gemini-principal", crear_evento_desde_mensaje=True,
                gemini_modelos_respaldo=["gemini-respaldo"])
    for c in cargar_ejemplo("ej3.txt", "2026-09-22")[:2]:
        s.buzon.recibir(c)
    s.ejecutar()
    assert any("gemini-respaldo" in u for u in s.gemini.llamadas)
    assert "Mensaje sin analizar por la IA" not in s.cal.ev("Antón Martín")["description"]
    assert not any("Gemini no responde" in a for a, _ in s.tecnicos)


def test_I_error_temporal_de_calendar_se_reintenta_y_luego_es_definitivo(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_IMANE)
    s.cal.fallar_escrituras = 1
    reg = s.ejecutar()
    assert len(s.cal.eventos) == 1 and not reg.fallos, "un micro-corte se resuelve dentro de la misma ejecución"

    s = Entorno(tmp_path / "corte")
    s.buzon.recibir(CONF_IMANE)
    s.cal.fallar_escrituras = 4                          # más que los reintentos internos
    reg = s.ejecutar()
    assert s.cal.eventos == {} and s.avisos == [] and reg.fallos, "se reintentará en la próxima ejecución"
    s.ejecutar()
    assert len(s.cal.eventos) == 1 and not Registro(s.ruta_estado).fallos

    s2 = Entorno(tmp_path / "otro")
    s2.buzon.recibir(CONF_IMANE)
    s2.cal.fallar_escrituras = 99
    for _ in range(3):
        s2.ejecutar()
    assert len(s2.avisos) == 1 and "HTTP 503" in s2.avisos[0][1], "tras 3 intentos se avisa y se deja"
    s2.ejecutar()
    assert len(s2.avisos) == 1


def test_J_remitente_que_no_es_de_airbnb_y_primera_ejecucion_sin_correos_antiguos(tmp_path):
    s = Entorno(tmp_path)
    id_falso = s.buzon.recibir(CONF_IMANE, remitente="Fake <airbnb.com@gmail.com>")
    reg = s.ejecutar()
    assert s.cal.eventos == {} and reg.ya_procesado(id_falso)

    s2 = Entorno(tmp_path / "x", procesar_desde="")
    s2.buzon.recibir(CONF_IMANE)                         # llegó el 01/10, antes de la primera ejecución
    reg = s2.ejecutar()
    assert s2.cal.eventos == {}, "no se cargan reservas anteriores a la instalación"
    assert reg.inicio == s2.ahora.isoformat()
    s2.buzon.recibir(replace(MENSAJE_IMANE, fecha_hora="2026-10-08 15:00"))     # posterior: sí
    s2.ahora += timedelta(hours=2)
    s2.ejecutar()
    assert len(s2.avisos) == 1 and "NO está en el calendario" in s2.avisos[0][1]


def test_K_estado_largo_se_trocea_y_al_acortarse_se_borran_los_trozos_sobrantes(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_IMANE)
    s.ejecutar()
    cal = Calendario(s.cfg, s.cal)
    eventos = cal.buscar_eventos_reserva("HMTSBCMZH9", None, None, None)
    from airbnb_calendar.calendario import leer_estado
    estado = leer_estado(eventos)
    estado["notas"] = [{"f": "01/10", "t": "x" * 200}] * 5
    cal.guardar_reserva(estado, eventos, None)
    props = s.cal.ev("HMTSBCMZH9")["extendedProperties"]["private"]
    n = int(props["airbnb_estado_n"])
    assert n >= 2 and all(len(props[f"airbnb_estado_{i}"]) <= 900 for i in range(n))
    estado["notas"] = []
    cal.guardar_reserva(estado, cal.buscar_eventos_reserva("HMTSBCMZH9", None, None, None), None)
    props = s.cal.ev("HMTSBCMZH9")["extendedProperties"]["private"]
    assert props["airbnb_estado_n"] == "1" and "airbnb_estado_1" not in props
    assert leer_estado(cal.buscar_eventos_reserva("HMTSBCMZH9", None, None, None))["notas"] == []


# ---------------------------------------------------------------------------
#  Evento único, limpieza entre reservas y purga
# ---------------------------------------------------------------------------


def _reserva(codigo, checkin, checkout, alojamiento="Lavapies - Peaceful 2 bedrooms Fresh A/C", **kw):
    from airbnb_calendar import reglas
    e = reglas.crear_estado()
    e.update(codigo=codigo, alojamiento=alojamiento, alias="Lavapiés", checkin=checkin, checkout=checkout,
             hora_checkin="15:00", hora_checkout="11:00", total=2, **kw)
    return e


def _guardar(cal, e):
    cal.guardar_reserva(e, cal.buscar_eventos_reserva(e["codigo"], e["alojamiento"], e["checkin"], e["checkout"]), None)


def test_L_rotacion_limpieza_ok_alerta_y_recalculo_al_cambiar_la_salida(tmp_path):
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("SALE1", "2026-11-01", "2026-11-05"))
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08"))
    ev = s.cal.ev("ENTRA2")
    assert ev["summary"] == "[LIMPIEZA OK] 🏠 Lavapiés · 2 pers"
    assert "Ventana: 05/11 de 11:00 a 15:00" in ev["description"]
    assert "🔑 Entrega de llaves en persona a las 15:00" in ev["description"]
    assert not s.cal.ev("SALE1")["summary"].startswith("["), "la reserva sin huésped anterior no lleva etiqueta"

    # El huésped saliente dice que se va a las 13:30: sin acuerdo expreso se mantiene la salida estándar (11:00)
    _guardar(cal, _reserva("SALE1", "2026-11-01", "2026-11-05", hora_salida="13:30"))
    ev = s.cal.ev("ENTRA2")
    assert ev["summary"] == "[LIMPIEZA OK] 🏠 Lavapiés · 2 pers", "se recalcula y sigue OK"
    assert "sale hoy mismo a las 11:00" in ev["description"]
    assert s.cal.ev("SALE1")["end"]["dateTime"].startswith("2026-11-05T11:00")

    # Si cancela el que sale, ya no hay rotación
    _guardar(cal, _reserva("SALE1", "2026-11-01", "2026-11-05", hora_salida="13:30", cancelada=True))
    assert s.cal.ev("ENTRA2")["summary"] == "🏠 Lavapiés · 2 pers"


def test_M_reglas_de_limpieza():
    from airbnb_calendar.reglas import evaluar_limpieza
    from conftest import config_prueba
    cfg = config_prueba()
    sale = _reserva("A", "2026-11-01", "2026-11-05")
    # Pedir entrar antes de las 15:00 o salir después de las 11:00 no cambia el horario estándar
    r = evaluar_limpieza(sale, _reserva("B", "2026-11-05", "2026-11-09", hora_llegada="12:30"), cfg)
    assert r["ok"] and r["entrada"] == "15:00"
    r = evaluar_limpieza(_reserva("A", "2026-11-01", "2026-11-05", hora_salida="14:00"), _reserva("B", "2026-11-05", "2026-11-09"), cfg)
    assert r["ok"] and r["salida"] == "11:00"
    # Salir antes de las 11:00 sí se respeta
    assert evaluar_limpieza(_reserva("A", "2026-11-01", "2026-11-05", hora_salida="09:30"),
                            _reserva("B", "2026-11-05", "2026-11-09"), cfg)["salida"] == "09:30"
    # Llega a las 18:00: el equipo igualmente tiene que acabar a las 15:00 (11:00–15:00 = 4 h)
    r = evaluar_limpieza(sale, _reserva("B", "2026-11-05", "2026-11-09", hora_llegada="18:00"), cfg)
    assert r["ok"] and r["hasta"] == "15:00" and r["entrada"] == "18:00"
    # Con un día entero de margen, no hay rotación
    assert evaluar_limpieza(_reserva("A", "2026-11-01", "2026-11-03"), _reserva("B", "2026-11-05", "2026-11-09"), cfg) is None


def test_N_reserva_del_formato_anterior_se_unifica_en_un_evento(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_IMANE)
    s.ejecutar()
    ev = next(iter(s.cal.eventos.values()))
    # Simula el formato anterior: el evento pasa a 'entrada' y se añade su 'salida'
    ev["extendedProperties"]["private"]["airbnb_tipo"] = "entrada"
    salida = copy.deepcopy(ev)
    salida["id"] = "antiguo-salida"
    salida["extendedProperties"]["private"]["airbnb_tipo"] = "salida"
    s.cal.eventos[salida["id"]] = salida
    s.buzon.recibir(MENSAJE_IMANE)
    s.ejecutar()
    assert list(s.cal.eventos) == [ev["id"]], "queda un único evento"
    assert s.cal.eventos[ev["id"]]["extendedProperties"]["private"]["airbnb_tipo"] == "reserva"


def test_O_purga_solo_borra_eventos_propios(tmp_path, monkeypatch):
    import sincronizar
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_IMANE)
    s.ejecutar()
    s.cal.eventos["personal"] = {"id": "personal", "summary": "Dentista",
                                 "extendedProperties": {"private": {"otra_app": "1"}}}
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    assert sincronizar.purgar(cal, simulacion=True, confirmado=False) == 0
    assert len(s.cal.eventos) == 2, "--dry-run no borra nada"
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "no")
    sincronizar.purgar(cal, simulacion=False, confirmado=False)
    assert len(s.cal.eventos) == 2, "sin confirmar no se borra"
    sincronizar.purgar(cal, simulacion=False, confirmado=True)
    assert list(s.cal.eventos) == ["personal"]


def test_P_limpieza_con_salida_el_dia_anterior():
    from airbnb_calendar.reglas import evaluar_limpieza
    from conftest import config_prueba
    r = evaluar_limpieza(_reserva("A", "2026-11-01", "2026-11-04"), _reserva("B", "2026-11-05", "2026-11-09"),
                         config_prueba())
    assert r["ok"] and not r["mismo_dia"]
    assert (r["dia"], r["desde"], r["hasta"]) == ("2026-11-05", "08:00", "15:00"), "se elige la ventana más amplia"


def test_Q_cada_ejecucion_recalcula_limpiezas_sin_correos_nuevos(tmp_path):
    s = Entorno(tmp_path)
    s.ahora = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("SALE1", "2026-11-01", "2026-11-05"))
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08"))
    assert s.cal.ev("ENTRA2")["summary"].startswith("[LIMPIEZA OK]")
    # Cambio de fechas fuera de este flujo (p. ej. modificación revisada a mano): la salida pasa al 03/11
    sale = next(e for e in s.cal.eventos.values() if e["extendedProperties"]["private"].get("airbnb_codigo") == "SALE1")
    s.cal.eventos.pop(sale["id"])
    _guardar(cal, _reserva("SALE1", "2026-11-01", "2026-11-03"))
    ev = s.cal.ev("ENTRA2")
    ev["summary"] = "[LIMPIEZA OK] 🏠 Lavapiés · 2 pers"                       # sigue con la etiqueta antigua
    ev["extendedProperties"]["private"]["airbnb_limpieza"] = "obsoleta"
    s.ejecutar()                                                              # sin correos
    assert s.cal.ev("ENTRA2")["summary"] == "🏠 Lavapiés · 2 pers"


def test_R_migrar_formato_anterior(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_IMANE)
    s.ejecutar()
    ev = next(iter(s.cal.eventos.values()))
    ev["extendedProperties"]["private"]["airbnb_tipo"] = "entrada"
    del ev["extendedProperties"]["private"]["airbnb_origen"]
    salida = copy.deepcopy(ev)
    salida["id"] = "antiguo-salida"
    salida["extendedProperties"]["private"]["airbnb_tipo"] = "salida"
    s.cal.eventos[salida["id"]] = salida
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    assert cal.migrar(simulacion=True) == 1 and len(s.cal.eventos) == 2, "la simulación no cambia nada"
    assert cal.migrar() == 1
    assert list(s.cal.eventos) == [ev["id"]]
    p = s.cal.eventos[ev["id"]]["extendedProperties"]["private"]
    assert p["airbnb_tipo"] == "reserva" and p["airbnb_origen"] == "airbnb-calendar"
    assert cal.migrar() == 0, "idempotente"


def test_S_listado_paginado(tmp_path):
    s = Entorno(tmp_path)
    paginas = {None: {"items": [{"id": "a"}], "nextPageToken": "t2"}, "t2": {"items": [{"id": "b"}]}}
    s.cal.request = lambda m, u, params=None, **kw: Respuesta(200, paginas[dict(params).get("pageToken")])
    assert [e["id"] for e in Calendario(s.cfg, s.cal)._listar({"x": "1"})] == ["a", "b"]


def test_T_ntfy_sin_tema_no_envia_y_con_tema_publica():
    from airbnb_calendar.ntfy import NotificadorNtfy
    enviados = []
    sesion = type("S", (), {"post": lambda _, url, **kw: enviados.append((url, kw)) or
                            type("R", (), {"raise_for_status": lambda self: None})()})()
    NotificadorNtfy("", sesion).enviar("x", "y")
    assert enviados == []
    NotificadorNtfy("tema-secreto", sesion).enviar("Gemini no responde", "detalle")
    assert enviados[0][0] == "https://ntfy.sh/tema-secreto" and enviados[0][1]["params"]["title"] == "Gemini no responde"
