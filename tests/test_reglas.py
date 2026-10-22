"""Reglas (estado, horario, limpieza), formato de los eventos, avisos y análisis de mensajes (Gemini y sin IA)."""
import pytest

from airbnb_calendar import avisos, formato, gemini, reglas
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
            "otras_peticiones": [], **kw}


def test_confirmacion_titulos_y_descripcion(r0, cfg):
    e = reglas.crear_estado(r0)
    reglas.aplicar_reglas(e, None, cfg)
    assert formato.titulo_entrada(e) == "🟢 ENTRADA Corazón de Madrid · 2 pers"
    assert formato.titulo_salida(e) == "🔴 SALIDA Corazón de Madrid · 2 pers"
    d = formato.descripcion_evento(e)
    assert d == "🕒 Llegada: jue 26/11 a las 15:00   ·   Salida: mié 02/12 a las 11:00\n🛋️ Sofá cama: no\n🍼 Cuna: no\n\n👤 Lucia Romero · Código HMTEST0002"
    assert avisos.avisos_pendientes(e, cfg) == []


def test_bebe_cuna_por_regla_luego_confirmada_por_el_huesped(r3, cfg):
    e = reglas.crear_estado(r3)
    reglas.aplicar_reglas(e, None, cfg)
    assert e["cuna"] == {"v": True, "f": "regla"}
    assert avisos.avisos_pendientes(e, cfg) == ["🍼 Cuna — 1 bebé en la reserva"]
    avisos.marcar_avisado(e)

    reglas.fusionar_analisis(e, analisis(hora_llegada="15:05", cuna="si",
                                         otras_peticiones=["Entrada anticipada hacia las 12:00"]), "huesped")
    reglas.aplicar_reglas(e, None, cfg)
    assert e["cuna"] == {"v": True, "f": "huesped"}
    assert avisos.avisos_pendientes(e, cfg) == ["📝 Entrada anticipada hacia las 12:00"]
    avisos.marcar_avisado(e)
    assert formato.titulo_entrada(e) == "🟢 ENTRADA Antón Martín · 3 pers · llega 15:05 · 🍼 cuna"
    d = formato.descripcion_evento(e)
    assert "🕒 Llegada: mié 23/09 a las 15:05   ·   Salida: dom 27/09 a las 11:00" in d
    assert "🍼 Cuna: SÍ (lo indica el huésped)" in d
    assert "👥" not in d, "niños y bebés ya se ven en sofá cama y cuna"
    assert "📝 Peticiones:\n   • Entrada anticipada hacia las 12:00" in d

    # La misma petición reformulada igual no se duplica
    reglas.fusionar_analisis(e, analisis(otras_peticiones=["entrada anticipada hacia las 12:00"]), "huesped")
    assert len(e["peticiones"]) == 1

    reglas.fusionar_analisis(e, analisis(hora_salida="10:50"), "huesped")
    assert formato.titulo_salida(e) == "🔴 SALIDA Antón Martín · 3 pers · sale 10:50"
    assert formato.titulo_entrada(e) == "🟢 ENTRADA Antón Martín · 3 pers · llega 15:05 · 🍼 cuna"
    assert avisos.avisos_pendientes(e, cfg) == []


def test_ya_no_necesita_cuna_prevalece_sobre_la_regla(r3, cfg):
    e = reglas.crear_estado(r3)
    reglas.aplicar_reglas(e, None, cfg)
    avisos.marcar_avisado(e)
    reglas.fusionar_analisis(e, analisis(cuna="no"), "huesped")
    reglas.aplicar_reglas(e, None, cfg)
    assert e["cuna"]["v"] is False
    assert avisos.avisos_pendientes(e, cfg) == ["🍼 Ya NO necesita cuna"]
    # Las palabras clave (respaldo) no pueden contradecir al huésped
    reglas.fusionar_analisis(e, gemini.analisis_por_palabras_clave("the crib"), "palabras")
    assert e["cuna"]["v"] is False


