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
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import requests

from airbnb_calendar.calendario import Calendario, leer_estado
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
    """API REST v3 de Google Calendar en memoria (list con privateExtendedProperty y timeMin/timeMax, insert, patch,
    delete)."""

    def __init__(self):
        self.eventos: dict[str, dict] = {}
        self.ids = itertools.count(1)
        self.fallar_escrituras = 0
        self.cuenta = "airbnb@pruebas.iam.gserviceaccount.com"   # cuenta de servicio que crea los eventos

    def request(self, metodo, url, params=None, json=None, timeout=None):
        assert url.startswith("https://www.googleapis.com/calendar/v3/calendars/cal-prueba/events")
        if metodo == "GET":
            pares = params.items() if isinstance(params, dict) else (params or [])
            filtros = [v.split("=", 1) for k, v in pares if k == "privateExtendedProperty"]
            rango = {k: datetime.fromisoformat(v) for k, v in pares if k in ("timeMin", "timeMax")}
            items = [e for e in self.eventos.values()
                     if all(e.get("extendedProperties", {}).get("private", {}).get(k) == v for k, v in filtros)
                     and ("timeMin" not in rango or _instante(e["end"]) > rango["timeMin"])
                     and ("timeMax" not in rango or _instante(e["start"]) < rango["timeMax"])]
            return Respuesta(200, {"items": copy.deepcopy(items)})
        if self.fallar_escrituras:
            self.fallar_escrituras -= 1
            return Respuesta(503, texto="backendError")
        if "start" in (json or {}) or "end" in (json or {}):
            actual = self.eventos.get(url.rsplit("/", 1)[1], {}) if metodo == "PATCH" else {}
            inicio, fin = json.get("start") or actual["start"], json.get("end") or actual["end"]
            if ("date" in inicio) != ("date" in fin):
                return Respuesta(400, texto="Start and end times must either both be date or both be dateTime.")
            if _instante(fin) <= _instante(inicio):
                return Respuesta(400, texto="The specified time range is empty.")
        if metodo == "POST":
            ev = copy.deepcopy(json)
            ev["id"] = f"ev{next(self.ids)}"
            ev["creator"] = {"email": self.cuenta}
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

    def ev(self, codigo_o_alias, parte="entrada"):
        """Evento de esa parte de la reserva (un evento sin parte es del formato anterior y hace de entrada)."""
        for e in self.eventos.values():
            p = e["extendedProperties"]["private"]
            if ((p.get("airbnb_codigo") == codigo_o_alias or codigo_o_alias in e["summary"])
                    and (p.get("airbnb_parte") or "entrada") == parte):
                return e
        return None


def _instante(campo):
    if "date" in campo:                         # evento de día completo
        return datetime.fromisoformat(campo["date"]).replace(tzinfo=ZoneInfo("Europe/Madrid"))
    d = datetime.fromisoformat(campo["dateTime"].replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=ZoneInfo(campo.get("timeZone") or "Europe/Madrid"))


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
        if self.modo == "cuota":
            return Respuesta(429, texto='{"error": {"code": 429, "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}')
        txt = json["contents"][0]["parts"][0]["text"]
        huesped = txt.split('"""')[1]
        enviado = re.search(r"Mensaje enviado el: \d{4}-\d{2}-\d{2} (\d{2}):(\d{2})", txt)
        r = {"hora_llegada": None, "hora_salida": None, "sofa_cama": "sin_mencion", "cuna": "sin_mencion",
             "otras_peticiones": []}
        if re.search(r"crib|cot\b", huesped, re.I): r["cuna"] = "si"
        if re.search(r"sofa bed", huesped, re.I): r["sofa_cama"] = "si"
        if re.search(r"around 6pm", huesped, re.I): r["hora_llegada"] = "18:00"
        if re.search(r"arrive before 1500", huesped, re.I): r["hora_llegada"] = "15:00"
        if re.search(r"in one hour", huesped, re.I) and enviado:
            r["hora_llegada"] = f"{int(enviado.group(1)) + 1:02d}:{enviado.group(2)}"
        if re.search(r"around noon", huesped, re.I): r["otras_peticiones"].append("Pregunta si puede entrar hacia las 12:00")
        if re.search(r"out right before 11", huesped, re.I): r["hora_salida"] = "11:00"
        if re.search(r"before 5pm", huesped, re.I): r["hora_llegada"] = "17:00"
        if re.search(r"check-in early", huesped, re.I): r["entrada_anticipada"] = "si"
        if re.search(r"arrive at 12", huesped, re.I): r["hora_llegada"] = "12:00"
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
        self.ahora = datetime(2026, 10, 8, 12, tzinfo=UTC)
        self.fallar_avisos = 0                           # envíos al anfitrión que fallan (SMTP caído)

    def avisar(self, asunto, cuerpo):
        if self.fallar_avisos:
            self.fallar_avisos -= 1
            raise OSError("SMTP de Yahoo no responde")
        self.avisos.append((asunto, cuerpo))

    def ejecutar(self, desde: datetime | None = None):
        """Una ejecución completa, como en GitHub Actions: carga el registro, procesa y lo guarda.
        desde: carga manual (--desde), que vuelve a procesar todos los correos del tramo."""
        registro = Registro(self.ruta_estado)
        notificador = type("N", (), {"enviar": lambda _, a, c: self.avisar(a, c)})()
        tecnico = type("T", (), {"enviar": lambda _, a, c: self.tecnicos.append((a, c))})()
        analizador = AnalizadorMensajes(self.cfg, "clave-falsa", sesion=self.gemini, dormir=lambda s: None)
        Sincronizador(self.cfg, self.buzon, Calendario(self.cfg, self.cal, dormir=lambda s: None), analizador, notificador,
                      registro, ahora=self.ahora, desde=desde, notificador_tecnico=tecnico).ejecutar()
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


CONF_LUCIA = cargar_ejemplo("ej0.txt", "2026-10-01")[0]
CANC_MARC = cargar_ejemplo("ej1.txt", "2026-10-01")[0]
MENSAJE_LUCIA = replace(CONF_LUCIA, fecha_ref="2026-10-03", fecha_hora="2026-10-03 10:00",
                        asunto="RE: Reservation for Espacioso y luminoso apartamento Corazón de Madrid, Nov 26 – Dec 2",
                        cuerpo="\n".join([
                            "Reservation for Espacioso y luminoso apartamento Corazón de Madrid, Nov 26 – Dec 2",
                            "For your protection and safety, always communicate through Airbnb.", "",
                            "Lucia", "Lucia", "Booker", "",
                            "Hi! We will arrive around 6pm and we will need the sofa bed for my sister, thanks!", "",
                            "Reply", "You can also respond by replying directly to this email.", "",
                            "Espacioso y luminoso apartamento Corazón de Madrid", "Rental unit - Entire home/apt hosted by Julian", "",
                            "Julian", "Check-in", "", "Thursday", "November 26, 2026", "", "3:00 PM", "",
                            "Checkout", "", "Wednesday", "December 2, 2026", "", "11:00 AM", "", "Guests", "2 adults"]))
CANC_LUCIA = replace(CANC_MARC, fecha_hora="2026-10-04 12:00", fecha_ref="2026-10-04",
                     cuerpo=re.sub(r"Feb 1\s*–\s*4, 2027", "Nov 26 – Dec 2, 2026",
                                   re.sub(r"Feb 1\s*–\s*4, 1 guest", "Nov 26 – Dec 2, 2 guests",
                                          CANC_MARC.cuerpo.replace("HMTEST0003", "HMTEST0002"))).replace("guest Marc", "guest Lucia"))

# ---------------------------------------------------------------------------


def test_A_confirmacion_mensaje_reejecucion_idempotente_cancelacion(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_LUCIA)
    s.ejecutar()
    assert len(s.cal.eventos) == 2, "una entrada y una salida por reserva"
    entrada, salida = s.cal.ev("HMTEST0002"), s.cal.ev("HMTEST0002", "salida")
    assert entrada["summary"] == "🟢 ENTRADA Corazón de Madrid · 2 pers"
    assert hora(entrada) == "2026-11-26 15:00" and hora(entrada, "end") == "2026-11-26 15:30"
    assert salida["summary"] == "🔴 SALIDA Corazón de Madrid · sale 11:00"
    assert hora(salida) == "2026-12-02 11:00" and hora(salida, "end") == "2026-12-02 11:30"
    assert salida["colorId"] == "11"
    assert entrada["start"]["timeZone"] == "Europe/Madrid"
    assert entrada["colorId"] == "10"
    assert entrada["extendedProperties"]["private"]["airbnb_origen"] == "airbnb-calendar"
    assert len(s.gemini.llamadas) == 1, "Gemini analiza el mensaje inicial (francés)"
    assert s.avisos == [], "sin peticiones → sin aviso"

    s.buzon.recibir(MENSAJE_LUCIA)
    s.ejecutar()
    assert len(s.cal.eventos) == 2, "no se duplican eventos"
    entrada = s.cal.ev("HMTEST0002")
    assert entrada["summary"] == "🟢 ENTRADA Corazón de Madrid · 2 pers · llega 18:00 · 🛋️ sofá cama"
    assert hora(entrada) == "2026-11-26 18:00", "el evento se mueve a la hora de llegada"
    assert "Hora prevista" not in entrada["description"], "la hora ya está en el título"
    assert len(s.avisos) == 1 and "sofá cama" in s.asuntos[0]
    assert "we will need the sofa bed" in s.avisos[0][1]
    assert "Correo en Yahoo Mail: «RE: Reservation for" in s.avisos[0][1]

    llamadas = len(s.gemini.llamadas)
    s.ejecutar()
    assert len(s.gemini.llamadas) == llamadas, "re-ejecutar no reprocesa (registro persistente)"
    assert len(s.avisos) == 1, "re-ejecutar no repite avisos"

    s.buzon.recibir(CANC_LUCIA)
    s.ejecutar()
    entrada = s.cal.ev("HMTEST0002")
    assert entrada["summary"].startswith("❌ CANCELADA · 🟢 ENTRADA Corazón de Madrid")
    assert entrada["colorId"] == "8"
    assert entrada["description"] == "—— Airbnb · automático (escribe tus notas encima de esta línea) ——\n👤 Lucia Romero · Código HMTEST0002", \
        "cancelada: sin datos de preparación"
    assert "👤 Lucia Romero" in entrada["description"]
    assert len(s.cal.eventos) == 2 and len(s.avisos) == 1


