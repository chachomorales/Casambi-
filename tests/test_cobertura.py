"""
Importar proyectos del Simulador de Cobertura con varias redes Casambi.

Cada red del simulador corresponde a una red de la nube distinta, así que un
proyecto con dos se importa una red a la vez: solo sus nodos, y solo en los
niveles donde tiene alguno.
"""

from __future__ import annotations

import base64
import io
import json

import pytest

import cobertura
from conftest import RED_ID
from test_humo import _png_de_prueba

_PLANO = "data:image/png;base64," + base64.b64encode(_png_de_prueba()).decode()


def _nodo(ident: str, x: float, red: int | None = None) -> dict:
    nodo = {"id": ident, "label": ident, "x": x, "y": 1, "ptxDbm": 4}
    if red is not None:
        nodo["network"] = red
    return nodo


def _nivel(nombre: str, red: int, nodos: list[dict]) -> dict:
    return {"id": nombre, "name": nombre, "network": red, "image": _PLANO,
            "metersPerPixel": 0.1, "nodes": nodos, "walls": []}


def _proyecto(version: int = 5) -> bytes:
    """Planta baja con la red 1 y un nodo de la 2; arriba, solo la red 3."""
    return json.dumps({
        "version": version, "projectName": "Edificio", "clientName": "", "nExponent": 2.5,
        "materials": [], "networkNames": [{"network": 2, "name": "Pasillos"}],
        "levels": [
            _nivel("PB", 1, [_nodo("a", 1), _nodo("b", 2), _nodo("c", 3, red=2)]),
            _nivel("N1", 3, [_nodo("d", 1)]),
        ],
    }).encode()


def test_dice_que_redes_trae():
    redes = cobertura.redes_del_proyecto(_proyecto())
    assert [(r["red"], r["titulo"], r["nodos"]) for r in redes] == [
        (1, "Red 1", 2), (2, "Red 2 · Pasillos", 1), (3, "Red 3", 1),
    ]


def test_sin_red_entra_todo_como_antes():
    planos, avisos = cobertura.parse_proyecto(_proyecto())
    assert avisos == []
    assert [len(d["nodos"]) for _, d in planos] == [3, 1]
    assert all(d["red"] is None and d["red_titulo"] is None for _, d in planos)


def test_una_red_deja_solo_sus_nodos():
    planos, _ = cobertura.parse_proyecto(_proyecto(), red=2)
    assert len(planos) == 1, "el nivel de arriba no tiene nodos de la red 2"
    _, datos = planos[0]
    assert [n["label"] for n in datos["nodos"]] == ["c"]
    assert datos["red"] == 2
    assert datos["red_titulo"] == "Red 2 · Pasillos"
    assert datos["nivel"] == "PB"


def test_la_red_del_nivel_cuenta_para_los_nodos_sin_red_propia():
    planos, _ = cobertura.parse_proyecto(_proyecto(), red=3)
    assert [d["nivel"] for _, d in planos] == ["N1"]
    assert [n["label"] for n in planos[0][1]["nodos"]] == ["d"]


def test_una_red_que_no_esta_se_rechaza_diciendo_cuales_hay():
    with pytest.raises(cobertura.CoberturaError, match="Red 2 · Pasillos"):
        cobertura.parse_proyecto(_proyecto(), red=7)


def test_un_proyecto_anterior_a_las_redes_por_equipo_tiene_la_de_su_nivel():
    proyecto = json.dumps({
        "version": 1, "projectName": "Casa", "clientName": "", "nExponent": 2.5,
        "materials": [], "image": _PLANO, "metersPerPixel": 0.1,
        "nodes": [_nodo("x", 1)], "walls": [],
    }).encode()
    assert [r["red"] for r in cobertura.redes_del_proyecto(proyecto)] == [1]
    planos, _ = cobertura.parse_proyecto(proyecto, red=1)
    assert planos[0][1]["red_titulo"] == "Red 1"


# ── La subida ─────────────────────────────────────────────────────────────────

def _sube(cliente, red: str | None = None):
    data = {"file": (io.BytesIO(_proyecto()), "edificio.casambi")}
    if red is not None:
        data["red"] = red
    return cliente.post(f"/network/{RED_ID}/planos/upload", data=data,
                        content_type="multipart/form-data")


def test_la_subida_importa_solo_la_red_elegida(cliente, datos_limpios):
    _sube(cliente, "2")
    planos = json.loads((datos_limpios / f"planos_{RED_ID}.json").read_text())
    assert len(planos) == 1
    assert planos[0]["name"] == "Edificio · PB · Red 2 · Pasillos"
    assert [n["label"] for n in planos[0]["cobertura"]["nodos"]] == ["c"]


def test_la_subida_sin_elegir_red_importa_todo(cliente, datos_limpios):
    _sube(cliente, "")
    planos = json.loads((datos_limpios / f"planos_{RED_ID}.json").read_text())
    assert [p["name"] for p in planos] == ["Edificio · PB", "Edificio · N1"]


def test_una_red_invalida_no_importa_nada(cliente, datos_limpios):
    _sube(cliente, "dos")
    ruta = datos_limpios / f"planos_{RED_ID}.json"
    assert not ruta.exists() or json.loads(ruta.read_text()) == []


def test_formato_6_con_modo_de_red_se_lee_igual():
    """El modo va en la entrada de la red; una red con modo y sin nombre no
    gana un título vacío."""
    proyecto = json.loads(_proyecto(version=6))
    proyecto["networkNames"] = [
        {"network": 2, "name": "Pasillos", "mode": "longRange"},
        {"network": 3, "name": "", "mode": "balanced"},
    ]
    redes = cobertura.redes_del_proyecto(json.dumps(proyecto).encode())
    assert [(r["red"], r["titulo"]) for r in redes] == [
        (1, "Red 1"), (2, "Red 2 · Pasillos"), (3, "Red 3"),
    ]
