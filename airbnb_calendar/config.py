"""
Configuración: config.toml (datos no secretos, versionados) + variables de entorno (secretos).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Alojamiento:
    nombre: str                         # título EXACTO del anuncio en Airbnb
    alias: str                          # nombre corto para el título del evento
    id_anuncio: str = ""                # nº de airbnb.com/rooms/<nº> (lo más fiable)
    otros_nombres: list[str] = field(default_factory=list)
    capacidad_sin_sofa: int | None = None   # personas que caben sin abrir el sofá cama
    locker: str = ""                    # enlace del locker de llaves
    locker_coincide_con: str = ""       # alias de otro alojamiento que comparte el locker...
    locker_si_coincide: str = ""        # ...y locker que se usa si los dos tienen entrada el mismo día


@dataclass
class Config:
    # Yahoo
    carpetas: list[str] = field(default_factory=lambda: ["INBOX"])
    remitentes: list[str] = field(default_factory=lambda: ["airbnb.com"])
    ventana_dias: int = 3
    procesar_desde: str = ""            # 'yyyy-mm-dd'; '' = desde la primera ejecución
    # Calendario
    calendar_id: str = ""
    zona_horaria: str = "Europe/Madrid"
    duracion_evento_min: int = 30
    hora_entrada_por_defecto: str = "15:00"
    hora_salida_por_defecto: str = "11:00"
    colores: dict[str, str] = field(default_factory=lambda: {"reserva": "10", "alerta_limpieza": "6", "cancelada": "8"})
    crear_evento_desde_mensaje: bool = True
    # Limpieza entre reservas
    limpieza_duracion_min: int = 120
    limpieza_inicio: str = "08:00"      # el equipo solo trabaja por la mañana
    limpieza_fin: str = "15:00"
    # Reglas
    alojamientos: list[Alojamiento] = field(default_factory=list)
    cuna_si_hay_bebes: bool = True
    max_reintentos: int = 3
    # Avisos
    email_avisos: str = ""              # '' = la propia cuenta de Yahoo
    avisar_peticiones: bool = True
    avisar_errores: bool = True
    avisar_modificaciones: bool = True
    # Gemini
    usar_gemini: bool = True
    gemini_modelo: str = "gemini-3.5-flash-lite"
    gemini_modelos_respaldo: list[str] = field(default_factory=list)
    gemini_nivel_razonamiento: str = "low"
    gemini_timeout_s: int = 30

    def alojamiento_por_nombre(self, nombre: str | None) -> Alojamiento | None:
        """Por el nombre del anuncio (u otros_nombres) o por el alias."""
        from .parser import clave_normalizada
        k = clave_normalizada(nombre)
        if not k:
            return None
        return (next((a for a in self.alojamientos
                      if any(clave_normalizada(n) == k for n in [a.nombre, *a.otros_nombres])), None)
                or next((a for a in self.alojamientos if clave_normalizada(a.alias) == k), None))


@dataclass
class Secretos:
    yahoo_usuario: str
    yahoo_clave_app: str
    google_service_account_json: str
    gemini_api_key: str = ""

    @classmethod
    def desde_entorno(cls) -> Secretos:
        faltan = [k for k in ("YAHOO_USUARIO", "YAHOO_CLAVE_APP", "GOOGLE_SERVICE_ACCOUNT_JSON") if not os.environ.get(k)]
        if faltan:
            raise SystemExit("Faltan variables de entorno: " + ", ".join(faltan))
        return cls(os.environ["YAHOO_USUARIO"], os.environ["YAHOO_CLAVE_APP"].replace(" ", ""),
                   os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], os.environ.get("GEMINI_API_KEY", ""))

    @staticmethod
    def google_desde_entorno() -> str:
        if not os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
            raise SystemExit("Falta la variable de entorno GOOGLE_SERVICE_ACCOUNT_JSON")
        return os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]


def cargar_config(ruta: str | Path = "config.toml") -> Config:
    with open(ruta, "rb") as f:
        t = tomllib.load(f)
    y, c, av, g = t.get("yahoo", {}), t.get("calendario", {}), t.get("avisos", {}), t.get("gemini", {})
    cfg = Config(
        carpetas=y.get("carpetas", ["INBOX"]),
        remitentes=y.get("remitentes", ["airbnb.com"]),
        ventana_dias=y.get("ventana_dias", 3),
        procesar_desde=y.get("procesar_desde", ""),
        calendar_id=os.environ.get("CALENDAR_ID") or c.get("id", ""),
        zona_horaria=c.get("zona_horaria", "Europe/Madrid"),
        duracion_evento_min=c.get("duracion_evento_min", 30),
        hora_entrada_por_defecto=c.get("hora_entrada_por_defecto", "15:00"),
        hora_salida_por_defecto=c.get("hora_salida_por_defecto", "11:00"),
        colores={"reserva": "10", "alerta_limpieza": "6", "cancelada": "8", **c.get("colores", {})},
        crear_evento_desde_mensaje=c.get("crear_evento_desde_mensaje", True),
        limpieza_duracion_min=t.get("limpieza", {}).get("duracion_min", 120),
        limpieza_inicio=t.get("limpieza", {}).get("inicio", "08:00"),
        limpieza_fin=t.get("limpieza", {}).get("fin", "15:00"),
        alojamientos=[Alojamiento(**a) for a in t.get("alojamientos", [])],
        cuna_si_hay_bebes=t.get("reglas", {}).get("cuna_si_hay_bebes", True),
        max_reintentos=t.get("reglas", {}).get("max_reintentos", 3),
        email_avisos=av.get("email", ""),
        avisar_peticiones=av.get("peticiones", True),
        avisar_errores=av.get("errores", True),
        avisar_modificaciones=av.get("modificaciones", True),
        usar_gemini=g.get("usar", True),
        gemini_modelo=g.get("modelo", "gemini-3.5-flash-lite"),
        gemini_modelos_respaldo=g.get("modelos_respaldo", []),
        gemini_nivel_razonamiento=g.get("nivel_razonamiento", "low"),
        gemini_timeout_s=g.get("timeout_s", 30),
    )
    from .parser import normalizar_hora
    for campo in ("hora_entrada_por_defecto", "hora_salida_por_defecto"):
        hora = normalizar_hora(getattr(cfg, campo))
        if not hora:
            raise SystemExit(f"[calendario] {campo} no es una hora válida (formato HH:MM): {getattr(cfg, campo)!r}")
        setattr(cfg, campo, hora)          # '9:00' → '09:00'
    if not cfg.calendar_id or "PEGA_AQUI" in cfg.calendar_id:
        raise SystemExit("Rellena [calendario] id en config.toml (o la variable CALENDAR_ID).")
    return cfg
