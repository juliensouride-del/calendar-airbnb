"""
Estado de una reserva y reglas: fusión de lo que llega en cada correo, horario y limpieza.

El estado de cada reserva se guarda (JSON) dentro de su evento de Calendar y se va
enriqueciendo con cada correo:
  confirmación → datos base · mensajes → horas / sofá / cuna / peticiones · cancelación.
El texto de los eventos está en formato.py y los avisos al anfitrión en avisos.py.

Funciones puras.
"""
from __future__ import annotations

import re
from datetime import date

from .config import Alojamiento, Config
from .parser import DatosCorreo, clave_normalizada, normalizar_hora

PRIORIDAD_FUENTE = {"regla": 1, "palabras": 2, "huesped": 3}   # lo que dice el huésped manda
MADRUGADA = "06:00"      # una llegada antes de esta hora es de noche (tarde), no una entrada anticipada


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


def fusionar_analisis(e: dict, a: dict | None, fuente: str, cfg: Config | None = None) -> dict:
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
     - pide una hora concreta → esa hora; 'no' → la anula.
     - 'si' sin hora → la hora a la que dice que llega (sale), si es antes (después) del horario estándar;
       si no la dice, no borra una hora pedida antes.
     - si no pide nada pero dice a qué hora llega (sale): antes (después) del horario estándar es en la
       práctica una petición; dentro del horario, la petición anterior ya no hace falta.
    """
    if es_hora_valida(pedida):
        return pedida
    if pedida == "no":
        return None
    hora = normalizar_hora(hora_dicha) if es_hora_valida(hora_dicha) and estandar else None
    # Llegar a las 00:30 es llegar tarde (de noche), no pedir entrar antes
    fuera_de_horario = hora if hora and (MADRUGADA <= hora < estandar if antes else hora > estandar) else None
    if pedida == "si":
        return fuera_de_horario or actual or "si"
    return fuera_de_horario if hora else actual


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


def llegada_prevista(e: dict, cfg: Config) -> str:
    """
    Hora a la que se espera al huésped: la acordada o, si no, la que dice, salvo que sea antes de la hora de
    entrada (sin acuerdo no puede entrar antes: vale la estándar). Llegar de madrugada es llegar tarde y se muestra.
    """
    if e.get("acuerdo_entrada"):
        return e["acuerdo_entrada"]
    estandar = cfg.hora_entrada_por_defecto
    llegada = normalizar_hora(e["hora_llegada"])
    return llegada if llegada and not MADRUGADA <= llegada < estandar else estandar


def hora_salida(e: dict, cfg: Config) -> str:
    """Hora de salida: la acordada (#salida HH:MM); si no, la estándar, o la del huésped si sale antes."""
    if e.get("acuerdo_salida"):
        return e["acuerdo_salida"]
    estandar = cfg.hora_salida_por_defecto
    salida = normalizar_hora(e["hora_salida"])
    return min(salida, estandar) if salida else estandar

def misma_reserva(a: dict, b: dict) -> bool:
    if a.get("codigo") and b.get("codigo"):
        return a["codigo"] == b["codigo"]
    return a.get("checkin") == b.get("checkin") and a.get("checkout") == b.get("checkout")


def es_relevo(anterior: dict | None, e: dict) -> bool:
    """
    ¿El huésped de `anterior` sale el mismo día que entra el de `e`, en el mismo alojamiento? Entonces la salida de uno
    y la entrada del otro son un solo evento (relevo). Salir el día anterior no cuenta.
    """
    return bool(anterior and e["checkin"] and e["alojamiento"] and anterior.get("checkout") == e["checkin"]
                and not anterior.get("cancelada") and not e["cancelada"]
                and clave_normalizada(anterior.get("alojamiento")) == clave_normalizada(e["alojamiento"])
                and not misma_reserva(anterior, e))

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


def _min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm(minutos: int) -> str:
    return f"{minutos // 60:02d}:{minutos % 60:02d}"


def es_hora_valida(h) -> bool:
    return isinstance(h, str) and bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", h))
