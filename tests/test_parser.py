"""Parser con los correos reales de ./ejemplos (mismos casos que la versión Apps Script)."""
from dataclasses import asdict, replace

from airbnb_calendar.parser import Huespedes, parsear_correo

from conftest import ALOJAMIENTOS_PRUEBA, cargar_ejemplo, cargar_eml


def parsear(e, **cambios):
    e = replace(e, **cambios)
    return parsear_correo(e.asunto, e.cuerpo, e.fecha_ref, ALOJAMIENTOS_PRUEBA)


# --------------------------------------------------------------------------- ej0 · confirmación

def test_confirmacion_ej0():
    r = parsear(cargar_ejemplo("ej0.txt", "2026-10-01")[0])
    assert r.tipo == "confirmacion"
    assert r.codigo == "HMTEST0002"
    assert r.alojamiento == "Espacioso y luminoso apartamento Corazón de Madrid"
    assert r.alias == "Corazón de Madrid"
    assert r.huesped == "Lucia Romero"
    assert (r.checkin, r.checkout) == ("2026-11-26", "2026-12-02")
    assert (r.hora_checkin, r.hora_checkout) == ("15:00", "11:00")
    assert r.huespedes == Huespedes(adultos=2, ninos=0, bebes=0, mascotas=0, total=2)
    assert [m.texto for m in r.mensajes] == ["Bonjour nous sommes de passage moi et ma sœur à madrid"]
    assert r.faltan == [] and r.avisos == []


def test_anio_deducido_aunque_el_correo_sea_de_diciembre_del_anio_anterior():
    r = parsear(cargar_ejemplo("ej0.txt", "2026-10-01")[0], fecha_ref="2025-12-20")
    assert r.checkin == "2026-11-26"     # jueves 26 nov → 2026 (en 2025 fue miércoles)

# --------------------------------------------------------------------------- ej1 · cancelación


def test_cancelacion_ej1():
    r = parsear(cargar_ejemplo("ej1.txt", "2026-10-01")[0])
    assert r.tipo == "cancelacion"
    assert r.codigo == "HMTEST0003"
    assert r.huesped == "Marc"
    assert (r.checkin, r.checkout) == ("2027-02-01", "2027-02-04")
    assert r.huespedes.total == 1
    assert r.alojamiento == "Espacioso y luminoso apartamento Corazón de Madrid"
    assert r.faltan == []


def test_rango_que_cruza_meses_y_anios():
    r = parsear_correo("", "Reservation canceled\nLavapies - Peaceful 2 bedrooms Fresh A/C\nListing #1\n"
                       "Dec 30 – Jan 2, 2 guests\nUnfortunately, your guest Ana had to cancel reservation "
                       "HMAAAA1111 for Dec 30 – Jan 2, 2027.", "2026-10-01", ALOJAMIENTOS_PRUEBA)
    assert (r.checkin, r.checkout) == ("2026-12-30", "2027-01-02")
    assert r.alias == "Lavapiés"

# --------------------------------------------------------------------------- ej2 · mensajes


def test_mensajes_ej2():
    correos = cargar_ejemplo("ej2.txt", "2026-10-01")
    rs = [parsear(c) for c in correos]
    assert len(rs) == 3
    for r in rs:
        assert r.tipo == "mensaje" and r.codigo is None
        assert r.alojamiento == "Lavapies - Peaceful 2 bedrooms Fresh A/C"
        assert (r.checkin, r.checkout) == ("2026-10-09", "2026-10-13")
        assert (r.hora_checkin, r.hora_checkout) == ("15:00", "11:00")
        assert r.huespedes.total == 3
        assert r.huesped == "Tom"
        assert r.faltan == []
    assert rs[0].mensajes[0].texto.startswith("Airbnb keeps telling me")
    assert "I do not think we arrive before 1500" in rs[1].mensajes[0].texto
    assert rs[2].mensajes[0].texto == "Will the check in information come via e-mail or text?"
    assert correos[1].fecha_hora == "2026-10-02 09:18"

# --------------------------------------------------------------------------- ej3 · mensajes con bebé


def test_mensajes_ej3():
    correos = cargar_ejemplo("ej3.txt", "2026-09-22")
    rs = [parsear(c) for c in correos]
    assert len(rs) == 7
    for r in rs:
        assert r.tipo == "mensaje"
        assert r.alojamiento == "Anton Martin Agradable, Anton Martin - Apartament."
        assert r.alias == "Antón Martín"
        assert (r.checkin, r.checkout) == ("2026-09-23", "2026-09-27")
        assert r.huespedes == Huespedes(adultos=2, ninos=0, bebes=1, mascotas=0, total=3)
        assert r.huesped == "Peter"
    assert "providing a crib for our baby" in rs[0].mensajes[0].texto
    assert len(rs[3].mensajes) == 2
    assert rs[3].mensajes[0].texto.startswith("Sorry, I have a question")
    assert rs[3].mensajes[1].texto == "Thank you"
    assert rs[5].mensajes[1].texto == "We are planning to be out right before 11"
    # mensaje enviado DESPUÉS de la salida: conserva el año correcto
    assert correos[6].fecha_ref == "2026-09-28" and rs[6].checkin == "2026-09-23"

# --------------------------------------------------------------------------- .eml reales de Yahoo


def test_confirmed_eml_dos_columnas():
    r = parsear(cargar_eml("confirmed.eml"))
    assert r.tipo == "confirmacion"
    assert r.codigo == "HMTEST0001"
    assert r.alias == "Antón Martín"
    assert r.huesped == "Laura Garcia"
    assert (r.checkin, r.checkout) == ("2026-10-20", "2026-10-23")
    assert (r.hora_checkin, r.hora_checkout) == ("15:00", "11:00")
    assert r.huespedes == Huespedes(adultos=2, ninos=0, bebes=0, mascotas=0, total=2)
    assert r.mensajes == [] and r.faltan == [] and r.avisos == []


def test_cunasolicitud_eml_nombre_recortado_fecha_larga_y_bebe():
    r = parsear(cargar_eml("cunasolicitud.eml"))
    assert r.tipo == "mensaje"
    assert r.alias == "Antón Martín"
    assert (r.checkin, r.checkout) == ("2026-09-16", "2026-09-19")
    assert r.huespedes.bebes == 1 and r.huespedes.total == 3
    assert r.mensajes == []          # solo una reacción ❤️ → nada que analizar
    assert r.faltan == []


def test_dos_columnas_con_mensaje_del_huesped():
    e = cargar_eml("confirmed.eml")
    cuerpo = e.cuerpo.replace("Bristol, United Kingdom",
                              "Bristol, United Kingdom\n\nHi! We will arrive around 8pm and need the sofa bed for my son")
    r = parsear(e, cuerpo=cuerpo)
    assert len(r.mensajes) == 1 and "sofa bed" in r.mensajes[0].texto
    assert r.checkin == "2026-10-20"


def test_correos_que_no_son_reservas():
    assert parsear_correo("Your monthly earnings report", "Here is how you did.", "2026-10-05", []).tipo == "desconocido"
    r = parsear_correo("Reservation altered", "Your guest changed the reservation", "2026-10-05", [])
    assert asdict(r)["tipo"] == "modificacion"
