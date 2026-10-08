"""Utilidades compartidas por las pruebas (correos reales de ./ejemplos)."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from airbnb_calendar.config import Alojamiento, Config  # noqa: E402
from airbnb_calendar.yahoo import leer_eml  # noqa: E402

MADRID = ZoneInfo("Europe/Madrid")
MESES_FR = {"janv": 1, "févr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6, "juil": 7, "août": 8,
            "sept": 9, "oct": 10, "nov": 11, "déc": 12}

# Alojamientos fijos: cambiar alias o capacidades en config.toml no rompe las pruebas
ALOJAMIENTOS_PRUEBA = [
    Alojamiento("Espacioso y luminoso apartamento Corazón de Madrid", "Corazón de Madrid", "809290025626258590"),
    Alojamiento("Lavapies - Peaceful 2 bedrooms Fresh A/C", "Lavapiés", "1065868788427158053"),
    Alojamiento("Anton Martin Agradable, Anton Martin - Apartament.", "Antón Martín", "1025328659441162105",
                otros_nombres=["Apartamentos de Julian, Anton Martin - Apartament."]),
]


@dataclass
class Ejemplo:
    asunto: str
    cuerpo: str
    fecha_ref: str          # 'yyyy-mm-dd'
    fecha_hora: str         # 'yyyy-mm-dd HH:MM' (Madrid)

    @property
    def fecha(self) -> datetime:
        return datetime.strptime(self.fecha_hora, "%Y-%m-%d %H:%M").replace(tzinfo=MADRID)


def config_prueba(**cambios) -> Config:
    cfg = Config(calendar_id="cal-prueba", alojamientos=ALOJAMIENTOS_PRUEBA, gemini_modelo="gemini-principal")
    for k, v in cambios.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture
def cfg() -> Config:
    return config_prueba()


def cargar_ejemplo(nombre: str, fecha_por_defecto: str) -> list[Ejemplo]:
    """
    Divide un .txt de ./ejemplos en correos. Los ej2/ej3 son hilos copiados desde Gmail web:
    se separan por la línea "Corps du message" y la fecha de cada correo se lee del texto
    ("ven. 2 oct. à 09:18"). La 1ª línea del archivo (comentario) se descarta.
    """
    txt = (RAIZ / "ejemplos" / nombre).read_text("utf-8")
    sin_comentario = "\n".join(txt.splitlines()[1:])
    correos, fecha, hora = [], fecha_por_defecto, "10:00"
    for trozo in re.split(r"^Corps du message\s*$", sin_comentario, flags=re.M):
        correos.append(Ejemplo("", trozo, fecha, f"{fecha} {hora}"))
        m = re.search(r"(\d{1,2}) ([a-zéû]+)\.? à (\d{2}:\d{2})\s*$", trozo.strip())
        if m and m.group(2) in MESES_FR:
            fecha = f"2026-{MESES_FR[m.group(2)]:02d}-{int(m.group(1)):02d}"
            hora = m.group(3)
    return correos


def eml_crudo(nombre: str) -> bytes:
    """Bytes originales de un .eml de ./ejemplos (algunos se guardaron como RTF desde TextEdit)."""
    raw = (RAIZ / "ejemplos" / nombre).read_bytes().decode("latin1")
    if raw.startswith("{\\rtf"):
        raw = _rtf_a_texto(raw)
    return raw.replace("\r\n", "\n").replace("\n", "\r\n").encode("latin1", errors="strict")


def cargar_eml(nombre: str) -> Ejemplo:
    crudo = eml_crudo(nombre)
    _, asunto, texto = leer_eml(crudo)
    cab = crudo.split(b"\r\n\r\n", 1)[0].decode("latin1")
    fecha = re.search(r"^Date:\s*(.+)$", cab, re.M | re.I).group(1).strip()
    local = parsedate_to_datetime(re.sub(r"\s*\(.*\)$", "", fecha)).astimezone(MADRID)
    return Ejemplo(asunto, texto, local.date().isoformat(), local.strftime("%Y-%m-%d %H:%M"))


def _rtf_a_texto(rtf: str) -> str:
    """Texto plano de un RTF sencillo de TextEdit (el .eml original que contiene)."""
    m = re.search(r"\n(Received|Return-Path|Delivered-To|MIME-Version|From|Date|Subject):", rtf)
    s = rtf[m.start() + 1:] if m else rtf
    s = re.sub(r"^[^\n]*?\\strokec\d+ ", "", s, count=1, flags=re.M)
    s = s.replace("\\\n", "\n")
    s = re.sub(r"\\'([0-9a-f]{2})", lambda x: chr(int(x.group(1), 16)), s, flags=re.I)
    s = re.sub(r"\\uc0\\u(\d+) ?", lambda x: chr(int(x.group(1))), s)
    s = re.sub(r"\\([{}\\])", r"\1", s)
    return re.sub(r"\}\s*$", "", s)
