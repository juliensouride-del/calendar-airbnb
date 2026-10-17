"""Cliente IMAP (contra un servidor IMAP falso en local), lectura MIME y remitentes."""
import imaplib
import socketserver
import threading
from datetime import datetime, timezone

import pytest

from airbnb_calendar.yahoo import BuzonYahoo, NotificadorYahoo, es_remitente, leer_eml, parsear_internaldate

from conftest import eml_crudo

CORREO_8BIT = ("Message-ID: <ana-8bit@airbnb.com>\r\nFrom: Airbnb <automated@airbnb.com>\r\nSubject: =?UTF-8?Q?Reserva_confirmada_=E2=80=93_Ana?=\r\n"
               "Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
               "Check-in: Corazón de Madrid\r\n").encode()
BUZON = {
    "INBOX": {"validez": 1700000000, "correos": [
        {"uid": 10, "fecha": "06-Oct-2026 23:59:00 +0000", "de": "airbnb.com", "datos": eml_crudo("confirmed.eml")},
        {"uid": 11, "fecha": "07-Oct-2026 10:31:07 +0200", "de": "airbnb.com", "datos": CORREO_8BIT},
        {"uid": 12, "fecha": "07-Oct-2026 11:00:00 +0000", "de": "otro.com", "datos": b"Subject: x\r\n\r\nx"},
    ]},
    "Bulk": {"validez": 5, "correos": []},
    # El correo 11 archivado (otro UID, mismo Message-ID) y uno que solo está en el archivo
    "Archive": {"validez": 7, "correos": [
        {"uid": 3, "fecha": "07-Oct-2026 10:31:07 +0200", "de": "airbnb.com", "datos": CORREO_8BIT},
        {"uid": 4, "fecha": "07-Oct-2026 12:00:00 +0000", "de": "airbnb.com",
         "datos": b"Message-ID: <archivado@airbnb.com>\r\nFrom: automated@airbnb.com\r\nSubject: Archivado\r\n\r\nx"},
    ]},
}


def cabecera_message_id(datos: bytes) -> bytes:
    cab = datos.split(b"\r\n\r\n", 1)[0]
    return b"".join(l + b"\r\n" for l in cab.split(b"\r\n") if l.lower().startswith(b"message-id:")) + b"\r\n"


class ServidorImapFalso(socketserver.StreamRequestHandler):
    comandos: list[str] = []

    def enviar(self, b):
        self.wfile.write(b if isinstance(b, bytes) else b.encode())

    def handle(self):
        self.enviar("* OK IMAP ready\r\n")
        carpeta = None
        for linea in self.rfile:
            tag, _, cmd = linea.decode().rstrip("\r\n").partition(" ")
            self.comandos.append(cmd)
            if cmd == "CAPABILITY":
                self.enviar(f"* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n{tag} OK done\r\n")
            elif cmd.startswith("LOGIN "):
                ok = cmd == 'LOGIN yo@yahoo.es "clave-app"'
                self.enviar(f"{tag} OK LOGIN completed\r\n" if ok else f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials\r\n")
            elif cmd.startswith("EXAMINE "):
                if cmd[8:].strip('"') not in BUZON:
                    self.enviar(f"{tag} NO [NONEXISTENT] Folder does not exist\r\n")
                    continue
                carpeta = BUZON[cmd[8:].strip('"')]
                self.enviar(f"* {len(carpeta['correos'])} EXISTS\r\n* OK [UIDVALIDITY {carpeta['validez']}] ok\r\n"
                            f"{tag} OK [READ-ONLY] EXAMINE completed\r\n")
            elif cmd.startswith("UID SEARCH "):
                uids = [str(c["uid"]) for c in carpeta["correos"] if c["de"] in cmd]
                self.enviar(f"* SEARCH {' '.join(uids)}\r\n{tag} OK SEARCH completed\r\n")
            elif cmd.startswith("UID FETCH "):
                pedidos = {int(u) for u in cmd.split()[2].split(",")}
                for n, c in enumerate(x for x in carpeta["correos"] if x["uid"] in pedidos):
                    if "BODY.PEEK[]" in cmd:   # INTERNALDATE DESPUÉS del literal, como hacen algunos servidores
                        self.enviar(f"* {n + 1} FETCH (UID {c['uid']} BODY[] {{{len(c['datos'])}}}\r\n")
                        self.enviar(c["datos"])
                        self.enviar(f' INTERNALDATE "{c["fecha"]}")\r\n')
                    elif "HEADER.FIELDS" in cmd:
                        cab = cabecera_message_id(c["datos"])
                        self.enviar(f'* {n + 1} FETCH (UID {c["uid"]} INTERNALDATE "{c["fecha"]}" '
                                    f'BODY[HEADER.FIELDS (MESSAGE-ID)] {{{len(cab)}}}\r\n')
                        self.enviar(cab + b")\r\n")
                    else:
                        self.enviar(f'* {n + 1} FETCH (INTERNALDATE "{c["fecha"]}" UID {c["uid"]})\r\n')
                self.enviar(f"* 3 FETCH (FLAGS (\\Seen))\r\n{tag} OK FETCH completed\r\n")
            elif cmd == "LOGOUT":
                self.enviar(f"* BYE\r\n{tag} OK LOGOUT completed\r\n")
                return
            else:
                self.enviar(f"{tag} BAD unknown\r\n")


@pytest.fixture
def puerto():
    ServidorImapFalso.comandos = []
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), ServidorImapFalso)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def buzon(puerto, clave="clave-app"):
    return BuzonYahoo("yo@yahoo.es", clave, conectar=lambda: imaplib.IMAP4("127.0.0.1", puerto))


