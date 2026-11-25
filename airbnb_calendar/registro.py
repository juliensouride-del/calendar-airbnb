"""
Registro persistente entre ejecuciones (estado.json).

En GitHub Actions el archivo vive en la rama `estado` del repositorio: el workflow
lo descarga antes de ejecutar y sube los cambios al terminar.

El repositorio es PÚBLICO: en claro solo van identificadores y fechas. Lo que lleva datos
de huéspedes (los borradores que esperan a que la bandeja responda) va cifrado en
"privado" con una clave derivada de BANDEJA_SECRETO. Sin esa clave no se guarda.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)


def _fernet(secreto: str):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(f"estado|{secreto}".encode()).digest()))


class Registro:
    def __init__(self, ruta: str | Path, secreto: str = ""):
        self.ruta = Path(ruta)
        self._cifra = _fernet(secreto) if secreto else None
        d = json.loads(self.ruta.read_text("utf-8")) if self.ruta.exists() else {}
        self.inicio: str | None = d.get("inicio")               # ISO; primera ejecución
        self.procesados: dict[str, str] = d.get("procesados", {})   # id → fecha ISO del correo
        self.fallos: dict[str, int] = d.get("fallos", {})           # id → nº de intentos fallidos
        self.fecha_fallos: dict[str, str] = d.get("fecha_fallos", {})   # id → fecha ISO del correo que falla
        self.ultimo_aviso: dict[str, str] = d.get("ultimo_aviso", {})   # tipo → fecha ISO
        self.ultima_ok: str | None = d.get("ultima_ok")         # ISO; última ejecución normal que terminó bien
        self.borradores: dict[str, str] = d.get("borradores", {})       # id del borrador → fecha ISO (ya en la bandeja)
        privado = self._descifrar(d.get("privado"))
        self.por_publicar: list[dict] = privado.get("por_publicar", [])     # borradores que la bandeja no aceptó: se reintentan

    def _descifrar(self, token: str | None) -> dict:
        if not token:
            return {}
        if not self._cifra:
            log.warning("Registro: hay datos cifrados pero falta BANDEJA_SECRETO: se ignoran")
            return {}
        try:
            return json.loads(self._cifra.decrypt(token.encode()))
        except Exception:                                # noqa: BLE001 — clave cambiada: se empieza de cero
            log.warning("Registro: no se pueden descifrar los datos privados (¿cambió BANDEJA_SECRETO?): se ignoran")
            return {}

    def fecha_inicio(self, procesar_desde: str, ahora: datetime) -> datetime:
        """Desde cuándo se procesan correos: config.procesar_desde o la primera ejecución."""
        if procesar_desde:
            return datetime.fromisoformat(procesar_desde).replace(tzinfo=UTC)
        if not self.inicio:
            self.inicio = ahora.isoformat()
        return datetime.fromisoformat(self.inicio)

    def marcar_procesado(self, id_: str, fecha: datetime) -> None:
        self.procesados[id_] = fecha.isoformat()
        self.fallos.pop(id_, None)
        self.fecha_fallos.pop(id_, None)

    def sumar_fallo(self, id_: str, fecha: datetime | None = None) -> int:
        self.fallos[id_] = self.fallos.get(id_, 0) + 1
        if fecha:
            self.fecha_fallos[id_] = fecha.isoformat()
        return self.fallos[id_]

    def puede_avisar(self, tipo: str, ahora: datetime, cada=timedelta(days=1)) -> bool:
        """Limita avisos repetitivos (p. ej. Gemini caído) a uno por periodo."""
        ultimo = self.ultimo_aviso.get(tipo)
        if ultimo and ahora - datetime.fromisoformat(ultimo) < cada:
            return False
        self.ultimo_aviso[tipo] = ahora.isoformat()
        return True

    def borrador_publicado(self, id_: str, ahora: datetime) -> None:
        self.borradores[id_] = ahora.isoformat()

    def limpiar(self, antes_de: datetime) -> None:
        """Olvida los correos que ya quedan fuera de la ventana de búsqueda."""
        self.borradores = {k: v for k, v in self.borradores.items() if datetime.fromisoformat(v) >= antes_de - timedelta(days=60)}
        self.procesados = {k: v for k, v in self.procesados.items() if datetime.fromisoformat(v) >= antes_de}
        # Fallos de correos que ya no se van a volver a leer (fuera de la ventana, o sin fecha: versión anterior)
        self.fecha_fallos = {k: v for k, v in self.fecha_fallos.items()
                             if k in self.fallos and datetime.fromisoformat(v) >= antes_de}
        self.fallos = {k: v for k, v in self.fallos.items() if k in self.fecha_fallos}

    def guardar(self) -> None:
        datos = {"inicio": self.inicio, "procesados": dict(sorted(self.procesados.items(), key=lambda x: x[1])),
                 "fallos": self.fallos, "fecha_fallos": self.fecha_fallos, "ultimo_aviso": self.ultimo_aviso,
                 "ultima_ok": self.ultima_ok, "borradores": self.borradores}
        privado = {"por_publicar": self.por_publicar} if self.por_publicar else {}
        if privado and self._cifra:
            datos["privado"] = self._cifra.encrypt(json.dumps(privado, ensure_ascii=False).encode()).decode()
        elif privado:
            log.warning("Registro: sin BANDEJA_SECRETO no se guardan los borradores pendientes")
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        # Escritura atómica: nunca queda un estado.json a medias
        fd, tmp = tempfile.mkstemp(dir=self.ruta.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, self.ruta)
