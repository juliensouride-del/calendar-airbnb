"""
La bandeja de aprobación: crear borradores, mostrarlos en Telegram, editarlos y enviarlos.

Garantías:
  - Nada se envía sin que un aprobador (APROBADORES) lo confirme: botón ✅ Enviar + ✅ Sí, o
    «Enviar» en la pantalla de edición (con su confirmación).
  - Un borrador se envía como mucho una vez (cerrojo + estado).
  - Solo se escribe a direcciones …@reply.airbnb.com (el hilo de Airbnb), y solo desde la cuenta que recibió esa
    dirección: si el bot no tiene esa cuenta (o ninguna), el borrador queda para copiar y pegar en Airbnb.
  - No se envía un texto con huecos [[COMPLETAR]].
  - Borradores de PRUEBAS: se simula el envío, nunca llega nada al huésped.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from .almacen import Almacen
from .correo import CorreoSMTP
from .telegram import ErrorTelegram, Telegram
from .vista import botones, se_puede_enviar, tarjeta, tiene_huecos

log = logging.getLogger(__name__)

RE_ID = re.compile(r"^[0-9a-f]{16}$")
CAMPOS = ("id", "tipo", "texto")
EDITABLE = ("pendiente", "error")


class Rechazo(Exception):
    """Acción no permitida: el mensaje se muestra tal cual al usuario."""


def ahora() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Bandeja:
    def __init__(self, almacen: Almacen, telegram: Telegram, correo: CorreoSMTP, aprobadores: list[int], url_base: str):
        self.almacen, self.tg, self.correo = almacen, telegram, correo
        self.aprobadores = aprobadores
        self.url_base = url_base

    def autorizado(self, usuario_id) -> bool:
        try:
            return int(usuario_id) in self.aprobadores
        except (TypeError, ValueError):
            return False

    # ------------------------------------------------------- desde el script

    def crear(self, datos: dict) -> bool:
        faltan = [c for c in CAMPOS if c not in datos]
        if faltan or not RE_ID.match(str(datos.get("id", ""))):
            raise Rechazo(f"Borrador no válido (faltan {faltan or 'id'})")
        b = {k: v for k, v in datos.items() if k != "reemplaza"}
        b.update(estado="pendiente", texto_original=datos.get("texto", ""), editado=False, creado=ahora(),
                 telegram=[], historial=[{"t": ahora(), "accion": "creado"}])
        if b.get("responder_a") and not b.get("pruebas"):
            motivo = ("el bot no tiene cuenta de correo" if not self.correo.configurado
                      else None if self.correo.es_la_cuenta(b.get("cuenta"))
                      else "el hilo es de otra cuenta de correo que la del bot")
            if motivo:
                b.update(responder_a="", notas=[*(b.get("notas") or []), f"📋 No se puede enviar desde aquí ({motivo}): "
                                                 "copia el texto y pégalo en Airbnb."])
        if not self.almacen.crear(b):
            return False
        for viejo in datos.get("reemplaza") or []:
            self._sustituir(viejo, b["id"])
        self._mostrar(b)
        return True

    def pendientes_de(self, codigo: str, estado: str | None = None) -> list[dict]:
        res = [b for b in self.almacen.de_reserva(codigo) if not estado or b["estado"] == estado]
        return [{k: b.get(k) for k in ("id", "tipo", "momento", "estado", "mensaje_huesped", "recibido")} for b in res]

    def _sustituir(self, id_: str, por: str) -> None:
        b = self.almacen.leer(id_)
        if b and b["estado"] in EDITABLE:
            b.update(estado="sustituido", sustituido_por=por)
            b["historial"].append({"t": ahora(), "accion": "sustituido", "por": por})
            self.almacen.guardar(b)
            self._refrescar(b)

    # ----------------------------------------------------------- Telegram

    def _mostrar(self, b: dict, solo_chat: int | None = None) -> None:
        """Manda la tarjeta a los aprobadores (o a uno) y apunta dónde está para poder actualizarla."""
        for chat in [solo_chat] if solo_chat else self.aprobadores:
            try:
                mid = self.tg.enviar(chat, tarjeta(b), botones(b, self.url_base))
            except ErrorTelegram as err:
                log.error("No se pudo mandar el borrador %s a %s: %s", b["id"], chat, err)
                continue
            b["telegram"].append([chat, mid])
            self.almacen.tarjeta(chat, mid, b["id"])
        self.almacen.guardar(b)

    def _refrescar(self, b: dict, confirmar_en: tuple[int, int] | None = None) -> None:
        for chat, mid in b.get("telegram", []):
            confirmar = confirmar_en == (chat, mid)
            try:
                self.tg.editar(chat, mid, tarjeta(b, confirmar), botones(b, self.url_base, confirmar))
            except ErrorTelegram as err:
                log.warning("No se pudo actualizar la tarjeta %s en %s: %s", b["id"], chat, err)

    def _leer(self, id_: str) -> dict:
        b = self.almacen.leer(id_) if RE_ID.match(id_ or "") else None
        if not b:
            raise Rechazo("Este borrador ya no existe.")
        return b

    def boton(self, usuario_id, accion: str, id_: str, chat: int, mid: int) -> str:
        """Pulsación de un botón de una tarjeta. Devuelve el texto breve que ve el usuario."""
        if not self.autorizado(usuario_id):
            raise Rechazo("No estás autorizado para aprobar mensajes.")
        b = self._leer(id_)
        if accion == "env":
            if b["estado"] not in EDITABLE:
                self._refrescar(b)
                raise Rechazo("Este borrador ya no está pendiente.")
            if not se_puede_enviar(b):
                raise Rechazo("Este borrador no se puede enviar desde aquí (sin hilo de Airbnb o con huecos [[COMPLETAR]]).")
            self._refrescar(b, confirmar_en=(chat, mid))
            return "¿Seguro? Confirma abajo."
        if accion == "vol":
            self._refrescar(b)
            return "Sin enviar."
        if accion == "ok":
            return self.enviar(usuario_id, id_)
        if accion == "des":
            return self._cerrar(b, "descartado", usuario_id, "Descartado.")
        if accion == "peg":
            return self._cerrar(b, "copiado", usuario_id, "Marcado como pegado en Airbnb.")
        raise Rechazo("Acción desconocida.")

    def _cerrar(self, b: dict, estado: str, usuario_id, aviso: str) -> str:
        if b["estado"] not in EDITABLE:
            self._refrescar(b)
            raise Rechazo("Este borrador ya no está pendiente.")
        b["estado"] = estado
        b["historial"].append({"t": ahora(), "accion": estado, "por": usuario_id})
        self.almacen.guardar(b)
        self._refrescar(b)
        return aviso

    def editar(self, usuario_id, id_: str, texto: str) -> dict:
        if not self.autorizado(usuario_id):
            raise Rechazo("No estás autorizado.")
        texto = (texto or "").strip()
        if not texto:
            raise Rechazo("El mensaje está vacío.")
        if len(texto) > 5000:
            raise Rechazo("El mensaje es demasiado largo (máx. 5000 caracteres).")
        b = self._leer(id_)
        if b["estado"] not in EDITABLE:
            raise Rechazo("Este borrador ya no está pendiente.")
        if texto != b["texto"]:
            b.update(texto=texto, editado=texto != b.get("texto_original"))
            b["historial"].append({"t": ahora(), "accion": "editado", "por": usuario_id})
            self.almacen.guardar(b)
            self._refrescar(b)
        return b

    def enviar(self, usuario_id, id_: str, texto: str | None = None) -> str:
        """Envía el borrador al hilo de Airbnb (o lo simula si es de PRUEBAS). Como mucho una vez."""
        if not self.autorizado(usuario_id):
            raise Rechazo("No estás autorizado.")
        if texto is not None:
            self.editar(usuario_id, id_, texto)
        if self._leer(id_)["estado"] not in EDITABLE:
            raise Rechazo("Este borrador ya no está pendiente.")
        if not self.almacen.bloquear(id_, 24 * 3600):   # si algo falla tras enviar, no se repite en 24 h
            raise Rechazo("Ya se está enviando.")
        try:
            b = self._leer(id_)            # lo último guardado, ya con el cerrojo
            if b["estado"] not in EDITABLE:
                raise Rechazo("Este borrador ya no está pendiente.")
            if not b.get("responder_a"):
                raise Rechazo("No hay hilo de Airbnb para este huésped: copia el texto y pégalo en Airbnb.")
            if tiene_huecos(b["texto"]):
                raise Rechazo("Quedan huecos [[COMPLETAR]] por rellenar.")
            if not (b.get("texto") or "").strip():
                raise Rechazo("El mensaje está vacío.")
            if b.get("pruebas"):
                b["estado"] = "simulado"
            elif not self.correo.es_la_cuenta(b.get("cuenta")):
                raise Rechazo("Este hilo es de otra cuenta de correo: copia el texto y pégalo en Airbnb.")
            else:
                try:
                    self.correo.enviar(b["responder_a"], b.get("asunto") or "", b["texto"])
                except Exception as err:   # noqa: BLE001 — se muestra en la tarjeta y se puede reintentar
                    b.update(estado="error", error=str(err)[:300])
                    b["historial"].append({"t": ahora(), "accion": "error", "por": usuario_id, "error": str(err)[:300]})
                    self.almacen.guardar(b)
                    self._refrescar(b)
                    self.almacen.desbloquear(id_)
                    raise Rechazo(f"No se pudo enviar: {err}") from None
                b["estado"] = "enviado"
            b.update(enviado=ahora(), enviado_por=usuario_id, error=None)
            b["historial"].append({"t": ahora(), "accion": b["estado"], "por": usuario_id})
            self.almacen.guardar(b)
        except Rechazo:
            self.almacen.desbloquear(id_)
            raise
        self._refrescar(b)
        return "🧪 Simulado (pruebas): no se ha enviado nada." if b["estado"] == "simulado" else "✅ Enviado al huésped."

    def para_mini_app(self, usuario_id, id_: str) -> dict:
        if not self.autorizado(usuario_id):
            raise Rechazo("No estás autorizado.")
        b = self._leer(id_)
        return {k: b.get(k) for k in ("id", "tipo", "momento", "piso", "huesped", "checkin", "checkout", "personas",
                                      "mensaje_huesped", "texto", "texto_original", "notas", "faltan", "idioma", "estado",
                                      "pruebas")} | {"se_envia": bool(b.get("responder_a"))}

    def respuesta_a_tarjeta(self, usuario_id, chat: int, mid_tarjeta: int, texto: str) -> str:
        """El usuario responde (deslizar → responder) a una tarjeta con el texto corregido."""
        id_ = self.almacen.de_tarjeta(chat, mid_tarjeta)
        if not id_:
            raise Rechazo("Responde a la tarjeta de un borrador para cambiar su texto.")
        self.editar(usuario_id, id_, texto)
        return "✏️ Texto cambiado. Revisa la tarjeta y toca ✅ Enviar cuando quieras."

    def reenviar_pendientes(self, usuario_id, chat: int) -> int:
        if not self.autorizado(usuario_id):
            raise Rechazo("No estás autorizado.")
        pend = self.almacen.pendientes()
        for b in pend:
            self._mostrar(b, solo_chat=chat)
        return len(pend)
