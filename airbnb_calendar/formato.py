"""
Texto de los eventos de Calendar: título y descripción (de lo más urgente a lo menos).

Funciones puras.
"""
from __future__ import annotations

from datetime import date

from .config import Config
from .reglas import es_hora_valida, personas_que_duermen

DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def prefijo_limpieza(l: dict | None) -> str:
    if not l:
        return ""
    return "[LIMPIEZA OK] " if l["ok"] else "[ALERTA LIMPIEZA / AJUSTE HORARIO] "


def lineas_limpieza(l: dict | None, cfg: Config) -> list[str]:
    """Ventana para limpiar entre la salida del huésped anterior y la entrada de este (o el aviso si no cabe)."""
    if not l:
        return []
    cuando = "ese día" if l["mismo_dia"] else "el día anterior"
    if l["ok"]:
        return [f"🧹 Limpieza: {fecha_corta_es(l['dia'])} de {l['desde']} a {l['hasta']}"
                f" (el huésped anterior sale {cuando} a las {l['salida']})"]
    return [f"⚠️ LIMPIEZA: no caben {cfg.limpieza_duracion_min // 60} h entre las {cfg.limpieza_inicio} y las"
            f" {cfg.limpieza_fin} ({l['motivo']}). Pide al huésped anterior que salga antes o a este que"
            " llegue más tarde."]


def titulo_evento(e: dict, limpieza: dict | None = None) -> str:
    """'[LIMPIEZA OK] 🏠 Lavapiés · 3 pers · llega 17:30 · sale 10:00 · 🛋️ sofá cama · 🍼 cuna'"""
    partes = ["🏠 " + (e["alias"] or e["alojamiento"] or "Airbnb")]
    if e["total"]:
        partes.append(f"{e['total']} pers")
    if e.get("acuerdo_entrada"):
        partes.append(f"entra {e['acuerdo_entrada']} ✅")
    else:
        if e["hora_llegada"]:
            partes.append("llega " + e["hora_llegada"])
        if e.get("entrada_anticipada"):
            partes.append("⏰ pide entrar " + (e["entrada_anticipada"] if es_hora_valida(e["entrada_anticipada"]) else "antes"))
    if e.get("acuerdo_salida"):
        partes.append(f"sale {e['acuerdo_salida']} ✅")
    else:
        if e["hora_salida"]:
            partes.append("sale " + e["hora_salida"])
        if e.get("salida_tardia"):
            partes.append("⌛ pide salir " + (e["salida_tardia"] if es_hora_valida(e["salida_tardia"]) else "después"))
    if e["sofa"] and e["sofa"]["v"]:
        partes.append("🛋️ sofá cama")
    if e["cuna"] and e["cuna"]["v"]:
        partes.append("🍼 cuna")
    t = " · ".join(partes)
    return "❌ CANCELADA · " + t if e["cancelada"] else prefijo_limpieza(limpieza) + t


def descripcion_evento(e: dict, limpieza: dict | None = None, cfg: Config | None = None) -> str:
    """
    Debajo del título, de lo más urgente a lo menos:
      1. Avisos (solo si los hay): limpieza que no cabe, petición de horario pendiente, mensajes sin analizar.
      2. Datos de la reserva: llegada y salida previstas, sofá cama, cuna y ventana de limpieza.
      3. Peticiones del huésped.
      4. Locker.
      y al final, el huésped y el código de la reserva.
    No se repiten el alojamiento ni las fechas, que ya se ven en el evento.
    """
    # Las etiquetas #entrada / #salida las escribe el anfitrión en su parte de la descripción
    # (encima de la línea automática); el script las lee pero no las reescribe.
    bloques = []
    if not e["cancelada"]:
        avisos = []
        if limpieza and cfg and not limpieza["ok"]:
            avisos += lineas_limpieza(limpieza, cfg)
        for tipo, campo, icono, texto in (("entrada", "entrada_anticipada", "⏰", "ENTRAR ANTES"),
                                          ("salida", "salida_tardia", "⌛", "SALIR DESPUÉS")):
            pedida = e.get(campo)
            if pedida and not e.get(f"acuerdo_{tipo}"):
                hora = pedida if es_hora_valida(pedida) else "HH:MM"
                cuando = f"a las {pedida}" if es_hora_valida(pedida) else "sin hora concreta"
                avisos.append(f"{icono} PIDE {texto} ({cuando}). Si lo aceptas, escribe arriba: #{tipo} {hora}")
        if e.get("sin_analizar") or any(n["t"].startswith("⚠️") for n in e.get("notas") or []):
            avisos.append("⚠️ Hay mensajes sin analizar por la IA: revísalos en Yahoo Mail.")
        bloques.append(avisos)

        # Sin información que diga otra cosa: horario estándar (o el acordado) y sin sofá cama ni cuna
        llegada = e["hora_llegada"] or e.get("acuerdo_entrada") or (cfg.hora_entrada_por_defecto if cfg else "15:00")
        salida = e["hora_salida"] or e.get("acuerdo_salida") or (cfg.hora_salida_por_defecto if cfg else "11:00")
        datos = [f"🕒 Llegada: {llegada}   ·   Salida: {salida}",
                 "🛋️ Sofá cama: " + _texto_marca(e, "sofa"),
                 "🍼 Cuna: " + _texto_marca(e, "cuna")]
        if limpieza and cfg and limpieza["ok"]:
            datos += lineas_limpieza(limpieza, cfg)
        bloques.append(datos)

        if e["peticiones"]:
            bloques.append(["📝 Peticiones:"] + ["   • " + x for x in e["peticiones"]])
        if e.get("locker"):
            bloques.append(["🔑 Locker: " + e["locker"]])
    bloques.append([f"👤 {e['huesped'] or '?'} · Código {e['codigo'] or 'desconocido'}"])
    if e.get("desde_mensaje"):
        bloques.append(["ℹ️ Evento creado a partir de un mensaje del huésped: no se encontró la confirmación de "
                        "la reserva, así que pueden faltar datos (código, nº de personas…). Se completará si llega."])
    return "\n\n".join("\n".join(b) for b in bloques if b)


def _texto_marca(e: dict, campo: str) -> str:
    m = e[campo]
    if not m:
        return "no"
    return ("SÍ" if m["v"] else "no") + " (" + texto_fuente(e, campo) + ")"


def texto_personas(e: dict) -> str:
    """'3 (2 adultos, 1 bebé)'"""
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


def fecha_larga_es(iso: str | None) -> str:
    """'2026-11-26' → 'jueves 26/11/2026'"""
    if not iso:
        return "?"
    d = date.fromisoformat(iso)
    return f"{DIAS_ES[d.weekday()]} {d:%d/%m/%Y}"


def fecha_corta_es(iso: str | None) -> str:
    """'2026-11-26' → '26/11'"""
    return f"{date.fromisoformat(iso):%d/%m}" if iso else "?"
