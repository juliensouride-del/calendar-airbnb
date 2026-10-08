"""
Estado de una reserva, fusión de información, avisos y formato de los eventos.

El estado de cada reserva se guarda (JSON) dentro de sus dos eventos de Calendar
y se va enriqueciendo con cada correo:
  confirmación → datos base · mensajes → horas / sofá / cuna · cancelación.

Funciones puras.
"""
from __future__ import annotations

import re
from datetime import date

from .config import Alojamiento, Config
from .parser import DatosCorreo, clave_normalizada

PRIORIDAD_FUENTE = {"regla": 1, "palabras": 2, "huesped": 3}   # lo que dice el huésped manda
DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def crear_estado(datos: DatosCorreo | None = None) -> dict:
    e = {
        "v": 1,
        "codigo": None, "alojamiento": None, "alias": None, "huesped": None,
        "checkin": None, "checkout": None,
        "hora_checkin": None, "hora_checkout": None,        # horas oficiales del anuncio
        "adultos": None, "ninos": None, "bebes": None, "mascotas": None, "total": None,
        "hora_llegada": None, "hora_salida": None,          # lo que dice el huésped
        "sofa": None, "cuna": None,                          # {"v": bool, "f": "huesped"|"palabras"|"regla"}
        "peticiones": [],                                    # otras peticiones especiales
        "notas": [],                                         # [{"f": "dd/mm", "t": resumen}]
        "cancelada": False,
        "avisado": {"sofa": False, "cuna": False, "peticiones": [], "hora_llegada": None, "hora_salida": None},
    }
    return actualizar_con_datos(e, datos) if datos else e


def actualizar_con_datos(e: dict, d: DatosCorreo) -> dict:
    """Vuelca los datos objetivos de un correo (parser) sobre el estado."""
    for k in ("codigo", "checkin", "checkout", "hora_checkin", "hora_checkout"):
        if getattr(d, k):
            e[k] = getattr(d, k)
    if d.alojamiento and (d.alojamiento_configurado or not e["alojamiento"]):
        e["alojamiento"] = d.alojamiento
        e["alias"] = d.alias or d.alojamiento
    # Nombre: se prefiere el completo ("Imane Bensalah" mejor que "Imane")
    if d.huesped:
        actual = e["huesped"] or ""
        if not actual or (len(d.huesped) > len(actual) and d.huesped.lower().startswith(actual.split(" ")[0].lower())):
            e["huesped"] = d.huesped
    h = d.huespedes
    if h:
        if h.adultos is not None:
            e.update(adultos=h.adultos, ninos=h.ninos, bebes=h.bebes, mascotas=h.mascotas)
        if h.total:
            e["total"] = h.total
    return e


def fusionar_analisis(e: dict, a: dict | None, fuente: str, fecha_txt: str = "") -> dict:
    """
    Fusiona el análisis de un mensaje.
    a: {hora_llegada, hora_salida, sofa_cama, cuna, otras_peticiones, resumen}
    fuente: 'huesped' (Gemini) | 'palabras' (respaldo por palabras clave)
    """
    if not a:
        return e
    if es_hora_valida(a.get("hora_llegada")):
        e["hora_llegada"] = a["hora_llegada"]
    if es_hora_valida(a.get("hora_salida")):
        e["hora_salida"] = a["hora_salida"]
    e["sofa"] = _fusionar_marca(e["sofa"], a.get("sofa_cama"), fuente)
    e["cuna"] = _fusionar_marca(e["cuna"], a.get("cuna"), fuente)

    for p in a.get("otras_peticiones") or []:
        txt = str(p or "").strip()[:150]
        if txt and not any(clave_normalizada(x) == clave_normalizada(txt) for x in e["peticiones"]):
            e["peticiones"].append(txt)
    e["peticiones"] = e["peticiones"][-10:]

    resumen = str(a.get("resumen") or "").strip()
    if resumen:
        e["notas"] = (e["notas"] + [{"f": fecha_txt, "t": resumen[:200]}])[-5:]
    return e


