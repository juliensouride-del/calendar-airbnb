"""Pruebas del bot de aprobación (sin red: Telegram, Upstash y SMTP son imitaciones)."""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

import index  # noqa: E402
from _bandeja.almacen import Almacen, Memoria  # noqa: E402
from _bandeja.correo import CorreoYahoo  # noqa: E402
from _bandeja.nucleo import Bandeja  # noqa: E402
from _bandeja.telegram import secreto_webhook, usuario_mini_app  # noqa: E402
from _bandeja.vista import LIMITE, tarjeta  # noqa: E402

SECRETO = "s" * 32
TOKEN = "123456:ABC-token-de-prueba"
YO, OTRO, INTRUSO = 111, 222, 999


class TelegramFalso:
    def __init__(self):
        self.enviados, self.editados, self.respuestas, self.n = [], [], [], 100

    def enviar(self, chat_id, texto, teclado=None, responder_a=None):
        self.n += 1
        self.enviados.append({"chat": chat_id, "id": self.n, "texto": texto, "teclado": teclado})
        return self.n

    def editar(self, chat_id, message_id, texto, teclado):
        self.editados.append({"chat": chat_id, "id": message_id, "texto": texto, "teclado": teclado})

    def responder_boton(self, callback_id, texto="", alerta=False):
        self.respuestas.append((texto, alerta))

    def configurar_webhook(self, url, secreto):
        self.webhook = (url, secreto)

    def info_webhook(self):
        return {"url": getattr(self, "webhook", ("",))[0]}

    def ultima(self, chat=YO):
        """Última versión de la última tarjeta de ese chat."""
        cand = [x for x in self.enviados + self.editados if x["chat"] == chat]
        return cand[-1]


class SMTPFalso:
    def __init__(self, fallar=False):
        self.mensajes, self.fallar = [], fallar

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, u, c):
        pass

    def send_message(self, msg):
        if self.fallar:
            raise OSError("SMTP caído")
        self.mensajes.append(msg)


@pytest.fixture
def entorno():
    tg, smtp = TelegramFalso(), SMTPFalso()
    bandeja = Bandeja(Almacen(Memoria()), tg, CorreoYahoo("anfitrion@yahoo.fr", "clave app", conectar=smtp),
                      [YO, OTRO], "https://bot.example.app")
    index.app.config.update(BANDEJA=bandeja, SECRETO=SECRETO, TOKEN=TOKEN, TESTING=True)
    return index.app.test_client(), bandeja, tg, smtp


def borrador(**kw):
    b = {"id": "a1b2c3d4e5f6a7b8", "tipo": "respuesta", "codigo": "HMAAAA0001", "piso": "Torrecilla", "huesped": "Laura Garcia",
         "checkin": "2026-12-05", "checkout": "2026-12-08", "personas": "2 adultos", "recibido": "2026-11-25T17:30:00+01:00",
         "mensaje_huesped": "Hi! Can we check in at 12?", "asunto": "RE: Reservation for Lavapies", "idioma": "en",
         "responder_a": "hilo123@reply.airbnb.com", "texto": "Hi Laura! We always try to be flexible…", "notas": ["🧹 nota"],
         "faltan": [], "confianza": "media", "reemplaza": [], "pruebas": False}
    b.update(kw)
    return b


def publicar(cliente, **kw):
    return cliente.post("/api/borradores", json=borrador(**kw), headers={"Authorization": f"Bearer {SECRETO}"})


def pulsar(cliente, accion, usuario=YO, id_="a1b2c3d4e5f6a7b8", tg=None):
    msg = next(x for x in reversed(tg.enviados) if x["chat"] == usuario) if tg and any(x["chat"] == usuario for x in tg.enviados) else {"chat": usuario, "id": 1}
    return cliente.post("/api/telegram", headers={"X-Telegram-Bot-Api-Secret-Token": secreto_webhook(SECRETO)}, json={
        "callback_query": {"id": "cb", "from": {"id": usuario}, "data": f"a:{accion}:{id_}",
                           "message": {"chat": {"id": msg["chat"]}, "message_id": msg["id"]}}})