def _mensaje_lucia(texto, fecha_hora):
    return replace(MENSAJE_LUCIA, fecha_hora=fecha_hora, fecha_ref=fecha_hora[:10],
                   cuerpo=MENSAJE_LUCIA.cuerpo.replace(
                       "Hi! We will arrive around 6pm and we will need the sofa bed for my sister, thanks!", texto))


def test_A2_segunda_peticion_de_entrar_antes_se_detecta_y_avisa(tmp_path):
    """Caso real (Stina): primero llega 'antes de las 17:00'; después pregunta si puede entrar antes."""
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_LUCIA)
    s.buzon.recibir(_mensaje_lucia("We will arrive before 5pm.", "2026-10-03 10:00"))
    s.ejecutar()
    assert s.avisos == [], "17:00 no es antes de la hora de entrada"
    s.buzon.recibir(_mensaje_lucia("Thank you Julian! Will you notify me when the apartment is ready, "
                                   "so I know if it’s possible to check-in early? :)", "2026-10-05 12:00"))
    s.ejecutar()
    entrada = s.cal.ev("HMTEST0002")
    assert "⏰ pide entrar antes" in entrada["summary"], entrada["summary"]
    assert "escribe arriba: #entrada HH:MM" in entrada["description"]
    assert len(s.avisos) == 1 and "entrar antes" in s.asuntos[0], s.asuntos
    assert "⏰ Pide ENTRAR ANTES de las 15:00" in s.avisos[0][1]
    assert hora(entrada) == "2026-11-26 17:00", "sin acuerdo, el calendario no adelanta la entrada"


def test_A3_sin_gemini_las_palabras_clave_detectan_la_entrada_anticipada(tmp_path):
    s = Entorno(tmp_path, gemini="falla")
    s.buzon.recibir(CONF_LUCIA)
    s.buzon.recibir(_mensaje_lucia("Is it possible to check-in early?", "2026-10-03 10:00"))
    s.ejecutar()
    assert any("entrar antes" in a for a in s.asuntos), s.asuntos


def test_A4_recarga_manual_reconstruye_la_reserva_con_lo_ultimo_del_huesped(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_LUCIA)
    s.buzon.recibir(MENSAJE_LUCIA)
    s.ejecutar()
    # Estado con datos antiguos o mal interpretados por una versión anterior
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    eventos = cal.buscar_eventos_reserva("HMTEST0002", None, None, None)
    e = leer_estado(eventos)
    e.update(hora_llegada="17:00", entrada_anticipada="12:00", peticiones=["Petición antigua"])
    cal.guardar_reserva(e, eventos, None)
    assert "llega 17:00" in s.cal.ev("HMTEST0002")["summary"]

    s.ejecutar(desde=datetime(2026, 9, 1, tzinfo=UTC))
    ev = s.cal.ev("HMTEST0002")
    assert ev["summary"] == "🟢 ENTRADA Corazón de Madrid · 2 pers · llega 18:00 · 🛋️ sofá cama", ev["summary"]
    assert "Petición antigua" not in ev["description"]
    assert len(s.cal.eventos) == 2


def test_A5_nombre_corto_de_config_y_formato_antiguo_se_corrigen_sin_correos(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_LUCIA)
    s.ejecutar()
    ev = s.cal.ev("HMTEST0002")
    ev["description"] = "#entrada 16:00\n🏠 Alojamiento: Espacioso...\n🕒 Hora prevista de llegada: No indicada"
    del ev["extendedProperties"]["private"]["airbnb_escrito"]       # evento escrito por una versión anterior
    s.cfg.alojamientos = [replace(a, alias="Tirso") if a.alias == "Corazón de Madrid" else a for a in s.cfg.alojamientos]
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    cal.recalcular_limpiezas(datetime(2026, 10, 8).date(), dias=None)
    ev = s.cal.ev("HMTEST0002")
    assert ev["summary"] == "🟢 ENTRADA Tirso · 2 pers · entra 16:00 ✅"
    assert ev["description"] == "#entrada 16:00\n\n—— Airbnb · automático (escribe tus notas encima de esta línea) ——\n🕒 Llegada: jue 26/11 a las 16:00   ·   Salida: mié 02/12 a las 11:00\n🛋️ Sofá cama: no\n🍼 Cuna: no\n\n👤 Lucia Romero · Código HMTEST0002", \
        "del formato anterior solo se conservan las etiquetas de acuerdo"
    assert s.cal.ev("HMTEST0002", "salida")["summary"] == "🔴 SALIDA Tirso · sale 11:00"
    assert leer_estado(cal.buscar_eventos_reserva("HMTEST0002", None, None, None))["alias"] == "Tirso"
    escrituras = cal.stats["limpiezas_recalculadas"]
    cal.recalcular_limpiezas(datetime(2026, 10, 8).date(), dias=None)
    assert cal.stats["limpiezas_recalculadas"] == escrituras, "si ya está al día no se reescribe"


def test_A6_recarga_manual_espera_al_limite_de_gemini_y_si_no_responde_se_detiene(tmp_path):
    from airbnb_calendar.gemini import GeminiAgotado

    class Limitado:
        def __init__(self, fallos):
            self.fallos, self.n = fallos, 0

        def post(self, url, json=None, headers=None, timeout=None):
            self.n += 1
            if self.n <= self.fallos:
                return Respuesta(429, texto='{"error": {"details": [{"retryDelay": "37s"}]}}')
            return GeminiFalso().post(url, json=json)

    esperas = []
    cfg = config_prueba()
    a = AnalizadorMensajes(cfg, "k", sesion=Limitado(2), dormir=esperas.append, paciente=True)
    assert a.analizar("We will arrive around 6pm", {}).analisis["hora_llegada"] == "18:00"
    assert esperas == [38.0, 38.0], "respeta el retryDelay de Gemini"
    with pytest.raises(GeminiAgotado):
        AnalizadorMensajes(cfg, "k", sesion=Limitado(99), dormir=lambda _: None, paciente=True).analizar("x", {})
    # Sin paciencia (ejecución normal): respaldo por palabras clave, no se para
    assert AnalizadorMensajes(cfg, "k", sesion=Limitado(99), dormir=lambda _: None).analizar("x", {}).fuente == "palabras"


def test_A7_calendario_inaccesible_detiene_la_ejecucion_sin_contar_fallos(tmp_path):
    from airbnb_calendar.calendario import CalendarioInaccesible
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_LUCIA)
    original = s.cal.request

    def sin_acceso(metodo, url, params=None, json=None, timeout=None):
        if metodo == "GET" and url.endswith("/events"):
            return Respuesta(404, texto='{"error": {"code": 404, "message": "Not Found"}}')
        return original(metodo, url, params=params, json=json, timeout=timeout)

    s.cal.request = sin_acceso
    for _ in range(4):
        with pytest.raises(CalendarioInaccesible):
            s.ejecutar()
    reg = Registro(s.ruta_estado)
    assert reg.fallos == {} and reg.procesados == {}, "el correo se procesará cuando el calendario funcione"
    assert s.tecnicos == [] and s.avisos == []
    s.cal.request = original
    s.ejecutar()
    assert len(s.cal.eventos) == 2


def test_B_mensajes_de_reserva_que_no_esta_en_el_calendario(tmp_path):
    s = Entorno(tmp_path, crear_evento_desde_mensaje=False)
    s.ahora = datetime(2026, 9, 22, 12, tzinfo=UTC)
    ids = [s.buzon.recibir(c) for c in cargar_ejemplo("ej3.txt", "2026-09-22")]
    registro = s.ejecutar()
    assert s.cal.eventos == {}
    assert len(s.avisos) == 1, s.asuntos
    asunto, cuerpo = s.avisos[0]
    assert "Antón Martín" in asunto and "cuna" in asunto
    assert "NO está en el calendario" in cuerpo
    assert "Pregunta si puede entrar hacia las 12:00" in cuerpo
    assert all(i in registro.procesados for i in ids)


def test_B2_no_se_avisa_de_reservas_que_ya_han_terminado(tmp_path):
    """Caso real: en la carga inicial se avisó de una petición de una estancia de septiembre ya pasada."""
    s = Entorno(tmp_path)                       # ahora = 08/10; la reserva de ej3 terminó el 27/09
    for c in cargar_ejemplo("ej3.txt", "2026-09-22"):
        s.buzon.recibir(c)
    s.ejecutar()
    assert s.avisos == []


def test_B3_evento_creado_desde_mensaje_lleva_aviso_y_se_completa_con_la_confirmacion(tmp_path):
    s = Entorno(tmp_path)                       # por defecto se crean eventos desde mensajes
    s.buzon.recibir(MENSAJE_LUCIA)
    s.ejecutar()
    assert len(s.cal.eventos) == 2
    ev = next(iter(s.cal.eventos.values()))
    assert ev["description"].endswith("Se completará si llega."), ev["description"]
    assert "ℹ️ Evento creado a partir de un mensaje del huésped" in ev["description"]
    # Llega la confirmación: es el mismo evento, se completa y desaparece el aviso
    s.buzon.recibir(replace(CONF_LUCIA, fecha_hora="2026-10-04 09:00", fecha_ref="2026-10-04"))
    s.ejecutar()
    assert len(s.cal.eventos) == 2
    ev = s.cal.ev("HMTEST0002")
    assert "creado a partir de un mensaje" not in ev["description"]


def test_B5_mensaje_de_anuncio_no_configurado_y_reserva_terminada_no_gasta_ia(tmp_path):
    s = Entorno(tmp_path)                       # ahora = 08/10; la reserva de ej3 terminó el 27/09
    for c in cargar_ejemplo("ej3.txt", "2026-09-22"):
        s.buzon.recibir(replace(c, cuerpo=c.cuerpo.replace("Anton Martin Agradable", "Great Studio")
                                .replace("Apartamentos de Julian", "Great Studio")))
    s.ejecutar()
    assert s.gemini.llamadas == [] and s.cal.eventos == {} and s.avisos == []


