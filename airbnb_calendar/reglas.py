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
from .parser import DatosCorreo, clave_normalizada, normalizar_hora

PRIORIDAD_FUENTE = {"regla": 1, "palabras": 2, "huesped": 3}   # lo que dice el huésped manda
MADRUGADA = "06:00"      # una llegada antes de esta hora es de noche (tarde), no una entrada anticipada
DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def crear_estado(datos: DatosCorreo | None = None) -> dict:
    e = {
        "v": 1,
        "codigo": None, "alojamiento": None, "alias": None, "huesped": None,
        "checkin": None, "checkout": None,
        "hora_checkin": None, "hora_checkout": None,        # horas oficiales del anuncio
        "adultos": None, "ninos": None, "bebes": None, "mascotas": None, "total": None,
        "hora_llegada": None, "hora_salida": None,          # lo que dice el huésped
        "entrada_anticipada": None, "salida_tardia": None,  # lo que PIDE: 'HH:MM' o 'si' (sin hora concreta)
        "acuerdo_entrada": None, "acuerdo_salida": None,    # lo acordado por el anfitrión (#entrada / #salida)
        "sofa": None, "cuna": None,                          # {"v": bool, "f": "huesped"|"palabras"|"regla"}
        "peticiones": [],                                    # otras peticiones especiales
        "sin_analizar": False,                               # algún mensaje no lo pudo analizar la IA
        "cancelada": False,
        "avisado": {"sofa": False, "cuna": False, "peticiones": [], "hora_llegada": None, "hora_salida": None,
                    "entrada_anticipada": None, "salida_tardia": None},
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


def fusionar_analisis(e: dict, a: dict | None, fuente: str, fecha_txt: str = "", cfg: Config | None = None) -> dict:
    """
    Fusiona el análisis de un mensaje. Los mensajes se procesan en orden: lo último que dice el huésped manda.
    a: {hora_llegada, hora_salida, entrada_anticipada, salida_tardia, sofa_cama, cuna, otras_peticiones}
    fuente: 'huesped' (Gemini) | 'palabras' (respaldo por palabras clave)
    cfg: con él, una hora de llegada (salida) anterior (posterior) al horario estándar cuenta como
         petición de entrar antes (salir después), y una posterior (anterior) la anula.
    """
    if not a:
        return e
    if es_hora_valida(a.get("hora_llegada")):
        e["hora_llegada"] = a["hora_llegada"]
    if es_hora_valida(a.get("hora_salida")):
        e["hora_salida"] = a["hora_salida"]
    estandar = (cfg.hora_entrada_por_defecto, cfg.hora_salida_por_defecto) if cfg else (None, None)
    for k, hora, std, antes in (("entrada_anticipada", a.get("hora_llegada"), estandar[0], True),
                                ("salida_tardia", a.get("hora_salida"), estandar[1], False)):
        e[k] = _fusionar_solicitud(e.get(k), a.get(k), hora, std, antes)
    e["sofa"] = _fusionar_marca(e["sofa"], a.get("sofa_cama"), fuente)
    e["cuna"] = _fusionar_marca(e["cuna"], a.get("cuna"), fuente)

    for p in a.get("otras_peticiones") or []:
        txt = str(p or "").strip()[:150]
        if txt and not any(clave_normalizada(x) == clave_normalizada(txt) for x in e["peticiones"]):
            e["peticiones"].append(txt)
    e["peticiones"] = e["peticiones"][-10:]
    return e


def _fusionar_solicitud(actual: str | None, pedida, hora_dicha, estandar: str | None, antes: bool) -> str | None:
    """
    Petición de entrar antes / salir después tras un mensaje nuevo (lo más reciente manda):
     - pide una hora concreta → esa hora; 'si' sin hora → no borra una hora pedida antes; 'no' → la anula.
     - si no pide nada pero dice a qué hora llega (sale): antes (después) del horario estándar es en la
       práctica una petición; dentro del horario, la petición anterior ya no hace falta.
    """
    if es_hora_valida(pedida):
        return pedida
    if pedida == "si":
        return actual or "si"
    if pedida == "no":
        return None
    hora = normalizar_hora(hora_dicha) if es_hora_valida(hora_dicha) else None
    if hora and estandar:
        if antes and hora < MADRUGADA:
            return None                     # llegar a las 00:30 es llegar tarde (de noche), no pedir entrar antes
        return hora if (hora < estandar if antes else hora > estandar) else None
    return actual


def resetear_mensajes(e: dict) -> dict:
    """
    Olvida lo deducido de los mensajes (al re-procesar una reserva desde su confirmación), para que se
    reconstruya con el análisis actual y no queden datos antiguos o mal interpretados. Se conservan los
    datos de la reserva, los acuerdos del anfitrión y lo ya avisado.
    """
    vacio = crear_estado()
    e.pop("notas", None)                          # campo de versiones anteriores
    for k in ("hora_llegada", "hora_salida", "entrada_anticipada", "salida_tardia", "sofa", "cuna", "peticiones",
              "sin_analizar"):
        e[k] = vacio[k]
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


ETIQUETA_ACUERDO = re.compile(r"#\s*(entrada|salida)\s*:?\s*(\d{1,2}(?:\s*[:.h]\s*\d{2})?)(?!\d)", re.I)


def leer_acuerdos(descripcion: str | None) -> dict:
    """
    Horas acordadas por el anfitrión, escritas a mano en la descripción del evento:
    '#entrada 12:00' / '#salida 13:30' (también '#entrada 12h', '#salida 13.30').
    → {'acuerdo_entrada': 'HH:MM' | None, 'acuerdo_salida': 'HH:MM' | None}
    """
    texto = re.sub(r"<[^>]+>|&nbsp;", " ", descripcion or "")     # Calendar guarda HTML si se edita en la web
    r = {"acuerdo_entrada": None, "acuerdo_salida": None}
    for tipo, hora in ETIQUETA_ACUERDO.findall(texto):
        h = normalizar_hora(re.sub(r"\s*[.h]\s*", ":", hora.replace(" ", "")))
        if h:
            r[f"acuerdo_{tipo.lower()}"] = h
    return r


def hora_entrada(e: dict, cfg: Config) -> str:
    """
    Hora de entrada en el calendario. Manda la acordada por el anfitrión (#entrada HH:MM), aunque no
    deje tiempo de limpieza. Si no la hay, el horario estándar (config.toml): si el huésped dice que
    llega más tarde se usa su hora; si pide llegar antes, no.
    """
    if e.get("acuerdo_entrada"):
        return e["acuerdo_entrada"]
    estandar = cfg.hora_entrada_por_defecto
    llegada = normalizar_hora(e["hora_llegada"])
    return max(llegada, estandar) if llegada else estandar


def hora_salida(e: dict, cfg: Config) -> str:
    """Hora de salida: la acordada (#salida HH:MM); si no, la estándar, o la del huésped si sale antes."""
    if e.get("acuerdo_salida"):
        return e["acuerdo_salida"]
    estandar = cfg.hora_salida_por_defecto
    salida = normalizar_hora(e["hora_salida"])
    return min(salida, estandar) if salida else estandar

# ----------------------------------------------------------------------------
#  Limpieza entre reservas (rotación)
# ----------------------------------------------------------------------------


def evaluar_limpieza(anterior: dict | None, e: dict, cfg: Config) -> dict | None:
    """
    Ventana de limpieza entre la salida de `anterior` y la entrada de `e` (mismo alojamiento).

    Reglas: la limpieza dura `cfg.limpieza_duracion_min`; el equipo trabaja de
    `cfg.limpieza_inicio` a `cfg.limpieza_fin` (solo mañanas) y entrega las llaves
    en persona a la hora de entrada del huésped.
    Devuelve None si no hay rotación (sin salida el mismo día ni el día anterior).
    """
    if not anterior or anterior.get("cancelada") or e["cancelada"] or not e["checkin"] or not anterior.get("checkout"):
        return None
    dias = (date.fromisoformat(e["checkin"]) - date.fromisoformat(anterior["checkout"])).days
    if dias not in (0, 1):
        return None
    sale, entra = _min(hora_salida(anterior, cfg)), _min(hora_entrada(e, cfg))
    ini_j, fin_j, dur = _min(cfg.limpieza_inicio), _min(cfg.limpieza_fin), cfg.limpieza_duracion_min
    if dias == 0:      # mismo día: entre la salida y la entrada, dentro de la jornada
        opciones = [(e["checkin"], max(sale, ini_j), min(entra, fin_j))]
    else:              # salida ayer: la tarde de ayer (hasta el fin de jornada) o la mañana de hoy
        opciones = [(anterior["checkout"], max(sale, ini_j), fin_j), (e["checkin"], ini_j, min(entra, fin_j))]
    dia, desde, hasta = max(opciones, key=lambda o: o[2] - o[1])
    ok = hasta - desde >= dur
    r = {"ok": ok, "mismo_dia": dias == 0, "dia": dia, "salida": _hhmm(sale), "entrada": _hhmm(entra),
         "desde": _hhmm(desde), "hasta": _hhmm(max(hasta, desde)), "margen_min": hasta - desde}
    if not ok:
        motivos = []
        if dias == 0 and max(sale, ini_j) + dur > fin_j:
            motivos.append(f"la salida ({_hhmm(sale)}) es demasiado tarde para acabar antes de las {cfg.limpieza_fin}")
        if entra < (max(sale, ini_j) if dias == 0 else ini_j) + dur:
            motivos.append(f"la entrada ({_hhmm(entra)}) no deja {dur // 60} h de limpieza")
        r["motivo"] = "; ".join(motivos) or f"solo hay {max(hasta - desde, 0)} min"
    return r


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


def _min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm(minutos: int) -> str:
    return f"{minutos // 60:02d}:{minutos % 60:02d}"

# ----------------------------------------------------------------------------
#  Formato de los eventos (un único evento por reserva: de la entrada a la salida)
# ----------------------------------------------------------------------------


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
#  Respaldo sin IA: palabras clave (sin horas; sí detecta si pide entrar antes / salir después)
# ----------------------------------------------------------------------------


PALABRAS_CUNA = re.compile(r"\b(crib|cot|cuna|berceau|lit (?:pour |de )?b[ée]b[ée]|lit parapluie|baby ?bed|travel cot"
                           r"|babybett|kinderbett|reisebett|culla|lettino)\b", re.I)
# Entrada anticipada / salida tardía (en, es, fr, de, it, pt)
PALABRAS_ENTRADA = re.compile(
    r"early\s*check[- ]?in|check[- ]?in\s+(?:a\s+(?:bit|little)\s+)?(?:early|earlier|sooner)"
    r"|arriv\w*\s+(?:a\s+(?:bit|little)\s+)?(?:early|earlier|sooner)|(?:drop|leave)\s+(?:off\s+)?(?:our|the|my)?\s*(?:luggage|bags|suitcases)"
    r"|(?:entrada|check[- ]?in|llegada)\s+(?:anticipad[ao]|temprano|antes)|(?:llegar|entrar)\s+(?:m[áa]s\s+)?(?:antes|temprano|pronto)"
    r"|dejar\s+(?:las\s+)?(?:maletas|equipaje)|(?:arriv\w+|check[- ]?in|entr\w+)\s+plus\s+t[ôo]t"
    r"|fr[üu]her\s+(?:einchecken|ankommen|anreisen)|fr[üu]hes?\s+check[- ]?in|(?:arrivare|entrare)\s+prima|chegar\s+mais\s+cedo", re.I)
PALABRAS_SALIDA = re.compile(
    r"late\s*check[- ]?out|check[- ]?out\s+(?:a\s+(?:bit|little)\s+)?later|leave\s+(?:a\s+(?:bit|little)\s+)?later"
    r"|(?:salida|check[- ]?out)\s+(?:tard[íi]a|m[áa]s\s+tarde)|salir\s+m[áa]s\s+tarde|(?:partir|d[ée]part)\s+plus\s+tard"
    r"|sp[äa]te[rs]?\s+(?:auschecken|check[- ]?out|abreisen)|(?:uscire|partire)\s+pi[ùu]\s+tardi|sair\s+mais\s+tarde", re.I)
PALABRAS_SOFA = re.compile(r"\b(sofa[ -]?bed|sof[áa][ -]?cama|canap[ée][ -]?lit|schlafsofa|divano letto|sof[áa]"
                           r"|canap[ée]|couch)\b", re.I)


# Mensajes que son SOLO cortesía ("Thanks!", "Perfecto 👍", "Merci beaucoup") no necesitan la IA.
# Criterio muy prudente: todas las palabras deben estar en esta lista, sin números, sin preguntas
# y como mucho 8 palabras. Con cualquier otra palabra (un nombre, "llegamos", "maleta"...) se analiza.
PALABRAS_CORTESIA = set("""
thanks thank thx ty you so much very many lot a again in advance ok okay okey k perfect great super cool nice
awesome wonderful amazing excellent good fine sounds lovely noted got it understood sure yes yeah alright all right
hi hello hey see soon later bye cheers regards best kind welcome np no problem
gracias muchas mil muchisimas muchísimas vale perfecto perfecta genial estupendo estupenda excelente bien bueno
buena buenas buenos hola adios adiós hasta luego pronto saludos claro entendido de nada si sí ok igualmente
merci beaucoup bien parfait super génial genial top bonjour bonsoir salut à a bientôt bientot oui daccord d'accord
danke vielen schön schon super perfekt alles klar hallo tschüss bis bald ja
grazie mille perfetto ciao presto si sì
obrigado obrigada muito perfeito ótimo otimo olá ola até logo sim
""".split())


def es_mensaje_de_cortesia(texto: str) -> bool:
    """True si el mensaje es solo un agradecimiento / saludo / emoji, sin ninguna información útil."""
    t = str(texto or "").lower()
    if re.search(r"[0-9?¿]", t):            # una hora, una fecha o una pregunta: siempre a la IA
        return False
    palabras = re.findall(r"[a-záéíóúüñçàèêëîïôûœäöß']+", t)
    return len(palabras) <= 8 and all(p.strip("'") in PALABRAS_CORTESIA for p in palabras)


def analisis_por_palabras_clave(texto: str) -> dict:
    t = str(texto or "")
    return {
        "hora_llegada": None,
        "hora_salida": None,
        "entrada_anticipada": "si" if PALABRAS_ENTRADA.search(t) else None,
        "salida_tardia": "si" if PALABRAS_SALIDA.search(t) else None,
        "sofa_cama": "si" if PALABRAS_SOFA.search(t) else "sin_mencion",
        "cuna": "si" if PALABRAS_CUNA.search(t) else "sin_mencion",
        "otras_peticiones": [],
    }
