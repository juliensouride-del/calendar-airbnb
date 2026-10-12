"""
Avisos técnicos SOLO al desarrollador: notificación push con https://ntfy.sh (gratis, sin cuenta).

Secreto ALERTA_NTFY_TOPIC = nombre de tema largo y difícil de adivinar. Sin él, los
avisos técnicos solo quedan en el log (nunca se envían al cliente).
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class NotificadorNtfy:
    def __init__(self, tema: str = "", sesion=None):
        self.tema = tema
        self.sesion = sesion or requests

    def enviar(self, asunto: str, cuerpo: str) -> None:
        if not self.tema:
            log.warning("Aviso técnico (sin ALERTA_NTFY_TOPIC, no se envía): %s", asunto)
            return
        try:
            r = self.sesion.post(f"https://ntfy.sh/{self.tema}", data=cuerpo.encode(),
                                 params={"title": asunto, "tags": "warning"}, timeout=15)   # admite UTF-8
            r.raise_for_status()
            log.info("Aviso técnico enviado: %s", asunto)
        except Exception as e:                       # noqa: BLE001 — un aviso perdido no debe parar la sincronización
            log.warning("No se pudo enviar el aviso técnico (%s): %s", e, asunto)
