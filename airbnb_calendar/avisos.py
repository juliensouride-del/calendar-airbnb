"""
Avisos al anfitrión: qué novedades de una reserva no se le han contado todavía.

Funciones puras.
"""
from __future__ import annotations

from .config import Config
from .formato import texto_fuente
from .parser import clave_normalizada
from .reglas import es_hora_valida


def avisos_pendientes(e: dict, cfg: Config) -> list[str]:
    """Novedades que todavía no se han avisado (no modifica el estado)."""
    if e["cancelada"]:
        return []
    av, av0 = [], e.get("avisado") or {}
    if cfg.avisar_peticiones:
        # Entrar antes / salir después es una petición (aunque [avisos] horas esté desactivado)
        if e.get("entrada_anticipada") and e["entrada_anticipada"] != av0.get("entrada_anticipada"):
            av.append(f"⏰ Pide ENTRAR ANTES de las {cfg.hora_entrada_por_defecto}: "
                      + _texto_solicitud(e["entrada_anticipada"]))
        if e.get("salida_tardia") and e["salida_tardia"] != av0.get("salida_tardia"):
            av.append(f"⌛ Pide SALIR DESPUÉS de las {cfg.hora_salida_por_defecto}: "
                      + _texto_solicitud(e["salida_tardia"]))
        sofa, cuna = bool(e["sofa"] and e["sofa"]["v"]), bool(e["cuna"] and e["cuna"]["v"])
        if sofa and not av0.get("sofa"):
            av.append("🛋️ Sofá cama — " + texto_fuente(e, "sofa"))
        if not sofa and av0.get("sofa"):
            av.append("🛋️ Ya NO necesita sofá cama")
        if cuna and not av0.get("cuna"):
            av.append("🍼 Cuna — " + texto_fuente(e, "cuna"))
        if not cuna and av0.get("cuna"):
            av.append("🍼 Ya NO necesita cuna")
        ya = {clave_normalizada(p) for p in av0.get("peticiones", [])}
        av += ["📝 " + p for p in e["peticiones"] if clave_normalizada(p) not in ya]
    return av


def marcar_avisado(e: dict) -> dict:
    """Marca como avisado todo lo que hay ahora en el estado."""
    e["avisado"] = {
        "sofa": bool(e["sofa"] and e["sofa"]["v"]),
        "cuna": bool(e["cuna"] and e["cuna"]["v"]),
        "peticiones": list(e["peticiones"]),
        "entrada_anticipada": e.get("entrada_anticipada"),
        "salida_tardia": e.get("salida_tardia"),
    }
    return e


def _texto_solicitud(v: str) -> str:
    return f"a las {v}" if es_hora_valida(v) else "sin hora concreta (pregunta si es posible)"
