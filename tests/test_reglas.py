"""Reglas (estado, avisos, formato) y Gemini (petición y respuesta)."""
import pytest

from airbnb_calendar import reglas
from airbnb_calendar.config import Alojamiento
from airbnb_calendar.gemini import construir_peticion, interpretar_respuesta
from airbnb_calendar.parser import parsear_correo

from conftest import ALOJAMIENTOS_PRUEBA, cargar_ejemplo


def parsear(e):
    return parsear_correo(e.asunto, e.cuerpo, e.fecha_ref, ALOJAMIENTOS_PRUEBA)


@pytest.fixture
def r0():
    return parsear(cargar_ejemplo("ej0.txt", "2026-10-01")[0])


@pytest.fixture
def r3():
    return parsear(cargar_ejemplo("ej3.txt", "2026-09-22")[0])


def analisis(**kw):
    return {"hora_llegada": None, "hora_salida": None, "sofa_cama": "sin_mencion", "cuna": "sin_mencion",
            "otras_peticiones": [], "resumen": "", **kw}


def test_confirmacion_titulos_y_descripcion(r0, cfg):
    e = reglas.crear_estado(r0)
    reglas.aplicar_reglas(e, None, cfg)
    assert reglas.titulo_evento(e) == "🏠 Corazón de Madrid · 2 pers"
    d = reglas.descripcion_evento(e, "x")
    assert "📅 Check-in: jueves 26/11/2026 · desde 15:00" in d
    assert "📅 Check-out: miércoles 02/12/2026 · hasta 11:00" in d
    assert "👥 Personas: 2 (2 adultos)" in d
    assert "Hora prevista de llegada: No indicada" in d
    assert "Sofá cama: No indicado" in d
    assert "Código: HMTSBCMZH9" in d
    assert reglas.avisos_pendientes(e, cfg) == []


def test_bebe_cuna_por_regla_luego_confirmada_por_el_huesped(r3, cfg):
    e = reglas.crear_estado(r3)
    reglas.aplicar_reglas(e, None, cfg)
    assert e["cuna"] == {"v": True, "f": "regla"}
    assert reglas.avisos_pendientes(e, cfg) == ["🍼 Cuna — 1 bebé en la reserva"]
    reglas.marcar_avisado(e)

    reglas.fusionar_analisis(e, analisis(hora_llegada="15:05", cuna="si", otras_peticiones=["Entrada anticipada hacia las 12:00"],
                                         resumen="Llegan en una hora; agradecen la cuna."), "huesped", "23/09")
    reglas.aplicar_reglas(e, None, cfg)
    assert e["cuna"] == {"v": True, "f": "huesped"}
    assert reglas.avisos_pendientes(e, cfg) == ["📝 Entrada anticipada hacia las 12:00"]
    reglas.marcar_avisado(e)
    assert reglas.titulo_evento(e) == "🏠 Antón Martín · 3 pers · llega 15:05 · 🍼 cuna"
    assert "🍼 Cuna: SÍ — lo indica el huésped" in reglas.descripcion_evento(e)
    assert "23/09: Llegan en una hora; agradecen la cuna." in reglas.descripcion_evento(e)

    # La misma petición reformulada igual no se duplica
    reglas.fusionar_analisis(e, analisis(otras_peticiones=["entrada anticipada hacia las 12:00"]), "huesped", "24/09")
    assert len(e["peticiones"]) == 1

    reglas.fusionar_analisis(e, analisis(hora_salida="10:50"), "huesped", "27/09")
    assert reglas.titulo_evento(e) == "🏠 Antón Martín · 3 pers · llega 15:05 · sale 10:50 · 🍼 cuna"
    assert reglas.avisos_pendientes(e, cfg) == []


def test_ya_no_necesita_cuna_prevalece_sobre_la_regla(r3, cfg):
    e = reglas.crear_estado(r3)
    reglas.aplicar_reglas(e, None, cfg)
    reglas.marcar_avisado(e)
    reglas.fusionar_analisis(e, analisis(cuna="no"), "huesped")
    reglas.aplicar_reglas(e, None, cfg)
    assert e["cuna"]["v"] is False
    assert reglas.avisos_pendientes(e, cfg) == ["🍼 Ya NO necesita cuna"]
    # Las palabras clave (respaldo) no pueden contradecir al huésped
    reglas.fusionar_analisis(e, reglas.analisis_por_palabras_clave("the crib"), "palabras")
    assert e["cuna"]["v"] is False


