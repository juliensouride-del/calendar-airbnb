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
    """API REST v3 de Google Calendar en memoria (list con privateExtendedProperty, insert, patch)."""

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

    def ev(self, codigo_o_alias, tipo):
        for e in self.eventos.values():
            p = e["extendedProperties"]["private"]
            if p["airbnb_tipo"] == tipo and (p.get("airbnb_codigo") == codigo_o_alias or codigo_o_alias in e["summary"]):
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
        self.avisos: list[tuple[str, str]] = []
        self.ruta_estado = tmp_path / "estado.json"
        self.ahora = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)

    def ejecutar(self):
        """Una ejecución completa, como en GitHub Actions: carga el registro, procesa y lo guarda."""
        registro = Registro(self.ruta_estado)
        notificador = type("N", (), {"enviar": lambda _, a, c: self.avisos.append((a, c))})()
        analizador = AnalizadorMensajes(self.cfg, "clave-falsa", sesion=self.gemini, dormir=lambda s: None)
        Sincronizador(self.cfg, self.buzon, Calendario(self.cfg, self.cal, dormir=lambda s: None), analizador, notificador,
                      registro, ahora=self.ahora).ejecutar()
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
    assert len(s.cal.eventos) == 2
    entrada, salida = s.cal.ev("HMTSBCMZH9", "entrada"), s.cal.ev("HMTSBCMZH9", "salida")
    assert entrada["summary"] == "🟢 Check-in · Corazón de Madrid · 2 pers"
    assert hora(entrada) == "2026-11-26 15:00" and hora(entrada, "end") == "2026-11-26 15:30"
    assert entrada["start"]["timeZone"] == "Europe/Madrid"
    assert entrada["colorId"] == "10"
    assert salida["summary"] == "🔴 Check-out · Corazón de Madrid · 2 pers"
    assert hora(salida) == "2026-12-02 11:00" and salida["colorId"] == "11"
    assert len(s.gemini.llamadas) == 1, "Gemini analiza el mensaje inicial (francés)"
    assert s.avisos == [], "sin peticiones → sin aviso"

    s.buzon.recibir(MENSAJE_IMANE)
    s.ejecutar()
    assert len(s.cal.eventos) == 2, "no se duplican eventos"
    entrada = s.cal.ev("HMTSBCMZH9", "entrada")
    assert entrada["summary"] == "🟢 Check-in · Corazón de Madrid · 2 pers · llega 18:00 · 🛋️ sofá cama"
    assert hora(entrada) == "2026-11-26 18:00", "el evento se mueve a la hora de llegada"
    assert "🛋️ sofá cama" in s.cal.ev("HMTSBCMZH9", "salida")["summary"]
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
    entrada = s.cal.ev("HMTSBCMZH9", "entrada")
    assert entrada["summary"].startswith("❌ CANCELADA · 🟢 Check-in · Corazón de Madrid")
    assert s.cal.ev("HMTSBCMZH9", "salida")["summary"].startswith("❌ CANCELADA")
    assert entrada["colorId"] == "8"
    assert entrada["description"].startswith("❌ RESERVA CANCELADA")
    assert "Huésped: Imane Bensalah" in entrada["description"]
    assert len(s.cal.eventos) == 2 and len(s.avisos) == 1


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
    assert len(s.cal.eventos) == 4
    e3, x3 = s.cal.ev("Antón Martín", "entrada"), s.cal.ev("Antón Martín", "salida")
    assert e3["summary"] == "🟢 Check-in · Antón Martín · 3 pers · llega 15:05 · 🍼 cuna"
    assert hora(e3) == "2026-09-23 15:05"
    assert x3["summary"] == "🔴 Check-out · Antón Martín · 3 pers · sale 11:00 · 🍼 cuna"
    assert "🍼 Cuna: SÍ — lo indica el huésped" in e3["description"]
    assert "Pregunta si puede entrar hacia las 12:00" in e3["description"]
    e2 = s.cal.ev("Lavapiés", "entrada")
    assert e2["summary"] == "🟢 Check-in · Lavapiés · 3 pers · llega 15:00"
    assert e2["start"]["dateTime"].startswith("2026-10-09")
    assert s.cal.ev("Lavapiés", "salida")["summary"] == "🔴 Check-out · Lavapiés · 3 pers"
    # Un único aviso (cuna + petición, en el primer mensaje); las horas no avisan (avisar_horas = false)
    assert len(s.avisos) == 1 and "cuna" in s.asuntos[0] and "petición" in s.asuntos[0]


