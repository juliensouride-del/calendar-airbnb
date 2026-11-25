"""
Borradores de respuesta (bandeja de aprobación en Telegram), de extremo a extremo y sin red:
el flujo real con imitaciones de Calendar, Yahoo, Gemini y la bandeja.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from airbnb_calendar.bandeja import ErrorBandeja
from airbnb_calendar.base import BaseConocimiento
from airbnb_calendar.calendario import Calendario, _estado_guardado
from airbnb_calendar.config import cargar_config
from airbnb_calendar.gemini import AnalizadorMensajes
from airbnb_calendar.redactor import Redactor
from airbnb_calendar.registro import Registro
from airbnb_calendar.sincronizador import Sincronizador
from airbnb_calendar.yahoo import Correo
from conftest import RAIZ
from test_sincronizador import CONF_LUCIA, CalendarFalso, GeminiFalso, Respuesta, _mensaje_lucia

MADRID = ZoneInfo("Europe/Madrid")
HILO = "hilo-lucia@reply.airbnb.com"

BASE = {
    "general": {"firma": "PENDIENTE", "hora_entrada": "15:00", "hora_salida": "11:00"},
    "pisos": {"Corazón de Madrid": {"acceso": "There is an elevator.", "wifi": "PENDIENTE",
                                    "supermercado": "Lidl next door.", "consigna_cercana": "Calle Atocha, 7",
                                    "anuncio_reciente": False}},
    "comun": {"normas": "No parties.", "consigna": "Locker: https://lockerinthecity.com/en near {consigna_cercana}",
              "restaurantes": "Try https://goo.gl/maps/NWK7o4tC1aiekhpCA", "anuncio_reciente": "New listing!"},
    "preguntas": [{"id": "wifi", "ejemplos": ["wifi?"], "usar": ["pisos.wifi"]}],
    "momentos": [
        {"id": "pedir_hora_llegada", "dia": "confirmacion", "idioma": "es", "texto": "Hola {nombre}, ¿a qué hora llegas?"},
        {"id": "bienvenida", "dia": "checkin", "hora": "12:00",
         "piezas": ["Welcome {nombre}!", "pisos.supermercado", "pisos.wifi", "comun.normas", "comun.anuncio_reciente"]},
        {"id": "pedir_hora_salida", "dia": "con_bienvenida", "texto": "What time will you leave on {dia_salida}?"},
        {"id": "mitad_estancia", "dia": "mitad", "hora": "11:00", "texto": "PENDIENTE"},
        {"id": "dia_antes_salida", "dia": "checkout-1", "hora": "10:00", "texto": "Hello {nombre}! Tomorrow you leave."},
    ],
}


class GeminiConRedaccion(GeminiFalso):
    """El análisis de siempre y, para los borradores, una respuesta fija que deja ver lo que recibió."""

    def __init__(self, *a, borrador=None, falla_redaccion=False, **kw):
        super().__init__(*a, **kw)
        self.prompts, self.cuerpos, self.borrador, self.falla_redaccion = [], [], borrador, falla_redaccion

    def post(self, url, json=None, headers=None, timeout=None):
        txt = json["contents"][0]["parts"][0]["text"]
        if "# BASE DE CONOCIMIENTO" not in txt:
            return super().post(url, json, headers, timeout)
        self.prompts.append(txt)
        self.cuerpos.append(json)
        if self.falla_redaccion:
            return Respuesta(503, texto='{"error":"overloaded"}')
        r = self.borrador or {"idioma": "en", "borrador": "Hi Lucia! See you soon.", "notas_para_ti": ["Usado: acceso"],
                              "faltan_datos": [], "confianza": "alta", "necesita_respuesta": True}
        return Respuesta(200, {"candidates": [{"content": {"parts": [{"text": __import__("json").dumps(r)}]}}]})


class BandejaFalsa:
    def __init__(self):
        self.borradores: dict[str, dict] = {}
        self.fallar = 0

    def publicar(self, b):
        if self.fallar:
            self.fallar -= 1
            raise ErrorBandeja("Vercel caído")
        nuevo = b["id"] not in self.borradores
        self.borradores.setdefault(b["id"], {**b, "estado": "pendiente"})
        for viejo in b.get("reemplaza", []):
            self.borradores[viejo]["estado"] = "sustituido"
        return nuevo

    def pendientes(self, codigo):
        return [b for b in self.borradores.values() if b["codigo"] == codigo and b["estado"] == "pendiente"]

    @property
    def lista(self):
        return list(self.borradores.values())


class Entorno:
    def __init__(self, tmp_path, ahora, **cfg):
        from conftest import config_prueba
        self.cfg = config_prueba(**{"procesar_desde": "2026-09-01", "ventana_dias": 400, "respuestas_activar": True,
                                    "respuestas_momentos": ["pedir_hora_llegada", "bienvenida", "pedir_hora_salida",
                                                            "mitad_estancia", "dia_antes_salida"], **cfg})
        self.cal, self.correos, self.bandeja = CalendarFalso(), [], BandejaFalsa()
        self.gemini = GeminiConRedaccion("ok")
        self.base = BaseConocimiento(BASE)
        self.ruta = tmp_path / "estado.json"
        self.ahora = ahora
        self.tecnicos = []
        self.pruebas = False

    def recibir(self, e, responder_a="", remitente="Airbnb <express@airbnb.com>"):
        self.correos.append(Correo(f"INBOX:1:{len(self.correos) + 1}", e.fecha, remitente, e.asunto, e.cuerpo,
                                   f"mid:<{len(self.correos) + 1}@airbnb>", responder_a))

    def correos_desde(self, desde, carpetas, remitentes, excluir=frozenset(), hasta=None):
        return sorted((c for c in self.correos if c.fecha >= desde and c.id not in excluir and c.id_mensaje not in excluir),
                      key=lambda c: c.fecha)

    def ejecutar(self, desde=None):
        registro = Registro(self.ruta, secreto="secreto-de-pruebas")
        nada = type("N", (), {"enviar": lambda *_: None})()
        tecnico = type("T", (), {"enviar": lambda _, a, c: self.tecnicos.append(a)})()
        cal = Calendario(self.cfg, self.cal, dormir=lambda s: None)
        Sincronizador(self.cfg, self, cal, AnalizadorMensajes(self.cfg, "k", sesion=self.gemini, dormir=lambda s: None), nada,
                      registro, ahora=self.ahora, desde=desde, notificador_tecnico=tecnico,
                      redactor=Redactor(self.cfg, self.base, "k", sesion=self.gemini, dormir=lambda s: None),
                      bandeja=self.bandeja, pruebas=self.pruebas).ejecutar()
        registro.guardar()
        return registro


def _utc(local: str) -> datetime:
    return datetime.strptime(local, "%Y-%m-%d %H:%M").replace(tzinfo=MADRID).astimezone(UTC)


def _mensaje(texto, cuando):
    return _mensaje_lucia(texto, cuando)


# ---------------------------------------------------------------------------
#  Respuestas a mensajes
# ---------------------------------------------------------------------------


def test_mensaje_del_huesped_genera_un_borrador_con_su_hilo_y_sin_repetirse(tmp_path):
    s = Entorno(tmp_path, _utc("2026-10-03 10:30"), respuestas_momentos=[])
    s.recibir(CONF_LUCIA, remitente="Airbnb <automated@airbnb.com>")
    s.recibir(_mensaje("Hi! What is the wifi password? Is there an elevator?", "2026-10-03 10:00"), responder_a=HILO)
    s.ejecutar()
    [b] = s.bandeja.lista
    assert b["tipo"] == "respuesta" and b["responder_a"] == HILO and b["piso"] == "Corazón de Madrid"
    assert b["texto"] == "Hi Lucia! See you soon." and b["idioma"] == "es" and b["codigo"] == "HMTEST0002"
    assert "wifi password" in b["mensaje_huesped"] and b["asunto"].startswith("RE: Reservation for")
    assert b["pruebas"] is False and re.fullmatch(r"[0-9a-f]{16}", b["id"])
    prompt = s.gemini.prompts[0]
    assert "wifi: (FALTA ESTE DATO" in prompt and "There is an elevator." in prompt
    assert "Lucia" in prompt and "<mensajes_huesped>" in prompt and "€" not in prompt, "sin importes"
    # El hilo de Airbnb queda en la reserva (para la agenda) y no se repite el borrador
    assert _estado_guardado(s.cal.ev("HMTEST0002"))["responder_a"] == HILO
    s.ejecutar()
    s.ejecutar(desde=datetime(2026, 10, 1, tzinfo=UTC))
    assert len(s.bandeja.lista) == 1 and len(s.gemini.prompts) == 1


def test_cortesia_recarga_y_mensajes_antiguos_no_generan_borrador(tmp_path):
    s = Entorno(tmp_path, _utc("2026-10-08 10:00"), respuestas_momentos=[])
    s.recibir(CONF_LUCIA)
    s.recibir(_mensaje("Where can we park?", "2026-10-03 10:00"), responder_a=HILO)      # 5 días: demasiado antiguo
    s.recibir(_mensaje("Thanks!", "2026-10-08 09:00"), responder_a=HILO)                 # cortesía
    s.ejecutar()
    assert s.bandeja.lista == []
    s2 = Entorno(tmp_path / "b", _utc("2026-10-03 11:00"))
    s2.recibir(CONF_LUCIA)
    s2.recibir(_mensaje("Where can we park?", "2026-10-03 10:00"), responder_a=HILO)
    s2.ejecutar(desde=datetime(2026, 9, 1, tzinfo=UTC))                                  # recarga manual
    assert s2.bandeja.lista == []


def test_varios_mensajes_sin_contestar_van_en_un_solo_borrador(tmp_path):
    s = Entorno(tmp_path, _utc("2026-10-03 10:30"), respuestas_momentos=[])
    s.recibir(CONF_LUCIA)
    s.recibir(_mensaje("Is there an elevator?", "2026-10-03 10:00"), responder_a=HILO)
    s.ejecutar()
    s.ahora = _utc("2026-10-03 12:30")
    s.recibir(_mensaje("Also, can we check-in early?", "2026-10-03 12:00"), responder_a=HILO)
    s.ejecutar()
    primero, segundo = s.bandeja.lista
    assert primero["estado"] == "sustituido" and segundo["reemplaza"] == [primero["id"]]
    assert "elevator" in segundo["mensaje_huesped"] and "check-in early" in segundo["mensaje_huesped"]
    assert "Is there an elevator?" in s.gemini.prompts[1]
    # Pide entrar antes: el anfitrión recibe la nota de la limpieza con los datos del calendario
    assert any(n.startswith("🧹 Entrada antes") for n in segundo["notas"]), segundo["notas"]


def test_bandeja_caida_guarda_el_borrador_y_lo_reintenta(tmp_path):
    s = Entorno(tmp_path, _utc("2026-10-03 10:30"), respuestas_momentos=[])
    s.bandeja.fallar = 1
    s.recibir(CONF_LUCIA)
    s.recibir(_mensaje("Is there an elevator?", "2026-10-03 10:00"), responder_a=HILO)
    reg = s.ejecutar()
    assert s.bandeja.lista == [] and len(reg.por_publicar) == 1
    assert s.tecnicos == ["⚠️ Airbnb → Telegram: la bandeja no responde"]
    assert _estado_guardado(s.cal.ev("HMTEST0002")), "el calendario se actualiza igual"
    reg = s.ejecutar()
    assert len(s.bandeja.lista) == 1 and reg.por_publicar == [] and len(s.gemini.prompts) == 1


def test_gemini_caido_deja_igualmente_aviso_en_telegram_para_contestar_a_mano(tmp_path):
    s = Entorno(tmp_path, _utc("2026-10-03 10:30"), respuestas_momentos=[])
    s.gemini.falla_redaccion = True
    s.recibir(CONF_LUCIA)
    s.recibir(_mensaje("Is there an elevator?", "2026-10-03 10:00"), responder_a=HILO)
    s.ejecutar()
    [b] = s.bandeja.lista
    assert b["texto"] == "" and b["confianza"] == "baja" and b["notas"][0].startswith("⚠️ Gemini")


def test_entorno_de_pruebas_marca_los_borradores(tmp_path):
    s = Entorno(tmp_path, _utc("2026-10-03 10:30"), respuestas_momentos=[])
    s.pruebas = True
    s.recibir(CONF_LUCIA)
    s.recibir(_mensaje("Is there an elevator?", "2026-10-03 10:00"), responder_a=HILO)
    s.ejecutar()
    assert s.bandeja.lista[0]["pruebas"] is True


def test_borrador_con_enlace_inventado_o_huecos_baja_la_confianza(tmp_path):
    s = Entorno(tmp_path, _utc("2026-10-03 10:30"), respuestas_momentos=[])
    s.gemini.borrador = {"idioma": "en", "borrador": "Park here https://parking-falso.com and wifi [[COMPLETAR: wifi de Tirso]]. "
                                                     "Food: https://goo.gl/maps/NWK7o4tC1aiekhpCA.",
                         "notas_para_ti": [], "faltan_datos": [], "confianza": "alta", "necesita_respuesta": True}
    s.recibir(CONF_LUCIA)
    s.recibir(_mensaje("Parking? Wifi?", "2026-10-03 10:00"), responder_a=HILO)
    s.ejecutar()
    [b] = s.bandeja.lista
    assert b["confianza"] == "baja" and b["faltan"] == ["wifi de Tirso"]
    assert b["notas"][0] == "⚠️ Enlace que NO está en la base (compruébalo o quítalo): https://parking-falso.com"


# ---------------------------------------------------------------------------
#  Agenda
# ---------------------------------------------------------------------------


def test_confirmacion_propone_pedir_la_hora_de_llegada_para_copiar(tmp_path):
    s = Entorno(tmp_path, _utc("2026-10-01 10:30"))
    s.recibir(replace(CONF_LUCIA, fecha_hora="2026-10-01 10:00"), remitente="Airbnb <automated@airbnb.com>")
    s.ejecutar()
    [b] = s.bandeja.lista
    assert b["tipo"] == "momento" and b["momento"] == "pedir_hora_llegada" and b["responder_a"] == ""
    assert any("cópialo y pégalo" in n for n in b["notas"])
    assert "Hola Lucia, ¿a qué hora llegas?" in s.gemini.prompts[0] and "<plantilla>" in s.gemini.prompts[0]


def test_agenda_bienvenida_el_dia_de_llegada_una_sola_vez_y_con_lo_que_falta(tmp_path):
    s = Entorno(tmp_path, _utc("2026-10-03 10:30"), respuestas_momentos=["bienvenida", "pedir_hora_salida"])
    s.recibir(CONF_LUCIA)
    s.recibir(_mensaje("Is there an elevator?", "2026-10-03 10:00"), responder_a=HILO)
    s.ejecutar()
    n = len(s.bandeja.lista)
    s.ahora = _utc("2026-11-26 11:00")                 # día de llegada, antes de las 12:00
    s.ejecutar()
    assert len(s.bandeja.lista) == n
    s.ahora = _utc("2026-11-26 12:05")
    s.ejecutar()
    s.ejecutar()
    [bienvenida] = [b for b in s.bandeja.lista if b.get("momento") == "bienvenida"]
    assert bienvenida["responder_a"] == HILO, "usa el hilo que dejó el mensaje anterior"
    assert bienvenida["faltan"][0] == "wifi de Corazón de Madrid"
    plantilla = s.gemini.prompts[-1].split("<plantilla>")[1]
    assert "Welcome Lucia!" in plantilla and "Lidl next door." in plantilla and "New listing!" not in plantilla
    assert "What time will you leave on Wednesday 02/12?" in plantilla, "no sabemos a qué hora se va"


def test_agenda_no_propone_mensajes_pendientes_ni_desactivados(tmp_path):
    s = Entorno(tmp_path, _utc("2026-10-03 10:30"), respuestas_momentos=["mitad_estancia"])
    s.recibir(CONF_LUCIA)
    s.ejecutar()
    s.ahora = _utc("2026-11-29 11:30")                 # mitad de la estancia (26/11 → 02/12)
    s.ejecutar()
    assert [b for b in s.bandeja.lista if b["tipo"] == "momento"] == [], "mitad_estancia está PENDIENTE en la base"
    s.ahora = _utc("2026-12-01 10:30")                 # día antes de salir, pero no está activado
    s.ejecutar()
    assert [b for b in s.bandeja.lista if b["tipo"] == "momento"] == []


# ---------------------------------------------------------------------------
#  Base de conocimiento
# ---------------------------------------------------------------------------


def test_momentos_fechas():
    b = BaseConocimiento(BASE)
    assert str(b.momentos["dia_antes_salida"].fecha("2026-11-26", "2026-12-02")) == "2026-12-01"
    assert str(b.momentos["mitad_estancia"].fecha("2026-11-26", "2026-12-02")) == "2026-11-29"
    assert b.momentos["mitad_estancia"].fecha("2026-11-26", "2026-11-29") is None, "menos de 4 noches"
    assert b.momentos["pedir_hora_llegada"].fecha("2026-11-26", "2026-12-02") is None


PRIVADA_EJEMPLO = (RAIZ / "base_privada.ejemplo.toml").read_text("utf-8")


def test_base_real_coincide_con_config_y_no_mezcla_pisos():
    base = BaseConocimiento.cargar(RAIZ / "base_conocimiento.toml", privada=PRIVADA_EJEMPLO.replace(
        "NOMBRE_DE_LA_RED", "Red-Buenavista", 1).replace("NOMBRE_DE_LA_RED", "Red-Torrecilla"))
    cfg = cargar_config(RAIZ / "config.toml") if "PEGA_AQUI" not in (RAIZ / "config.toml").read_text("utf-8") else None
    with open(RAIZ / "config.toml", "rb") as f:
        t = tomllib.load(f)
    alias = {a["alias"] for a in t["alojamientos"]}
    assert set(base.pisos) == alias, "los pisos de la base deben llamarse como los alias de config.toml"
    for m in t.get("respuestas", {}).get("momentos", []):
        assert m in base.momentos, m
    tirso, torre = base.texto_para("Tirso"), base.texto_para("Torrecilla")
    assert "Calle Atocha, 7" in tirso and "Calle Atocha, 86" in torre
    assert "Red-" not in tirso and "Red-Torrecilla" in torre and "Red-Buenavista" not in torre, "cada piso solo ve su wifi"
    assert "common areas" not in tirso and "common areas" in torre
    assert cfg is None or cfg.respuestas_base == "base_conocimiento.toml"
    json.dumps(base.datos)       # serializable


def test_dias_de_la_agenda_por_defecto_son_validos():
    base = BaseConocimiento.cargar(RAIZ / "base_conocimiento.toml")
    for m in base.momentos.values():
        assert m.dia in {"confirmacion", "con_bienvenida", "mitad"} or re.fullmatch(r"check(in|out)([+-]\d+)?", m.dia), m.id
        assert re.fullmatch(r"\d{2}:\d{2}", m.hora), m.id
    assert timedelta(0) == timedelta(0)


def test_mitad_de_estancia_con_nota_para_personalizar_y_solo_en_estancias_largas(tmp_path):
    s = Entorno(tmp_path, _utc("2026-10-03 10:30"), respuestas_momentos=["mitad_estancia"])
    s.base = BaseConocimiento({**BASE, "momentos": [{"id": "mitad_estancia", "dia": "mitad", "hora": "11:00", "noches_minimas": 4,
                                                     "nota": "Personalízalo o descártalo.", "texto": "Hi {nombre}! All OK?"}]})
    s.recibir(CONF_LUCIA)                               # 26/11 → 02/12: 6 noches, mitad el 29/11
    s.ejecutar()
    s.ahora = _utc("2026-11-29 11:30")
    s.ejecutar()
    [b] = [b for b in s.bandeja.lista if b["tipo"] == "momento"]
    assert b["momento"] == "mitad_estancia" and b["notas"][0] == "Personalízalo o descártalo."
    assert "Hi Lucia! All OK?" in s.gemini.prompts[-1]


def test_base_real_rellena_el_locker_de_cada_piso_y_marca_lo_que_falta():
    base = BaseConocimiento.cargar(RAIZ / "base_conocimiento.toml", privada=PRIVADA_EJEMPLO)
    assert "at Calle Atocha 86, so you can plan" in base.texto_para("Torrecilla")
    assert "at Calle Atocha 7, so you can plan" in base.texto_para("Tirso") and base.pendientes("Tirso") == []
    assert base.firma == "" and "firma de los mensajes" not in base.pendientes("Tirso")
    assert "QR code" in base.texto_para("Tirso") and "doctor cortezo" in base.texto_para("Buenavista")


# ---------------------------------------------------------------------------
#  Repositorio público: datos privados y registro cifrado
# ---------------------------------------------------------------------------


def test_la_base_publica_no_lleva_contrasenas_y_sin_base_privada_el_wifi_queda_pendiente():
    texto = (RAIZ / "base_conocimiento.toml").read_text("utf-8")
    assert not re.search(r"^\s*Password:", texto, re.M), "la base del repositorio público no puede llevar contraseñas"
    base = BaseConocimiento.cargar(RAIZ / "base_conocimiento.toml", privada="")
    assert "wifi: (FALTA ESTE DATO" in base.texto_para("Torrecilla")
    assert "wifi de Torrecilla (falta el secreto BASE_PRIVADA)" in base.pendientes("Torrecilla")
    con = BaseConocimiento.cargar(RAIZ / "base_conocimiento.toml", privada=PRIVADA_EJEMPLO)
    assert "Password: CONTRASEÑA" in con.texto_para("Torrecilla") and con.pendientes("Torrecilla") == []
    assert con.pisos["Torrecilla"]["camas"] == base.pisos["Torrecilla"]["camas"], "lo privado se superpone, no sustituye el piso"


def test_base_privada_mal_escrita_para_con_un_error_claro():
    with pytest.raises(SystemExit, match="BASE_PRIVADA"):
        BaseConocimiento.cargar(RAIZ / "base_conocimiento.toml", privada="[pisos.Torrecilla\nwifi = 1")


def test_el_registro_publico_no_guarda_datos_de_huespedes_en_claro(tmp_path):
    ruta = tmp_path / "estado.json"
    r = Registro(ruta, secreto="clave-larga")
    r.por_publicar.append({"id": "x", "texto": "Password: secreta", "mensaje_huesped": "Hola soy Lucía"})
    r.guardar()
    crudo = ruta.read_text("utf-8")
    assert "secreta" not in crudo and "Lucía" not in crudo
    r2 = Registro(ruta, secreto="clave-larga")
    assert r2.por_publicar[0]["texto"] == "Password: secreta"
    assert Registro(ruta, secreto="otra").por_publicar == [], "con otra clave no se puede leer"
    sin = Registro(tmp_path / "otro.json")
    sin.por_publicar.append({"id": "y", "texto": "Password: secreta"})
    sin.guardar()
    assert "secreta" not in (tmp_path / "otro.json").read_text("utf-8"), "sin clave, no se guarda"


def test_el_borrador_lleva_la_cuenta_que_recibio_el_hilo(tmp_path):
    s = Entorno(tmp_path, _utc("2026-10-03 10:30"), respuestas_momentos=[])
    s.recibir(CONF_LUCIA)
    s.recibir(_mensaje("Hi! What is the wifi password? Is there an elevator?", "2026-10-03 10:00"), responder_a=HILO)
    s.ejecutar()
    [b] = s.bandeja.lista
    assert b["responder_a"] == HILO and "cuenta" in b


def test_los_borradores_van_en_espanol_aunque_el_huesped_escriba_en_ingles(tmp_path):
    s = Entorno(tmp_path, _utc("2026-10-03 10:30"), respuestas_momentos=[])
    s.recibir(CONF_LUCIA)
    s.recibir(_mensaje("Hi! Is there an elevator?", "2026-10-03 10:00"), responder_a=HILO)
    s.ejecutar()
    [b] = s.bandeja.lista
    assert b["idioma"] == "es", "el idioma lo fija config.toml, no lo que diga la IA"
    sistema = s.gemini.cuerpos[-1]["systemInstruction"]["parts"][0]["text"]
    assert "SIEMPRE en español" in sistema and "el del ÚLTIMO mensaje" not in sistema


def test_con_idioma_vacio_se_contesta_en_el_del_huesped(tmp_path):
    s = Entorno(tmp_path, _utc("2026-10-03 10:30"), respuestas_momentos=[], respuestas_idioma="")
    s.recibir(CONF_LUCIA)
    s.recibir(_mensaje("Hi! Is there an elevator?", "2026-10-03 10:00"), responder_a=HILO)
    s.ejecutar()
    [b] = s.bandeja.lista
    assert b["idioma"] == "en"
    assert "el del ÚLTIMO mensaje" in s.gemini.cuerpos[-1]["systemInstruction"]["parts"][0]["text"]


def test_config_real_pide_espanol():
    with open(RAIZ / "config.toml", "rb") as f:
        assert tomllib.load(f)["respuestas"]["idioma"] == "es"