def init_data(usuario=YO, edad_s=0, token=TOKEN):
    datos = {"auth_date": str(int(time.time()) - edad_s), "query_id": "q1", "user": json.dumps({"id": usuario, "first_name": "Yo"})}
    comprobacion = "\n".join(f"{k}={v}" for k, v in sorted(datos.items()))
    clave = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    datos["hash"] = hmac.new(clave, comprobacion.encode(), hashlib.sha256).hexdigest()
    return urlencode(datos)


def botones(tarjeta_tg):
    return [b["text"] for fila in tarjeta_tg["teclado"] or [] for b in fila]


# --------------------------------------------------------------------------


def test_crear_es_idempotente_y_llega_a_todos_los_aprobadores(entorno):
    cliente, bandeja, tg, _ = entorno
    assert publicar(cliente).get_json() == {"creado": True}
    assert publicar(cliente).get_json() == {"creado": False}
    assert [x["chat"] for x in tg.enviados] == [YO, OTRO]
    assert botones(tg.enviados[0]) == ["✅ Enviar", "✏️ Editar", "❌ Descartar"]
    assert "Laura Garcia" in tg.enviados[0]["texto"] and "<pre>Hi Laura!" in tg.enviados[0]["texto"]
    assert cliente.get("/api/borradores?codigo=HMAAAA0001&estado=pendiente",
                       headers={"Authorization": f"Bearer {SECRETO}"}).get_json()["borradores"][0]["id"] == "a1b2c3d4e5f6a7b8"


def test_sin_secreto_no_se_puede_crear_ni_listar(entorno):
    cliente, *_ = entorno
    assert cliente.post("/api/borradores", json=borrador()).status_code == 401
    assert cliente.post("/api/borradores", json=borrador(), headers={"Authorization": "Bearer otro"}).status_code == 401
    assert cliente.get("/api/borradores?codigo=HMAAAA0001").status_code == 401


def test_enviar_pide_confirmacion_y_envia_una_sola_vez(entorno):
    cliente, bandeja, tg, smtp = entorno
    publicar(cliente)
    pulsar(cliente, "env", tg=tg)
    assert not smtp.mensajes                                     # primero, confirmar
    assert botones(tg.ultima()) == ["✅ Sí, enviar", "↩️ No"]
    pulsar(cliente, "ok", tg=tg)
    assert len(smtp.mensajes) == 1
    m = smtp.mensajes[0]
    assert m["To"] == "hilo123@reply.airbnb.com" and m["From"] == "anfitrion@yahoo.fr"
    assert m["Subject"] == "RE: Reservation for Lavapies" and "We always try" in m.get_content()
    assert "✅ <b>Enviado</b>" in tg.ultima(YO)["texto"] and "✅ <b>Enviado</b>" in tg.ultima(OTRO)["texto"]
    assert tg.ultima()["teclado"] == []
    pulsar(cliente, "ok", tg=tg)                                 # otra vez (o el otro aprobador): no se repite
    pulsar(cliente, "ok", usuario=OTRO, tg=tg)
    assert len(smtp.mensajes) == 1
    assert tg.respuestas[-1] == ("Este borrador ya no está pendiente.", True)


def test_volver_no_envia(entorno):
    cliente, _, tg, smtp = entorno
    publicar(cliente)
    pulsar(cliente, "env", tg=tg)
    pulsar(cliente, "vol", tg=tg)
    assert botones(tg.ultima()) == ["✅ Enviar", "✏️ Editar", "❌ Descartar"] and not smtp.mensajes


def test_un_intruso_no_puede_hacer_nada(entorno):
    cliente, bandeja, tg, smtp = entorno
    publicar(cliente)
    for accion in ("env", "ok", "des"):
        pulsar(cliente, accion, usuario=INTRUSO, tg=tg)
    assert not smtp.mensajes and bandeja.almacen.leer("a1b2c3d4e5f6a7b8")["estado"] == "pendiente"
    assert tg.respuestas[-1] == ("No estás autorizado para aprobar mensajes.", True)
    r = cliente.post("/api/telegram", json={"callback_query": {}}, headers={"X-Telegram-Bot-Api-Secret-Token": "mal"})
    assert r.status_code == 401


