"""
Base de conocimiento para responder a los huéspedes (base_conocimiento.toml).

Es la ÚNICA fuente de datos que puede usar la IA al redactar: lo que no está aquí
no se responde. Un valor "PENDIENTE" es un dato que falta: nunca se rellena, se
señala al anfitrión.

Datos privados (contraseñas de wifi...): el repositorio es público, así que en
base_conocimiento.toml van como "PRIVADO" y el valor real llega aparte, en el
secreto BASE_PRIVADA (o en el archivo local base_privada.toml, que no se sube).
Es un TOML con las mismas secciones, que se superpone a la base. Sin él, un dato
"PRIVADO" se trata como PENDIENTE: el borrador lo marca y nunca lo inventa.

Funciones puras (sin red).
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

PENDIENTE = "PENDIENTE"
PRIVADO = "PRIVADO"
RE_URL = re.compile(r"https?://[^\s<>\"')\]]+")


def es_pendiente(v) -> bool:
    """Dato que falta: PENDIENTE, o PRIVADO sin su valor en BASE_PRIVADA."""
    return isinstance(v, str) and v.strip().upper().startswith((PENDIENTE, PRIVADO))


def es_privado(v) -> bool:
    return isinstance(v, str) and v.strip().upper().startswith(PRIVADO)


def superponer(base: dict, encima: dict) -> dict:
    """Mezcla recursiva: las tablas se combinan y el resto de valores de `encima` sustituyen a los de `base`."""
    res = dict(base)
    for k, v in encima.items():
        res[k] = superponer(res[k], v) if isinstance(v, dict) and isinstance(res.get(k), dict) else v
    return res


def _limpio(v) -> str:
    return v.strip() if isinstance(v, str) else ""


@dataclass
class Momento:
    id: str
    cuando: str
    dia: str = ""                   # checkin, checkin-1, checkout, checkout-1, mitad, confirmacion, con_bienvenida
    hora: str = "00:00"
    texto: str = ""
    piezas: list[str] = field(default_factory=list)
    idioma: str = "en"
    noches_minimas: int = 4         # solo "mitad": en estancias más cortas no se propone
    nota: str = ""                  # nota para el anfitrión en la tarjeta

    def fecha(self, checkin: str, checkout: str) -> date | None:
        """Día en que toca este momento en una reserva (None si no va en la agenda o no aplica)."""
        if not checkin or not checkout:
            return None
        ci, co = date.fromisoformat(checkin), date.fromisoformat(checkout)
        m = re.fullmatch(r"(checkin|checkout)([+-]\d+)?", self.dia)
        if m:
            base = ci if m.group(1) == "checkin" else co
            return base + timedelta(days=int(m.group(2) or 0))
        if self.dia == "mitad":
            noches = (co - ci).days
            return ci + timedelta(days=noches // 2) if noches >= self.noches_minimas else None
        return None


class BaseConocimiento:
    def __init__(self, datos: dict):
        self.datos = datos
        self.general: dict = datos.get("general", {})
        self.pisos: dict[str, dict] = datos.get("pisos", {})
        self.comun: dict = datos.get("comun", {})
        self.preguntas: list[dict] = datos.get("preguntas", [])
        self.momentos: dict[str, Momento] = {
            m["id"]: Momento(id=m["id"], cuando=m.get("cuando", ""), dia=m.get("dia", ""), hora=m.get("hora", "00:00"),
                             texto=_limpio(m.get("texto")), piezas=list(m.get("piezas", [])),
                             idioma=m.get("idioma", self.general.get("idioma_original", "en")),
                             noches_minimas=m.get("noches_minimas", 4), nota=_limpio(m.get("nota")))
            for m in datos.get("momentos", [])
        }

    @classmethod
    def cargar(cls, ruta: str | Path, privada: str | None = None) -> BaseConocimiento:
        """
        La base y, encima, sus datos privados: `privada` (texto TOML) o, si no se pasa, el secreto BASE_PRIVADA
        o el archivo base_privada.toml junto a la base.
        """
        with open(ruta, "rb") as f:
            datos = tomllib.load(f)
        if privada is None:
            privada = os.environ.get("BASE_PRIVADA", "")
            local = Path(ruta).with_name("base_privada.toml")
            if not privada.strip() and local.exists():
                privada = local.read_text("utf-8")
        if privada and privada.strip():
            try:
                datos = superponer(datos, tomllib.loads(privada))
            except tomllib.TOMLDecodeError as e:
                raise SystemExit(f"BASE_PRIVADA no es un TOML válido: {e}") from None
        return cls(datos)

    # ------------------------------------------------------------------ datos

    @property
    def firma(self) -> str:
        f = _limpio(self.general.get("firma"))
        return "" if es_pendiente(f) else f

    def tiene_piso(self, alias: str | None) -> bool:
        return bool(alias) and alias in self.pisos

    def valor(self, ruta: str, alias: str) -> str:
        """'pisos.wifi' → el del piso · 'comun.normas' → el común (en Tirso, 'normas_tirso' si existe)."""
        seccion, _, clave = ruta.partition(".")
        if seccion == "pisos":
            return _limpio(self.pisos.get(alias, {}).get(clave))
        if seccion == "comun":
            propio = self.comun.get(f"{clave}_{alias.lower()}")
            return _limpio(propio if propio is not None else self.comun.get(clave))
        return ""

    def pendientes(self, alias: str) -> list[str]:
        """Datos que faltan en la base para ese piso (y los comunes)."""
        faltan = [f"{k} de {alias}" + (" (falta el secreto BASE_PRIVADA)" if es_privado(v) else "")
                  for k, v in self.pisos.get(alias, {}).items() if es_pendiente(v)]
        faltan += ["firma de los mensajes"] if es_pendiente(self.general.get("firma")) else []
        faltan += [f"mensaje «{m.id}»" for m in self.momentos.values() if es_pendiente(m.texto)]
        return faltan

    def urls(self) -> set[str]:
        """Todos los enlaces de la base (para comprobar que un borrador no se inventa ninguno)."""
        return {u.rstrip(".,;:!?") for u in RE_URL.findall(_todo_el_texto(self.datos))}

    # -------------------------------------------------------------- prompt

    def texto_para(self, alias: str) -> str:
        """La base, solo con lo que vale para ese piso, como texto para la IA."""
        piso = self.pisos.get(alias, {})
        l = [f"## Apartamento: {alias}"]
        for k, v in piso.items():
            if k.startswith("foto") or isinstance(v, bool):
                continue
            l.append(f"- {k}: {'(FALTA ESTE DATO: no lo respondas, márcalo para el anfitrión)' if es_pendiente(v) else _limpio(v) or '(no aplica)'}")
        if piso.get("anuncio_reciente"):
            l.append(f"- anuncio_reciente: {self.valor('comun.anuncio_reciente', alias)}")
        l += ["", "## Textos comunes"]
        propios = {a.lower() for a in self.pisos}
        for k in self.comun:
            if k == "anuncio_reciente" or ("_" in k and k.rsplit("_", 1)[1] in propios):
                continue
            texto = self.rellenar(self.valor(f"comun.{k}", alias), alias)
            l.append(f"- {k}: {texto}")
        l += ["", "## Preguntas frecuentes (qué textos usar)"]
        for p in self.preguntas:
            extra = " · DECIDE EL ANFITRIÓN: no confirmes ni niegues" if p.get("decide_anfitrion") else ""
            usar = ", ".join(p.get("usar", [])) or "plantilla"
            l.append(f"- {p['id']} (p. ej. {' / '.join(p.get('ejemplos', []))}): usar {usar}{extra}")
            if p.get("plantilla"):
                l.append(f"  plantilla: {_limpio(p['plantilla'])}")
        l += ["", f"## Horario estándar: entrada {self.general.get('hora_entrada', '15:00')} · salida {self.general.get('hora_salida', '11:00')}"]
        return "\n".join(l)

    def rellenar(self, texto: str, alias: str) -> str:
        """{campo} → el dato de ese piso (p. ej. {locker_llaves}); si falta, un aviso para que la IA no lo invente."""
        piso = self.pisos.get(alias, {})

        def dato(m: re.Match) -> str:
            v = piso.get(m.group(1))
            if v is None:
                return m.group(0)
            return "(FALTA ESTE DATO: no lo respondas, márcalo para el anfitrión)" if es_pendiente(v) else _limpio(v)
        return re.sub(r"\{([a-z_]+)\}", dato, texto)

    def texto_momento(self, momento_id: str, alias: str, nombre: str, dia_salida: str = "") -> tuple[str, list[str]]:
        """(texto de la plantilla con los marcadores puestos, datos que faltan). Texto '' si la plantilla está pendiente."""
        m = self.momentos.get(momento_id)
        if not m:
            return "", [f"no hay mensaje «{momento_id}» en la base"]
        faltan: list[str] = []
        if m.piezas:
            partes = []
            for p in m.piezas:
                if "." in p and p.split(".", 1)[0] in ("pisos", "comun"):
                    v = self.valor(p, alias)
                    if es_pendiente(v):
                        faltan.append(f"{p.split('.', 1)[1]} de {alias}")
                    elif v:
                        partes.append(self.rellenar(v, alias))
                else:
                    partes.append(p)
            if self.pisos.get(alias, {}).get("anuncio_reciente") is False and "comun.anuncio_reciente" in m.piezas:
                partes = [x for x in partes if x != self.valor("comun.anuncio_reciente", alias)]
            texto = "\n\n".join(partes)
        elif es_pendiente(m.texto) or not m.texto:
            return "", [f"texto del mensaje «{momento_id}»"]
        else:
            texto = m.texto
        texto = texto.replace("{nombre}", nombre or "").replace("{dia_salida}", dia_salida or "")
        return texto.strip(), faltan


def _todo_el_texto(d) -> str:
    if isinstance(d, dict):
        return "\n".join(_todo_el_texto(v) for v in d.values())
    if isinstance(d, list):
        return "\n".join(_todo_el_texto(v) for v in d)
    return d if isinstance(d, str) else ""