def test_B4_mensaje_de_un_anuncio_no_configurado_no_crea_evento(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(replace(MENSAJE_LUCIA, cuerpo=MENSAJE_LUCIA.cuerpo.replace(
        "Espacioso y luminoso apartamento Corazón de Madrid", "Great Studio"),
        asunto="RE: Reservation for Great Studio, Nov 26 – Dec 2"))
    s.ejecutar()
    assert s.cal.eventos == {}


def test_C_crear_desde_mensaje_ej3_y_ej2_con_horas_cuna_y_avisos(tmp_path):
    s = Entorno(tmp_path, crear_evento_desde_mensaje=True)
    s.ahora = datetime(2026, 9, 22, 12, tzinfo=UTC)
    for c in cargar_ejemplo("ej3.txt", "2026-09-22") + cargar_ejemplo("ej2.txt", "2026-10-01"):
        s.buzon.recibir(c)
    s.ejecutar()
    assert len(s.cal.eventos) == 4
    e3 = s.cal.ev("Antón Martín")
    assert e3["summary"] == "🟢 ENTRADA Antón Martín · 3 pers · llega 15:05 · 🍼 cuna"
    assert hora(e3) == "2026-09-23 15:05"
    assert s.cal.ev("Antón Martín", "salida")["summary"] == "🔴 SALIDA Antón Martín · sale 11:00"
    assert "🍼 Cuna: SÍ (lo indica el huésped)" in e3["description"]
    assert "🕒 Llegada: mié 23/09 a las 15:05   ·   Salida: dom 27/09 a las 11:00" in e3["description"]
    assert "Pregunta si puede entrar hacia las 12:00" in e3["description"]
    e2 = s.cal.ev("Lavapiés")
    assert e2["summary"] == "🟢 ENTRADA Lavapiés · 3 pers · llega 15:00"
    assert e2["start"]["dateTime"].startswith("2026-10-09")
    # Un único aviso (cuna + petición, en el primer mensaje); las horas no avisan (avisar_horas = false)
    assert len(s.avisos) == 1 and "cuna" in s.asuntos[0] and "petición" in s.asuntos[0]


def test_D_gemini_caido_palabras_clave_nota_y_un_solo_aviso_al_dia(tmp_path):
    s = Entorno(tmp_path, gemini="falla", crear_evento_desde_mensaje=True)
    s.ahora = datetime(2026, 9, 22, 12, tzinfo=UTC)
    for c in cargar_ejemplo("ej3.txt", "2026-09-22")[:2]:
        s.buzon.recibir(c)
    s.ejecutar()
    e = s.cal.ev("Antón Martín")
    assert "🍼 cuna" in e["summary"]
    assert "mensajes sin analizar por la IA" in e["description"]
    assert sum("Gemini no responde" in a for a, _ in s.tecnicos) == 1
    assert not any("Gemini" in a for a in s.asuntos), "los avisos técnicos no llegan al cliente"
    assert sum("cuna" in a for a in s.asuntos) == 1
    s.buzon.recibir(cargar_ejemplo("ej3.txt", "2026-09-22")[2])
    s.ejecutar()
    assert sum("Gemini no responde" in a for a, _ in s.tecnicos) == 1, "el aviso de Gemini se limita a uno al día"


def test_E_plantilla_irreconocible_aviso_y_sin_reintentos(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(replace(CONF_LUCIA, asunto="New booking confirmed!", cuerpo="New booking confirmed! Bob arrives soon.\nThis template changed."))
    s.ejecutar()
    assert s.avisos == [], "los errores técnicos no llegan al anfitrión"
    assert len(s.tecnicos) == 1 and "No se pudieron extraer: codigo" in s.tecnicos[0][1]
    s.ejecutar()
    assert len(s.tecnicos) == 1, "no se vuelve a intentar"


def test_F_correos_de_airbnb_que_no_son_reservas_se_ignoran(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(replace(CONF_LUCIA, asunto="Your monthly earnings report", cuerpo="Here is how you did this month."))
    s.ejecutar()
    assert s.cal.eventos == {} and s.avisos == []


def test_G_eml_reales_confirmacion_y_mensaje_con_hora_sofa_y_cuna(tmp_path):
    s = Entorno(tmp_path)
    conf = cargar_eml("confirmed.eml")
    s.buzon.recibir(conf)
    s.ejecutar()
    assert len(s.cal.eventos) == 2
    entrada = s.cal.ev("HMTEST0001")
    assert entrada["summary"] == "🟢 ENTRADA Antón Martín · 2 pers"
    assert hora(entrada) == "2026-10-20 15:00"
    assert hora(s.cal.ev("HMTEST0001", "salida")) == "2026-10-23 11:00"

    # Mensaje con la plantilla real de cunasolicitud.eml, adaptado a la reserva de Laura
    base = cargar_eml("cunasolicitud.eml")
    cuerpo = base.cuerpo
    for viejo, nuevo in [(r"SEP 16\s*–\s*19", "OCT 20 – 23"), (r"WEDNESDAY(\s+)SATURDAY", r"TUESDAY\1FRIDAY"),
                         (r"September 16, 2026(\s+)September 19, 2026", r"October 20, 2026\1October 23, 2026"),
                         (r"2 adults, 1 infant", "2 adults"), (r"PABLO", "LAURA"),
                         (r'Reacted ❤️ to "Hi Pablo! Yes no problem! "',
                          "Hi! We will arrive around 6pm, we will need the sofa bed for my son and a crib, thanks!")]:
        cuerpo = re.sub(viejo, nuevo, cuerpo)
    s.buzon.recibir(replace(base, fecha_hora="2026-10-07 09:00", fecha_ref="2026-10-07", cuerpo=cuerpo,
                            asunto="RE: Reservation for Apartamentos de Julian, Anton Martin - Apartame..., Oct 20 – 23"))
    s.ejecutar()
    assert len(s.cal.eventos) == 2, "no se duplican eventos"
    entrada = s.cal.ev("HMTEST0001")
    assert entrada["summary"].startswith("🟢 ENTRADA Antón Martín · 2 pers · llega 18:00"), entrada["summary"]
    assert "🛋️ sofá cama" in entrada["summary"] and "🍼 cuna" in entrada["summary"]
    assert "Alojamiento" not in entrada["description"]
    assert len(s.avisos) == 1


def test_H_modelo_gemini_colgado_timeout_y_respaldo(tmp_path):
    s = Entorno(tmp_path, colgado="gemini-principal", crear_evento_desde_mensaje=True,
                gemini_modelos_respaldo=["gemini-respaldo"])
    s.ahora = datetime(2026, 9, 22, 12, tzinfo=UTC)
    for c in cargar_ejemplo("ej3.txt", "2026-09-22")[:2]:
        s.buzon.recibir(c)
    s.ejecutar()
    assert any("gemini-respaldo" in u for u in s.gemini.llamadas)
    assert "sin analizar por la IA" not in s.cal.ev("Antón Martín")["description"]
    assert not any("Gemini no responde" in a for a, _ in s.tecnicos)


def test_I_error_temporal_de_calendar_se_reintenta_y_luego_es_definitivo(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_LUCIA)
    s.cal.fallar_escrituras = 1
    reg = s.ejecutar()
    assert len(s.cal.eventos) == 2 and not reg.fallos, "un micro-corte se resuelve dentro de la misma ejecución"

    s = Entorno(tmp_path / "corte")
    s.buzon.recibir(CONF_LUCIA)
    s.cal.fallar_escrituras = 4                          # más que los reintentos internos
    reg = s.ejecutar()
    assert s.cal.eventos == {} and s.avisos == [] and reg.fallos, "se reintentará en la próxima ejecución"
    s.ejecutar()
    assert len(s.cal.eventos) == 2 and not Registro(s.ruta_estado).fallos

    s2 = Entorno(tmp_path / "otro")
    s2.buzon.recibir(CONF_LUCIA)
    s2.cal.fallar_escrituras = 99
    for _ in range(3):
        s2.ejecutar()
    assert len(s2.tecnicos) == 1 and "HTTP 503" in s2.tecnicos[0][1], "tras 3 intentos se avisa y se deja"
    assert s2.avisos == []
    s2.ejecutar()
    assert len(s2.tecnicos) == 1


def test_J_remitente_que_no_es_de_airbnb_y_primera_ejecucion_sin_correos_antiguos(tmp_path):
    s = Entorno(tmp_path)
    id_falso = s.buzon.recibir(CONF_LUCIA, remitente="Fake <airbnb.com@gmail.com>")
    reg = s.ejecutar()
    assert s.cal.eventos == {} and id_falso in reg.procesados

    s2 = Entorno(tmp_path / "x", procesar_desde="")
    s2.buzon.recibir(CONF_LUCIA)                         # llegó el 01/10, antes de la primera ejecución
    reg = s2.ejecutar()
    assert s2.cal.eventos == {}, "no se cargan reservas anteriores a la instalación"
    assert reg.inicio == s2.ahora.isoformat()
    s2.buzon.recibir(replace(MENSAJE_LUCIA, fecha_hora="2026-10-08 15:00"))     # posterior: sí
    s2.ahora += timedelta(hours=2)
    s2.ejecutar()
    assert len(s2.avisos) == 1 and "NO está en el calendario" not in s2.avisos[0][1]
    assert len(s2.cal.eventos) == 2, "el mensaje crea los eventos aunque la confirmación sea anterior"
    assert "creado a partir de un mensaje" in next(iter(s2.cal.eventos.values()))["description"]


def test_K_estado_largo_se_trocea_y_al_acortarse_se_borran_los_trozos_sobrantes(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_LUCIA)
    s.ejecutar()
    cal = Calendario(s.cfg, s.cal)
    eventos = cal.buscar_eventos_reserva("HMTEST0002", None, None, None)
    from airbnb_calendar.calendario import leer_estado
    estado = leer_estado(eventos)
    estado["peticiones"] = [f"{i} " + "x" * 140 for i in range(10)]
    cal.guardar_reserva(estado, eventos, None)
    props = s.cal.ev("HMTEST0002")["extendedProperties"]["private"]
    n = int(props["airbnb_estado_n"])
    assert n >= 2 and all(len(props[f"airbnb_estado_{i}"]) <= 900 for i in range(n))
    estado["peticiones"] = []
    cal.guardar_reserva(estado, cal.buscar_eventos_reserva("HMTEST0002", None, None, None), None)
    props = s.cal.ev("HMTEST0002")["extendedProperties"]["private"]
    assert props["airbnb_estado_n"] == "1" and "airbnb_estado_1" not in props
    assert leer_estado(cal.buscar_eventos_reserva("HMTEST0002", None, None, None))["peticiones"] == []


# ---------------------------------------------------------------------------
#  Limpieza entre reservas, horarios acordados y purga
# ---------------------------------------------------------------------------


def _reserva(codigo, checkin, checkout, alojamiento="Lavapies - Peaceful 2 bedrooms Fresh A/C", **kw):
    from airbnb_calendar import reglas
    e = reglas.crear_estado()
    e.update({"codigo": codigo, "alojamiento": alojamiento, "alias": "Lavapiés", "checkin": checkin, "checkout": checkout,
              "hora_checkin": "15:00", "hora_checkout": "11:00", "total": 2, **kw})
    return e


def _guardar(cal, e):
    cal.guardar_reserva(e, cal.buscar_eventos_reserva(e["codigo"], e["alojamiento"], e["checkin"], e["checkout"]), None)


def test_L_rotacion_limpieza_ok_alerta_y_recalculo_al_cambiar_la_salida(tmp_path):
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("SALE1", "2026-11-01", "2026-11-05"))
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08"))
    ev = s.cal.ev("ENTRA2")
    assert ev["summary"] == "🟢 ENTRADA Lavapiés · [LIMPIEZA OK] · 2 pers"
    assert "🧹 Limpieza: 05/11 de 11:00 a 15:00 (el huésped anterior sale ese día a las 11:00)" in ev["description"]
    assert s.cal.ev("SALE1")["summary"] == "🟢 ENTRADA Lavapiés · 2 pers", "la reserva sin huésped anterior no lleva etiqueta"
    assert "Limpieza" not in s.cal.ev("ENTRA2", "salida")["description"], "la salida no lleva nada de limpieza"

    # El huésped saliente dice que se va a las 13:30: sin acuerdo expreso se mantiene la salida estándar (11:00)
    _guardar(cal, _reserva("SALE1", "2026-11-01", "2026-11-05", hora_salida="13:30"))
    ev = s.cal.ev("ENTRA2")
    assert ev["summary"] == "🟢 ENTRADA Lavapiés · [LIMPIEZA OK] · 2 pers", "se recalcula y sigue OK"
    assert "sale ese día a las 11:00" in ev["description"]
    assert s.cal.ev("SALE1", "salida")["start"]["dateTime"].startswith("2026-11-05T11:00")

    # Si cancela el que sale, ya no hay rotación
    _guardar(cal, _reserva("SALE1", "2026-11-01", "2026-11-05", hora_salida="13:30", cancelada=True))
    assert s.cal.ev("ENTRA2")["summary"] == "🟢 ENTRADA Lavapiés · 2 pers"


def test_L2_recalcular_corrige_el_horario_de_eventos_antiguos(tmp_path):
    from datetime import date
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("SALE1", "2026-11-01", "2026-11-05"))
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08", hora_llegada="12:30"))
    _guardar(cal, _reserva("SOLA3", "2026-12-01", "2026-12-04"))
    # Eventos guardados con las reglas anteriores (llegada anticipada / salida tardía del huésped)
    s.cal.ev("ENTRA2")["start"]["dateTime"] = "2026-11-05T12:30:00"
    s.cal.ev("SOLA3", "salida")["start"]["dateTime"] = "2026-12-04T13:00:00"
    for c, parte in (("ENTRA2", "entrada"), ("SOLA3", "salida")):
        del s.cal.ev(c, parte)["extendedProperties"]["private"]["airbnb_escrito"]   # sin huella: versión anterior

    # Simulación: cuenta los cambios pero no escribe nada
    cal.recalcular_limpiezas(date(2026, 10, 13), dias=None, simulacion=True)
    assert cal.stats["limpiezas_recalculadas"] == 2
    assert s.cal.ev("ENTRA2")["start"]["dateTime"] == "2026-11-05T12:30:00"

    # Ejecución real (todas las reservas futuras, más allá de 30 días)
    cal.stats["limpiezas_recalculadas"] = 0
    cal.recalcular_limpiezas(date(2026, 10, 13), dias=None)
    assert cal.stats["limpiezas_recalculadas"] == 2
    assert s.cal.ev("ENTRA2")["start"]["dateTime"] == "2026-11-05T15:00:00"
    assert s.cal.ev("SOLA3", "salida")["start"]["dateTime"] == "2026-12-04T11:00:00"
    # Ya está todo al día: una segunda pasada no toca nada
    cal.recalcular_limpiezas(date(2026, 10, 13), dias=None)
    assert cal.stats["limpiezas_recalculadas"] == 2


def test_L3_horario_acordado_por_el_anfitrion_manda_y_se_conserva(tmp_path):
    from datetime import date
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("SALE1", "2026-11-01", "2026-11-05"))
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08"))
    ev = s.cal.ev("ENTRA2")

    # Julien acuerda entrada a las 12:00 (deja 1 h de limpieza) y lo escribe en la web de Calendar (HTML)
    ev["description"] = "#entrada 12h<br>" + ev["description"]
    cal.recalcular_limpiezas(date(2026, 10, 13))
    ev = s.cal.ev("ENTRA2")
    assert ev["start"]["dateTime"] == "2026-11-05T12:00:00", "el evento se mueve a la hora acordada"
    assert ev["summary"] == "🟢 ENTRADA Lavapiés · [ALERTA LIMPIEZA / AJUSTE HORARIO] · 2 pers · entra 12:00 ✅"
    assert ev["description"].startswith("#entrada 12h\n\n—— Airbnb · automático (escribe tus notas encima de esta línea) ——\n"), "lo escrito a mano se conserva (como texto)"

    # Llega un correo nuevo de la reserva (p. ej. mensaje del huésped pidiendo las 10:00): el acuerdo sigue
    from airbnb_calendar.calendario import leer_estado
    eventos = cal.buscar_eventos_reserva("ENTRA2", None, None, None)
    e = leer_estado(eventos)
    e["hora_llegada"] = "10:00"
    cal.guardar_reserva(e, eventos, None)
    assert s.cal.ev("ENTRA2")["start"]["dateTime"] == "2026-11-05T12:00:00"

    # Un acuerdo de salida tardía del huésped anterior también cuenta para la limpieza (la etiqueta vale
    # en cualquiera de los dos eventos: aquí, en el de entrada)
    sale = s.cal.ev("SALE1")
    sale["description"] = "#salida 13:30\n" + sale["description"]
    cal.recalcular_limpiezas(date(2026, 10, 13))
    assert s.cal.ev("SALE1", "salida")["start"]["dateTime"] == "2026-11-05T13:30:00"
    assert s.cal.ev("SALE1", "salida")["summary"] == "🔴 SALIDA Lavapiés · sale 13:30 ✅"

    # Si Julien borra la etiqueta, vuelve el horario estándar
    ev = s.cal.ev("ENTRA2")
    ev["description"] = ev["description"].replace("#entrada 12h", "")
    cal.recalcular_limpiezas(date(2026, 10, 13))
    assert s.cal.ev("ENTRA2")["start"]["dateTime"] == "2026-11-05T15:00:00"