def test_sofa_cama_por_capacidad_del_alojamiento(cfg):
    r2 = parsear(cargar_ejemplo("ej2.txt", "2026-10-01")[0])      # 3 adultos
    e = reglas.crear_estado(r2)
    reglas.aplicar_reglas(e, Alojamiento("x", "x", capacidad_sin_sofa=2), cfg)
    assert e["sofa"] == {"v": True, "f": "regla"}
    assert "🛋️ sofá cama" in formato.titulo_entrada(e)
    e2 = reglas.crear_estado(r2)
    reglas.aplicar_reglas(e2, Alojamiento("x", "x", capacidad_sin_sofa=4), cfg)
    assert e2["sofa"] is None


def test_cancelacion_titulo_marcado_y_sin_avisos(r0, cfg):
    r1 = parsear(cargar_ejemplo("ej1.txt", "2026-10-01")[0])
    e = reglas.crear_estado(r0)
    reglas.fusionar_analisis(e, analisis(sofa_cama="si"), "huesped")
    reglas.actualizar_con_datos(e, r1)
    e["cancelada"] = True
    assert formato.titulo_entrada(e) == "❌ CANCELADA · 🟢 ENTRADA Corazón de Madrid · 1 pers · 🛋️ sofá cama"
    assert formato.titulo_salida(e) == "❌ CANCELADA · 🔴 SALIDA Corazón de Madrid · 1 pers"
    assert formato.descripcion_evento(e) == "👤 Lucia Romero · Código HMTEST0003"
    assert avisos.avisos_pendientes(e, cfg) == []
    assert e["huesped"] == "Lucia Romero"      # "Marc" (más corto y distinto) no lo pisa


def test_palabras_clave(r3):
    assert gemini.analisis_por_palabras_clave(r3.mensajes[0].texto)["cuna"] == "si"
    assert gemini.analisis_por_palabras_clave("On aura besoin du canapé-lit")["sofa_cama"] == "si"
    assert gemini.analisis_por_palabras_clave("¿Podemos usar el sofá cama?")["sofa_cama"] == "si"
    assert gemini.analisis_por_palabras_clave("Will the check in information come via e-mail?")["cuna"] == "sin_mencion"


def test_palabras_clave_entrada_anticipada_y_salida_tardia():
    pk = gemini.analisis_por_palabras_clave
    # Mensaje real (Stina, 06/10): pregunta condicional, sin hora
    assert pk("Will you notify me when the apartment is ready, so I know if it’s possible to check-in early? :)")[
        "entrada_anticipada"] == "si"
    assert pk("Can we arrive earlier, before 3pm?")["entrada_anticipada"] == "si"
    assert pk("¿Podríamos llegar antes de las 15:00?")["entrada_anticipada"] == "si"
    assert pk("Est-ce qu'on peut arriver plus tôt ?")["entrada_anticipada"] == "si"
    assert pk("Is a late check-out possible?")["salida_tardia"] == "si"
    assert pk("We will arrive around 6pm")["entrada_anticipada"] is None
    assert pk("Thanks, see you!")["salida_tardia"] is None


def test_peticion_de_entrar_antes_se_avisa_aunque_haya_una_anterior(r0, cfg):
    """Caso real: primero 'llegaremos antes de las 17:00', después pide entrar antes de las 15:00."""
    e = reglas.crear_estado(r0)
    reglas.fusionar_analisis(e, analisis(hora_llegada="17:00"), "huesped", cfg=cfg)
    assert e["entrada_anticipada"] is None, "17:00 es después de la hora de entrada: no pide nada"
    avisos.marcar_avisado(e)

    reglas.fusionar_analisis(e, analisis(entrada_anticipada="si"), "huesped")
    av = avisos.avisos_pendientes(e, cfg)
    assert av == ["⏰ Pide ENTRAR ANTES de las 15:00: sin hora concreta (pregunta si es posible)"]
    assert "⏰ pide entrar antes" in formato.titulo_entrada(e)
    assert "escribe arriba: #entrada HH:MM" in formato.descripcion_evento(e)
    avisos.marcar_avisado(e)
    assert avisos.avisos_pendientes(e, cfg) == [], "no se repite"

    # Luego concreta la hora: nuevo aviso; un 'si' posterior sin hora no la borra
    reglas.fusionar_analisis(e, analisis(entrada_anticipada="12:30"), "huesped")
    assert avisos.avisos_pendientes(e, cfg) == ["⏰ Pide ENTRAR ANTES de las 15:00: a las 12:30"]
    avisos.marcar_avisado(e)
    reglas.fusionar_analisis(e, analisis(entrada_anticipada="si"), "huesped")
    assert e["entrada_anticipada"] == "12:30" and avisos.avisos_pendientes(e, cfg) == []

    # Con acuerdo del anfitrión el título muestra el acuerdo, no la petición
    e["acuerdo_entrada"] = "12:30"
    assert "pide entrar" not in formato.titulo_entrada(e)
    assert "escribe arriba" not in formato.descripcion_evento(e)