def test_borrador_de_pruebas_se_simula(entorno):
    cliente, bandeja, tg, smtp = entorno
    publicar(cliente, pruebas=True)
    assert "🧪 <b>PRUEBAS</b>" in tg.enviados[0]["texto"]
    pulsar(cliente, "ok", tg=tg)
    assert not smtp.mensajes and bandeja.almacen.leer("a1b2c3d4e5f6a7b8")["estado"] == "simulado"


def test_sin_hilo_de_airbnb_solo_se_copia(entorno):
    cliente, bandeja, tg, smtp = entorno
    publicar(cliente, responder_a="")
    assert botones(tg.enviados[0]) == ["✏️ Editar", "✅ Ya lo pegué", "❌ Descartar", "🔗 Abrir Airbnb"]
    pulsar(cliente, "ok", tg=tg)
    assert not smtp.mensajes and tg.respuestas[-1][1]
    pulsar(cliente, "peg", tg=tg)
    assert bandeja.almacen.leer("a1b2c3d4e5f6a7b8")["estado"] == "copiado"


def test_huecos_completar_bloquean_el_envio_hasta_editarlos(entorno):
    cliente, bandeja, tg, smtp = entorno
    publicar(cliente, texto="Hi! Parking: [[COMPLETAR: parking de Torrecilla]]", faltan=["parking de Torrecilla"])
    assert "✅ Enviar" not in botones(tg.enviados[0])
    pulsar(cliente, "ok", tg=tg)
    assert not smtp.mensajes
    r = cliente.post("/api/b/a1b2c3d4e5f6a7b8/enviar", json={"texto": "Hi! Sorry, there is no parking."},
                     headers={"X-Init-Data": init_data()})
    assert r.status_code == 200 and smtp.mensajes[0].get_content().strip() == "Hi! Sorry, there is no parking."
    b = bandeja.almacen.leer("a1b2c3d4e5f6a7b8")
    assert b["estado"] == "enviado" and b["editado"] and b["texto_original"].startswith("Hi! Parking")


def test_mini_app_valida_la_firma_de_telegram(entorno):
    cliente, *_ = entorno
    publicar(cliente)
    assert cliente.get("/api/b/a1b2c3d4e5f6a7b8").status_code == 401
    assert cliente.get("/api/b/a1b2c3d4e5f6a7b8", headers={"X-Init-Data": init_data(token="otro:token")}).status_code == 401
    assert cliente.get("/api/b/a1b2c3d4e5f6a7b8", headers={"X-Init-Data": init_data(edad_s=2 * 86400)}).status_code == 401
    assert cliente.get("/api/b/a1b2c3d4e5f6a7b8", headers={"X-Init-Data": init_data(INTRUSO)}).status_code == 409
    d = cliente.get("/api/b/a1b2c3d4e5f6a7b8", headers={"X-Init-Data": init_data()}).get_json()
    assert d["texto"].startswith("Hi Laura") and d["se_envia"] and "responder_a" not in d
    assert "telegram-web-app.js" in cliente.get("/editar?id=x").get_data(as_text=True)


def test_guardar_desde_la_mini_app_actualiza_las_tarjetas_sin_enviar(entorno):
    cliente, bandeja, tg, smtp = entorno
    publicar(cliente)
    r = cliente.post("/api/b/a1b2c3d4e5f6a7b8/guardar", json={"texto": "Hola Laura, texto nuevo"}, headers={"X-Init-Data": init_data()})
    assert r.status_code == 200 and not smtp.mensajes
    assert "texto nuevo" in tg.ultima(YO)["texto"] and "texto nuevo" in tg.ultima(OTRO)["texto"] and "✏️ editado" in tg.ultima()["texto"]


