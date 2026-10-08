"""
Extracción determinista de datos de los correos de Airbnb (plantillas en inglés).

Funciones puras: no hacen red ni E/S, se prueban con los correos reales de ./ejemplos.
Si Airbnb cambia sus plantillas, los textos que hay que ajustar están en PATRONES.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from html import unescape

# ----------------------------------------------------------------------------
#  Patrones
# ----------------------------------------------------------------------------

RE_MES = (r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
          r"|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\b\.?")
RE_DSEM = r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?,?"
RE_HORA = r"(\d{1,2}(?::\d{2})?\s*[AaPp]\.?[Mm]\.?|\d{1,2}:\d{2})"

I = re.IGNORECASE


class PATRONES:
    # Tipo de correo. Gana el marcador que aparezca ANTES en el texto (así un
    # huésped que escribe "cancel" en un mensaje no lo convierte en cancelación).
    tipos = {
        "cancelacion": re.compile(r"Reservation cancel+ed|Canceled: Reservation|had to cancel reservation"
                                  r"|cancel+ed (?:your|this|the) reservation", I),
        "confirmacion": re.compile(r"(?:New )?booking confirmed!|Reservation confirmed", I),
        "mensaje": re.compile(r"Reservation for\s", I),
        "modificacion": re.compile(r"Reservation (?:altered|alteration)|(?:Booking|Reservation) change"
                                   r"|Alteration (?:request|accepted)|changed (?:the|their|your) reservation", I),
        "pendiente": re.compile(r"Booking request|Reservation request|Request to book|Inquiry for|Pre-approve|Pending:", I),
    }

    codigo_confirmacion = re.compile(r"Confirmation code:?\s*(HM[A-Z0-9]{8})\b", I)
    codigo_cancelacion = re.compile(r"[Cc]ancel\w*\s+(?:the\s+|your\s+)?reservation\s+([A-Z0-9]{10})\b")
    codigo_generico = re.compile(r"\b(HM[A-Z0-9]{8})\b")

    nombre_confirmacion = [
        re.compile(r"booking confirmed!\s*(.+?)\s+arrives\b", I),
        re.compile(r"Reservation confirmed\s*[-–:]\s*(.+?)\s+arrives\b", I),
    ]
    nombre_cancelacion = re.compile(r"your guest\s+(.+?)\s+(?:had to\s+)?cancel", I)

    rol_huesped = re.compile(r"^(Booker|Guest|Co-?travell?er)$", I)        # línea con el rol de quien escribe
    fin_mensaje = re.compile(r"^(Reply|You can also respond)\b", I)       # final de los mensajes del huésped
    inicio_mensajes = re.compile(r"always communicate through Airbnb\.?", I)

    tipo_alojamiento = re.compile(r"^(Entire home/apt|Entire [a-z ]+|Private room|Shared room|Hotel room)$", I)
    linea_resenas = re.compile(r"Identity verified|\b\d+\s+reviews?\b", I)
    linea_enviar_mensaje = re.compile(r"^Send .+ a Message", I)
    linea_cabecera_confirmacion = re.compile(r"booking confirmed!|Reservation confirmed", I)
    linea_confirmar_detalles = re.compile(r"^Send a message to confirm", I)
    procedencia = re.compile(r"^[^.!?¿¡]{2,50},\s*[^.!?¿¡,]{2,40}$")       # "Ciudad, País"

    noches = re.compile(r"x\s*(\d+)\s+nights?", I)
    listing_id = re.compile(r"Listing\s*#\s*(\d+)", I)
    id_anuncio_url = re.compile(r"airbnb\.[a-z.]+/rooms/(\d{5,})", I)     # se busca ANTES de quitar las URLs
    reaccion = re.compile(r"^Reacted\s+.{1,12}\s+to\s+\"", I)               # "Reacted ❤️ to ..." no aporta nada
    linea_hosted_by = re.compile(r"\bhosted by\b", I)
    huespedes = re.compile(r"Guests?:?\s+(\d+\s+(?:adults?|guests?)(?:\s*,\s*\d+\s+(?:adults?|child(?:ren)?|infants?|pets?))*)", I)


MESES_EN = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
            "sep": 9, "oct": 10, "nov": 11, "dec": 12}
DIAS_EN = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}   # = date.weekday()

# ----------------------------------------------------------------------------
#  Resultado
# ----------------------------------------------------------------------------


@dataclass
class Huespedes:
    adultos: int | None = 0
    ninos: int | None = 0
    bebes: int | None = 0
    mascotas: int | None = 0
    total: int = 0


@dataclass
class Mensaje:
    autor: str | None
    texto: str


@dataclass
class DatosCorreo:
    tipo: str                           # confirmacion | cancelacion | mensaje | modificacion | pendiente | desconocido
    codigo: str | None = None           # código de reserva (HM...)
    alojamiento: str | None = None      # nombre del anuncio
    alias: str | None = None
    alojamiento_configurado: bool = False
    huesped: str | None = None
    checkin: str | None = None          # 'yyyy-mm-dd'
    checkout: str | None = None
    hora_checkin: str | None = None     # 'HH:MM' (hora oficial del anuncio)
    hora_checkout: str | None = None
    huespedes: Huespedes | None = None
    mensajes: list[Mensaje] = field(default_factory=list)   # texto libre escrito por el huésped
    avisos: list[str] = field(default_factory=list)         # advertencias del parser
    faltan: list[str] = field(default_factory=list)         # campos obligatorios no encontrados


@dataclass
class _Fecha:
    """Fecha etiquetada encontrada en el texto (Check-in / Checkout)."""
    dsem: str | None
    mes: str
    dia: str
    anio: str | None
    hora: str | None
    fin: int

# ----------------------------------------------------------------------------
#  Punto de entrada
# ----------------------------------------------------------------------------


def parsear_correo(asunto: str, cuerpo: str, fecha_ref: str, alojamientos: list) -> DatosCorreo:
    """
    Analiza un correo de Airbnb.
    fecha_ref: fecha del correo 'yyyy-mm-dd' (para deducir el año cuando falta).
    alojamientos: lista de Alojamiento de la configuración.
    """
    id_anuncio = primer_grupo(PATRONES.id_anuncio_url, cuerpo or "")
    texto = limpiar_texto(cuerpo or "")
    lineas = [l.strip() for l in texto.split("\n") if l.strip()]
    plano = " ".join(lineas)
    asunto = re.sub(r"\s+", " ", limpiar_texto(asunto or "")).strip()

    r = DatosCorreo(tipo=detectar_tipo(asunto, plano))
    if r.tipo == "confirmacion":
        _parsear_confirmacion(r, lineas, plano, asunto, fecha_ref)
    elif r.tipo == "cancelacion":
        _parsear_cancelacion(r, lineas, plano, asunto, fecha_ref)
    elif r.tipo == "mensaje":
        _parsear_mensaje(r, lineas, plano, fecha_ref)
    else:
        return r

    # Alojamiento: primero contra la configuración; si no, lo que deduzca el tipo de correo
    conocido = buscar_alojamiento(asunto + " " + plano, alojamientos, id_anuncio, r.alojamiento)
    if conocido:
        r.alojamiento = conocido.nombre
        r.alias = conocido.alias or conocido.nombre
        r.alojamiento_configurado = True
    elif r.alojamiento:
        r.alias = r.alojamiento
        r.avisos.append(f'Alojamiento no configurado en config.toml: "{r.alojamiento}"')

    obligatorios = {"confirmacion": ["codigo", "alojamiento", "checkin", "checkout"],
                    "cancelacion": ["codigo"],
                    "mensaje": ["alojamiento", "checkin"]}[r.tipo]
    r.faltan = [c for c in obligatorios if not getattr(r, c)]
    return r

# ----------------------------------------------------------------------------
#  Limpieza y tipo
# ----------------------------------------------------------------------------


def limpiar_texto(t: str) -> str:
    t = re.sub(r"\r\n?", "\n", str(t))
    t = re.sub(r"[  -   　\t]", " ", t)
    t = re.sub(r"[​-‍⁠﻿͏­]", "", t)
    t = re.sub(r"[‒-―−]", "–", t)
    # URLs (en texto plano Airbnb suele incluirlas entre paréntesis o corchetes)
    t = re.sub(r"[(<\[]\s*https?://[^\s)>\]]*\s*[)>\]]", " ", t)
    t = re.sub(r"https?://\S+", " ", t)
    return re.sub(r" {2,}", " ", t)


def html_a_texto(html: str) -> str:
    """Respaldo cuando el correo no trae parte de texto plano."""
    t = re.sub(r"<(style|script|head)[\s\S]*?</\1>", " ", html or "", flags=I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=I)
    t = re.sub(r"</(p|div|tr|td|th|h[1-6]|li|table)>", "\n", t, flags=I)
    t = re.sub(r"<[^>]+>", " ", t)
    return unescape(t)


def detectar_tipo(asunto: str, plano: str) -> str:
    for t in (asunto, plano[:1500]):          # el asunto tiene prioridad
        encontrados = [(m.start(), tipo) for tipo, re_ in PATRONES.tipos.items() if (m := re_.search(t))]
        if encontrados:
            return min(encontrados, key=lambda x: x[0])[1]
    return "desconocido"

# ----------------------------------------------------------------------------
#  Por tipo de correo
# ----------------------------------------------------------------------------


def _parsear_confirmacion(r: DatosCorreo, lineas, plano, asunto, fecha_ref):
    r.codigo = primer_grupo(PATRONES.codigo_confirmacion, plano) or primer_grupo(PATRONES.codigo_generico, plano)

    nombre_corto = None
    for re_ in PATRONES.nombre_confirmacion:
        nombre_corto = primer_grupo(re_, asunto) or primer_grupo(re_, plano)
        if nombre_corto:
            break

    _extraer_bloque_fechas_y_huespedes(r, plano, sumar_dias(fecha_ref, -2))

    # Coherencia con "€X x N nights"
    noches = int(primer_grupo(PATRONES.noches, plano) or 0)
    if noches and r.checkin:
        calc = sumar_dias(r.checkin, noches)
        if not r.checkout:
            r.checkout = calc
        elif r.checkout != calc:
            r.avisos.append(f"Check-out ({r.checkout}) no cuadra con {noches} noches")

    # Nombre completo y mensaje inicial: bloque entre la cabecera y "Send X a Message"
    i_enviar = _indice(lineas, lambda l, i: PATRONES.linea_enviar_mensaje.search(l))
    i_resenas = _indice(lineas, lambda l, i: (i_enviar < 0 or i < i_enviar) and PATRONES.linea_resenas.search(l))
    r.huesped = nombre_corto
    if i_resenas > 0:
        candidato = lineas[i_resenas - 1]
        if not nombre_corto or candidato.lower().startswith(nombre_corto.lower()):
            r.huesped = candidato
    if i_enviar > 0:
        if i_resenas >= 0:
            ini = i_resenas + 1
            # La línea siguiente a las reseñas suele ser la procedencia ("Ciudad, País")
            if ini < i_enviar and PATRONES.procedencia.search(lineas[ini]):
                ini += 1
        else:
            ini = _indice(lineas, lambda l, i: PATRONES.linea_cabecera_confirmacion.search(l)) + 1
            while ini < i_enviar and (PATRONES.linea_confirmar_detalles.search(lineas[ini]) or lineas[ini] == r.huesped):
                ini += 1
        texto = "\n".join(lineas[ini:i_enviar]).strip()
        if texto:
            r.mensajes.append(Mensaje(r.huesped, texto))

    # Alojamiento (respaldo): línea anterior al tipo de alojamiento
    i_tipo = _indice(lineas, lambda l, i: PATRONES.tipo_alojamiento.search(l))
    if i_tipo > 0:
        r.alojamiento = lineas[i_tipo - 1]


def _parsear_cancelacion(r: DatosCorreo, lineas, plano, asunto, fecha_ref):
    r.codigo = (primer_grupo(PATRONES.codigo_cancelacion, plano)
                or primer_grupo(PATRONES.codigo_cancelacion, asunto)
                or primer_grupo(PATRONES.codigo_generico, asunto + " " + plano))
    r.huesped = primer_grupo(PATRONES.nombre_cancelacion, plano)

    rango = extraer_rango_fechas(plano, sumar_dias(fecha_ref, -2))
    if rango:
        r.checkin, r.checkout = rango
    n = primer_grupo(re.compile(r"\b(\d+)\s+guests?\b", I), plano)
    if n:
        r.huespedes = Huespedes(adultos=None, ninos=None, bebes=None, mascotas=None, total=int(n))

    # Alojamiento (respaldo): línea anterior a "Listing #..."
    i_listing = _indice(lineas, lambda l, i: PATRONES.listing_id.search(l))
    if i_listing > 0:
        r.alojamiento = lineas[i_listing - 1]


def _parsear_mensaje(r: DatosCorreo, lineas, plano, fecha_ref):
    # Los mensajes pueden llegar después de la estancia → margen de 30 días al deducir el año
    fecha_minima = sumar_dias(fecha_ref, -30)
    _extraer_bloque_fechas_y_huespedes(r, plano, fecha_minima)

    # Respaldo de fechas: cabecera "Reservation for X, Oct 9 – 13"
    if not r.checkin:
        rango = extraer_rango_fechas(plano, fecha_minima)
        if rango:
            r.checkin = rango[0]
            r.checkout = r.checkout or rango[1]

    r.mensajes = extraer_mensajes_huesped(lineas, plano)
    if r.mensajes:
        r.huesped = r.mensajes[0].autor

    # Alojamiento (respaldo): cabecera, o línea anterior a "... hosted by ..."
    re_cab = re.compile(r"^Reservation for\s+(.+),\s+" + RE_MES + r"\s+\d{1,2}\s*[–-]", I)
    for l in lineas:
        if m := re_cab.search(l):
            r.alojamiento = m.group(1).strip()
            break
    if not r.alojamiento:
        i = _indice(lineas, lambda l, i: PATRONES.linea_hosted_by.search(l))
        if i > 0:
            r.alojamiento = lineas[i - 1]

# ----------------------------------------------------------------------------
#  Bloques comunes
# ----------------------------------------------------------------------------


def _re_fecha_etiquetada(etiqueta: str) -> re.Pattern:
    return re.compile(etiqueta + r":?\s+(?:" + RE_DSEM + r"\s+)?" + RE_MES +
                      r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\s+" + RE_HORA, I)


RE_CHECKIN = _re_fecha_etiquetada(r"Check-?\s?in")
RE_CHECKOUT = _re_fecha_etiquetada(r"Check-?\s?out")


def _fecha_de(m: re.Match) -> _Fecha:
    return _Fecha(m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.end())


def _extraer_bloque_fechas_y_huespedes(r: DatosCorreo, plano: str, fecha_minima: str):
    """Bloque "Check-in <fecha> <hora> Checkout <fecha> <hora> Guests N adults..." """
    # El bloque oficial va DESPUÉS de cualquier texto del huésped → se toma el último
    entradas = list(RE_CHECKIN.finditer(plano))
    f_in = f_out = None
    if entradas:
        m_in = entradas[-1]
        f_in = _fecha_de(m_in)
        m_out = next((m for m in RE_CHECKOUT.finditer(plano) if m.start() >= m_in.end()), None)
        f_out = _fecha_de(m_out) if m_out else None
    else:
        # Formato real del texto plano de Airbnb, en dos columnas:
        #   "Check-in Checkout Tue, Oct 20 Fri, Oct 23 3:00 PM 11:00 AM"
        #   "Check-in Checkout WEDNESDAY SATURDAY September 16, 2026 September 19, 2026 3:00 PM 11:00 AM"
        cols = _extraer_bloque_dos_columnas(plano)
        if cols:
            f_in, f_out = cols
    if not f_in:
        r.avisos.append("No se encontró el bloque de Check-in")
        return

    r.checkin = _fecha_desde(f_in, fecha_minima, r)
    r.hora_checkin = normalizar_hora(f_in.hora)
    fin_bloque = f_in.fin
    if f_out:
        r.checkout = _fecha_desde(f_out, sumar_dias(r.checkin, 1) if r.checkin else fecha_minima, r)
        r.hora_checkout = normalizar_hora(f_out.hora)
        fin_bloque = max(fin_bloque, f_out.fin)

    if m := PATRONES.huespedes.search(plano[fin_bloque:]):
        r.huespedes = parsear_huespedes(m.group(1))


def _extraer_bloque_dos_columnas(plano: str):
    """Bloque en dos columnas (entrada y salida intercaladas por filas)."""
    cabs = list(re.finditer(r"Check-?\s?in\s+Check-?\s?out\b", plano, I))
    if not cabs:
        return None
    ini = cabs[-1].end()
    resto = plano[ini:ini + 300]
    m_fin = re.search(r"\bGuests?\b|Confirmation code", resto, I)
    seg = resto[:m_fin.start()] if m_fin else resto

    fechas = list(re.finditer(RE_MES + r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", seg, I))
    if len(fechas) < 2:
        return None
    fin_fechas = fechas[1].end()
    dsem = [m.group(1) for m in re.finditer(r"\b" + RE_DSEM, seg[:fin_fechas], I)]
    horas = [m.group(1) for m in re.finditer(RE_HORA, seg[fin_fechas:])]
    fin = ini + (seg.rfind(horas[1]) + len(horas[1]) if len(horas) >= 2 else fin_fechas)

    def fecha(k):
        return _Fecha(dsem[k] if len(dsem) >= 2 else None, fechas[k].group(1), fechas[k].group(2),
                      fechas[k].group(3), horas[k] if k < len(horas) else None, fin)
    return fecha(0), fecha(1)


def _fecha_desde(f: _Fecha, fecha_minima: str, r: DatosCorreo) -> str | None:
    mes, dia = numero_mes(f.mes), int(f.dia)
    if f.anio:
        anio = int(f.anio)
        if not es_fecha_valida(anio, mes, dia):
            return None
        iso = fecha_iso(anio, mes, dia)
        if f.dsem and DIAS_EN[f.dsem[:3].lower()] != dia_semana(iso):
            r.avisos.append("El día de la semana no coincide con " + iso)
        return iso
    iso = inferir_anio(mes, dia, f.dsem, fecha_minima)
    if not iso:
        r.avisos.append(f"No se pudo deducir el año de {f.mes} {f.dia}")
    return iso


def extraer_rango_fechas(plano: str, fecha_minima: str):
    """
    Rango "Feb 1 – 4, 2027" / "Jan 30 – Feb 2, 2027" / "Oct 9 – 13" → (checkin, checkout).
    Prefiere el primer rango que traiga año.
    """
    re_ = re.compile(RE_MES + r"\s+(\d{1,2})\s*[–-]\s*(?:" + RE_MES + r"\s+)?(\d{1,2})(?:,\s*(\d{4}))?")
    todos = list(re_.finditer(plano))
    if not todos:
        return None
    m = next((x for x in todos if x.group(5)), todos[0])
    mes1, dia1 = numero_mes(m.group(1)), int(m.group(2))
    mes2 = numero_mes(m.group(3)) if m.group(3) else mes1
    dia2 = int(m.group(4))
    if m.group(5):
        anio2 = int(m.group(5))
        anio1 = anio2 - 1 if mes1 > mes2 else anio2
        if not es_fecha_valida(anio1, mes1, dia1) or not es_fecha_valida(anio2, mes2, dia2):
            return None
        return fecha_iso(anio1, mes1, dia1), fecha_iso(anio2, mes2, dia2)
    checkin = inferir_anio(mes1, dia1, None, fecha_minima)
    checkout = inferir_anio(mes2, dia2, None, sumar_dias(checkin, 1)) if checkin else None
    return checkin, checkout


def parsear_huespedes(txt: str) -> Huespedes:
    h = Huespedes()
    for m in re.finditer(r"(\d+)\s+(adult|child|infant|pet|guest)", txt, I):
        n, k = int(m.group(1)), m.group(2).lower()
        if k in ("adult", "guest"):
            h.adultos += n
        elif k == "child":
            h.ninos += n
        elif k == "infant":
            h.bebes += n
        else:
            h.mascotas += n
    h.total = h.adultos + h.ninos + h.bebes
    return h


def extraer_mensajes_huesped(lineas: list[str], plano: str) -> list[Mensaje]:
    """
    Mensajes del huésped. Formato:  Nombre / Nombre / Booker / <texto...> / Reply
    Puede haber varios bloques seguidos en el mismo correo.
    """
    roles = [i for i, l in enumerate(lineas) if i > 0 and PATRONES.rol_huesped.search(l)]
    mensajes = []
    for k, i_rol in enumerate(roles):
        fin = len(lineas)
        i_fin = next((j for j in range(i_rol + 1, len(lineas)) if PATRONES.fin_mensaje.search(lineas[j])), -1)
        if i_fin > 0:
            fin = min(fin, i_fin)
        if k + 1 < len(roles):
            sig = roles[k + 1]
            inicio_sig = sig - 2 if sig >= 2 and lineas[sig - 2] == lineas[sig - 1] else sig - 1
            fin = min(fin, inicio_sig)
        texto = "\n".join(l for l in lineas[i_rol + 1:fin] if not PATRONES.reaccion.search(l)).strip()
        if texto:
            mensajes.append(Mensaje(lineas[i_rol - 1], texto))
    if roles:
        return mensajes                # hay bloques de mensaje (quizá solo reacciones)

    # Respaldo: texto entre "always communicate through Airbnb." y "Reply"
    if m := PATRONES.inicio_mensajes.search(plano):
        resto = plano[m.end():]
        m_fin = re.search(r"\bReply\b|You can also respond", resto, I)
        texto = (resto[:m_fin.start()] if m_fin else resto[:3000]).strip()
        if texto:
            return [Mensaje(None, texto)]
    return []


def buscar_alojamiento(texto: str, alojamientos: list, id_anuncio: str | None, nombre_detectado: str | None):
    """
    Busca el alojamiento configurado por (en este orden):
     1. ID del anuncio (de los enlaces airbnb.com/rooms/...)
     2. nombre u otros_nombres contenidos en el texto del correo
     3. nombre truncado por Airbnb ("APARTAMENTOS DE JULIAN, ANTON MARTIN - APARTAME...")
    """
    if id_anuncio:
        for a in alojamientos:
            if a.id_anuncio and str(a.id_anuncio) == str(id_anuncio):
                return a

    def nombres(a):
        return [k for k in (clave_normalizada(n) for n in [a.nombre, *a.otros_nombres]) if k]

    t = clave_normalizada(texto)
    mejor, largo = None, 0
    for a in alojamientos:
        for k in nombres(a):
            if k in t and len(k) > largo:
                mejor, largo = a, len(k)
    if mejor:
        return mejor

    det = clave_normalizada(nombre_detectado)
    prefijo = re.sub(r"(\.\.\.|…)$", "", det).strip()
    if prefijo != det and len(prefijo) >= 10:
        return next((a for a in alojamientos if any(k.startswith(prefijo) for k in nombres(a))), None)
    return None

# ----------------------------------------------------------------------------
#  Utilidades de fecha y texto
# ----------------------------------------------------------------------------


def clave_normalizada(s) -> str:
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()


def primer_grupo(re_: re.Pattern, texto: str) -> str | None:
    m = re_.search(texto or "")
    return m.group(1).strip() if m else None


def _indice(lineas: list[str], cond) -> int:
    return next((i for i, l in enumerate(lineas) if cond(l, i)), -1)


def numero_mes(txt: str) -> int:
    return MESES_EN[txt[:3].lower()]


def fecha_iso(y: int, m: int, d: int) -> str:
    return f"{y:04d}-{m:02d}-{d:02d}"


def sumar_dias(iso: str, n: int) -> str:
    return (date.fromisoformat(iso) + timedelta(days=n)).isoformat()


def dia_semana(iso: str) -> int:
    return date.fromisoformat(iso).weekday()


def es_fecha_valida(y: int, m: int, d: int) -> bool:
    try:
        date(y, m, d)
        return True
    except (ValueError, TypeError):
        return False


def inferir_anio(mes: int, dia: int, dsem_txt: str | None, fecha_minima: str) -> str | None:
    """Primera fecha >= fecha_minima con ese mes y día (y día de la semana, si se conoce)."""
    y0 = int(fecha_minima[:4])
    dsem = DIAS_EN.get(dsem_txt[:3].lower()) if dsem_txt else None
    for exigir_dia in (True, False):
        for y in range(y0, y0 + 4):
            if not es_fecha_valida(y, mes, dia):
                continue
            iso = fecha_iso(y, mes, dia)
            if iso < fecha_minima:
                continue
            if exigir_dia and dsem is not None and dia_semana(iso) != dsem:
                continue
            return iso
    return None


def normalizar_hora(txt: str | None) -> str | None:
    """'3:00 PM' → '15:00', '11 AM' → '11:00', '15:30' → '15:30'"""
    if not txt:
        return None
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*([AaPp])?", txt.strip())
    if not m:
        return None
    h, minutos = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        pm = m.group(3).lower() == "p"
        h = (12 if pm else 0) if h == 12 else h + 12 if pm else h
    if h > 23 or minutos > 59:
        return None
    return f"{h:02d}:{minutos:02d}"