def test_L5_cambios_a_mano_en_el_evento_no_se_sobrescriben(tmp_path):
    from datetime import date
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("SALE1", "2026-11-01", "2026-11-05"))
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08"))
    ev = s.cal.ev("ENTRA2")
    # Julien mueve el evento a las 13:30, cambia el título y el color y añade una nota encima del separador
    ev["start"]["dateTime"] = "2026-11-05T13:30:00+01:00"
    ev["summary"] = "Lavapiés — familia García (llaves en recepción)"
    ev["colorId"] = "3"
    ev["description"] = "Traer toallas extra\n" + ev["description"]

    # Llega un mensaje nuevo de la reserva y además pasa el recálculo diario
    eventos = cal.buscar_eventos_reserva("ENTRA2", None, None, None)
    e = leer_estado(eventos)
    assert e["acuerdo_entrada"] == "13:30", "mover el evento a mano cuenta como horario acordado"
    e["peticiones"] = ["Trona"]
    cal.guardar_reserva(e, eventos, None)
    cal.recalcular_limpiezas(date(2026, 10, 13))
    ev = s.cal.ev("ENTRA2")
    assert ev["start"]["dateTime"] == "2026-11-05T13:30:00+01:00"
    assert ev["summary"] == "Lavapiés — familia García (llaves en recepción)"
    assert ev["colorId"] == "3"
    assert ev["description"].startswith("Traer toallas extra\n\n—— Airbnb")
    assert "📝 Peticiones:\n   • Trona" in ev["description"], "la parte automática sí se actualiza"
    assert ev["end"]["dateTime"].startswith("2026-11-05T14:00"), "el final sigue a la hora acordada"
    salida = s.cal.ev("ENTRA2", "salida")
    assert salida["start"]["dateTime"].startswith("2026-11-08T11:00"), "lo no tocado sigue siendo automático"
    assert salida["summary"] == "🔴 SALIDA Lavapiés · sale 11:00"

    # Si edita también la parte automática, la descripción pasa a ser suya
    ev["description"] = ev["description"].replace("Trona", "Trona (ya está en el piso)")
    e = leer_estado(cal.buscar_eventos_reserva("ENTRA2", None, None, None))
    e["peticiones"] = ["Trona", "Parking"]
    cal.guardar_reserva(e, cal.buscar_eventos_reserva("ENTRA2", None, None, None), None)
    assert "Trona (ya está en el piso)" in s.cal.ev("ENTRA2")["description"]
    assert "Parking" not in s.cal.ev("ENTRA2")["description"]


def test_L7_nota_anadida_en_la_web_html_no_bloquea_las_actualizaciones(tmp_path):
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08"))
    ev = s.cal.ev("ENTRA2")
    # Calendar web guarda la descripción en HTML al editarla
    ev["description"] = ("Llamar al llegar &amp; dejar&nbsp;toallas<br>" +
                         ev["description"].replace("\n", "<br>"))
    eventos = cal.buscar_eventos_reserva("ENTRA2", None, None, None)
    e = leer_estado(eventos)
    e["peticiones"] = ["Trona"]
    cal.guardar_reserva(e, eventos, None)
    d = s.cal.ev("ENTRA2")["description"]
    assert d.startswith("Llamar al llegar & dejar toallas\n\n—— Airbnb"), d
    assert "• Trona" in d


