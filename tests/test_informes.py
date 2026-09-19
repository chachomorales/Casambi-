"""
El informe Excel como fichero: nombre, colisiones y acumulación.

`reportes/` guarda informes reales de clientes y nunca se limpiaba. Y el nombre
tenía granularidad de minuto, así que dos descargas seguidas de la misma red se
pisaban: la segunda sobrescribía el fichero mientras la primera podía estar
siendo enviada al navegador.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

import pytest

import report

from conftest import RED_ID


# ── El nombre ─────────────────────────────────────────────────────────────────

def test_dos_informes_de_la_misma_red_no_comparten_nombre():
    nombres = {report._nombre_informe("Red de prueba") for _ in range(50)}
    assert len(nombres) == 50, "dos informes del mismo segundo colisionarían"


@pytest.mark.parametrize("bruto,prohibidos", [
    ("URL Edif H,M,J,L", ",")  ,
    ("Planta 1/2", "/"),
    ("Sede: Málaga", ":"),
    ("../../escape", "/"),
    ("Red\\con\\barras", "\\"),
])
def test_el_nombre_de_la_red_se_sanea(bruto, prohibidos):
    """Viene del API de Casambi; hay informes reales con comas en el nombre."""
    nombre = report._nombre_informe(bruto)
    for c in prohibidos:
        assert c not in nombre
    assert nombre.startswith("Casambi_") and nombre.endswith(".xlsx")
    assert os.sep not in nombre


def test_un_nombre_vacio_no_deja_el_fichero_sin_nombre():
    for entrada in (None, "", "   ", "///", "..."):
        nombre = report._nombre_informe(entrada)
        assert nombre.startswith("Casambi_red_") or "Casambi__" not in nombre
        assert nombre.endswith(".xlsx")


def test_el_nombre_no_crece_sin_tope():
    nombre = report._nombre_informe("A" * 500)
    assert len(nombre) < 120


def test_el_nombre_lleva_la_fecha_legible():
    nombre = report._nombre_informe("Red")
    assert datetime.now().strftime("%Y%m%d") in nombre


# ── La purga ──────────────────────────────────────────────────────────────────

def test_se_borran_los_informes_viejos(tmp_path):
    viejo = tmp_path / "Casambi_Red_20260101_120000_abcdef.xlsx"
    nuevo = tmp_path / "Casambi_Red_20260918_120000_123456.xlsx"
    viejo.write_bytes(b"PK viejo")
    nuevo.write_bytes(b"PK nuevo")
    antiguo = time.time() - 30 * 86400
    os.utime(viejo, (antiguo, antiguo))

    assert report._purgar_informes(tmp_path, dias=7) == 1
    assert not viejo.exists()
    assert nuevo.exists()


def test_la_purga_no_toca_otros_ficheros(tmp_path):
    ajeno = tmp_path / "no-es-un-informe.xlsx"
    ajeno.write_bytes(b"x")
    antiguo = time.time() - 60 * 86400
    os.utime(ajeno, (antiguo, antiguo))

    report._purgar_informes(tmp_path, dias=1)
    assert ajeno.exists(), "solo deben borrarse los Casambi_*.xlsx"


def test_la_purga_no_rompe_la_descarga_si_no_puede_borrar(tmp_path, monkeypatch):
    """Limpiar es accesorio: un fallo ahí no debe tumbar la generación."""
    fichero = tmp_path / "Casambi_Red_20260101_120000_abcdef.xlsx"
    fichero.write_bytes(b"x")
    antiguo = time.time() - 30 * 86400
    os.utime(fichero, (antiguo, antiguo))

    def unlink_roto(self, **kw):
        raise OSError("permiso denegado")

    monkeypatch.setattr(report.Path, "unlink", unlink_roto)
    assert report._purgar_informes(tmp_path, dias=7) == 0  # no lanza


def test_un_directorio_inexistente_no_lanza(tmp_path):
    assert report._purgar_informes(tmp_path / "no-existe") == 0


# ── La fuente de los marcadores ───────────────────────────────────────────────

def test_la_fuente_se_cachea():
    report._marker_font.cache_clear()
    a = report._marker_font(22)
    b = report._marker_font(22)
    assert a is b, "se reabría la fuente en cada marcador dibujado"
    assert report._marker_font.cache_info().hits >= 1


def test_la_fuente_sirve_para_dibujar():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (60, 30), "white")
    ImageDraw.Draw(img).text((2, 2), "12", font=report._marker_font(18), fill="black")
    assert img.getbbox() is not None


# ── De punta a punta ──────────────────────────────────────────────────────────

def test_descargar_dos_veces_deja_dos_ficheros(cliente):
    import app as app_module

    informes = app_module.REPORTS_DIR
    informes.mkdir(parents=True, exist_ok=True)
    for f in informes.glob("Casambi_*.xlsx"):
        f.unlink()

    for _ in range(2):
        assert cliente.get(f"/network/{RED_ID}/excel").status_code == 200

    generados = list(informes.glob("Casambi_*.xlsx"))
    assert len(generados) == 2, "la segunda descarga sobrescribía la primera"
    assert all(f.stat().st_size > 0 for f in generados)