def test_responder_a_la_tarjeta_cambia_el_texto(entorno):
    cliente, bandeja, tg, smtp = entorno
    publicar(cliente)
    tarjeta_yo = tg.enviados[0]
    cliente.post("/api/telegram", headers={"X-Telegram-Bot-Api-Secret-Token": secreto_webhook(SECRETO)}, json={"message": {
        "chat": {"id": YO, "type": "private"}, "from": {"id": YO}, "text": "Texto corregido a mano",
        "reply_to_message": {"message_id": tarjeta_yo["id"]}}})
    assert bandeja.almacen.leer("a1b2c3d4e5f6a7b8")["texto"] == "Texto corregido a mano" and not smtp.mensajes


def test_un_borrador_nuevo_sustituye_al_pendiente_del_mismo_huesped(entorno):
    cliente, bandeja, tg, _ = entorno
    publicar(cliente)
    publicar(cliente, id="b" * 16, mensaje_huesped="Also, is there parking?", reemplaza=["a1b2c3d4e5f6a7b8"])
    viejo = bandeja.almacen.leer("a1b2c3d4e5f6a7b8")
    assert viejo["estado"] == "sustituido"
    assert any("Sustituido" in x["texto"] for x in tg.editados)
    assert [b["id"] for b in bandeja.almacen.pendientes()] == ["b" * 16]


def test_fallo_de_smtp_deja_reintentar(entorno):
    cliente, bandeja, tg, smtp = entorno
    smtp.fallar = True
    publicar(cliente)
    pulsar(cliente, "ok", tg=tg)
    b = bandeja.almacen.leer("a1b2c3d4e5f6a7b8")
    assert b["estado"] == "error" and "SMTP caído" in b["error"]
    assert "🔁 Reintentar" in botones(tg.ultima())
    smtp.fallar = False
    pulsar(cliente, "ok", tg=tg)
    assert len(smtp.mensajes) == 1 and bandeja.almacen.leer("a1b2c3d4e5f6a7b8")["estado"] == "enviado"


def test_nunca_se_escribe_fuera_de_airbnb(entorno):
    cliente, bandeja, tg, smtp = entorno
    publicar(cliente, responder_a="alguien@gmail.com")
    pulsar(cliente, "ok", tg=tg)
    assert not smtp.mensajes and bandeja.almacen.leer("a1b2c3d4e5f6a7b8")["estado"] == "error"


def test_configurar_pone_el_webhook_con_secreto(entorno):
    cliente, bandeja, tg, _ = entorno
    r = cliente.post("/api/configurar", headers={"Authorization": f"Bearer {SECRETO}"}).get_json()
    assert r["ok"] and tg.webhook == ("https://localhost/api/telegram", secreto_webhook(SECRETO))


def test_start_dice_el_id_y_si_esta_autorizado(entorno):
    cliente, _, tg, _ = entorno
    for usuario in (YO, INTRUSO):
        cliente.post("/api/telegram", headers={"X-Telegram-Bot-Api-Secret-Token": secreto_webhook(SECRETO)}, json={"message": {
            "chat": {"id": usuario, "type": "private"}, "from": {"id": usuario}, "text": "/start"}})
    assert "✅ Estás autorizado" in tg.enviados[0]["texto"] and str(YO) in tg.enviados[0]["texto"]
    assert "⛔" in tg.enviados[1]["texto"]


def test_pendientes_reenvia_las_tarjetas(entorno):
    cliente, _, tg, _ = entorno
    publicar(cliente)
    cliente.post("/api/telegram", headers={"X-Telegram-Bot-Api-Secret-Token": secreto_webhook(SECRETO)}, json={"message": {
        "chat": {"id": YO, "type": "private"}, "from": {"id": YO}, "text": "/pendientes"}})
    assert len([x for x in tg.enviados if x["chat"] == YO]) == 2


def test_tarjeta_larga_cabe_en_un_mensaje_de_telegram():
    b = borrador(texto="<a> & " * 2000, mensaje_huesped="x" * 5000, notas=["n" * 500] * 10)
    b.update(estado="pendiente")
    assert len(tarjeta(b)) <= LIMITE