def _fusionar_marca(actual: dict | None, valor: str | None, fuente: str) -> dict | None:
    if valor not in ("si", "no"):
        return actual
    nueva = {"v": valor == "si", "f": fuente}
    if not actual:
        return nueva
    return nueva if PRIORIDAD_FUENTE[fuente] >= PRIORIDAD_FUENTE[actual["f"]] else actual


def aplicar_reglas(e: dict, alojamiento: Alojamiento | None, cfg: Config) -> dict:
    """Reglas deterministas: bebés ⇒ cuna; más personas que camas fijas ⇒ sofá cama."""
    if cfg.cuna_si_hay_bebes and (not e["cuna"] or e["cuna"]["f"] == "regla"):
        e["cuna"] = {"v": True, "f": "regla"} if (e["bebes"] or 0) > 0 else None
    cap = alojamiento.capacidad_sin_sofa if alojamiento else None
    if cap and (not e["sofa"] or e["sofa"]["f"] == "regla"):
        e["sofa"] = {"v": True, "f": "regla"} if personas_que_duermen(e) > cap else None
    return e


def personas_que_duermen(e: dict) -> int:
    if e["adultos"] is not None:
        return (e["adultos"] or 0) + (e["ninos"] or 0)
    return e["total"] or 0

# ----------------------------------------------------------------------------
#  Avisos
# ----------------------------------------------------------------------------


def avisos_pendientes(e: dict, cfg: Config) -> list[str]:
    """Novedades que todavía no se han avisado (no modifica el estado)."""
    if e["cancelada"]:
        return []
    av, av0 = [], e.get("avisado") or {}
    if cfg.avisar_peticiones:
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
    if cfg.avisar_horas:
        if e["hora_llegada"] and e["hora_llegada"] != av0.get("hora_llegada"):
            av.append("🕒 Llegada prevista: " + e["hora_llegada"])
        if e["hora_salida"] and e["hora_salida"] != av0.get("hora_salida"):
            av.append("🕒 Salida prevista: " + e["hora_salida"])
    return av


def marcar_avisado(e: dict) -> dict:
    """Marca como avisado todo lo que hay ahora en el estado."""
    e["avisado"] = {
        "sofa": bool(e["sofa"] and e["sofa"]["v"]),
        "cuna": bool(e["cuna"] and e["cuna"]["v"]),
        "peticiones": list(e["peticiones"]),
        "hora_llegada": e["hora_llegada"],
        "hora_salida": e["hora_salida"],
    }
    return e


def texto_fuente(e: dict, campo: str) -> str:
    m = e[campo]
    if not m:
        return ""
    if m["f"] == "huesped":
        return "lo indica el huésped" if m["v"] else "el huésped indica que no lo necesita"
    if m["f"] == "palabras":
        return "posible (detectado por palabra clave, revisar el mensaje)"
    if campo == "cuna":
        return f"{e['bebes']} {'bebé' if e['bebes'] == 1 else 'bebés'} en la reserva"
    return f"{personas_que_duermen(e)} personas, más que las camas fijas del alojamiento"

# ----------------------------------------------------------------------------
#  Formato de los eventos
# ----------------------------------------------------------------------------


def titulo_evento(e: dict, tipo: str) -> str:
    """'🟢 Check-in · Lavapiés · 3 pers · llega 17:30 · 🛋️ sofá cama · 🍼 cuna'"""
    partes = ["🟢 Check-in" if tipo == "entrada" else "🔴 Check-out", e["alias"] or e["alojamiento"] or "Airbnb"]
    if e["total"]:
        partes.append(f"{e['total']} pers")
    hora = e["hora_llegada"] if tipo == "entrada" else e["hora_salida"]
    if hora:
        partes.append(("llega " if tipo == "entrada" else "sale ") + hora)
    if e["sofa"] and e["sofa"]["v"]:
        partes.append("🛋️ sofá cama")
    if e["cuna"] and e["cuna"]["v"]:
        partes.append("🍼 cuna")
    t = " · ".join(partes)
    return "❌ CANCELADA · " + t if e["cancelada"] else t


