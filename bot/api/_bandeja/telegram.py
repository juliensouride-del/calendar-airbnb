"""Cliente mínimo de la Bot API de Telegram."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

import requests

log = logging.getLogger(__name__)


class ErrorTelegram(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, sesion: requests.Session | None = None):
        self.token = token
        self.sesion = sesion or requests.Session()

    def _api(self, metodo: str, **datos):
        r = self.sesion.post(f"https://api.telegram.org/bot{self.token}/{metodo}", json=datos, timeout=15)
        cuerpo = r.json() if r.content else {}
        if not cuerpo.get("ok"):
            desc = cuerpo.get("description", f"HTTP {r.status_code}")
            if "message is not modified" in desc:
                return None
            raise ErrorTelegram(f"{metodo}: {desc}")
        return cuerpo.get("result")

    def enviar(self, chat_id: int, texto: str, teclado: list | None = None, responder_a: int | None = None) -> int:
        datos = {"chat_id": chat_id, "text": texto, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if teclado is not None:
            datos["reply_markup"] = {"inline_keyboard": teclado}
        if responder_a:
            datos["reply_parameters"] = {"message_id": responder_a, "allow_sending_without_reply": True}
        return self._api("sendMessage", **datos)["message_id"]

    def editar(self, chat_id: int, message_id: int, texto: str, teclado: list | None) -> None:
        self._api("editMessageText", chat_id=chat_id, message_id=message_id, text=texto, parse_mode="HTML",
                  link_preview_options={"is_disabled": True}, reply_markup={"inline_keyboard": teclado or []})

    def responder_boton(self, callback_id: str, texto: str = "", alerta: bool = False) -> None:
        try:
            self._api("answerCallbackQuery", callback_query_id=callback_id, text=texto[:190], show_alert=alerta)
        except ErrorTelegram as e:          # caducado (más de unos segundos): no importa
            log.warning("answerCallbackQuery: %s", e)

    def configurar_webhook(self, url: str, secreto: str) -> None:
        self._api("setWebhook", url=url, secret_token=secreto, allowed_updates=["message", "callback_query"],
                  drop_pending_updates=False)
        self._api("setMyCommands", commands=[
            {"command": "pendientes", "description": "Ver los borradores sin decidir"},
            {"command": "start", "description": "Tu ID de Telegram y si estás autorizado"},
        ])

    def info_webhook(self) -> dict:
        return self._api("getWebhookInfo") or {}


def secreto_webhook(secreto_bandeja: str) -> str:
    """Token que Telegram envía en cada llamada al webhook (derivado del secreto de la bandeja)."""
    return hashlib.sha256(f"telegram-webhook|{secreto_bandeja}".encode()).hexdigest()[:48]


def usuario_mini_app(init_data: str, token: str, max_edad_s: int = 24 * 3600, ahora: float | None = None) -> dict | None:
    """
    Usuario de la Mini App si `initData` está firmado por Telegram para este bot y es reciente; si no, None.
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not token:
        return None
    pares = dict(parse_qsl(init_data, keep_blank_values=True))
    recibido = pares.pop("hash", "")
    comprobacion = "\n".join(f"{k}={v}" for k, v in sorted(pares.items()))
    clave = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calculado = hmac.new(clave, comprobacion.encode(), hashlib.sha256).hexdigest()
    if not recibido or not hmac.compare_digest(calculado, recibido):
        return None
    try:
        if (ahora or time.time()) - int(pares.get("auth_date", "0")) > max_edad_s:
            return None
        return json.loads(pares.get("user", "null"))
    except (ValueError, TypeError):
        return None