def test_lee_correos_de_airbnb_en_solo_lectura_y_en_orden(puerto):
    with buzon(puerto) as b:
        correos = b.correos_desde(datetime(2026, 10, 6, 12, tzinfo=timezone.utc), ["INBOX"], ["airbnb.com"])
    assert [c.id for c in correos] == ["INBOX:1700000000:10", "INBOX:1700000000:11"]
    assert correos[0].asunto == "Reservation confirmed - Jennifer Carter arrives Oct 20"
    assert "Jennifer" in correos[0].texto
    assert correos[1].fecha == datetime(2026, 10, 7, 8, 31, 7, tzinfo=timezone.utc)
    assert correos[1].asunto == "Reserva confirmada – Ana"
    assert correos[1].texto.strip() == "Check-in: Corazón de Madrid"
    cmds = ServidorImapFalso.comandos
    assert 'EXAMINE "INBOX"' in cmds, "la carpeta se abre en solo lectura"
    assert any(c.startswith("UID FETCH") and "BODY.PEEK[]" in c for c in cmds), "BODY.PEEK no marca como leído"
    assert not any(c.startswith(("STORE", "UID STORE", "COPY", "MOVE", "EXPUNGE")) for c in cmds), "no modifica el buzón"
    assert any(c.startswith('UID SEARCH SINCE 6-Oct-2026 BEFORE 13-Oct-2026 FROM "airbnb.com"') for c in cmds)
    assert correos[1].id_mensaje == "mid:<ana-8bit@airbnb.com>"


