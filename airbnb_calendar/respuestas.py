"""
Borradores de mensajes para los huéspedes: cuándo se preparan y qué datos llevan.

  - Respuesta: llega un mensaje del huésped → borrador con la base de conocimiento.
  - Agenda: momentos de la estancia (bienvenida, día antes de salir...) → borrador de su plantilla.

Todo va a la bandeja de aprobación (bot de Telegram). NADA se envía al huésped sin que
el anfitrión lo apruebe allí. Aquí solo se decide QUÉ proponer; enviar lo hace el bot.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from . import formato
from .base import BaseConocimiento
from .config import Config
from .redactor import Borrador

DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
DAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]   # las plantillas están en inglés


def id_borrador(codigo: str | None, origen: str) -> str:
    """Id opaco y estable: el mismo mensaje (o momento) de la misma reserva da siempre el mismo id."""
    return hashlib.sha256(f"{codigo or '?'}|{origen}".encode()).hexdigest()[:16]


def nombre_de_pila(estado: dict) -> str:
    return (estado.get("huesped") or "").split(" ")[0]


def _dia(iso: str | None) -> str:
    if not iso:
        return "?"
    d = date.fromisoformat(iso)
    return f"{DIAS_ES[d.weekday()]} {d:%d/%m/%Y}"


def describir_reserva(e: dict, vecinos: dict, cfg: Config, ahora: datetime) -> str:
    """Datos de la reserva para la IA (sin importes ni datos de pago)."""
    hoy = ahora.date().isoformat()
    if e.get("checkin") and hoy < e["checkin"]:
        fase = f"antes de llegar (faltan {(date.fromisoformat(e['checkin']) - ahora.date()).days} días)"
    elif e.get("checkout") and hoy == e["checkout"]:
        fase = "hoy es el día de salida"
    elif e.get("checkout") and hoy > e["checkout"]:
        fase = "la estancia ya ha terminado"
    else:
        fase = "estancia en curso"
    l = [
        f"- Apartamento: {e.get('alias') or e.get('alojamiento')}",
        f"- Huésped: {e.get('huesped') or '?'} (nombre de pila: {nombre_de_pila(e) or '?'})",
        f"- Personas: {formato.texto_personas(e)}",
        f"- Entrada: {_dia(e.get('checkin'))} desde las {cfg.hora_entrada_por_defecto}",
        f"- Salida: {_dia(e.get('checkout'))} hasta las {cfg.hora_salida_por_defecto}",
        f"- Ahora: {DIAS_ES[ahora.weekday()]} {ahora:%d/%m/%Y %H:%M} (Madrid) · {fase}",
    ]
    if e.get("hora_llegada"):
        l.append(f"- El huésped dijo que llega a las {e['hora_llegada']}")
    if e.get("hora_salida"):
        l.append(f"- El huésped dijo que sale a las {e['hora_salida']}")
    if e.get("acuerdo_entrada"):
        l.append(f"- Entrada ACORDADA por el anfitrión: {e['acuerdo_entrada']} (puedes confirmarla)")
    if e.get("acuerdo_salida"):
        l.append(f"- Salida ACORDADA por el anfitrión: {e['acuerdo_salida']} (puedes confirmarla)")
    if e.get("peticiones"):
        l.append("- Peticiones ya registradas: " + "; ".join(e["peticiones"]))
    l += [f"- {n}" for n in notas_horario(e, vecinos, cfg, siempre=True)]
    return "\n".join(l)


def notas_horario(e: dict, vecinos: dict, cfg: Config, siempre: bool = False) -> list[str]:
    """
    Notas (para el anfitrión) que ayudan a decidir si entrar antes o salir más tarde, con los datos del calendario.
    Sin `siempre`, solo si el huésped lo ha pedido y aún no hay acuerdo.
    """
    notas = []
    if siempre or (e.get("entrada_anticipada") and not e.get("acuerdo_entrada")):
        lim = vecinos.get("limpieza_entrada")
        if lim:
            fin_limpieza = _sumar(lim["desde"], cfg.limpieza_duracion_min)
            cuando = "ese mismo día" if lim.get("mismo_dia") else "el día anterior"
            notas.append(f"🧹 Entrada antes: el huésped anterior sale {cuando} a las {lim['salida']}; la limpieza puede ir de "
                         f"{lim['desde']} a {lim['hasta']} ({'cabe' if lim['ok'] else 'NO cabe'}). "
                         + (f"El piso estaría listo hacia las {fin_limpieza}." if lim.get("mismo_dia") else ""))
        elif e.get("checkin"):
            notas.append("🧹 Entrada antes: según el calendario, nadie sale de este piso ese día ni el anterior "
                         "(confirma con el equipo de limpieza).")
    if siempre or (e.get("salida_tardia") and not e.get("acuerdo_salida")):
        sig = vecinos.get("siguiente")
        if sig:
            notas.append(f"🔄 Salida tarde: el siguiente huésped entra ese mismo día ({e['checkout']}), "
                         f"llegada prevista {sig.get('hora_llegada') or 'no indicada'}; la limpieza termina a las {cfg.limpieza_fin}.")
        elif e.get("checkout"):
            notas.append("🔄 Salida tarde: según el calendario, nadie entra en este piso el día de salida.")
    return [n.strip() for n in notas]


def _sumar(hhmm: str, minutos: int) -> str:
    h, m = map(int, hhmm.split(":"))
    t = h * 60 + m + minutos
    return f"{t // 60:02d}:{t % 60:02d}"


def cuerpo_borrador(*, id_: str, tipo: str, e: dict, b: Borrador, recibido: datetime, mensajes: list[str],
                    responder_a: str, cuenta: str = "", asunto: str = "", momento: str = "", cuando: str = "", notas_extra: list[str] | None = None,
                    reemplaza: list[str] | None = None, pruebas: bool = False) -> dict:
    """Lo que se deja en la bandeja (ver bot/bandeja/nucleo.py)."""
    notas = [*(notas_extra or []), *b.notas]
    if b.error:
        notas.insert(0, f"⚠️ {b.error}")
    if not responder_a:
        notas.append("📋 No hay dirección de respuesta de Airbnb para esta reserva todavía: cópialo y pégalo en Airbnb.")
    return {
        "id": id_, "tipo": tipo, "momento": momento or None, "cuando": cuando,
        "codigo": e.get("codigo"), "piso": e.get("alias") or e.get("alojamiento"), "huesped": e.get("huesped"),
        "checkin": e.get("checkin"), "checkout": e.get("checkout"), "personas": formato.texto_personas(e),
        "recibido": recibido.isoformat(), "mensaje_huesped": "\n\n---\n\n".join(mensajes)[:6000],
        "asunto": asunto, "responder_a": responder_a, "cuenta": cuenta, "idioma": b.idioma,
        "texto": b.texto, "notas": notas, "faltan": b.faltan, "confianza": "baja" if b.error else b.confianza,
        "necesita_respuesta": b.necesita_respuesta, "reemplaza": reemplaza or [], "pruebas": pruebas,
    }


# ----------------------------------------------------------------------------
#  Agenda
# ----------------------------------------------------------------------------


def momentos_de_hoy(e: dict, base: BaseConocimiento, activos: list[str], ahora: datetime) -> list[str]:
    """Momentos de la agenda que tocan hoy para la reserva, si ya es su hora. Sin repetir: eso lo lleva el registro."""
    hoy, hora = ahora.date(), f"{ahora:%H:%M}"
    res = []
    for mid in activos:
        m = base.momentos.get(mid)
        if not m or m.dia in ("confirmacion", "con_bienvenida", ""):
            continue
        if m.fecha(e.get("checkin"), e.get("checkout")) == hoy and hora >= m.hora:
            res.append(mid)
    return res


def plantilla_momento(mid: str, e: dict, base: BaseConocimiento, activos: list[str]) -> tuple[str, list[str]]:
    """(plantilla rellenada, datos que faltan). La bienvenida lleva al final pedir la hora de salida si no la sabemos."""
    alias = e.get("alias") or ""
    co = date.fromisoformat(e["checkout"]) if e.get("checkout") else None
    dia_salida = f"{DAYS_EN[co.weekday()]} {co:%d/%m}" if co else ""
    texto, faltan = base.texto_momento(mid, alias, nombre_de_pila(e), dia_salida)
    if mid == "bienvenida" and "pedir_hora_salida" in activos and not (e.get("hora_salida") or e.get("acuerdo_salida")):
        extra, f2 = base.texto_momento("pedir_hora_salida", alias, nombre_de_pila(e), dia_salida)
        if extra:
            texto = f"{texto}\n\n{extra}"
        faltan += f2
    return texto, faltan


def es_reciente(fecha: datetime, ahora: datetime, cfg: Config) -> bool:
    return ahora - fecha <= timedelta(hours=cfg.respuestas_antiguedad_max_horas)


def reserva_viva(e: dict, ahora: datetime, dias_tras_salida: int = 7) -> bool:
    """Se contesta hasta unos días después de la salida (objetos olvidados, reseñas...)."""
    if e.get("cancelada"):
        return False
    fin = e.get("checkout") or e.get("checkin")
    return not fin or date.fromisoformat(fin) + timedelta(days=dias_tras_salida) >= ahora.date()

