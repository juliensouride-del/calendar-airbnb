"""
Bot de Telegram para aprobar las respuestas a los huéspedes (Vercel, Python).

Rutas:
  POST /api/borradores        el script deja un borrador (Bearer BANDEJA_SECRETO)
  GET  /api/borradores        borradores de una reserva (?codigo=&estado=) (Bearer)
  POST /api/configurar        configura el webhook de Telegram y comprueba todo (Bearer)
  POST /api/telegram          webhook de Telegram (botones y mensajes)
  GET  /api/b/<id>            borrador para la pantalla de edición (initData de Telegram)
  POST /api/b/<id>/guardar    guarda el texto editado
  POST /api/b/<id>/enviar     guarda y envía (con confirmación en la pantalla)
  GET  /editar                pantalla de edición (Mini App)
  GET  /api/salud             comprobación sin datos

Variables de entorno (Vercel → Settings → Environment Variables):
  TELEGRAM_TOKEN, APROBADORES (IDs de Telegram separados por comas), BANDEJA_SECRETO,
  la base de datos de Upstash (KV_REST_API_URL, KV_REST_API_TOKEN) y, opcionales, para enviar desde aquí,
  YAHOO_USUARIO / YAHOO_CLAVE_APP (la cuenta del anfitrión). Sin ellas el bot funciona igual, pero cada
  borrador se copia y se pega en Airbnb.
"""
from __future__ import annotations

import hmac
import logging
import os
import re
import sys

from flask import Flask, Response, jsonify, request

sys.path.insert(0, os.path.dirname(__file__))

from _bandeja.almacen import Almacen, ErrorAlmacen, Upstash  # noqa: E402
from _bandeja.correo import CorreoSMTP  # noqa: E402
from _bandeja.nucleo import Bandeja, Rechazo  # noqa: E402
from _bandeja.pagina import HTML  # noqa: E402
from _bandeja.telegram import Telegram, secreto_webhook, usuario_mini_app  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

app = Flask(__name__)


def _aprobadores() -> list[int]:
    return [int(x) for x in re.findall(r"\d+", os.environ.get("APROBADORES", ""))]


def _url_base() -> str:
    return os.environ.get("URL_BASE") or f"https://{request.host}"


def crear_bandeja() -> Bandeja:
    """Se puede sustituir en las pruebas (app.config['BANDEJA'])."""
    if app.config.get("BANDEJA"):
        return app.config["BANDEJA"]
    return Bandeja(Almacen(Upstash.desde_entorno()), Telegram(os.environ.get("TELEGRAM_TOKEN", "")),
                   CorreoSMTP.desde_entorno(),
                   _aprobadores(), _url_base())


def _secreto() -> str:
    return app.config.get("SECRETO") or os.environ.get("BANDEJA_SECRETO", "")


def _token() -> str:
    return app.config.get("TOKEN") or os.environ.get("TELEGRAM_TOKEN", "")


def _con_secreto() -> bool:
    s = _secreto()
    recibido = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return bool(s) and len(s) >= 24 and hmac.compare_digest(recibido, s)


@app.errorhandler(Rechazo)
def _rechazo(e):
    return jsonify(error=str(e)), 409


@app.errorhandler(ErrorAlmacen)
def _almacen(e):
    log.error("Almacén: %s", e)
    return jsonify(error=str(e)), 503


# ------------------------------------------------------------------ script

@app.post("/api/borradores")
def crear_borrador():
    if not _con_secreto():
        return jsonify(error="no autorizado"), 401
    creado = crear_bandeja().crear(request.get_json(force=True))
    return jsonify(creado=creado), 201 if creado else 200


@app.get("/api/borradores")
def listar_borradores():
    if not _con_secreto():
        return jsonify(error="no autorizado"), 401
    codigo = request.args.get("codigo", "")
    if not re.fullmatch(r"[A-Z0-9]{6,14}", codigo):
        return jsonify(error="código de reserva no válido"), 400
    return jsonify(borradores=crear_bandeja().pendientes_de(codigo, request.args.get("estado") or None))


@app.post("/api/configurar")
def configurar():
    if not _con_secreto():
        return jsonify(error="no autorizado"), 401
    bandeja = crear_bandeja()
    res = {"aprobadores": len(bandeja.aprobadores), "correo": bandeja.correo.configurado}
    res["almacen"] = bandeja.almacen.ping()
    bandeja.tg.configurar_webhook(f"{_url_base()}/api/telegram", secreto_webhook(_secreto()))
    info = bandeja.tg.info_webhook()
    res["webhook"] = info.get("url")
    res["webhook_error"] = info.get("last_error_message")
    # Sin cuenta de correo también vale: los borradores se copian y se pegan en Airbnb
    res["ok"] = bool(res["almacen"] and res["aprobadores"] and res["webhook"])
    return jsonify(res)