def test_excluir_hora_exacta_varios_remitentes_y_carpetas(puerto):
    with buzon(puerto) as b:
        correos = b.correos_desde(datetime(2026, 10, 7, tzinfo=timezone.utc), ["INBOX", "Bulk"],
                                  ["airbnb.com", "otro.com", "x.com"], excluir={"INBOX:1700000000:12"},
                                  hasta=datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert [c.id for c in correos] == ["INBOX:1700000000:11"], "el 10 llegó el día 6 y el 12 está excluido"
    assert ('UID SEARCH SINCE 7-Oct-2026 BEFORE 8-Oct-2026 (OR OR FROM "airbnb.com" FROM "otro.com" FROM "x.com")'
            in ServidorImapFalso.comandos)
    assert 'EXAMINE "Bulk"' in ServidorImapFalso.comandos


def test_busqueda_por_tramos_semanales_para_no_chocar_con_el_limite_de_yahoo(puerto):
    with buzon(puerto) as b:
        b.correos_desde(datetime(2026, 9, 1, tzinfo=timezone.utc), ["INBOX"], ["airbnb.com"],
                        hasta=datetime(2026, 9, 20, tzinfo=timezone.utc))
    busquedas = [c for c in ServidorImapFalso.comandos if c.startswith("UID SEARCH")]
    assert busquedas == ['UID SEARCH SINCE 1-Sep-2026 BEFORE 8-Sep-2026 FROM "airbnb.com"',
                         'UID SEARCH SINCE 8-Sep-2026 BEFORE 15-Sep-2026 FROM "airbnb.com"',
                         'UID SEARCH SINCE 15-Sep-2026 BEFORE 20-Sep-2026 FROM "airbnb.com"']


def test_archivo_sin_duplicados_por_message_id_y_carpeta_inexistente_se_omite(puerto):
    hasta = datetime(2026, 10, 8, tzinfo=timezone.utc)
    with buzon(puerto) as b:
        correos = b.correos_desde(datetime(2026, 10, 7, tzinfo=timezone.utc), ["INBOX", "NoExiste", "Archive"],
                                  ["airbnb.com"], hasta=hasta)
    assert [c.id for c in correos] == ["INBOX:1700000000:11", "Archive:7:4"], "el 3 es el 11 archivado"
    # Ya procesado en una ejecución anterior (registro con su Message-ID) y archivado después: no se repite
    with buzon(puerto) as b:
        correos = b.correos_desde(datetime(2026, 10, 7, tzinfo=timezone.utc), ["Archive"], ["airbnb.com"],
                                  excluir={"mid:<ana-8bit@airbnb.com>"}, hasta=hasta)
    assert [c.id for c in correos] == ["Archive:7:4"]
    with pytest.raises(RuntimeError, match="ninguna carpeta"):
        with buzon(puerto) as b:
            b.correos_desde(datetime(2026, 10, 7, tzinfo=timezone.utc), ["NoExiste"], ["airbnb.com"], hasta=hasta)


def test_credenciales_incorrectas_error_claro(puerto):
    with pytest.raises(RuntimeError, match="contraseña de aplicación"):
        with buzon(puerto, clave="mala"):
            pass


def test_mime_de_los_eml_reales_de_yahoo():
    remitente, asunto, texto = leer_eml(eml_crudo("cunasolicitud.eml"))
    assert es_remitente(remitente, ["airbnb.com"])
    assert asunto == "RE: Reservation for Apartamentos de Julian, Anton Martin - Apartame..., Sep 16 – 19"
    assert "SEP 16 – 19" in texto        # quoted-printable con espacios finos (U+2009)


def test_solo_html_y_charset_latin1():
    html = "<p>Hola <b>Ana</b> &amp; Luis — ¿todo bien?</p>".encode()
    crudo = (b"From: automated@airbnb.com\r\nSubject: Hola\r\nContent-Type: text/html; charset=utf-8\r\n"
             b"Content-Transfer-Encoding: 8bit\r\n\r\n" + html)
    assert leer_eml(crudo)[2].strip() == "Hola  Ana  & Luis — ¿todo bien?"
    crudo = (b"From: express@airbnb.com\r\nContent-Type: text/plain; charset=ISO-8859-1\r\n"
             b"Content-Transfer-Encoding: quoted-printable\r\n\r\nLlegar=E9 a las 18:00, =BFhay cuna?\r\n")
    assert leer_eml(crudo)[2].strip() == "Llegaré a las 18:00, ¿hay cuna?"


def test_remitentes_y_fechas():
    assert es_remitente("Airbnb <automated@airbnb.com>", ["airbnb.com"])
    assert es_remitente("bounces@em5923.automated.airbnb.com", ["airbnb.com"])
    assert not es_remitente("Fake <airbnb.com@gmail.com>", ["airbnb.com"])
    assert not es_remitente("x@notairbnb.com", ["airbnb.com"])
    assert es_remitente("Airbnb <automated@airbnb.com>", ["automated@airbnb.com"])
    assert parsear_internaldate(" 7-Oct-2026 10:31:07 -0130") == datetime(2026, 10, 7, 12, 1, 7, tzinfo=timezone.utc)


def test_aviso_por_smtp_de_yahoo():
    enviados = []

    class SmtpFalso:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, c): enviados.append(("login", u, c))
        def send_message(self, m): enviados.append(m)

    NotificadorYahoo("julien@yahoo.fr", "clave", conectar=SmtpFalso).enviar("🔔 Airbnb · cuna", "Texto")
    assert enviados[0] == ("login", "julien@yahoo.fr", "clave")
    msg = enviados[1]
    assert msg["To"] == "julien@yahoo.fr" and msg["Subject"] == "🔔 Airbnb · cuna"
    assert "julien@yahoo.fr" in msg["From"]