def test_L6_lockers_y_locker_alternativo_si_entran_el_mismo_dia(tmp_path):
    from datetime import date
    s = Entorno(tmp_path)
    lav, anton = s.cfg.alojamientos[1], s.cfg.alojamientos[2]
    lav.locker, lav.locker_coincide_con, lav.locker_si_coincide = "https://k/compartido", "Antón Martín", "https://k/alternativo"
    anton.locker = "https://k/compartido"
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("LAV1", "2026-11-05", "2026-11-08"))
    assert "🔑 Locker: https://k/compartido" in s.cal.ev("LAV1")["description"]

    # Entra alguien en Antón Martín el mismo día → Torrecilla (Lavapiés) pasa al locker alternativo
    _guardar(cal, _reserva("ANT1", "2026-11-05", "2026-11-07", alojamiento=anton.nombre))
    assert "🔑 Locker: https://k/alternativo (Antón Martín también entra ese día)" in s.cal.ev("LAV1")["description"]
    assert "🔑 Locker: https://k/compartido\n" in s.cal.ev("ANT1")["description"]
    assert "Locker" not in s.cal.ev("LAV1", "salida")["description"], "el locker es para el que entra"

    # Si se cancela la de Antón Martín, vuelve al compartido (también en el recálculo diario)
    _guardar(cal, _reserva("ANT1", "2026-11-05", "2026-11-07", alojamiento=anton.nombre, cancelada=True))
    assert "🔑 Locker: https://k/compartido" in s.cal.ev("LAV1")["description"]
    cal.recalcular_limpiezas(date(2026, 10, 13), dias=None)
    assert "🔑 Locker: https://k/compartido" in s.cal.ev("LAV1")["description"]
    assert "Locker" not in s.cal.ev("ANT1")["description"], "las canceladas no llevan locker"


def test_L7_la_salida_solo_dice_a_que_hora_sale_y_nunca_despues_de_las_11_sin_acuerdo(tmp_path):
    from airbnb_calendar.calendario import SEPARADOR
    s = Entorno(tmp_path)
    s.cfg.alojamientos[1].locker = "https://k/compartido"
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    # Caso real: el huésped dice que se va a las 14:30 y luego retira la petición; nadie lo ha acordado con Julien
    _guardar(cal, _reserva("SALE1", "2026-11-01", "2026-11-05", hora_salida="14:30", peticiones=["Trona"]))
    salida, entrada = s.cal.ev("SALE1", "salida"), s.cal.ev("SALE1")
    assert salida["start"]["dateTime"] == "2026-11-05T11:00:00"
    assert salida["summary"] == "🔴 SALIDA Lavapiés · sale 11:00"
    assert salida["description"] == SEPARADOR + "\n🕒 Salida: jue 05/11 a las 11:00", "ni locker ni datos de la entrada"
    assert "14:30" not in entrada["description"] and "Salida: jue 05/11 a las 11:00" in entrada["description"]
    assert "🔑 Locker: https://k/compartido" in entrada["description"]

    # Si Julien lo acuerda, sale a esa hora
    salida = s.cal.ev("SALE1", "salida")
    salida["description"] = "#salida 14:30\n" + salida["description"]
    _guardar(cal, leer_estado(cal.buscar_eventos_reserva("SALE1", None, None, None)))
    salida = s.cal.ev("SALE1", "salida")
    assert salida["start"]["dateTime"] == "2026-11-05T14:30:00"
    assert salida["summary"] == "🔴 SALIDA Lavapiés · sale 14:30 ✅"
    assert salida["description"].endswith(SEPARADOR + "\n🕒 Salida: jue 05/11 a las 14:30 (acordada)")


def test_L8_la_sugerencia_de_etiqueta_en_la_parte_automatica_no_es_un_acuerdo(tmp_path):
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08", entrada_anticipada="12:30"))
    assert "escribe arriba: #entrada 12:30" in s.cal.ev("ENTRA2")["description"]
    e = leer_estado(cal.buscar_eventos_reserva("ENTRA2", None, None, None))
    assert e["acuerdo_entrada"] is None, "solo cuenta lo que escribe el anfitrión encima de la línea"
    assert s.cal.ev("ENTRA2")["start"]["dateTime"].startswith("2026-11-05T15:00")


def test_L9_direccion_del_piso_como_ubicacion_y_cambiada_a_mano_se_respeta(tmp_path):
    s = Entorno(tmp_path)
    s.cfg.alojamientos[1].direccion = "Calle de la Torrecilla del Leal 22, Madrid"
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("SOLO1", "2026-11-05", "2026-11-08", alojamiento=s.cfg.alojamientos[0].nombre))
    assert "location" not in s.cal.ev("SOLO1"), "piso sin dirección: no se toca la ubicación"
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08"))
    assert s.cal.ev("ENTRA2")["location"] == "Calle de la Torrecilla del Leal 22, Madrid"
    assert s.cal.ev("ENTRA2", "salida")["location"] == "Calle de la Torrecilla del Leal 22, Madrid"
    s.cal.ev("ENTRA2", "salida")["location"] = "Portal B, 2º izq."     # cambiada a mano: se respeta
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08", hora_llegada="18:00", hora_salida="10:00"))
    assert s.cal.ev("ENTRA2", "salida")["location"] == "Portal B, 2º izq."
    assert s.cal.ev("ENTRA2", "salida")["start"]["dateTime"] == "2026-11-08T10:00:00", "el resto sigue al día"
    assert s.cal.ev("ENTRA2")["location"] == "Calle de la Torrecilla del Leal 22, Madrid"


# ---------------------------------------------------------------------------
#  Entrada y salida: dos eventos por reserva
# ---------------------------------------------------------------------------


def _duracion_min(ev):
    return (datetime.fromisoformat(ev["end"]["dateTime"]) - datetime.fromisoformat(ev["start"]["dateTime"])).seconds // 60


def test_N1_una_confirmacion_crea_una_entrada_y_una_salida_cortas_con_la_ubicacion(tmp_path):
    s = Entorno(tmp_path)
    s.cfg.alojamientos[0].direccion = "Plaza de Tirso de Molina 18, Madrid"
    s.buzon.recibir(CONF_LUCIA)
    s.ejecutar()
    assert len(s.cal.eventos) == 2
    entrada, salida = s.cal.ev("HMTEST0002"), s.cal.ev("HMTEST0002", "salida")
    assert (hora(entrada), hora(entrada, "end")) == ("2026-11-26 15:00", "2026-11-26 15:30")
    assert (hora(salida), hora(salida, "end")) == ("2026-12-02 11:00", "2026-12-02 11:30")
    assert [_duracion_min(ev) for ev in s.cal.eventos.values()] == [30, 30]
    assert entrada["summary"] == "🟢 ENTRADA Corazón de Madrid · 2 pers"
    assert salida["summary"] == "🔴 SALIDA Corazón de Madrid · sale 11:00"
    assert (entrada["colorId"], salida["colorId"]) == ("10", "11")
    assert entrada["location"] == salida["location"] == "Plaza de Tirso de Molina 18, Madrid"
    assert salida["description"].endswith("\n🕒 Salida: mié 02/12 a las 11:00")


def test_N1b_titulos_completos_de_la_entrada_y_la_salida(tmp_path):
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("SALE1", "2026-11-01", "2026-11-05"))
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08", total=3, hora_llegada="18:30", entrada_anticipada="12:00",
                           hora_salida="10:00", salida_tardia="13:00", sofa={"v": True, "f": "huesped"},
                           cuna={"v": True, "f": "huesped"}))
    entrada, salida = s.cal.ev("ENTRA2"), s.cal.ev("ENTRA2", "salida")
    assert entrada["summary"] == "🟢 ENTRADA Lavapiés · [LIMPIEZA OK] · 3 pers · llega 18:30 · ⏰ pide entrar 12:00 · 🛋️ sofá cama · 🍼 cuna"
    assert salida["summary"] == "🔴 SALIDA Lavapiés · sale 10:00"
    assert hora(entrada) == "2026-11-05 18:30" and hora(salida) == "2026-11-08 10:00"
    assert "🧹 Limpieza" in entrada["description"] and "Limpieza" not in salida["description"]


def test_N2_mover_el_evento_de_salida_es_un_acuerdo_y_cambia_la_limpieza_del_siguiente(tmp_path):
    from datetime import date
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("SALE1", "2026-11-01", "2026-11-05"))
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08"))
    assert s.cal.ev("ENTRA2")["summary"] == "🟢 ENTRADA Lavapiés · [LIMPIEZA OK] · 2 pers"
    # Julien arrastra el evento de salida a las 14:00 (Calendar mueve el inicio y el final)
    sale = s.cal.ev("SALE1", "salida")
    sale["start"]["dateTime"], sale["end"]["dateTime"] = "2026-11-05T14:00:00+01:00", "2026-11-05T14:30:00+01:00"
    assert leer_estado(cal.buscar_eventos_reserva("SALE1", None, None, None))["acuerdo_salida"] == "14:00"
    cal.recalcular_limpiezas(date(2026, 10, 13))
    sale = s.cal.ev("SALE1", "salida")
    assert sale["start"]["dateTime"] == "2026-11-05T14:00:00+01:00", "se respeta"
    assert sale["summary"] == "🔴 SALIDA Lavapiés · sale 14:00 ✅"
    entra = s.cal.ev("ENTRA2")
    assert entra["summary"] == "🟢 ENTRADA Lavapiés · [ALERTA LIMPIEZA / AJUSTE HORARIO] · 2 pers"
    assert entra["colorId"] == "6"
    # Un correo nuevo de la reserva que sale no deshace el acuerdo
    eventos = cal.buscar_eventos_reserva("SALE1", None, None, None)
    e = leer_estado(eventos)
    e["peticiones"] = ["Trona"]
    cal.guardar_reserva(e, eventos, None)
    assert s.cal.ev("SALE1", "salida")["start"]["dateTime"] == "2026-11-05T14:00:00+01:00"
    assert s.cal.ev("ENTRA2")["colorId"] == "6"