def test_sofa_cama_por_capacidad_del_alojamiento(cfg):
    r2 = parsear(cargar_ejemplo("ej2.txt", "2026-10-01")[0])      # 3 adultos
    e = reglas.crear_estado(r2)
    reglas.aplicar_reglas(e, Alojamiento("x", "x", capacidad_sin_sofa=2), cfg)
    assert e["sofa"] == {"v": True, "f": "regla"}
    assert "🛋️ sofá cama" in reglas.titulo_evento(e)
    e2 = reglas.crear_estado(r2)
    reglas.aplicar_reglas(e2, Alojamiento("x", "x", capacidad_sin_sofa=4), cfg)
    assert e2["sofa"] is None


def test_cancelacion_titulo_marcado_y_sin_avisos(r0, cfg):
    r1 = parsear(cargar_ejemplo("ej1.txt", "2026-10-01")[0])
    e = reglas.crear_estado(r0)
    reglas.fusionar_analisis(e, analisis(sofa_cama="si"), "huesped")
    reglas.actualizar_con_datos(e, r1)
    e["cancelada"] = True
    assert reglas.titulo_evento(e).startswith("❌ CANCELADA · 🏠 Corazón de Madrid")
    assert reglas.descripcion_evento(e).startswith("❌ RESERVA CANCELADA")
    assert reglas.avisos_pendientes(e, cfg) == []
    assert e["huesped"] == "Imane Bensalah"      # "Sam" (más corto y distinto) no lo pisa


def test_palabras_clave(r3):
    assert reglas.analisis_por_palabras_clave(r3.mensajes[0].texto)["cuna"] == "si"
    assert reglas.analisis_por_palabras_clave("On aura besoin du canapé-lit")["sofa_cama"] == "si"
    assert reglas.analisis_por_palabras_clave("¿Podemos usar el sofá cama?")["sofa_cama"] == "si"
    assert reglas.analisis_por_palabras_clave("Will the check in information come via e-mail?")["cuna"] == "sin_mencion"


def test_peticion_gemini_incluye_contexto_esquema_y_texto():
    p = construir_peticion("We will be there in one hour", {
        "alojamiento": "X", "checkin": "2026-09-23", "checkout": "2026-09-27", "hora_checkin": "15:00",
        "hora_checkout": "11:00", "personas": "3 (2 adultos, 1 bebé)", "enviado_el": "2026-09-23 14:05",
        "peticiones_previas": ["Trona"]}, "gemini-3.5-flash-lite", "low")
    txt = p["contents"][0]["parts"][0]["text"]
    assert "Mensaje enviado el: 2026-09-23 14:05" in txt
    assert "Peticiones ya registradas: Trona" in txt
    assert "We will be there in one hour" in txt
    assert p["generationConfig"]["responseMimeType"] == "application/json"
    assert p["generationConfig"]["responseSchema"]["properties"]["cuna"]["enum"] == ["si", "no", "sin_mencion"]
    assert p["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    assert "thinkingConfig" not in construir_peticion("x", {}, "gemini-2.5-flash", "low")["generationConfig"]


def test_respuesta_gemini_se_valida_y_normaliza():
    a = interpretar_respuesta({"candidates": [{"content": {"parts": [
        {"text": "pensando...", "thought": True},
        {"text": '```json\n{"hora_llegada":"9:30","hora_salida":"25:00","sofa_cama":"SI","cuna":"si",'
                 '"otras_peticiones":["Trona",""],"resumen":" ok "}\n```'},
    ]}}]})
    assert a == {"hora_llegada": "09:30", "hora_salida": None, "sofa_cama": "sin_mencion", "cuna": "si",
                 "otras_peticiones": ["Trona"], "resumen": "ok"}
    with pytest.raises(RuntimeError, match="SAFETY"):
        interpretar_respuesta({"promptFeedback": {"blockReason": "SAFETY"}})