def test_valores_por_defecto_si_no_hay_informacion(r0, cfg):
    e = reglas.crear_estado(r0)
    d = formato.descripcion_evento(e, None, cfg)
    assert "🕒 Llegada: jue 26/11 a las 15:00   ·   Salida: mié 02/12 a las 11:00" in d
    assert "🛋️ Sofá cama: no\n" in d and "🍼 Cuna: no" in d
    e.update(acuerdo_salida="13:00", hora_llegada="19:00", sofa={"v": True, "f": "regla"})
    d = formato.descripcion_evento(e, None, cfg)
    assert "🕒 Llegada: jue 26/11 a las 19:00   ·   Salida: mié 02/12 a las 13:00" in d, "lo que dice el huésped o lo acordado manda"
    assert "🛋️ Sofá cama: SÍ (" in d


def test_pide_entrar_antes_y_dice_a_que_hora_llega(r0, cfg):
    """Caso real (recarga del 18/10): 'llegamos a las 13:00, ¿podemos entrar antes?' → pide entrar a las 13:00."""
    e = reglas.crear_estado(r0)
    reglas.fusionar_analisis(e, analisis(hora_llegada="13:00", entrada_anticipada="si"), "huesped", cfg=cfg)
    assert e["entrada_anticipada"] == "13:00"
    assert avisos.avisos_pendientes(e, cfg) == ["⏰ Pide ENTRAR ANTES de las 15:00: a las 13:00"]
    assert formato.titulo_entrada(e) == "🟢 ENTRADA Corazón de Madrid · 2 pers · ⏰ pide entrar 13:00"


def test_llegar_de_madrugada_es_llegar_tarde_no_entrar_antes(r0, cfg):
    e = reglas.crear_estado(r0)
    reglas.fusionar_analisis(e, analisis(hora_llegada="00:30"), "huesped", cfg=cfg)
    assert e["hora_llegada"] == "00:30" and e["entrada_anticipada"] is None
    reglas.fusionar_analisis(e, analisis(hora_llegada="09:00"), "huesped", cfg=cfg)
    assert e["entrada_anticipada"] == "09:00"


def test_descripcion_ordenada_avisos_primero_locker_y_huesped_al_final(r0, cfg):
    e = reglas.crear_estado(r0)
    e.update(hora_llegada="18:00", entrada_anticipada="12:30", peticiones=["Trona"], locker="https://k/1",
             sofa={"v": True, "f": "huesped"})
    d = formato.descripcion_evento(e, None, cfg).split("\n\n")
    assert d[0] == "⏰ PIDE ENTRAR ANTES (a las 12:30). Si lo aceptas, escribe arriba: #entrada 12:30"
    assert d[1].startswith("🕒 Llegada: jue 26/11 a las 18:00   ·   Salida: mié 02/12 a las 11:00\n🛋️ Sofá cama: SÍ (lo indica el huésped)")
    assert d[2] == "📝 Peticiones:\n   • Trona"
    assert d[3] == "🔑 Locker: https://k/1"
    assert d[4] == "👤 Lucia Romero · Código HMTEST0002"