def descripcion_evento(e: dict, actualizado_txt: str = "") -> str:
    no = "No indicada"
    l = []
    if e["cancelada"]:
        l += ["❌ RESERVA CANCELADA", ""]
    l.append("🏠 Alojamiento: " + (e["alojamiento"] or "?"))
    l.append("📅 Check-in: " + fecha_larga_es(e["checkin"]) + (" · desde " + e["hora_checkin"] if e["hora_checkin"] else ""))
    l.append("📅 Check-out: " + fecha_larga_es(e["checkout"]) + (" · hasta " + e["hora_checkout"] if e["hora_checkout"] else ""))
    l.append("👥 Personas: " + texto_personas(e))
    l.append("🕒 Hora prevista de llegada: " + (e["hora_llegada"] or no))
    l.append("🕒 Hora prevista de salida: " + (e["hora_salida"] or no))
    l.append("🛋️ Sofá cama: " + _texto_marca(e, "sofa"))
    l.append("🍼 Cuna: " + _texto_marca(e, "cuna"))
    if e["peticiones"]:
        l.append("📝 Otras peticiones:")
        l += ["   • " + p for p in e["peticiones"]]
    if e["notas"]:
        l.append("💬 Notas de los mensajes:")
        l += ["   • " + (n["f"] + ": " if n["f"] else "") + n["t"] for n in e["notas"]]
    l += ["", "────────────"]
    l.append(f"Huésped: {e['huesped'] or '?'} · Código: {e['codigo'] or 'desconocido'}")
    l.append("Sincronizado desde Yahoo Mail" + (" · " + actualizado_txt if actualizado_txt else ""))
    return "\n".join(l)


def _texto_marca(e: dict, campo: str) -> str:
    m = e[campo]
    if not m:
        return "No indicado"
    return ("SÍ" if m["v"] else "No") + " — " + texto_fuente(e, campo)


def texto_personas(e: dict) -> str:
    if not e["total"] and not e["adultos"]:
        return "?"
    total = e["total"] or e["adultos"]
    if e["adultos"] is None:
        return str(total)

    def plural(n, s, pl):
        return f"{n} {s if n == 1 else pl}"
    p = []
    if e["adultos"]:
        p.append(plural(e["adultos"], "adulto", "adultos"))
    if e["ninos"]:
        p.append(plural(e["ninos"], "niño", "niños"))
    if e["bebes"]:
        p.append(plural(e["bebes"], "bebé", "bebés"))
    if e["mascotas"]:
        p.append(plural(e["mascotas"], "mascota", "mascotas"))
    return f"{total} ({', '.join(p)})"


def fecha_larga_es(iso: str | None) -> str:
    """'2026-11-26' → 'jueves 26/11/2026'"""
    if not iso:
        return "?"
    d = date.fromisoformat(iso)
    return f"{DIAS_ES[d.weekday()]} {d:%d/%m/%Y}"


def fecha_corta_es(iso: str | None) -> str:
    """'2026-11-26' → '26/11'"""
    return f"{date.fromisoformat(iso):%d/%m}" if iso else "?"


def es_hora_valida(h) -> bool:
    return isinstance(h, str) and bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", h))

# ----------------------------------------------------------------------------
#  Respaldo sin IA: palabras clave (no detecta horas)
# ----------------------------------------------------------------------------


PALABRAS_CUNA = re.compile(r"\b(crib|cot|cuna|berceau|lit (?:pour |de )?b[ée]b[ée]|lit parapluie|baby ?bed|travel cot"
                           r"|babybett|kinderbett|reisebett|culla|lettino)\b", re.I)
PALABRAS_SOFA = re.compile(r"\b(sofa[ -]?bed|sof[áa][ -]?cama|canap[ée][ -]?lit|schlafsofa|divano letto|sof[áa]"
                           r"|canap[ée]|couch)\b", re.I)


def analisis_por_palabras_clave(texto: str) -> dict:
    t = str(texto or "")
    return {
        "hora_llegada": None,
        "hora_salida": None,
        "sofa_cama": "si" if PALABRAS_SOFA.search(t) else "sin_mencion",
        "cuna": "si" if PALABRAS_CUNA.search(t) else "sin_mencion",
        "otras_peticiones": [],
        "resumen": "",
    }
