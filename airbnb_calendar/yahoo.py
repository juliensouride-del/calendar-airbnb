"""
Yahoo Mail: lectura por IMAP (solo lectura) y envío de avisos por SMTP.

El buzón NO se modifica: la carpeta se abre en modo solo lectura (EXAMINE) y los
correos se descargan con BODY.PEEK[], así que nada se marca como leído. Qué
correos se han procesado ya lo guarda el registro (estado.json).
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from .parser import html_a_texto

log = logging.getLogger(__name__)

IMAP_HOST, IMAP_PUERTO = "imap.mail.yahoo.com", 993
SMTP_HOST, SMTP_PUERTO = "smtp.mail.yahoo.com", 465
MESES_IMAP = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class Correo:
    id: str                 # "carpeta:UIDVALIDITY:UID" — único y estable en IMAP
    fecha: datetime         # llegada al buzón (INTERNALDATE), con zona horaria
    remitente: str
    asunto: str
    texto: str


class BuzonYahoo:
    """Uso:  with BuzonYahoo(usuario, clave) as b:  for c in b.correos_desde(...): ..."""

    def __init__(self, usuario: str, clave: str, conectar=None):
        self.usuario, self.clave = usuario, clave
        self._conectar = conectar or (lambda: imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PUERTO, timeout=60))
        self.imap: imaplib.IMAP4 | None = None

    def __enter__(self):
        self.imap = self._conectar()
        try:
            self.imap.login(self.usuario, self.clave)
        except imaplib.IMAP4.error as e:
            raise RuntimeError(f"Yahoo rechazó el inicio de sesión IMAP ({e}). "
                               "Revisa YAHOO_USUARIO y la contraseña de aplicación YAHOO_CLAVE_APP.") from None
        return self

    def __exit__(self, *exc):
        try:
            self.imap.logout()
        except Exception:                               # noqa: BLE001 — la conexión puede estar ya cerrada
            pass

    def correos_desde(self, desde: datetime, carpetas: list[str], remitentes: list[str],
                      excluir: set[str] = frozenset()) -> list[Correo]:
        """Correos de los remitentes llegados desde `desde`, sin los de `excluir`, del más antiguo al más nuevo."""
        res = []
        for carpeta in carpetas:
            typ, _ = self.imap.select(_citar(carpeta), readonly=True)
            if typ != "OK":
                raise RuntimeError(f'No se puede abrir la carpeta "{carpeta}" de Yahoo')
            validez = (self.imap.response("UIDVALIDITY")[1] or [b"0"])[0].decode()
            uids = self._buscar(desde, remitentes)
            ids = {u: f"{carpeta}:{validez}:{u}" for u in uids}
            uids = [u for u in uids if ids[u] not in excluir]
            if not uids:
                continue
            # SINCE solo compara el día: se afina con la hora exacta de llegada
            fechas = {u: f for u, f, _ in self._fetch(uids, "(UID INTERNALDATE)")}
            nuevos = [u for u in uids if fechas.get(u) and fechas[u] >= desde]
            for i in range(0, len(nuevos), 20):
                for uid, fecha, crudo in self._fetch(nuevos[i:i + 20], "(UID INTERNALDATE BODY.PEEK[])"):
                    remitente, asunto, texto = leer_eml(crudo)
                    res.append(Correo(ids[uid], fecha, remitente, asunto, texto))
        return sorted(res, key=lambda c: c.fecha)

    def _buscar(self, desde: datetime, remitentes: list[str]) -> list[str]:
        criterio = ["SINCE", f"{desde.day}-{MESES_IMAP[desde.month - 1]}-{desde.year}"]
        if remitentes:
            # OR a b  /  OR OR a b c ...
            de = ["FROM " + _citar(r) for r in remitentes]
            criterio.append(("(" + "OR " * (len(de) - 1) + " ".join(de) + ")") if len(de) > 1 else de[0])
        typ, datos = self.imap.uid("SEARCH", None, *criterio)
        if typ != "OK":
            raise RuntimeError(f"Búsqueda IMAP fallida: {datos}")
        return sorted({u.decode() for linea in datos if linea for u in linea.split()}, key=int)

    def _fetch(self, uids: list[str], partes: str):
        """[(uid, fecha, bytes del correo | None)]"""
        typ, datos = self.imap.uid("FETCH", ",".join(uids), partes)
        if typ != "OK":
            raise RuntimeError(f"Descarga IMAP fallida: {datos}")
        # imaplib entrega cada respuesta como (cabecera, literal) o bytes sueltos; lo que el
        # servidor envíe tras el literal (p. ej. 'INTERNALDATE "..." )') llega como bytes aparte.
        respuestas = []
        for item in datos:
            if isinstance(item, tuple):
                respuestas.append([item[0], item[1]])
            elif isinstance(item, bytes) and (not respuestas or re.match(rb"\d+ \(", item)):
                respuestas.append([item, None])
            elif isinstance(item, bytes):
                respuestas[-1][0] += b" " + item
        res = []
        for cab, cuerpo in respuestas:
            m_uid = re.search(rb"\bUID (\d+)", cab)
            m_fecha = re.search(rb'INTERNALDATE "([^"]+)"', cab)
            if m_uid and m_fecha:
                res.append((m_uid.group(1).decode(), parsear_internaldate(m_fecha.group(1).decode()), cuerpo))
        return res


def leer_eml(crudo: bytes) -> tuple[str, str, str]:
    """Bytes de un correo → (remitente, asunto, texto). Usa la parte text/plain; si no hay, la HTML."""
    msg = email.message_from_bytes(crudo, policy=policy.default)
    texto = ""
    for tipo in ("plain", "html"):
        parte = msg.get_body(preferencelist=(tipo,))
        if parte is None:
            continue
        try:
            contenido = parte.get_content()
        except (LookupError, UnicodeError):           # charset desconocido o mal declarado
            contenido = (parte.get_payload(decode=True) or b"").decode("utf-8", "replace")
        texto = contenido if tipo == "plain" else html_a_texto(contenido)
        if texto.strip():
            break
    return str(msg.get("From", "")), str(msg.get("Subject", "")), texto


def parsear_internaldate(s: str) -> datetime:
    """'08-Oct-2026 10:31:07 +0200' → datetime con zona (sin depender del idioma del sistema)."""
    m = re.fullmatch(r"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})", s)
    if not m:
        raise ValueError(f"INTERNALDATE no válido: {s}")
    signo = -1 if m.group(7) == "-" else 1
    tz = timezone(signo * timedelta(hours=int(m.group(8)), minutes=int(m.group(9))))
    mes = MESES_IMAP.index(m.group(2).capitalize()) + 1
    return datetime(int(m.group(3)), mes, int(m.group(1)), int(m.group(4)), int(m.group(5)), int(m.group(6)), tzinfo=tz)


def es_remitente(remitente: str, permitidos: list[str]) -> bool:
    """¿La dirección del From es (o pertenece al dominio de) alguno de los permitidos?"""
    direccion = parseaddr(remitente)[1].lower()
    dominio = direccion.rpartition("@")[2]
    for p in (x.lower() for x in permitidos):
        if direccion == p or dominio == p or dominio.endswith("." + p):
            return True
    return False


def _citar(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class NotificadorYahoo:
    """Avisos por el SMTP de Yahoo, con la misma contraseña de aplicación."""

    def __init__(self, usuario: str, clave: str, destino: str = "", conectar=None):
        self.usuario, self.clave = usuario, clave
        self.destino = destino or usuario
        self._conectar = conectar or (lambda: smtplib.SMTP_SSL(SMTP_HOST, SMTP_PUERTO, timeout=60))

    def enviar(self, asunto: str, cuerpo: str) -> None:
        msg = EmailMessage()
        msg["From"] = formataddr(("Airbnb → Calendar", self.usuario))
        msg["To"] = self.destino
        msg["Subject"] = asunto
        msg.set_content(cuerpo)
        with self._conectar() as smtp:
            smtp.login(self.usuario, self.clave)
            smtp.send_message(msg)
        log.info("Aviso enviado: %s", asunto)
