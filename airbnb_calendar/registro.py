"""
Registro persistente entre ejecuciones (estado.json).

En GitHub Actions el archivo vive en la rama `estado` del repositorio: el workflow
lo descarga antes de ejecutar y sube los cambios al terminar.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


class Registro:
    def __init__(self, ruta: str | Path):
        self.ruta = Path(ruta)
        d = json.loads(self.ruta.read_text("utf-8")) if self.ruta.exists() else {}
        self.inicio: str | None = d.get("inicio")               # ISO; primera ejecución
        self.procesados: dict[str, str] = d.get("procesados", {})   # id → fecha ISO del correo
        self.fallos: dict[str, int] = d.get("fallos", {})           # id → nº de intentos fallidos
        self.ultimo_aviso: dict[str, str] = d.get("ultimo_aviso", {})   # tipo → fecha ISO

    def fecha_inicio(self, procesar_desde: str, ahora: datetime) -> datetime:
        """Desde cuándo se procesan correos: config.procesar_desde o la primera ejecución."""
        if procesar_desde:
            return datetime.fromisoformat(procesar_desde).replace(tzinfo=timezone.utc)
        if not self.inicio:
            self.inicio = ahora.isoformat()
        return datetime.fromisoformat(self.inicio)

    def ya_procesado(self, id_: str) -> bool:
        return id_ in self.procesados

    def marcar_procesado(self, id_: str, fecha: datetime) -> None:
        self.procesados[id_] = fecha.isoformat()
        self.fallos.pop(id_, None)

    def sumar_fallo(self, id_: str) -> int:
        self.fallos[id_] = self.fallos.get(id_, 0) + 1
        return self.fallos[id_]

    def puede_avisar(self, tipo: str, ahora: datetime, cada=timedelta(days=1)) -> bool:
        """Limita avisos repetitivos (p. ej. Gemini caído) a uno por periodo."""
        ultimo = self.ultimo_aviso.get(tipo)
        if ultimo and ahora - datetime.fromisoformat(ultimo) < cada:
            return False
        self.ultimo_aviso[tipo] = ahora.isoformat()
        return True

    def limpiar(self, antes_de: datetime) -> None:
        """Olvida los correos que ya quedan fuera de la ventana de búsqueda."""
        # (los fallos no hace falta limpiarlos: tras max_reintentos el correo pasa a procesados)
        self.procesados = {k: v for k, v in self.procesados.items() if datetime.fromisoformat(v) >= antes_de}

    def guardar(self) -> None:
        datos = {"inicio": self.inicio, "procesados": dict(sorted(self.procesados.items(), key=lambda x: x[1])),
                 "fallos": self.fallos, "ultimo_aviso": self.ultimo_aviso}
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        # Escritura atómica: nunca queda un estado.json a medias
        fd, tmp = tempfile.mkstemp(dir=self.ruta.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, self.ruta)