def test_init_data_caducado_o_sin_hash():
    assert usuario_mini_app("", TOKEN) is None
    assert usuario_mini_app("auth_date=1&user=%7B%7D", TOKEN) is None


def test_sin_borrador_no_se_puede_enviar_hasta_escribirlo(entorno):
    cliente, bandeja, tg, smtp = entorno
    publicar(cliente, texto="", notas=["⚠️ Gemini no responde: contesta a mano."])
    assert "✅ Enviar" not in botones(tg.enviados[0]) and "No hay borrador" in tg.enviados[0]["texto"]
    pulsar(cliente, "ok", tg=tg)
    assert not smtp.mensajes
    cliente.post("/api/b/a1b2c3d4e5f6a7b8/enviar", json={"texto": "Hola, ahora te digo."}, headers={"X-Init-Data": init_data()})
    assert smtp.mensajes[0].get_content().strip() == "Hola, ahora te digo."


# --------------------------------------------- cuenta de envío


def test_el_hilo_de_otra_cuenta_no_se_envia_desde_aqui_solo_se_copia(entorno):
    cliente, bandeja, tg, smtp = entorno
    publicar(cliente, cuenta="otra@yahoo.es")                  # el bot envía desde anfitrion@yahoo.fr
    b = bandeja.almacen.leer("a1b2c3d4e5f6a7b8")
    assert b["responder_a"] == "" and "otra cuenta" in b["notas"][-1]
    assert "✅ Enviar" not in botones(tg.enviados[0]) and "✅ Ya lo pegué" in botones(tg.enviados[0])
    pulsar(cliente, "ok", tg=tg)
    assert not smtp.mensajes


def test_la_misma_cuenta_se_envia_sin_distinguir_mayusculas(entorno):
    cliente, bandeja, tg, smtp = entorno
    publicar(cliente, cuenta="Anfitrion@Yahoo.fr")
    pulsar(cliente, "env", tg=tg)
    pulsar(cliente, "ok", tg=tg)
    assert len(smtp.mensajes) == 1 and smtp.mensajes[0]["To"] == "hilo123@reply.airbnb.com"


def test_sin_cuenta_de_correo_el_bot_sigue_sirviendo_para_copiar_y_pegar():
    tg = TelegramFalso()
    bandeja = Bandeja(Almacen(Memoria()), tg, CorreoYahoo("", ""), [YO], "https://bot.example.app")
    index.app.config.update(BANDEJA=bandeja, SECRETO=SECRETO, TOKEN=TOKEN, TESTING=True)
    cliente = index.app.test_client()
    publicar(cliente)
    assert bandeja.almacen.leer("a1b2c3d4e5f6a7b8")["responder_a"] == ""
    assert botones(tg.enviados[0])[:2] == ["✏️ Editar", "✅ Ya lo pegué"]
    r = cliente.post("/api/configurar", headers={"Authorization": f"Bearer {SECRETO}"}).get_json()
    assert r["ok"] and r["correo"] is False


def test_servidor_smtp_segun_la_cuenta():
    from _bandeja.correo import servidor_smtp
    assert servidor_smtp("x@yahoo.fr") == ("smtp.mail.yahoo.com", 465)
    assert servidor_smtp("x@empresa.com", "smtp.empresa.com:587") == ("smtp.empresa.com", 587)


def test_cuenta_desde_el_entorno(monkeypatch):
    from _bandeja.correo import CorreoSMTP
    monkeypatch.delenv("CORREO_USUARIO", raising=False)
    monkeypatch.setenv("YAHOO_USUARIO", "a@yahoo.fr")
    monkeypatch.setenv("YAHOO_CLAVE_APP", "abcd efgh")
    assert CorreoSMTP.desde_entorno().usuario == "a@yahoo.fr"
    monkeypatch.setenv("CORREO_USUARIO", "c@yahoo.es")
    monkeypatch.setenv("CORREO_CLAVE_APP", "wxyz wxyz")
    c = CorreoSMTP.desde_entorno()
    assert (c.usuario, c.clave) == ("c@yahoo.es", "wxyzwxyz")