def test_D_gemini_caido_palabras_clave_nota_y_un_solo_aviso_al_dia(tmp_path):
    s = Entorno(tmp_path, gemini="falla", crear_evento_desde_mensaje=True)
    for c in cargar_ejemplo("ej3.txt", "2026-09-22")[:2]:
        s.buzon.recibir(c)
    s.ejecutar()
    e = s.cal.ev("Antón Martín", "entrada")
    assert "🍼 cuna" in e["summary"]
    assert "Mensaje sin analizar por la IA" in e["description"]
    assert sum("Gemini no responde" in a for a in s.asuntos) == 1
    assert sum("cuna" in a for a in s.asuntos) == 1
    s.buzon.recibir(cargar_ejemplo("ej3.txt", "2026-09-22")[2])
    s.ejecutar()
    assert sum("Gemini no responde" in a for a in s.asuntos) == 1, "el aviso de Gemini se limita a uno al día"


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
    assert len(s.cal.eventos) == 2
    entrada = s.cal.ev("HMMW9HP8JC", "entrada")
    assert entrada["summary"] == "🟢 Check-in · Antón Martín · 2 pers"
    assert hora(entrada) == "2026-10-20 15:00"
    assert s.cal.ev("HMMW9HP8JC", "salida")["start"]["dateTime"].startswith("2026-10-23")

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
    assert len(s.cal.eventos) == 2, "no se duplican eventos"
    entrada = s.cal.ev("HMMW9HP8JC", "entrada")
    assert entrada["summary"].startswith("🟢 Check-in · Antón Martín · 2 pers · llega 18:00"), entrada["summary"]
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
    assert "Mensaje sin analizar por la IA" not in s.cal.ev("Antón Martín", "entrada")["description"]
    assert not any("Gemini no responde" in a for a in s.asuntos)


def test_I_error_temporal_de_calendar_se_reintenta_y_luego_es_definitivo(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_IMANE)
    s.cal.fallar_escrituras = 1
    reg = s.ejecutar()
    assert len(s.cal.eventos) == 2 and not reg.fallos, "un micro-corte se resuelve dentro de la misma ejecución"

    s = Entorno(tmp_path / "corte")
    s.buzon.recibir(CONF_IMANE)
    s.cal.fallar_escrituras = 4                          # más que los reintentos internos
    reg = s.ejecutar()
    assert s.cal.eventos == {} and s.avisos == [] and reg.fallos, "se reintentará en la próxima ejecución"
    s.ejecutar()
    assert len(s.cal.eventos) == 2 and not Registro(s.ruta_estado).fallos

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
    props = s.cal.ev("HMTSBCMZH9", "entrada")["extendedProperties"]["private"]
    n = int(props["airbnb_estado_n"])
    assert n >= 2 and all(len(props[f"airbnb_estado_{i}"]) <= 900 for i in range(n))
    estado["notas"] = []
    cal.guardar_reserva(estado, cal.buscar_eventos_reserva("HMTSBCMZH9", None, None, None), None)
    props = s.cal.ev("HMTSBCMZH9", "entrada")["extendedProperties"]["private"]
    assert props["airbnb_estado_n"] == "1" and "airbnb_estado_1" not in props
    assert leer_estado(cal.buscar_eventos_reserva("HMTSBCMZH9", None, None, None))["notas"] == []