def _evento_antiguo(s, codigo, checkin, checkout, inicio, fin, notas=""):
    """Un evento de la entrada a la salida, sin airbnb_parte, como lo escribía la versión anterior.
    inicio/fin: lo que hay ahora en el evento (distinto de 15:00/11:00 si el anfitrión lo movió a mano)."""
    from airbnb_calendar.calendario import SEPARADOR, _h, _hash
    estado = _reserva(codigo, checkin, checkout)
    auto = f"🕒 Llegada: 15:00   ·   Salida: 11:00\n🛋️ Sofá cama: no\n🍼 Cuna: no\n\n👤 ? · Código {codigo}"
    escrito = {"summary": _h("🏠 Lavapiés · 2 pers"), "colorId": _h("10"), "inicio": _h(f"{checkin}T15:00"),
               "fin": _h(f"{checkout}T11:00"), "auto": _h(auto)}
    s.cal.eventos[codigo] = {
        "id": codigo, "summary": "🏠 Lavapiés · 2 pers", "colorId": "10",
        "start": {"dateTime": inicio, "timeZone": "Europe/Madrid"}, "end": {"dateTime": fin, "timeZone": "Europe/Madrid"},
        "description": (notas + "\n\n" if notas else "") + SEPARADOR + "\n" + auto,
        "extendedProperties": {"private": {
            "airbnb_origen": "airbnb-calendar", "airbnb_tipo": "reserva", "airbnb_codigo": codigo,
            "airbnb_alojamiento": _hash(estado["alojamiento"]), "airbnb_checkin": checkin, "airbnb_checkout": checkout,
            "airbnb_limpieza": "", "airbnb_estado_n": "1", "airbnb_estado_0": json.dumps(estado, ensure_ascii=False),
            "airbnb_escrito": json.dumps(escrito)}}}


def test_N3_evento_antiguo_de_toda_la_estancia_pasa_a_entrada_y_salida_sin_perder_acuerdos(tmp_path):
    from datetime import date

    from airbnb_calendar.calendario import SEPARADOR
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    # Julien había movido a mano el inicio (entra a las 16:00) y el final (sale a las 13:00)
    _evento_antiguo(s, "ANTIGUO", "2026-11-05", "2026-11-08", "2026-11-05T16:00:00+01:00", "2026-11-08T13:00:00+01:00",
                    notas="Llaves en el buzón")

    cal.recalcular_limpiezas(date(2026, 10, 13))
    assert len(s.cal.eventos) == 2
    entrada, salida = s.cal.ev("ANTIGUO"), s.cal.ev("ANTIGUO", "salida")
    assert entrada["id"] == "ANTIGUO" and entrada["extendedProperties"]["private"]["airbnb_parte"] == "entrada"
    assert entrada["start"]["dateTime"] == "2026-11-05T16:00:00+01:00", "el inicio movido a mano se conserva"
    assert entrada["end"]["dateTime"] == "2026-11-05T16:30:00", "se acorta"
    assert entrada["summary"] == "🟢 ENTRADA Lavapiés · 2 pers · entra 16:00 ✅"
    assert entrada["description"].startswith("#salida 13:00\nLlaves en el buzón\n\n" + SEPARADOR + "\n"), \
        "el final movido a mano pasa a etiqueta, y las notas se conservan"
    assert (salida["start"]["dateTime"], salida["end"]["dateTime"]) == ("2026-11-08T13:00:00", "2026-11-08T13:30:00")
    assert salida["summary"] == "🔴 SALIDA Lavapiés · sale 13:00 ✅"

    antes = copy.deepcopy(s.cal.eventos)
    escrituras = cal.stats["limpiezas_recalculadas"]
    cal.recalcular_limpiezas(date(2026, 10, 13))
    assert s.cal.eventos == antes and cal.stats["limpiezas_recalculadas"] == escrituras, "otra pasada no cambia nada"


def test_N4_recalcular_con_la_salida_fuera_del_rango_no_la_duplica_y_lee_sus_acuerdos(tmp_path):
    from datetime import date
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("LARGA", "2026-11-10", "2026-11-20"))       # la salida queda después de hoy + 30 días
    salida = s.cal.ev("LARGA", "salida")
    salida["description"] = "#entrada 13:00\n" + salida["description"]   # acuerdo escrito en el evento de salida
    cal.recalcular_limpiezas(date(2026, 10, 13))
    assert sorted(ev["extendedProperties"]["private"]["airbnb_parte"] for ev in s.cal.eventos.values()) == ["entrada", "salida"]
    assert s.cal.ev("LARGA", "salida")["id"] == salida["id"]
    assert s.cal.ev("LARGA")["start"]["dateTime"] == "2026-11-10T13:00:00"
    assert s.cal.ev("LARGA")["summary"] == "🟢 ENTRADA Lavapiés · 2 pers · entra 13:00 ✅"


def test_N6_estancias_en_curso_se_revisan_sin_perder_la_limpieza_y_los_eventos_antiguos_se_convierten(tmp_path):
    from datetime import date
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("SALE1", "2026-10-01", "2026-10-05"))
    _guardar(cal, _reserva("ENCURSO", "2026-10-05", "2026-10-20"))
    assert s.cal.ev("ENCURSO")["summary"] == "🟢 ENTRADA Lavapiés · [LIMPIEZA OK] · 2 pers"
    # Hoy 13/10: SALE1 y la entrada de ENCURSO quedan antes del rango de fechas consultado
    cal.recalcular_limpiezas(date(2026, 10, 13))
    assert s.cal.ev("ENCURSO")["summary"] == "🟢 ENTRADA Lavapiés · [LIMPIEZA OK] · 2 pers", "la limpieza ya pasada no se pierde"
    assert cal.stats["limpiezas_recalculadas"] == 0 and cal.stats["reservas_revisadas"] == 1

    s2 = Entorno(tmp_path / "antiguo")
    cal2 = Calendario(s2.cfg, s2.cal, dormir=lambda _: None)
    _evento_antiguo(s2, "ANTIGUO", "2026-10-10", "2026-10-15", "2026-10-10T15:00:00", "2026-10-15T11:00:00")
    cal2.recalcular_limpiezas(date(2026, 10, 13))
    assert len(s2.cal.eventos) == 2
    assert s2.cal.ev("ANTIGUO")["end"]["dateTime"] == "2026-10-10T15:30:00"
    assert s2.cal.ev("ANTIGUO", "salida")["start"]["dateTime"] == "2026-10-15T11:00:00"


def test_N7_conversion_cortada_a_medias_converge_en_la_siguiente_ejecucion(tmp_path):
    from datetime import date
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _evento_antiguo(s, "ANTIGUO", "2026-11-05", "2026-11-08", "2026-11-05T15:00:00", "2026-11-08T13:00:00+01:00")
    original = s.cal.request
    s.cal.request = lambda metodo, url, **kw: (Respuesta(503, texto="backendError") if metodo == "POST"
                                              else original(metodo, url, **kw))
    eventos = cal.buscar_eventos_reserva("ANTIGUO", None, None, None)
    with pytest.raises(RuntimeError):
        cal.guardar_reserva(leer_estado(eventos), eventos, None)     # llega un correo; Google falla al crear la salida
    assert len(s.cal.eventos) == 1 and s.cal.ev("ANTIGUO")["description"].startswith("#salida 13:00\n")
    s.cal.request = original
    cal.recalcular_limpiezas(date(2026, 10, 13))
    cal.recalcular_limpiezas(date(2026, 10, 13))
    assert len(s.cal.eventos) == 2
    assert s.cal.ev("ANTIGUO", "salida")["start"]["dateTime"] == "2026-11-08T13:00:00", "el acuerdo sigue en la etiqueta"


def test_N5_cancelacion_los_dos_eventos_en_gris(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_LUCIA)
    s.buzon.recibir(CANC_LUCIA)
    s.ejecutar()
    assert len(s.cal.eventos) == 2
    entrada, salida = s.cal.ev("HMTEST0002"), s.cal.ev("HMTEST0002", "salida")
    assert entrada["summary"] == "❌ CANCELADA · 🟢 ENTRADA Corazón de Madrid · 2 pers"
    assert salida["summary"] == "❌ CANCELADA · 🔴 SALIDA Corazón de Madrid · sale 11:00"
    assert (entrada["colorId"], salida["colorId"]) == ("8", "8")


def test_L10_ubicacion_puesta_a_mano_antes_de_que_el_script_la_escribiera_se_respeta(tmp_path):
    from datetime import date
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08"))     # el piso aún no tiene dirección en config.toml
    s.cal.ev("ENTRA2")["location"] = "Portal B, 2º izq."
    s.cfg.alojamientos[1].direccion = "Calle de la Torrecilla del Leal 22, Madrid"
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08", hora_llegada="18:00"))
    assert s.cal.ev("ENTRA2")["location"] == "Portal B, 2º izq."
    assert s.cal.ev("ENTRA2")["start"]["dateTime"] == "2026-11-05T18:00:00", "el resto sigue al día"
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08", hora_llegada="19:00"))
    assert s.cal.ev("ENTRA2")["location"] == "Portal B, 2º izq.", "y lo sigue siendo"

    _evento_antiguo(s, "ANTIGUO", "2026-11-10", "2026-11-12", "2026-11-10T15:00:00", "2026-11-12T11:00:00")
    s.cal.ev("ANTIGUO")["location"] = "Portal C, bajo"
    cal.recalcular_limpiezas(date(2026, 10, 13))
    assert s.cal.ev("ANTIGUO")["location"] == "Portal C, bajo", "la conversión de un evento antiguo tampoco la pisa"
    assert s.cal.ev("ANTIGUO", "salida")["location"] == "Calle de la Torrecilla del Leal 22, Madrid"


def test_L4_leer_acuerdos():
    from airbnb_calendar.reglas import leer_acuerdos
    assert leer_acuerdos("#entrada 12:00\n#Salida: 13.30") == {"acuerdo_entrada": "12:00", "acuerdo_salida": "13:30"}
    assert leer_acuerdos("<p>#entrada&nbsp;9h</p>") == {"acuerdo_entrada": "09:00", "acuerdo_salida": None}
    assert leer_acuerdos("escribe: #entrada HH:MM o #salida HH:MM") == {"acuerdo_entrada": None, "acuerdo_salida": None}
    assert leer_acuerdos(None) == {"acuerdo_entrada": None, "acuerdo_salida": None}


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