def test_lo_ultimo_que_dice_el_huesped_manda(r0, cfg):
    e = reglas.crear_estado(r0)
    reglas.fusionar_analisis(e, analisis(hora_llegada="11:00"), "huesped", cfg=cfg)
    assert e["entrada_anticipada"] == "11:00"
    reglas.fusionar_analisis(e, analisis(hora_llegada="18:00"), "huesped", cfg=cfg)
    assert e["hora_llegada"] == "18:00" and e["entrada_anticipada"] is None, "ya no necesita entrar antes"
    reglas.fusionar_analisis(e, analisis(entrada_anticipada="12:00"), "huesped", cfg=cfg)
    reglas.fusionar_analisis(e, analisis(entrada_anticipada="no"), "huesped", cfg=cfg)
    assert e["entrada_anticipada"] is None, "retira la petición"
    reglas.fusionar_analisis(e, analisis(hora_salida="13:00"), "huesped", cfg=cfg)
    assert e["salida_tardia"] == "13:00"
    reglas.fusionar_analisis(e, analisis(hora_salida="10:00"), "huesped", cfg=cfg)
    assert e["salida_tardia"] is None

    # Re-procesar la reserva desde su confirmación: se olvida lo deducido de los mensajes
    e.update(acuerdo_entrada="13:00", peticiones=["Trona"])
    reglas.resetear_mensajes(e)
    assert e["hora_llegada"] is None and e["peticiones"] == [] and not e["sin_analizar"]
    assert e["acuerdo_entrada"] == "13:00" and e["codigo"] == "HMTEST0002"


def test_llegar_antes_o_salir_despues_del_horario_cuenta_como_peticion(r0, cfg):
    e = reglas.crear_estado(r0)
    reglas.fusionar_analisis(e, analisis(hora_llegada="11:00", hora_salida="14:00"), "huesped", cfg=cfg)
    assert e["entrada_anticipada"] == "11:00" and e["salida_tardia"] == "14:00"
    av = avisos.avisos_pendientes(e, cfg)       # [avisos] horas = false, pero esto es una petición
    assert "⏰ Pide ENTRAR ANTES de las 15:00: a las 11:00" in av
    assert "⌛ Pide SALIR DESPUÉS de las 11:00: a las 14:00" in av
    assert reglas.hora_entrada(e, cfg) == "15:00", "sin acuerdo no se adelanta la entrada en el calendario"


def test_estado_antiguo_sin_campos_nuevos(r0, cfg):
    e = reglas.crear_estado(r0)
    for k in ("entrada_anticipada", "salida_tardia"):
        del e[k]
        del e["avisado"][k]
    assert avisos.avisos_pendientes(e, cfg) == []
    formato.titulo_entrada(e), formato.titulo_salida(e), formato.descripcion_evento(e)
    reglas.fusionar_analisis(e, analisis(salida_tardia="si"), "palabras")
    assert avisos.avisos_pendientes(e, cfg) == ["⌛ Pide SALIR DESPUÉS de las 11:00: sin hora concreta (pregunta si es posible)"]


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
                 '"entrada_anticipada":"12:00","salida_tardia":"null",'
                 '"otras_peticiones":["Trona",""]}\n```'},
    ]}}]})
    assert a == {"hora_llegada": "09:30", "hora_salida": None, "entrada_anticipada": "12:00", "salida_tardia": None,
                 "sofa_cama": "sin_mencion", "cuna": "si", "otras_peticiones": ["Trona"]}
    sin_campos = interpretar_respuesta({"candidates": [{"content": {"parts": [{"text": '{"entrada_anticipada":"si"}'}]}}]})
    assert sin_campos["entrada_anticipada"] == "si" and sin_campos["salida_tardia"] is None
    with pytest.raises(RuntimeError, match="SAFETY"):
        interpretar_respuesta({"promptFeedback": {"blockReason": "SAFETY"}})


@pytest.mark.parametrize("texto", ["Thanks!", "Thank you so much 😊", "Perfecto, gracias!", "Merci beaucoup !", "👍",
                                   "Ok", "Vale, perfecto", "Muchas gracias, hasta pronto"])
def test_mensajes_solo_de_cortesia_no_van_a_la_ia(texto):
    assert gemini.es_mensaje_de_cortesia(texto)


@pytest.mark.parametrize("texto", ["Thank you Julian!", "Thanks, see you at 6", "Thanks! Is there a hair dryer?",
                                   "Perfect, we land at 11am", "Gracias, llegaremos tarde", "Ok, we will need the sofa bed",
                                   "Thank you, can we leave our bags?", "Merci, on arrive vers 18h",
                                   "Great, early check-in possible?", "Thanks we are here",
                                   "Perfect thanks, we are 3 people now", "We are planning to be out right before 11",
                                   "Σας ευχαριστώ πολύ", "Мы приедем в три часа", "我们下午三点到", "شكرا جزيلا"])
def test_cualquier_dato_o_pregunta_va_a_la_ia(texto):
    assert not gemini.es_mensaje_de_cortesia(texto)
