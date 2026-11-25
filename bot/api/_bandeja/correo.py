"""
Envío de la respuesta al hilo de Airbnb: un correo a la dirección Reply-To del aviso de Airbnb
(…@reply.airbnb.com), desde la MISMA cuenta que recibió ese aviso (el Yahoo del anfitrión). Airbnb lo
publica en la conversación.

Cuenta (Vercel → Environment Variables), opcional: sin ella, cada borrador se copia y se pega en Airbnb.
  YAHOO_USUARIO / YAHOO_CLAVE_APP     la cuenta del anfitrión y su contraseña de aplicación (las de GitHub).
  CORREO_USUARIO / CORREO_CLAVE_APP   lo mismo con otro nombre (tienen prioridad si están).
  CORREO_SMTP                         (opcional) servidor:puerto si la cuenta no es de Yahoo.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

DOMINIO_RESPUESTA = "@reply.airbnb.com"
YAHOO = ("smtp.mail.yahoo.com", 465)


def servidor_smtp(usuario: str, explicito: str = "") -> tuple[str, int]:
    if explicito:
        host, _, puerto = explicito.partition(":")
        return host, int(puerto or 465)
    return YAHOO


class CorreoSMTP:
    def __init__(self, usuario: str, clave: str, conectar=None, servidor: str = ""):
        self.usuario, self.clave = (usuario or "").strip(), (clave or "").replace(" ", "")
        host, puerto = servidor_smtp(self.usuario, servidor)
        self._conectar = conectar or (lambda: smtplib.SMTP_SSL(host, puerto, timeout=20))

    @classmethod
    def desde_entorno(cls) -> CorreoSMTP:
        if os.environ.get("CORREO_USUARIO"):
            return cls(os.environ["CORREO_USUARIO"], os.environ.get("CORREO_CLAVE_APP", ""),
                       servidor=os.environ.get("CORREO_SMTP", ""))
        return cls(os.environ.get("YAHOO_USUARIO", ""), os.environ.get("YAHOO_CLAVE_APP", ""))

    @property
    def configurado(self) -> bool:
        return bool(self.usuario and self.clave)

    def es_la_cuenta(self, cuenta: str | None) -> bool:
        """¿Este hilo se puede contestar desde aquí? (sin cuenta indicada: borradores antiguos, sí)."""
        return not cuenta or cuenta.strip().lower() == self.usuario.lower()

    def enviar(self, para: str, asunto: str, texto: str) -> None:
        if not para.lower().endswith(DOMINIO_RESPUESTA):
            # Nunca se escribe a otra dirección: solo al hilo de Airbnb
            raise ValueError(f"Dirección no permitida: {para}")
        if not self.configurado:
            raise RuntimeError("Falta la cuenta de correo en Vercel (CORREO_USUARIO / CORREO_CLAVE_APP)")
        msg = EmailMessage()
        msg["From"] = self.usuario
        msg["To"] = para
        asunto = (asunto or "Airbnb").strip()
        msg["Subject"] = asunto if asunto.lower().startswith("re:") else f"RE: {asunto}"
        msg.set_content(texto)
        with self._conectar() as smtp:
            smtp.login(self.usuario, self.clave)
            smtp.send_message(msg)


CorreoYahoo = CorreoSMTP        # nombre anterior