def test_O_purga_solo_borra_eventos_propios(tmp_path, monkeypatch):
    import sincronizar
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_LUCIA)
    s.ejecutar()
    s.cal.eventos["personal"] = {"id": "personal", "summary": "Dentista",
                                 "extendedProperties": {"private": {"otra_app": "1"}}}
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    assert sincronizar.purgar(cal, simulacion=True, confirmado=False) == 0
    assert len(s.cal.eventos) == 3, "--dry-run no borra nada"
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "no")
    sincronizar.purgar(cal, simulacion=False, confirmado=False)
    assert len(s.cal.eventos) == 3, "sin confirmar no se borra"
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
    s.ahora = datetime(2026, 11, 1, 9, tzinfo=UTC)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("SALE1", "2026-11-01", "2026-11-05"))
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08"))
    assert s.cal.ev("ENTRA2")["summary"] == "🟢 ENTRADA Lavapiés · [LIMPIEZA OK] · 2 pers"
    # Cambio de fechas fuera de este flujo (p. ej. modificación revisada a mano): la salida pasa al 03/11
    for sale in [e for e in s.cal.eventos.values() if e["extendedProperties"]["private"].get("airbnb_codigo") == "SALE1"]:
        s.cal.eventos.pop(sale["id"])
    _guardar(cal, _reserva("SALE1", "2026-11-01", "2026-11-03"))
    ev = s.cal.ev("ENTRA2")
    ev["summary"] = "🟢 ENTRADA Lavapiés · [LIMPIEZA OK] · 2 pers"            # sigue con la etiqueta antigua
    ev["extendedProperties"]["private"]["airbnb_limpieza"] = "obsoleta"
    s.ejecutar()                                                              # sin correos
    assert s.cal.ev("ENTRA2")["summary"] == "🟢 ENTRADA Lavapiés · 2 pers"


def test_S_listado_paginado(tmp_path):
    s = Entorno(tmp_path)
    paginas = {None: {"items": [{"id": "a"}], "nextPageToken": "t2"}, "t2": {"items": [{"id": "b"}]}}
    s.cal.request = lambda m, u, params=None, **kw: Respuesta(200, paginas[dict(params).get("pageToken")])
    assert [e["id"] for e in Calendario(s.cfg, s.cal)._listar({"x": "1"})] == ["a", "b"]


def test_T2_avisos_tecnicos_por_varios_canales_aunque_uno_falle():
    from airbnb_calendar.ntfy import Varios
    recibidos = []

    class Roto:
        def enviar(self, a, c):
            raise OSError("smtp caído")

    class Bien:
        def enviar(self, a, c):
            recibidos.append(a)

    Varios(Roto(), None, Bien()).enviar("⚠️ error", "x")
    assert recibidos == ["⚠️ error"]


def test_T_ntfy_sin_tema_no_envia_y_con_tema_publica():
    from airbnb_calendar.ntfy import NotificadorNtfy
    enviados = []
    sesion = type("S", (), {"post": lambda _, url, **kw: enviados.append((url, kw)) or
                            type("R", (), {"raise_for_status": lambda self: None})()})()
    NotificadorNtfy("", sesion).enviar("x", "y")
    assert enviados == []
    NotificadorNtfy("tema-secreto", sesion).enviar("Gemini no responde", "detalle")
    assert enviados[0][0] == "https://ntfy.sh/tema-secreto" and enviados[0][1]["params"]["title"] == "Gemini no responde"


def test_U_registro_olvida_los_fallos_de_correos_fuera_de_la_ventana(tmp_path):
    reg = Registro(tmp_path / "estado.json")
    reg.fallos["viejo-sin-fecha"] = 2                   # de una versión anterior
    reg.sumar_fallo("viejo", datetime(2026, 9, 1, tzinfo=UTC))
    reg.sumar_fallo("reciente", datetime(2026, 10, 7, tzinfo=UTC))
    reg.limpiar(datetime(2026, 9, 25, tzinfo=UTC))
    assert reg.fallos == {"reciente": 1}
    reg.guardar()
    assert Registro(tmp_path / "estado.json").fecha_fallos == {"reciente": "2026-10-07T00:00:00+00:00"}


# ---------------------------------------------------------------------------
#  Robustez: correos repetidos, cuota de Gemini, caídas largas y SMTP
# ---------------------------------------------------------------------------


def test_V_confirmacion_vista_otra_vez_no_borra_lo_que_dijo_el_huesped(tmp_path):
    """Fuera de una recarga, una confirmación que vuelve a aparecer (otro UID) no reinicia la reserva."""
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_LUCIA)
    s.buzon.recibir(MENSAJE_LUCIA)
    s.ejecutar()
    llamadas = len(s.gemini.llamadas)
    s.buzon.recibir(CONF_LUCIA)
    s.ejecutar()
    assert s.cal.ev("HMTEST0002")["summary"] == "🟢 ENTRADA Corazón de Madrid · 2 pers · llega 18:00 · 🛋️ sofá cama"
    assert s.asuntos == ["🔔 Airbnb · Corazón de Madrid · 26/11–02/12 · sofá cama"], "ni «Ya NO necesita sofá cama» ni avisos repetidos"
    assert len(s.gemini.llamadas) == llamadas, "el mensaje inicial ya se analizó"


def test_W_reserva_terminada_no_gasta_ia(tmp_path):
    """Recarga real del 18/10: 521 análisis de Gemini en reservas ya terminadas agotaron la cuota diaria."""
    s = Entorno(tmp_path)
    s.ahora = datetime(2026, 12, 5, 12, tzinfo=UTC)            # Lucía salió el 02/12
    s.buzon.recibir(CONF_LUCIA)
    s.buzon.recibir(MENSAJE_LUCIA)
    s.ejecutar(desde=datetime(2026, 9, 1, tzinfo=UTC))
    assert s.gemini.llamadas == []
    assert s.cal.ev("HMTEST0002")["summary"] == "🟢 ENTRADA Corazón de Madrid · 2 pers", "el evento se crea con los datos de la reserva"
    assert "sin analizar" not in s.cal.ev("HMTEST0002")["description"]
    assert s.avisos == []


def test_X_si_falla_el_aviso_al_anfitrion_el_calendario_se_actualiza_igual(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_LUCIA)
    s.ejecutar()
    s.buzon.recibir(MENSAJE_LUCIA)
    s.fallar_avisos = 1
    reg = s.ejecutar()
    assert s.cal.ev("HMTEST0002")["summary"] == "🟢 ENTRADA Corazón de Madrid · 2 pers · llega 18:00 · 🛋️ sofá cama"
    assert reg.fallos, "el aviso se reintentará en la próxima ejecución"
    s.ejecutar()
    assert s.asuntos == ["🔔 Airbnb · Corazón de Madrid · 26/11–02/12 · sofá cama"]
    s.ejecutar()
    assert len(s.avisos) == 1 and not Registro(s.ruta_estado).fallos


def test_Y_tras_varios_dias_caido_se_recuperan_los_correos_pendientes(tmp_path):
    s = Entorno(tmp_path, procesar_desde="", ventana_dias=3)
    s.ahora = datetime(2026, 10, 1, 7, tzinfo=UTC)
    s.ejecutar()                                               # instalación: 01/10 a las 07:00 UTC
    s.buzon.recibir(CONF_LUCIA)                                # llega el 01/10 a las 08:00 UTC
    s.ahora = datetime(2026, 10, 8, 12, tzinfo=UTC)            # 7 días sin ejecuciones correctas
    s.ejecutar()
    assert s.cal.ev("HMTEST0002"), "el correo del 01/10 no se pierde"
    reg = Registro(s.ruta_estado)
    assert reg.ultima_ok == s.ahora.isoformat()


def test_Z_cuota_diaria_de_gemini_agotada_aviso_claro_al_desarrollador(tmp_path):
    s = Entorno(tmp_path, gemini="cuota")
    s.buzon.recibir(CONF_LUCIA)
    s.ejecutar()
    assert len(s.tecnicos) == 1
    asunto, cuerpo = s.tecnicos[0]
    assert asunto == "⚠️ Airbnb → Calendar: cuota diaria de Gemini agotada"
    assert "se renueva hacia las 9:00" in cuerpo and "GEMINI_API_KEY" not in cuerpo
    assert "sin analizar por la IA" in s.cal.ev("HMTEST0002")["description"]


def test_Z2_la_descripcion_muestra_la_hora_acordada(tmp_path):
    s = Entorno(tmp_path)
    s.buzon.recibir(CONF_LUCIA)
    s.buzon.recibir(_mensaje_lucia("We will arrive at 12:00.", "2026-10-03 10:00"))
    s.ejecutar()
    ev = s.cal.ev("HMTEST0002")
    assert "⏰ pide entrar 12:00" in ev["summary"] and "llega 12:00" not in ev["summary"], "sin repetir la hora"
    ev["description"] = "#entrada 13:00\n" + ev["description"]       # Julien ofrece las 13:00
    Calendario(s.cfg, s.cal, dormir=lambda _: None).recalcular_limpiezas(datetime(2026, 10, 8).date(), dias=None)
    ev = s.cal.ev("HMTEST0002")
    assert "entra 13:00 ✅" in ev["summary"]
    assert "🕒 Llegada: jue 26/11 a las 13:00" in ev["description"], ev["description"]


def test_N8_evento_antiguo_movido_a_otro_dia_se_acorta_desde_su_inicio(tmp_path):
    from datetime import date
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    # Julien había arrastrado el evento entero: el huésped llegó al día siguiente a las 10:00
    _evento_antiguo(s, "ANTIGUO", "2026-11-05", "2026-11-08", "2026-11-06T10:00:00+01:00", "2026-11-09T06:00:00+01:00")
    cal.recalcular_limpiezas(date(2026, 10, 13))
    entrada = s.cal.ev("ANTIGUO")
    assert (entrada["start"]["dateTime"], entrada["end"]["dateTime"]) == ("2026-11-06T10:00:00+01:00", "2026-11-06T10:30:00")
    assert len(s.cal.eventos) == 2
    antes = copy.deepcopy(s.cal.eventos)
    cal.recalcular_limpiezas(date(2026, 10, 13))
    assert s.cal.eventos == antes, "la segunda pasada no deja el evento vacío ni cambia nada"


def test_N14_evento_antiguo_pasado_a_dia_completo_a_mano_se_convierte_sin_tocar_sus_fechas(tmp_path):
    from datetime import date
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _evento_antiguo(s, "TODOELDIA", "2026-10-21", "2026-10-30", "2026-10-21T15:00:00+02:00", "2026-10-30T11:00:00+01:00")
    # Julien lo convirtió a mano en un evento de día completo, solo el 22/10
    s.cal.ev("TODOELDIA")["start"], s.cal.ev("TODOELDIA")["end"] = {"date": "2026-10-22"}, {"date": "2026-10-23"}
    cal.recalcular_limpiezas(date(2026, 10, 22))
    entrada, salida = s.cal.ev("TODOELDIA"), s.cal.ev("TODOELDIA", "salida")
    assert (entrada["start"], entrada["end"]) == ({"date": "2026-10-22"}, {"date": "2026-10-23"})
    assert entrada["summary"] == "🟢 ENTRADA Lavapiés · 2 pers"
    assert (salida["start"]["dateTime"], salida["end"]["dateTime"]) == ("2026-10-30T11:00:00", "2026-10-30T11:30:00")
    antes = copy.deepcopy(s.cal.eventos)
    cal.recalcular_limpiezas(date(2026, 10, 22))
    assert s.cal.eventos == antes, "la segunda pasada no cambia nada"


