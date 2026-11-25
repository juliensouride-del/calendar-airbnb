"""Tarjetas de Telegram: texto (HTML) y botones de cada borrador según su estado."""
from __future__ import annotations

import re
from datetime import date, datetime
from html import escape

LIMITE = 4096
URL_MENSAJES = "https://www.airbnb.es/hosting/messages"
RE_COMPLETAR = re.compile(r"\[\[\s*COMPLETAR", re.I)

NOMBRES_MOMENTO = {
    "pedir_hora_llegada": "gracias por reservar + hora de llegada",
    "dia_antes_llegada": "día antes de la llegada",
    "bienvenida": "bienvenida",
    "mitad_estancia": "mitad de estancia",
    "dia_antes_salida": "día antes de la salida",
    "despedida": "despedida",
}


def tiene_huecos(texto: str) -> bool:
    return bool(RE_COMPLETAR.search(texto or ""))


def se_puede_enviar(b: dict) -> bool:
    return bool(b.get("responder_a")) and bool((b.get("texto") or "").strip()) and not tiene_huecos(b.get("texto", ""))


def _corta(d: str | None) -> str:
    return f"{date.fromisoformat(d):%d/%m}" if d else "?"


def _hora(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return f"{datetime.fromisoformat(iso):%d/%m %H:%M}"
    except ValueError:
        return ""


def _recorta(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def tarjeta(b: dict, confirmar: bool = False) -> str:
    e = escape
    pruebas = "🧪 <b>PRUEBAS</b> · " if b.get("pruebas") else ""
    if b.get("tipo") == "momento":
        titulo = f"📅 <b>Toca enviar: {e(NOMBRES_MOMENTO.get(b.get('momento'), b.get('momento') or ''))}</b>"
    else:
        titulo = "💬 <b>Mensaje nuevo</b>"
    l = [pruebas + titulo,
         f"🏠 <b>{e(b.get('piso') or '?')}</b> · {e(b.get('huesped') or '?')} · {_corta(b.get('checkin'))} → {_corta(b.get('checkout'))}"
         + (f" · {e(b['personas'])}" if b.get("personas") else "")]
    if b.get("cuando"):
        l.append(f"🕒 Cuándo: {e(b['cuando'])}")
    if b.get("mensaje_huesped"):
        l += ["", f"<b>Huésped</b> · {_hora(b.get('recibido'))}",
              f"<blockquote expandable>{e(_recorta(b['mensaje_huesped'], 900))}</blockquote>"]
    if b.get("notas"):
        l += ["", "🔎 <b>Para ti</b>"] + [f"• {e(_recorta(n, 400))}" for n in b["notas"][:6]]
    if b.get("faltan"):
        l += ["", "⚠️ <b>Falta completar:</b> " + e("; ".join(b["faltan"][:6]))]
    estado = _linea_estado(b, confirmar)
    cab = f"✍️ <b>Borrador</b> ({e((b.get('idioma') or '?').upper())} · confianza {e(b.get('confianza') or '?')})"
    if b.get("editado"):
        cab += " · ✏️ editado"
    cuerpo_fijo = "\n".join(l) + "\n\n" + cab + "\n<pre></pre>\n\n" + estado
    sitio = LIMITE - len(cuerpo_fijo) - 80
    texto = b.get("texto") or "(sin borrador: contesta tú)"
    if len(e(texto)) > sitio:
        corte = sitio - 40
        while corte > 100 and len(e(texto[:corte])) > sitio - 40:
            corte -= 50
        texto = texto[:corte].rstrip() + "…\n(sigue: ábrelo en ✏️ Editar)"
    return "\n".join(l) + "\n\n" + cab + f"\n<pre>{e(texto)}</pre>\n\n" + estado


def _linea_estado(b: dict, confirmar: bool) -> str:
    est = b.get("estado")
    if est == "enviado":
        return f"✅ <b>Enviado</b> {_hora(b.get('enviado'))}" + (" (editado)" if b.get("editado") else "")
    if est == "simulado":
        return "🧪 <b>PRUEBAS:</b> no se ha enviado nada al huésped."
    if est == "descartado":
        return "❌ Descartado. Si hace falta, contesta tú en Airbnb."
    if est == "sustituido":
        return "↪️ Sustituido por un borrador más reciente de este huésped."
    if est == "copiado":
        return "✅ Marcado como pegado en Airbnb."
    if est == "error":
        return (f"⚠️ <b>No se pudo enviar:</b> {escape(_recorta(b.get('error') or '', 300))}\n"
                "Mira en Airbnb que no haya llegado antes de reintentar.")
    if confirmar:
        return f"❓ <b>¿Enviar ahora a {escape((b.get('huesped') or 'el huésped').split(' ')[0])}?</b>"
    if not b.get("responder_a"):
        return "📋 Toca el texto para copiarlo y pégalo en Airbnb (aún no hay hilo para enviarlo desde aquí)."
    if not (b.get("texto") or "").strip():
        return "✏️ No hay borrador: escribe la respuesta con Editar."
    if tiene_huecos(b.get("texto", "")):
        return "✏️ Rellena los huecos [[COMPLETAR]] con Editar antes de enviarlo."
    return "Toca ✅ Enviar para mandarlo tal cual, o ✏️ Editar para cambiarlo."


def botones(b: dict, url_base: str, confirmar: bool = False) -> list:
    est = b.get("estado")
    if est not in ("pendiente", "error"):
        return []
    i = b["id"]
    if confirmar:
        return [[{"text": "✅ Sí, enviar", "callback_data": f"a:ok:{i}"}, {"text": "↩️ No", "callback_data": f"a:vol:{i}"}]]
    editar = {"text": "✏️ Editar", "web_app": {"url": f"{url_base.rstrip('/')}/editar?id={i}"}}
    fila = [editar]
    if not b.get("responder_a"):
        fila.append({"text": "✅ Ya lo pegué", "callback_data": f"a:peg:{i}"})
    elif se_puede_enviar(b):
        fila.insert(0, {"text": "🔁 Reintentar" if est == "error" else "✅ Enviar", "callback_data": f"a:env:{i}"})
    abajo = [{"text": "❌ Descartar", "callback_data": f"a:des:{i}"}]
    if est == "error" and b.get("responder_a"):
        # Si el correo no llega a Airbnb, también se puede pegar a mano
        abajo.insert(0, {"text": "✅ Ya lo pegué", "callback_data": f"a:peg:{i}"})
    if not b.get("responder_a") or est == "error":
        abajo.append({"text": "🔗 Abrir Airbnb", "url": URL_MENSAJES})
    return [fila, abajo]