@app.get("/api/salud")
def salud():
    return jsonify(ok=True)


# ---------------------------------------------------------------- Telegram

@app.post("/api/telegram")
def webhook():
    if len(_secreto()) < 24:        # sin secreto, el token del webhook sería predecible: no se acepta nada
        return jsonify(error="BANDEJA_SECRETO sin configurar"), 503
    if not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), secreto_webhook(_secreto())):
        return jsonify(error="no autorizado"), 401
    u = request.get_json(force=True) or {}
    bandeja = crear_bandeja()
    try:
        if "callback_query" in u:
            _boton(bandeja, u["callback_query"])
        elif "message" in u:
            _mensaje(bandeja, u["message"])
    except Exception:                                    # noqa: BLE001 — Telegram reintentaría sin fin
        log.exception("Error procesando la actualización de Telegram")
    return jsonify(ok=True)


def _boton(bandeja: Bandeja, cb: dict) -> None:
    partes = (cb.get("data") or "").split(":")
    msg = cb.get("message") or {}
    if len(partes) != 3 or partes[0] != "a":
        bandeja.tg.responder_boton(cb["id"])
        return
    try:
        aviso = bandeja.boton(cb["from"]["id"], partes[1], partes[2], msg.get("chat", {}).get("id"), msg.get("message_id"))
        bandeja.tg.responder_boton(cb["id"], aviso)
    except Rechazo as e:
        bandeja.tg.responder_boton(cb["id"], str(e), alerta=True)


def _mensaje(bandeja: Bandeja, m: dict) -> None:
    chat, usuario, texto = m["chat"]["id"], m.get("from", {}).get("id"), (m.get("text") or "").strip()
    if m["chat"].get("type") != "private":
        return
    try:
        if texto.startswith("/start"):
            estado = ("✅ Estás autorizado: aquí te llegarán los borradores." if bandeja.autorizado(usuario)
                      else "⛔ No estás autorizado. Añade este número a APROBADORES en Vercel.")
            bandeja.tg.enviar(chat, f"Hola 👋 Tu ID de Telegram es <code>{usuario}</code>.\n{estado}")
        elif texto.startswith("/pendientes"):
            n = bandeja.reenviar_pendientes(usuario, chat)
            if not n:
                bandeja.tg.enviar(chat, "No hay borradores pendientes 🎉")
        elif m.get("reply_to_message") and texto:
            aviso = bandeja.respuesta_a_tarjeta(usuario, chat, m["reply_to_message"]["message_id"], texto)
            bandeja.tg.enviar(chat, aviso, responder_a=m["reply_to_message"]["message_id"])
        elif bandeja.autorizado(usuario):
            bandeja.tg.enviar(chat, "Para cambiar un borrador, toca ✏️ Editar en su tarjeta (o respóndele a la tarjeta con el texto nuevo).\n"
                                    "/pendientes muestra los que faltan por decidir.")
    except Rechazo as e:
        bandeja.tg.enviar(chat, f"⚠️ {e}")


# ----------------------------------------------------------------- Mini App

def _usuario() -> int | None:
    u = usuario_mini_app(request.headers.get("X-Init-Data", ""), _token())
    return (u or {}).get("id")


@app.get("/editar")
def pagina():
    return Response(HTML, mimetype="text/html", headers={"Cache-Control": "no-store"})


@app.get("/api/b/<id_>")
def leer(id_: str):
    usuario = _usuario()
    if not usuario:
        return jsonify(error="Abre esta pantalla desde Telegram."), 401
    return jsonify(crear_bandeja().para_mini_app(usuario, id_))


@app.post("/api/b/<id_>/guardar")
def guardar(id_: str):
    usuario = _usuario()
    if not usuario:
        return jsonify(error="Abre esta pantalla desde Telegram."), 401
    crear_bandeja().editar(usuario, id_, (request.get_json(force=True) or {}).get("texto", ""))
    return jsonify(ok=True, mensaje="Guardado")


@app.post("/api/b/<id_>/enviar")
def enviar(id_: str):
    usuario = _usuario()
    if not usuario:
        return jsonify(error="Abre esta pantalla desde Telegram."), 401
    aviso = crear_bandeja().enviar(usuario, id_, (request.get_json(force=True) or {}).get("texto"))
    return jsonify(ok=True, mensaje=aviso)