def test_N9_final_alargado_a_mano_y_despues_otra_hora_de_inicio(tmp_path):
    from datetime import date
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("ENTRA2", "2026-11-05", "2026-11-08"))
    # Julien alarga a mano el final de los dos eventos (arrastra solo el borde de abajo)
    s.cal.ev("ENTRA2")["end"]["dateTime"] = "2026-11-05T16:00:00+01:00"
    s.cal.ev("ENTRA2", "salida")["end"]["dateTime"] = "2026-11-08T12:00:00+01:00"

    eventos = cal.buscar_eventos_reserva("ENTRA2", None, None, None)
    e = leer_estado(eventos)
    e["hora_llegada"] = "15:30"
    cal.guardar_reserva(e, eventos, None)
    entrada = s.cal.ev("ENTRA2")
    assert (entrada["start"]["dateTime"], entrada["end"]["dateTime"]) == ("2026-11-05T15:30:00", "2026-11-05T16:00:00+01:00"), \
        "el final a mano se respeta mientras quede después del inicio"

    # El huésped escribe que llega a las 18:00 y Julien acuerda la salida a las 13:00
    eventos = cal.buscar_eventos_reserva("ENTRA2", None, None, None)
    e = leer_estado(eventos)
    e["hora_llegada"] = "18:00"
    cal.guardar_reserva(e, eventos, None)
    salida = s.cal.ev("ENTRA2", "salida")
    salida["description"] = "#salida 13:00\n" + salida["description"]
    cal.recalcular_limpiezas(date(2026, 10, 13))
    entrada, salida = s.cal.ev("ENTRA2"), s.cal.ev("ENTRA2", "salida")
    assert (entrada["start"]["dateTime"], entrada["end"]["dateTime"]) == ("2026-11-05T18:00:00", "2026-11-05T18:30:00")
    assert (salida["start"]["dateTime"], salida["end"]["dateTime"]) == ("2026-11-08T13:00:00", "2026-11-08T13:30:00")
    antes = copy.deepcopy(s.cal.eventos)
    cal.recalcular_limpiezas(date(2026, 10, 13))
    assert s.cal.eventos == antes
    vacio = s.cal.request("PATCH", f"{cal.url}/{entrada['id']}", json={"end": {"dateTime": "2026-11-05T18:00:00"}})
    assert vacio.status_code == 400, "la imitación rechaza un rango vacío, como Google"


def test_N10_confirmacion_cortada_al_poner_el_codigo_no_duplica_la_salida(tmp_path):
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva(None, "2026-11-05", "2026-11-08"))          # creada desde un mensaje: sin código
    original = s.cal.request
    s.cal.request = lambda metodo, url, **kw: (
        Respuesta(503, texto="backendError")
        if metodo == "PATCH" and kw["json"]["extendedProperties"]["private"]["airbnb_parte"] == "salida"
        else original(metodo, url, **kw))
    with pytest.raises(RuntimeError):
        _guardar(cal, _reserva("COD1", "2026-11-05", "2026-11-08"))    # llega la confirmación; falla la salida
    s.cal.request = original
    _guardar(cal, _reserva("COD1", "2026-11-05", "2026-11-08"))        # se reintenta el correo
    partes = [ev["extendedProperties"]["private"] for ev in s.cal.eventos.values()]
    assert sorted(f"{p['airbnb_parte']} {p.get('airbnb_codigo')}" for p in partes) == ["entrada COD1", "salida COD1"]


def test_N13_recalcular_desde_una_fecha_pasada_convierte_las_reservas_ya_terminadas(tmp_path, monkeypatch):
    from datetime import date

    import sincronizar
    monkeypatch.setattr(sincronizar, "date", type("Hoy", (date,), {"today": staticmethod(lambda: date(2026, 10, 22))}))
    s = Entorno(tmp_path)
    _evento_antiguo(s, "SEPT", "2026-09-10", "2026-09-14", "2026-09-10T15:00:00", "2026-09-14T11:00:00")
    sincronizar.recalcular(Calendario(s.cfg, s.cal, dormir=lambda _: None), simulacion=False)
    assert len(s.cal.eventos) == 1, "sin --desde, una reserva terminada no se toca"

    sincronizar.recalcular(Calendario(s.cfg, s.cal, dormir=lambda _: None), simulacion=False, desde=date(2026, 9, 1))
    entrada, salida = s.cal.ev("SEPT"), s.cal.ev("SEPT", "salida")
    assert len(s.cal.eventos) == 2
    assert (hora(entrada), hora(entrada, "end")) == ("2026-09-10 15:00", "2026-09-10 15:30")
    assert (hora(salida), hora(salida, "end")) == ("2026-09-14 11:00", "2026-09-14 11:30")

    antes = copy.deepcopy(s.cal.eventos)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    sincronizar.recalcular(cal, simulacion=False, desde=date(2026, 9, 1))
    assert s.cal.eventos == antes and (cal.stats["creados"], cal.stats["limpiezas_recalculadas"]) == (0, 0), \
        "otra pasada no escribe nada"


def test_O2_purga_hasta_una_fecha_borra_las_reservas_terminadas_y_los_eventos_sin_marca(tmp_path, capsys):
    from datetime import date

    import sincronizar
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _guardar(cal, _reserva("AGOSTO", "2026-08-20", "2026-08-25"))
    _guardar(cal, _reserva("CRUZA", "2026-08-29", "2026-09-03"))
    _guardar(cal, _reserva("SEPTIEMBRE", "2026-09-10", "2026-09-12"))

    def sin_marca(id, inicio, fin, creador=s.cal.cuenta, **kw):
        s.cal.eventos[id] = {"id": id, "summary": id, "creator": {"email": creador}, "start": inicio, "end": fin, **kw}
    madrid = lambda t: {"dateTime": t, "timeZone": "Europe/Madrid"}     # noqa: E731
    # Primeras versiones del script: sin airbnb_origen
    sin_marca("Antiguo julio", madrid("2026-07-10T15:00:00+02:00"), madrid("2026-07-14T11:00:00+02:00"),
              extendedProperties={"private": {"airbnb_tipo": "entrada", "airbnb_codigo": "VIEJO1"}})
    sin_marca("Antiguo todo el día", {"date": "2026-08-30"}, {"date": "2026-09-01"})
    sin_marca("Antiguo que cruza", madrid("2026-08-31T15:00:00+02:00"), madrid("2026-09-02T11:00:00+02:00"))
    sin_marca("Dentista", madrid("2026-08-05T10:00:00+02:00"), madrid("2026-08-05T11:00:00+02:00"),
              creador="anfitriona@example.com")
    todos = sorted(s.cal.eventos)

    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None, creador=s.cal.cuenta)
    assert sincronizar.purgar(cal, simulacion=True, confirmado=False, hasta=date(2026, 9, 1)) == 0
    assert sorted(s.cal.eventos) == todos, "--dry-run no borra nada"
    assert capsys.readouterr().out.splitlines() == [
        "  2026-07-10 15:00  Antiguo julio  (sin marca)",
        "  2026-08-20 15:00  🟢 ENTRADA Lavapiés · 2 pers",
        "  2026-08-25 11:00  🔴 SALIDA Lavapiés · sale 11:00",
        "  2026-08-30  Antiguo todo el día  (sin marca)"]

    sincronizar.purgar(cal, simulacion=False, confirmado=True, hasta=date(2026, 9, 1))
    quedan = sorted("{airbnb_codigo} {airbnb_parte}".format(**ev["extendedProperties"]["private"])
                    if ev.get("extendedProperties") else ev["summary"] for ev in s.cal.eventos.values())
    assert quedan == ["Antiguo que cruza", "CRUZA entrada", "CRUZA salida", "Dentista",
                      "SEPTIEMBRE entrada", "SEPTIEMBRE salida"]


def test_N11_recalcular_borra_los_duplicados_de_una_reserva(tmp_path):
    from datetime import date
    s = Entorno(tmp_path)
    cal = Calendario(s.cfg, s.cal, dormir=lambda _: None)
    _evento_antiguo(s, "DUP", "2026-11-05", "2026-11-08", "2026-11-05T15:00:00", "2026-11-08T11:00:00")
    s.cal.eventos["DUP-bis"] = {**copy.deepcopy(s.cal.eventos["DUP"]), "id": "DUP-bis"}
    cal.recalcular_limpiezas(date(2026, 10, 13), simulacion=True)
    assert sorted(s.cal.eventos) == ["DUP", "DUP-bis"], "la simulación no borra nada"
    cal.recalcular_limpiezas(date(2026, 10, 13))
    assert sorted((ev["id"], ev["extendedProperties"]["private"].get("airbnb_parte")) for ev in s.cal.eventos.values()) == \
        [("DUP", "entrada"), ("ev1", "salida")]


def test_N12_recalcular_dice_cuantos_eventos_crea(tmp_path, monkeypatch, caplog):
    import logging
    from datetime import date

    import sincronizar
    monkeypatch.setattr(sincronizar, "date", type("Hoy", (date,), {"today": staticmethod(lambda: date(2026, 10, 13))}))
    caplog.set_level(logging.INFO)
    s = Entorno(tmp_path)
    _evento_antiguo(s, "ANTIGUO", "2026-11-05", "2026-11-08", "2026-11-05T15:00:00", "2026-11-08T11:00:00")
    sincronizar.recalcular(Calendario(s.cfg, s.cal, dormir=lambda _: None), simulacion=True)
    sincronizar.recalcular(Calendario(s.cfg, s.cal, dormir=lambda _: None), simulacion=False)
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("✔")] == [
        "✔ 1 reserva(s) en curso o futura(s) revisada(s) · 1 evento(s) por actualizar y 1 por crear (simulación)"
        " · 0 con alerta de limpieza",
        "✔ 1 reserva(s) en curso o futura(s) revisada(s) · 1 evento(s) actualizado(s) y 1 creado(s) · 0 con alerta de limpieza"]
