"""
Cliente de la bandeja de aprobación (el bot de Telegram en Vercel, carpeta bot/).

El script solo DEJA borradores en la bandeja. Enviar al huésped lo hace el bot y solo
cuando el anfitrión lo aprueba en Telegram.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class ErrorBandeja(RuntimeError):
    pass


class Bandeja:
    def __init__(self, url: str, secreto: str, sesion: requests.Session | None = None):
        self.url = url.rstrip("/")
        self.secreto = secreto
        self.sesion = sesion or requests.Session()

    def _pedir(self, metodo: str, ruta: str, **kw) -> dict:
        try:
            r = self.sesion.request(metodo, self.url + ruta, headers={"Authorization": f"Bearer {self.secreto}"}, timeout=30, **kw)
        except requests.RequestException as e:
            raise ErrorBandeja(f"No se pudo conectar con la bandeja ({e})") from None
        if r.status_code >= 300:
            raise ErrorBandeja(f"Bandeja: HTTP {r.status_code} en {ruta}: {r.text[:300]}")
        return r.json() if r.content else {}

    def publicar(self, borrador: dict) -> bool:
        """Deja el borrador en la bandeja y lo manda a Telegram. False si ya estaba (mismo id)."""
        creado = bool(self._pedir("POST", "/api/borradores", json=borrador).get("creado"))
        log.info("  Borrador %s %s", borrador["id"], "en Telegram" if creado else "ya estaba en la bandeja")
        return creado

    def pendientes(self, codigo: str) -> list[dict]:
        """Respuestas aún sin enviar ni descartar de esa reserva (para no contestar dos veces lo mismo)."""
        return self._pedir("GET", "/api/borradores", params={"codigo": codigo, "estado": "pendiente"}).get("borradores", [])

    def comprobar(self) -> dict:
        """Comprueba la bandeja y configura el webhook de Telegram (idempotente)."""
        return self._pedir("POST", "/api/configurar")


class BandejaDesactivada:
    """Sin BANDEJA_URL / BANDEJA_SECRETO: no se preparan borradores."""

    url = ""

    def publicar(self, borrador: dict) -> bool:
        log.info("  (bandeja no configurada: borrador %s descartado)", borrador.get("id"))
        return False

    def pendientes(self, codigo: str) -> list[dict]:
        return []

    def comprobar(self) -> dict:
        return {"ok": False, "motivo": "BANDEJA_URL / BANDEJA_SECRETO sin configurar"}
