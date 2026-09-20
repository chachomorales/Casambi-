"""
Excel report generator for Casambi networks.
Produces a styled .xlsx with sheets: Red, Elementos, Grupos, Escenas.
"""

from __future__ import annotations

import functools
import io
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps
from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import (
    Alignment,
    Border,
    Font,
    PatternFill,
    Side,
)
from openpyxl.utils import get_column_letter

LOGOS_DIR = Path(__file__).parent / "logos"

# ── Palette ───────────────────────────────────────────────────────────────────
COLOR_HEADER_BG  = "1F3864"   # dark navy
COLOR_HEADER_FG  = "FFFFFF"   # white
COLOR_SECTION_BG = "D6E4F0"   # light blue
COLOR_ALT_ROW    = "EFF5FB"   # very light blue
COLOR_BORDER     = "A9C4E0"

# Cover palette
COLOR_COVER_ACCENT  = "2E75B6"   # Casambi blue
COLOR_COVER_DARK    = "1F3864"   # navy
COLOR_COVER_LIGHT   = "DEEAF1"   # pale blue
COLOR_COVER_WHITE   = "FFFFFF"
COLOR_CARD_LUMI     = "2E75B6"   # blue  – luminarias
COLOR_CARD_SENSOR   = "70AD47"   # green – sensores
COLOR_CARD_BUTTON   = "ED7D31"   # orange – pulsadores
COLOR_CARD_GATEWAY  = "7030A0"   # purple – gateways
COLOR_CARD_GROUP    = "4472C4"   # mid blue – grupos
COLOR_CARD_SCENE    = "00B0F0"   # cyan – escenas
COLOR_CARD_TOTAL    = "1F3864"   # navy – total

# Diagnóstico de conectividad (hoja Conectividad)
COLOR_EST_OK       = "E2EFDA"   # verde muy claro – responde
COLOR_EST_FALLO    = "FBE0E0"   # rojo muy claro  – no responde
COLOR_EST_AVISO    = "FFF2CC"   # ámbar claro     – el equipo reporta una condición
COLOR_EST_REPOSO   = "F2F2F2"   # gris claro      – duerme por diseño
COLOR_TXT_OK       = "375623"
COLOR_TXT_FALLO    = "9C0006"
COLOR_TXT_AVISO    = "9C6500"
COLOR_TXT_REPOSO   = "6B6B6B"

# Planos importados del Simulador de Cobertura
COLOR_COB_PARED    = "C00000"   # rojo – paredes del modelo de propagación
COLOR_COB_NODO     = "00B0F0"   # cian – nodo simulado sin unidad real asociada
COLOR_COB_NOTA     = "6B6B6B"   # gris – texto del subtítulo de la sección
COLOR_COB_HUECO    = "4B5563"   # gris pizarra – huecos de losa del nivel

THIN = Side(style="thin", color=COLOR_BORDER)
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _header_fill() -> PatternFill:
    return PatternFill("solid", fgColor=COLOR_HEADER_BG)


def _section_fill() -> PatternFill:
    return PatternFill("solid", fgColor=COLOR_SECTION_BG)


def _alt_fill() -> PatternFill:
    return PatternFill("solid", fgColor=COLOR_ALT_ROW)


def _write_header_row(ws, row: int, columns: list[str]) -> None:
    for col_idx, title in enumerate(columns, start=1):
        cell = ws.cell(row=row, column=col_idx, value=title)
        cell.font = Font(bold=True, color=COLOR_HEADER_FG, name="Calibri", size=11)
        cell.fill = _header_fill()
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER


def _write_data_row(ws, row: int, values: list, alternate: bool = False) -> None:
    fill = _alt_fill() if alternate else PatternFill()
    for col_idx, val in enumerate(values, start=1):
        cell = ws.cell(row=row, column=col_idx, value=val)
        cell.font = Font(name="Calibri", size=10)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = BORDER
        if alternate:
            cell.fill = fill


def _auto_width(ws, min_width: int = 12, max_width: int = 50) -> None:
    for col_cells in ws.columns:
        length = max(
            len(str(c.value or "")) for c in col_cells
        )
        col_letter = get_column_letter(col_cells[0].column)
        ws.column_dimensions[col_letter].width = max(min_width, min(length + 4, max_width))


def _freeze(ws, cell: str = "A2") -> None:
    ws.freeze_panes = cell


# ── Device type helpers ───────────────────────────────────────────────────────

_CONTROL_TYPE_LABELS: dict[str, str] = {
    "Dimmer":            "Regulable",
    "ColorTemperature":  "Temp. Color (CCT)",
    "RGB":               "Color RGB",
    "XY":                "Color XY",
    "OnOff":             "On/Off",
    "PushButton":        "Pulsador",
    "Slider":            "Deslizador",
    "WhiteColorBalance": "Balance Blanco",
    "Colorsource":       "Fuente de Color",
}


def _classify_unit(unit: dict) -> str:
    """Return a human-readable device category in Spanish."""
    raw_type = (unit.get("type") or "").lower()
    controls = [c.get("type", "") for c in unit.get("controls", [])]

    if raw_type == "luminaire":
        return "Luminaria"
    if "sensor" in raw_type:
        return "Sensor"
    if "switch" in raw_type or "pushbutton" in raw_type:
        return "Pulsador / Botonera"
    if raw_type == "gateway":
        return "Gateway"
    if "PushButton" in controls:
        return "Pulsador / Botonera"
    if any(c in controls for c in ("Dimmer", "OnOff", "ColorTemperature", "RGB")):
        return "Luminaria"
    if raw_type:
        return raw_type  # mostrar el tipo original si no encaja en ninguna categoría
    return "Desconocido"


def _controls_summary(unit: dict) -> str:
    controls = unit.get("controls", [])
    labels = []
    for c in controls:
        ctype = c.get("type", "")
        labels.append(_CONTROL_TYPE_LABELS.get(ctype, ctype))
    return ", ".join(dict.fromkeys(labels))  # deduplicated, order preserved


# Capacidades declaradas en el fixture (el config de unidades no trae controles;
# la definición real de lo que soporta cada modelo vive en /v1/fixtures/{id}).
_FIXTURE_CONTROL_LABELS = {
    "dimmer":             "Regulable",
    "onoff":              "On/Off",
    "rgb":                "Color RGB",
    "xy":                 "Color XY",
    "white":              "Balance Blanco",
    "temperature":        "Temp. Color (CCT)",
    "cct":                "Temp. Color (CCT)",
    "vertical":           "Color Vertical",
    "colorsource":        "Fuente de Color",
    "pushbutton":         "Pulsador",
    "pushbuttonstate":    "Pulsador",
    "presence":           "Sensor de Presencia",
    "lux":                "Sensor de Luz",
    "ambienttemperature": "Sensor Temp. Ambiente",
    "battery":            "Batería",
}
# Entradas internas o de diagnóstico que no son capacidades del elemento
_FIXTURE_CONTROL_SKIP = {
    "revision", "overheat", "placeholder", "sensorgroup", "sensorgroupvalue",
    "ballastfailure", "lampfailure",
}


def _fixture_controls_summary(fixture: dict) -> str:
    labels = []
    for c in fixture.get("controls", []):
        ctype = (c.get("type") or "").lower()
        if not ctype or ctype in _FIXTURE_CONTROL_SKIP:
            continue
        name = (c.get("name") or "").strip()
        if ctype == "slider":
            labels.append(f"Deslizador: {name}" if name else "Deslizador")
        elif ctype == "sensor":
            labels.append(f"Sensor: {name}" if name else "Sensor")
        else:
            labels.append(_FIXTURE_CONTROL_LABELS.get(ctype, ctype))
    return ", ".join(dict.fromkeys(labels))


def _unit_soporta(unit: dict, fixtures: dict) -> str:
    """Capacidades del elemento: primero del fixture, si no de la unidad."""
    fid = unit.get("fixtureId")
    fixture = fixtures.get(fid, {}) if fid else {}
    return _fixture_controls_summary(fixture) or _controls_summary(unit) or "-"


# ── Diagnóstico de conectividad ───────────────────────────────────────────────
# El estado (/v1/networks/{id}/state) trae por unidad: online, on, dimLevel,
# activeSceneId, condition y —solo si la red es alcanzable— status.
#
# Dos cautelas, aprendidas mirando redes reales, que decidieron este diseño:
#
#  · `online` significa "responde ahora mismo", y eso pasa por el gateway. Si el
#    gateway está caído, las 130 unidades de la red salen offline a la vez: el
#    fallo es de la red, no de los equipos. Por eso solo se señala a un
#    dispositivo concreto cuando *algún* otro responde; si no responde ninguno,
#    no hay lectura que interpretar y decirlo así evita un informe que acusa a
#    130 luminarias sanas.
#  · Los pulsadores a pila (BatterySwitch) duermen y solo despiertan al
#    pulsarlos: no aparecen online jamás, y son los únicos que ni publican
#    firmwareVersion. Tratarlos como avería sería un falso positivo permanente,
#    así que se cuentan aparte.

# Tipos que están offline por diseño, no por avería.
_TIPOS_EN_REPOSO = {"batteryswitch"}

EST_RESPONDE    = "Responde"
EST_NO_RESPONDE = "No responde"
EST_REPOSO      = "En reposo"
EST_SIN_LECTURA = "Sin lectura"


def _dim_pct(unit: dict) -> str:
    """dimLevel viene en 0-1; se muestra en % como en el resto del informe."""
    level = unit.get("dimLevel")
    if level is None:
        return "-"
    return f"{round(level * 100)}%"


def _aviso_unidad(unit: dict) -> str:
    """
    Texto de aviso si el propio equipo reporta algo raro.

    `condition` es un indicador de la unidad que la API no documenta: en las
    redes medidas vale 0 en 1.544 de 1.550 unidades, y 128 en seis luminarias
    que por lo demás están online y con status "ok". No se traduce a una causa
    concreta porque no sabemos cuál es; se señala para que el técnico la mire.
    """
    avisos = []
    condition = unit.get("condition")
    if condition:
        avisos.append(f"El equipo reporta condition {condition} — revisar")
    status = unit.get("status")
    if status and str(status).lower() != "ok":
        avisos.append(f"status «{status}»")
    return "; ".join(avisos)


def diagnostico_conectividad(network: dict, state: dict) -> dict:
    """
    Estado de conexión de la red y de cada dispositivo, para reportar problemas.

    Devuelve el veredicto de la red y una lista `dispositivos` ordenada por
    urgencia (primero lo que no responde, luego lo que avisa). `fiable` indica
    si la lectura permite culpar a un dispositivo concreto: es False cuando no
    responde nadie, porque entonces lo único que se sabe es que la red no es
    alcanzable.

    Mantiene las claves que ya usaba la interfaz (`nivel`, `gateway`, `online`,
    `total`, `escenas_activas`) para que el aviso de la cabecera siga igual.
    """
    units       = network.get("units", [])
    state_units = {u.get("id"): u for u in state.get("units", [])}
    group_map   = {g.get("id"): g.get("name", "-") for g in network.get("groups", [])}
    scene_map   = {s.get("id"): s.get("name", "-") for s in network.get("scenes", [])}
    gateway     = (state.get("gateway") or network.get("gateway") or {}).get("name") or ""

    responden = sum(1 for u in state_units.values() if u.get("online"))
    fiable    = responden > 0

    dispositivos = []
    for unit in units:
        live      = state_units.get(unit.get("id"), {})
        en_reposo = (unit.get("type") or "").lower() in _TIPOS_EN_REPOSO

        if en_reposo:
            estado = EST_REPOSO
        elif live.get("online"):
            estado = EST_RESPONDE
        elif fiable:
            estado = EST_NO_RESPONDE
        else:
            estado = EST_SIN_LECTURA

        aviso    = _aviso_unidad(live)
        problema = estado == EST_NO_RESPONDE
        gid      = unit.get("groupId", 0)

        dispositivos.append({
            "id":            unit.get("id", "-"),
            "name":          unit.get("name", "-"),
            "category":      _classify_unit(unit),
            "type":          unit.get("type", "-"),
            "group":         group_map.get(gid, "-") if gid else "-",
            "estado":        estado,
            "problema":      problema,
            "aviso":         aviso,
            # on/dimLevel/escena solo dicen algo si el equipo está respondiendo
            "encendido":     ("Sí" if live.get("on") else "No") if estado == EST_RESPONDE else "-",
            "nivel":         _dim_pct(live) if estado == EST_RESPONDE else "-",
            "escena":        scene_map.get(live.get("activeSceneId"), "-")
                             if live.get("activeSceneId") else "-",
            "firmware":      live.get("firmwareVersion") or unit.get("firmwareVersion") or "-",
            "address":       unit.get("address", "-"),
        })

    # Primero lo que hay que mirar: averías, luego avisos, luego el resto.
    dispositivos.sort(key=lambda d: (
        0 if d["problema"] else 1 if d["aviso"] else 2,
        d["category"],
        str(d["name"]),
    ))

    no_responden = sum(1 for d in dispositivos if d["problema"])
    en_reposo    = sum(1 for d in dispositivos if d["estado"] == EST_REPOSO)
    sin_lectura  = sum(1 for d in dispositivos if d["estado"] == EST_SIN_LECTURA)
    con_aviso    = sum(1 for d in dispositivos if d["aviso"])

    if not units:
        nivel = "sin-dispositivos"
    elif fiable:
        nivel = "online"
    elif gateway:
        nivel = "gateway-offline"
    else:
        nivel = "sin-gateway"

    if nivel == "sin-dispositivos":
        veredicto = "La red no tiene ningún dispositivo dado de alta."
    elif nivel == "online":
        if no_responden:
            veredicto = (
                f"La red responde, pero {no_responden} de "
                f"{len(units) - en_reposo} dispositivos no dan señal. "
                "Revisar esos equipos: alimentación, alcance de malla o avería."
            )
        else:
            veredicto = "Todos los dispositivos que deben responder están en línea."
    elif nivel == "gateway-offline":
        veredicto = (
            f"Ningún dispositivo responde. La red tiene configurado el gateway "
            f"«{gateway}», así que lo primero a revisar es el gateway, no los "
            "equipos: mientras no haya pasarela no se puede saber cuáles fallan."
        )
    else:
        veredicto = (
            "Ningún dispositivo responde y la red no tiene gateway configurado. "
            "Sin pasarela la nube no ve el estado de los equipos."
        )

    return {
        "nivel":           nivel,
        "gateway":         gateway,
        "fiable":          fiable,
        "veredicto":       veredicto,
        "total":           len(units),
        "online":          responden,       # nombre histórico, lo usa la cabecera
        "responden":       responden,
        "no_responden":    no_responden,
        "en_reposo":       en_reposo,
        "sin_lectura":     sin_lectura,
        "con_aviso":       con_aviso,
        "escenas_activas": len(state.get("activeScenes") or {}),
        "dispositivos":    dispositivos,
    }


# ── Sheet builders ────────────────────────────────────────────────────────────

def _sheet_portada(wb: Workbook, network: dict, state: dict) -> None:
    ws = wb.active
    ws.title = "Portada"
    ws.sheet_view.showGridLines = False
    ws.sheet_view.showRowColHeaders = False

    ws.column_dimensions["A"].width = 3
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 18
    ws.column_dimensions["E"].width = 18
    ws.column_dimensions["F"].width = 18
    ws.column_dimensions["G"].width = 3

    def fill(color: str) -> PatternFill:
        return PatternFill("solid", fgColor=color)

    def cell(row, col, value="", bold=False, size=11, color="000000",
             bg=None, align="left", valign="center", wrap=False):
        c = ws.cell(row=row, column=col, value=value)
        c.font = Font(bold=bold, size=size, color=color, name="Calibri")
        c.alignment = Alignment(horizontal=align, vertical=valign, wrap_text=wrap)
        if bg:
            c.fill = fill(bg)
        return c

    def row_height(row, h):
        ws.row_dimensions[row].height = h

    # ── Row 1: Logo bar ───────────────────────────────────────────────────────
    ROW_HEIGHT_PT = 55
    row_height(1, ROW_HEIGHT_PT)
    for col in range(1, 8):
        ws.cell(row=1, column=col).fill = fill("FFFFFF")

    casambi_path = LOGOS_DIR / "casambi_logo.png"
    impelsa_path = LOGOS_DIR / "impelsa_logo.png"

    def _add_logo_centered(path, width_px, height_px, col_letter):
        from openpyxl.drawing.spreadsheet_drawing import OneCellAnchor, AnchorMarker
        from openpyxl.drawing.xdr import XDRPositiveSize2D

        EMU_PER_PX = 9144
        PT_TO_PX   = 96 / 72
        row_px     = ROW_HEIGHT_PT * PT_TO_PX
        v_off_emu  = max(0, int((row_px - height_px) / 2 * EMU_PER_PX))
        col_idx    = ord(col_letter) - ord("A")  # 0-based

        img = XLImage(str(path))
        img.width  = width_px
        img.height = height_px
        marker = AnchorMarker(col=col_idx, colOff=0, row=0, rowOff=v_off_emu)
        size   = XDRPositiveSize2D(width_px * EMU_PER_PX, height_px * EMU_PER_PX)
        img.anchor = OneCellAnchor(_from=marker, ext=size)
        ws.add_image(img)

    # Impelsa izquierda, Casambi derecha — 20% más grandes
    if impelsa_path.exists():
        _add_logo_centered(impelsa_path, width_px=192, height_px=48, col_letter="B")

    if casambi_path.exists():
        _add_logo_centered(casambi_path, width_px=192, height_px=29, col_letter="F")

    # ── Row 2: Accent line ────────────────────────────────────────────────────
    row_height(2, 5)
    ws.merge_cells("A2:G2")
    ws["A2"].fill = fill(COLOR_COVER_ACCENT)

    # ── Rows 3-5: Title banner ────────────────────────────────────────────────
    for r in range(3, 6):
        row_height(r, 18)
        for col in range(1, 8):
            ws.cell(row=r, column=col).fill = fill(COLOR_COVER_DARK)
    ws.merge_cells("A3:G5")
    c = ws.cell(row=3, column=1, value="INFORME DE RED CASAMBI")
    c.font = Font(bold=True, size=22, color=COLOR_COVER_WHITE, name="Calibri")
    c.alignment = Alignment(horizontal="center", vertical="center")
    c.fill = fill(COLOR_COVER_DARK)
    ws.row_dimensions[3].height = 55

    # ── Row 6: Accent line ────────────────────────────────────────────────────
    row_height(6, 6)
    ws.merge_cells("A6:G6")
    ws["A6"].fill = fill(COLOR_COVER_ACCENT)

    # ── Network info block (rows 7-13) ────────────────────────────────────────
    row_height(7, 8)   # spacer

    info_rows = [
        (8,  "Red:",    network.get("name", "-")),
        (9,  "Site:",   network.get("site_name", "-")),
        (10, "Tipo:",   network.get("type", "-")),
        (11, "Grado:",  network.get("grade", "-")),
        (12, "Fecha:",  datetime.now().strftime("%d/%m/%Y %H:%M")),
    ]
    for r, label, value in info_rows:
        row_height(r, 20)
        cell(r, 2, label, bold=True, size=11, color=COLOR_COVER_DARK)
        ws.merge_cells(f"C{r}:F{r}")
        cell(r, 3, value, bold=False, size=11, color="404040")

    row_height(13, 12)  # spacer

    # ── Section title ─────────────────────────────────────────────────────────
    row_height(14, 24)
    ws.merge_cells("B14:F14")
    c = ws.cell(row=14, column=2, value="RESUMEN DE LA RED")
    c.font = Font(bold=True, size=13, color=COLOR_COVER_WHITE, name="Calibri")
    c.fill = fill(COLOR_COVER_ACCENT)
    c.alignment = Alignment(horizontal="center", vertical="center")

    row_height(15, 8)

    # ── Metric cards ──────────────────────────────────────────────────────────
    units  = network.get("units", [])
    groups = network.get("groups", [])
    scenes = network.get("scenes", [])

    from collections import Counter
    type_counts = Counter(_classify_unit(u) for u in units)

    cards = [
        ("TOTAL",       len(units),                         COLOR_CARD_TOTAL,   "B"),
        ("LUMINARIAS",  type_counts["Luminaria"],           COLOR_CARD_LUMI,    "C"),
        ("SENSORES",    type_counts["Sensor"],              COLOR_CARD_SENSOR,  "D"),
        ("PULSADORES",  type_counts["Pulsador / Botonera"], COLOR_CARD_BUTTON,  "E"),
        ("GRUPOS",      len(groups),                        COLOR_CARD_GROUP,   "F"),
    ]
    row_height(16, 42)
    row_height(17, 18)
    for label, count, color, col in cards:
        ci = ord(col) - ord("A") + 1
        c = ws.cell(row=16, column=ci, value=count)
        c.font = Font(bold=True, size=28, color=COLOR_COVER_WHITE, name="Calibri")
        c.fill = fill(color)
        c.alignment = Alignment(horizontal="center", vertical="center")
        c = ws.cell(row=17, column=ci, value=label)
        c.font = Font(bold=True, size=9, color=COLOR_COVER_WHITE, name="Calibri")
        c.fill = fill(color)
        c.alignment = Alignment(horizontal="center", vertical="center")

    row_height(18, 8)

    cards2 = [
        ("ESCENAS",  len(scenes),                        COLOR_CARD_SCENE,   "C"),
        ("GATEWAYS", type_counts.get("Gateway", 0),      COLOR_CARD_GATEWAY, "D"),
    ]
    row_height(19, 42)
    row_height(20, 18)
    for label, count, color, col in cards2:
        ci = ord(col) - ord("A") + 1
        c = ws.cell(row=19, column=ci, value=count)
        c.font = Font(bold=True, size=28, color=COLOR_COVER_WHITE, name="Calibri")
        c.fill = fill(color)
        c.alignment = Alignment(horizontal="center", vertical="center")
        c = ws.cell(row=20, column=ci, value=label)
        c.font = Font(bold=True, size=9, color=COLOR_COVER_WHITE, name="Calibri")
        c.fill = fill(color)
        c.alignment = Alignment(horizontal="center", vertical="center")

    # ── Footer ────────────────────────────────────────────────────────────────
    row_height(21, 10)
    row_height(22, 20)
    ws.merge_cells("A22:G22")
    c = ws.cell(row=22, column=1,
                value="Generado automáticamente con Casambi Report Generator")
    c.font = Font(italic=True, size=9, color="888888", name="Calibri")
    c.alignment = Alignment(horizontal="center", vertical="center")


def _sheet_red(wb: Workbook, network: dict, state: dict) -> None:
    ws = wb.create_sheet("Red")
    ws.sheet_view.showGridLines = False

    units       = network.get("units", [])
    groups      = network.get("groups", [])
    scenes      = network.get("scenes", [])

    types = [_classify_unit(u) for u in units]
    from collections import Counter
    type_counts = Counter(types)
    luminarias = type_counts.pop("Luminaria", 0)
    sensores   = type_counts.pop("Sensor", 0)
    pulsadores = type_counts.pop("Pulsador / Botonera", 0)
    gateways   = type_counts.pop("Gateway", 0)

    # Title banner
    ws.merge_cells("A1:B1")
    title_cell = ws["A1"]
    title_cell.value = "INFORME DE RED CASAMBI"
    title_cell.font = Font(bold=True, size=16, color=COLOR_HEADER_FG, name="Calibri")
    title_cell.fill = _header_fill()
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30

    extra_rows = [(t, n) for t, n in type_counts.items()]

    rows = [
        ("Nombre de Red",          network.get("name", "-")),
        ("Tipo de Red",            network.get("type", "-")),
        ("Fecha del Informe",      datetime.now().strftime("%d/%m/%Y %H:%M")),
        (None, None),
        ("RESUMEN DE ELEMENTOS", None),
        ("Total de Dispositivos",  len(units)),
        ("Luminarias",             luminarias),
        ("Sensores",               sensores),
        ("Pulsadores / Botoneras", pulsadores),
        ("Gateways",               gateways),
        *extra_rows,
        (None, None),
        ("Total de Grupos",        len(groups)),
        ("Total de Escenas",       len(scenes)),
    ]

    for r_idx, (label, value) in enumerate(rows, start=2):
        ws.row_dimensions[r_idx].height = 20
        if label is None:
            continue
        if label == "RESUMEN DE ELEMENTOS":
            ws.merge_cells(f"A{r_idx}:B{r_idx}")
            cell = ws.cell(row=r_idx, column=1, value=label)
            cell.font = Font(bold=True, size=11, color="1F3864", name="Calibri")
            cell.fill = _section_fill()
            cell.alignment = Alignment(horizontal="left", vertical="center")
            cell.border = BORDER
            continue

        lbl_cell = ws.cell(row=r_idx, column=1, value=label)
        lbl_cell.font = Font(bold=True, size=10, name="Calibri")
        lbl_cell.alignment = Alignment(vertical="center")
        lbl_cell.border = BORDER

        val_cell = ws.cell(row=r_idx, column=2, value=value)
        val_cell.font = Font(size=10, name="Calibri")
        val_cell.alignment = Alignment(vertical="center")
        val_cell.border = BORDER

    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 35


def _sheet_conectividad(wb: Workbook, network: dict, state: dict) -> None:
    """
    Quién responde y quién no, para reportar problemas de red.

    Encabeza con el veredicto de la red entera a propósito: sin gateway todo
    sale offline, y la lista de abajo solo acusa a un equipo cuando la lectura
    permite distinguirlo (ver diagnostico_conectividad).
    """
    ws = wb.create_sheet("Conectividad")
    ws.sheet_view.showGridLines = False

    diag = diagnostico_conectividad(network, state)

    # Anchos fijos: el veredicto es un texto largo en una celda combinada y
    # _auto_width ensancharía la columna del ID hasta el tope.
    for col, width in zip("ABCDEFGHIJK",
                          (8, 30, 20, 20, 14, 34, 11, 9, 20, 12, 16)):
        ws.column_dimensions[col].width = width

    ws.merge_cells("A1:K1")
    title = ws["A1"]
    title.value = "DIAGNÓSTICO DE CONECTIVIDAD"
    title.font = Font(bold=True, size=16, color=COLOR_HEADER_FG, name="Calibri")
    title.fill = _header_fill()
    title.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30

    def _dato(row: int, label: str, value) -> None:
        ws.merge_cells(f"A{row}:C{row}")
        lbl = ws.cell(row=row, column=1, value=label)
        lbl.font = Font(bold=True, size=10, name="Calibri")
        lbl.alignment = Alignment(vertical="center")
        ws.merge_cells(f"D{row}:F{row}")
        val = ws.cell(row=row, column=4, value=value)
        val.font = Font(size=10, name="Calibri")
        val.alignment = Alignment(vertical="center")
        for col in range(1, 7):
            ws.cell(row=row, column=col).border = BORDER
        ws.row_dimensions[row].height = 18

    _dato(2, "Red", network.get("name", "-"))
    _dato(3, "Gateway configurado", diag["gateway"] or "— ninguno —")
    _dato(4, "Lectura tomada", datetime.now().strftime("%d/%m/%Y %H:%M"))

    # Veredicto de la red: lo primero que hay que leer antes de mirar la tabla
    _FILL_NIVEL = {
        "online":           (COLOR_EST_OK,     COLOR_TXT_OK),
        "gateway-offline":  (COLOR_EST_AVISO,  COLOR_TXT_AVISO),
        "sin-gateway":      (COLOR_EST_REPOSO, COLOR_TXT_REPOSO),
        "sin-dispositivos": (COLOR_EST_REPOSO, COLOR_TXT_REPOSO),
    }
    if diag["nivel"] == "online" and diag["no_responden"]:
        bg, fg = COLOR_EST_FALLO, COLOR_TXT_FALLO
    else:
        bg, fg = _FILL_NIVEL[diag["nivel"]]

    ws.merge_cells("A6:K6")
    ver = ws.cell(row=6, column=1, value=diag["veredicto"])
    ver.font = Font(bold=True, size=11, color=fg, name="Calibri")
    ver.fill = PatternFill("solid", fgColor=bg)
    ver.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    for col in range(1, 12):
        ws.cell(row=6, column=col).border = BORDER
    ws.row_dimensions[6].height = 34

    ws.merge_cells("A8:K8")
    sec = ws.cell(row=8, column=1, value="RESUMEN")
    sec.font = Font(bold=True, size=11, color=COLOR_COVER_DARK, name="Calibri")
    sec.fill = _section_fill()
    sec.alignment = Alignment(horizontal="left", vertical="center")
    for col in range(1, 12):
        ws.cell(row=8, column=col).border = BORDER
    ws.row_dimensions[8].height = 20

    resumen = [
        ("Dispositivos dados de alta", diag["total"], None),
        ("Responden", diag["responden"], COLOR_EST_OK),
        ("No responden", diag["no_responden"], COLOR_EST_FALLO),
        ("Sin lectura (la red no responde)", diag["sin_lectura"], COLOR_EST_REPOSO),
        ("En reposo (pulsadores a pila)", diag["en_reposo"], COLOR_EST_REPOSO),
        ("Con aviso del equipo", diag["con_aviso"], COLOR_EST_AVISO),
    ]
    for i, (label, value, color) in enumerate(resumen):
        row = 9 + i
        _dato(row, label, value)
        if color:
            ws.cell(row=row, column=4).fill = PatternFill("solid", fgColor=color)

    # Nota al pie del resumen: por qué "En reposo" no es una avería
    nota = ws.cell(
        row=15, column=1,
        value="Los pulsadores a pila duermen y solo despiertan al pulsarlos: "
              "que no respondan es normal, no es una avería.",
    )
    ws.merge_cells("A15:K15")
    nota.font = Font(size=9, italic=True, color=COLOR_TXT_REPOSO, name="Calibri")
    nota.alignment = Alignment(vertical="center")
    ws.row_dimensions[15].height = 16

    hdr = 17
    columns = ["ID", "Nombre", "Categoría", "Grupo", "Estado", "Aviso",
               "Encendido", "Nivel", "Escena activa", "Firmware", "Dirección MAC"]
    _write_header_row(ws, hdr, columns)
    ws.row_dimensions[hdr].height = 30

    _ESTADO_COLORES = {
        EST_RESPONDE:    (COLOR_EST_OK,     COLOR_TXT_OK),
        EST_NO_RESPONDE: (COLOR_EST_FALLO,  COLOR_TXT_FALLO),
        EST_REPOSO:      (COLOR_EST_REPOSO, COLOR_TXT_REPOSO),
        EST_SIN_LECTURA: (COLOR_EST_REPOSO, COLOR_TXT_REPOSO),
    }

    for i, d in enumerate(diag["dispositivos"]):
        row = hdr + 1 + i
        values = [
            d["id"], d["name"], d["category"], d["group"], d["estado"],
            d["aviso"] or "-", d["encendido"], d["nivel"], d["escena"],
            d["firmware"], d["address"],
        ]
        _write_data_row(ws, row, values, alternate=(row % 2 == 0))
        ws.row_dimensions[row].height = 18

        bg, fg = _ESTADO_COLORES[d["estado"]]
        estado_cell = ws.cell(row=row, column=5)
        estado_cell.fill = PatternFill("solid", fgColor=bg)
        estado_cell.font = Font(bold=True, size=10, color=fg, name="Calibri")
        estado_cell.alignment = Alignment(horizontal="center", vertical="center")

        if d["aviso"]:
            aviso_cell = ws.cell(row=row, column=6)
            aviso_cell.fill = PatternFill("solid", fgColor=COLOR_EST_AVISO)
            aviso_cell.font = Font(size=10, color=COLOR_TXT_AVISO, name="Calibri")

    if diag["dispositivos"]:
        last = hdr + len(diag["dispositivos"])
        # Filtro para aislar rápido lo que falla al hablar con el cliente
        ws.auto_filter.ref = f"A{hdr}:K{last}"
        _freeze(ws, f"A{hdr + 1}")
    else:
        ws.cell(row=hdr + 1, column=1, value="La red no tiene dispositivos dados de alta.")


def _sheet_elementos(wb: Workbook, network: dict, state: dict, fixtures: dict) -> None:
    ws = wb.create_sheet("Elementos")
    ws.sheet_view.showGridLines = False
    ws.row_dimensions[1].height = 30

    group_map = {g["id"]: g["name"] for g in network.get("groups", [])}

    columns = ["ID", "Nombre", "Categoría", "Fabricante", "Modelo", "Dirección MAC", "Grupo", "Soporta"]
    _write_header_row(ws, 1, columns)

    units = sorted(network.get("units", []), key=lambda u: _classify_unit(u))

    for row_idx, unit in enumerate(units, start=2):
        unit_id    = unit.get("id", "-")
        group_id   = unit.get("groupId", 0)
        group_name = group_map.get(group_id, "-") if group_id else "-"
        fid        = unit.get("fixtureId")
        fixture    = fixtures.get(fid, {}) if fid else {}
        vendor     = fixture.get("vendor") or fixture.get("manufacturer") or "-"
        model      = fixture.get("model") or fixture.get("name") or "-"

        values = [
            unit_id,
            unit.get("name", "-"),
            _classify_unit(unit),
            vendor,
            model,
            unit.get("address", "-"),
            group_name,
            _unit_soporta(unit, fixtures),
        ]
        _write_data_row(ws, row_idx, values, alternate=(row_idx % 2 == 0))
        ws.row_dimensions[row_idx].height = 18

    _auto_width(ws)
    _freeze(ws)


def _sheet_grupos(wb: Workbook, network: dict) -> None:
    """
    Una fila por dispositivo de cada grupo, con su ID. Las columnas del grupo
    (ID, Nombre, Nº Dispositivos) se combinan verticalmente por bloque.
    """
    ws = wb.create_sheet("Grupos")
    ws.sheet_view.showGridLines = False
    ws.row_dimensions[1].height = 30

    units = network.get("units", [])
    # Map groupId → list of (unit_id, unit_name)
    group_units: dict[int, list[tuple]] = {}
    for u in units:
        gid = u.get("groupId")
        if gid:
            group_units.setdefault(gid, []).append(
                (u.get("id"), u.get("name", str(u.get("id"))))
            )

    columns = ["ID", "Nombre", "Nº Dispositivos", "ID Disp.", "Dispositivo"]
    _write_header_row(ws, 1, columns)

    groups = sorted(network.get("groups", []), key=lambda g: g.get("name", ""))

    N_GROUP_COLS = 3  # columnas del grupo que se combinan por bloque

    row = 2
    for group_idx, group in enumerate(groups):
        gid = group.get("id")
        devs = sorted(group_units.get(gid, []), key=lambda d: d[1])

        block = max(1, len(devs))
        alternate = group_idx % 2 == 1

        for i in range(block):
            if devs:
                dev_id, dev_name = devs[i]
            else:
                dev_id, dev_name = "-", "-"
            values = [
                gid if i == 0 else None,
                group.get("name", "-") if i == 0 else None,
                len(devs) if i == 0 else None,
                dev_id,
                dev_name,
            ]
            _write_data_row(ws, row + i, values, alternate=alternate)
            ws.row_dimensions[row + i].height = 18

        if block > 1:
            for col in range(1, N_GROUP_COLS + 1):
                col_letter = get_column_letter(col)
                ws.merge_cells(f"{col_letter}{row}:{col_letter}{row + block - 1}")
        for col in range(1, N_GROUP_COLS + 1):
            cell = ws.cell(row=row, column=col)
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)

        row += block

    if not groups:
        ws.cell(row=2, column=1, value="No hay grupos en esta red.")

    _auto_width(ws)
    _freeze(ws)


def _sheet_escenas(wb: Workbook, network: dict, scene_levels: dict | None = None) -> None:
    """
    Una fila por dispositivo de cada escena, con su intensidad anotada.
    Las columnas de la escena (ID, Nombre, Tipo, Nº) se combinan verticalmente
    por bloque. scene_levels: {scene_id: {unit_id: nivel}} (niveles en %).
    """
    ws = wb.create_sheet("Escenas")
    ws.sheet_view.showGridLines = False
    ws.row_dimensions[1].height = 30

    if scene_levels is None:
        scene_levels = {}

    unit_map = {u.get("id"): u.get("name", str(u.get("id"))) for u in network.get("units", [])}

    columns = ["ID", "Nombre", "Tipo", "Nº Dispositivos", "Dispositivo", "Intensidad"]
    _write_header_row(ws, 1, columns)

    scenes = sorted(network.get("scenes", []), key=lambda s: s.get("position", 0))

    row = 2
    for scene_idx, scene in enumerate(scenes):
        scene_units_raw = scene.get("units", {})
        if isinstance(scene_units_raw, dict):
            scene_unit_ids = [v.get("id") for v in scene_units_raw.values()]
        else:
            scene_unit_ids = [v.get("id") for v in scene_units_raw]

        levels = scene_levels.get(str(scene.get("id")), {})
        devices = sorted(
            (
                (unit_map.get(uid, str(uid)), levels.get(str(uid), ""))
                for uid in scene_unit_ids if uid is not None
            ),
            key=lambda d: d[0],
        )

        block = max(1, len(devices))
        alternate = scene_idx % 2 == 1

        for i in range(block):
            if devices:
                dev_name, dev_level = devices[i]
                dev_level = f"{dev_level} %" if dev_level != "" else "-"
            else:
                dev_name, dev_level = "-", "-"
            values = [
                scene.get("id", "-") if i == 0 else None,
                scene.get("name", "-") if i == 0 else None,
                scene.get("type", "-") if i == 0 else None,
                len(devices) if i == 0 else None,
                dev_name,
                dev_level,
            ]
            _write_data_row(ws, row + i, values, alternate=alternate)
            ws.row_dimensions[row + i].height = 18

        # Combinar las columnas de la escena a lo alto del bloque
        if block > 1:
            for col in range(1, 5):
                col_letter = get_column_letter(col)
                ws.merge_cells(f"{col_letter}{row}:{col_letter}{row + block - 1}")
        for col in range(1, 5):
            cell = ws.cell(row=row, column=col)
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)

        row += block

    if not scenes:
        ws.cell(row=2, column=1, value="No hay escenas en esta red.")

    _auto_width(ws)
    _freeze(ws)


def _sheet_luminarias(wb: Workbook, network: dict, state: dict, fixtures: dict | None = None) -> None:
    if fixtures is None:
        fixtures = {}
    ws = wb.create_sheet("Luminarias")
    ws.sheet_view.showGridLines = False
    ws.row_dimensions[1].height = 30

    group_map = {g["id"]: g["name"] for g in network.get("groups", [])}

    luminarias = [
        u for u in network.get("units", [])
        if u.get("type") == "Luminaire"
        or (u.get("type") not in ("BatterySwitch", "Switch", "Sensor", "Gateway")
            and any(c.get("type", "") in ("Dimmer", "OnOff", "ColorTemperature", "RGB")
                    for c in u.get("controls", [])))
    ]
    luminarias.sort(key=lambda u: (u.get("groupId") or 0, u.get("position") or 0))

    columns = [
        "ID", "Nombre", "Dirección MAC", "Versión Firmware",
        "Fixture ID", "Grupo", "Soporta",
    ]
    _write_header_row(ws, 1, columns)

    for row_idx, unit in enumerate(luminarias, start=2):
        uid = unit.get("id")
        gid = unit.get("groupId", 0)

        values = [
            uid,
            unit.get("name", "-"),
            unit.get("address", "-"),
            unit.get("firmwareVersion", "-"),
            unit.get("fixtureId", "-"),
            group_map.get(gid, "-") if gid else "-",
            _unit_soporta(unit, fixtures),
        ]
        _write_data_row(ws, row_idx, values, alternate=(row_idx % 2 == 0))
        ws.row_dimensions[row_idx].height = 18

    if not luminarias:
        ws.cell(row=2, column=1, value="No se encontraron luminarias en esta red.")

    _auto_width(ws)
    _freeze(ws)


def _sheet_sensores(wb: Workbook, network: dict, state: dict, fixtures: dict | None = None,
                    sensor_config: dict | None = None) -> None:
    if fixtures is None:
        fixtures = {}
    if sensor_config is None:
        sensor_config = {}
    ws = wb.create_sheet("Sensores")
    ws.sheet_view.showGridLines = False
    ws.row_dimensions[1].height = 30

    SENSOR_TYPES = {"sensor", "occupancysensor", "lightsensor", "motionsensor", "multisensor"}
    SENSOR_CONTROLS = {"Presence", "Lux", "Motion", "Temperature", "Humidity", "PIR",
                       "OccupancySensor", "LightSensor"}

    sensores = [
        u for u in network.get("units", [])
        if (u.get("type") or "").lower() in SENSOR_TYPES
        or any(c.get("type", "") in SENSOR_CONTROLS for c in u.get("controls", []))
    ]
    sensores.sort(key=lambda u: u.get("name", ""))

    columns = [
        "ID", "Nombre", "Tipo (API)", "Dirección MAC",
        "Versión Firmware", "Fixture ID", "Soporta",
        "Modo", "Escena (Presencia)", "Escena (Ausencia)",
    ]
    _write_header_row(ws, 1, columns)

    for row_idx, unit in enumerate(sensores, start=2):
        uid = unit.get("id")
        cfg = sensor_config.get(str(uid), {})

        values = [
            uid,
            unit.get("name", "-"),
            unit.get("type", "-"),
            unit.get("address", "-"),
            unit.get("firmwareVersion", "-"),
            unit.get("fixtureId", "-"),
            _unit_soporta(unit, fixtures),
            cfg.get("modo", "") or "-",
            cfg.get("escena_presencia", "") or "-",
            cfg.get("escena_ausencia", "") or "-",
        ]
        _write_data_row(ws, row_idx, values, alternate=(row_idx % 2 == 0))
        ws.row_dimensions[row_idx].height = 18

    if not sensores:
        ws.cell(row=2, column=1, value="No se encontraron sensores en esta red.")

    _auto_width(ws)
    _freeze(ws)


def _sheet_pulsadores(wb: Workbook, network: dict, state: dict,
                      button_config: dict | None = None) -> None:
    """
    Una fila por botón de cada pulsador, con su uso y programación anotados.
    button_config: {unit_id: {"count": n, "buttons": {n: {"usa", "programado"}}}}.
    """
    ws = wb.create_sheet("Pulsadores")
    ws.sheet_view.showGridLines = False
    ws.row_dimensions[1].height = 30

    if button_config is None:
        button_config = {}

    SWITCH_TYPES = {"batteryswitch", "switch", "pushbutton"}

    pulsadores = [
        u for u in network.get("units", [])
        if (u.get("type") or "").lower() in SWITCH_TYPES
        or "PushButton" in [c.get("type", "") for c in u.get("controls", [])]
    ]
    pulsadores.sort(key=lambda u: u.get("name", ""))

    columns = ["ID", "Nombre", "Tipo (API)", "Dirección MAC",
               "Nº Botones", "Botón", "Usa", "Programado"]
    _write_header_row(ws, 1, columns)

    N_UNIT_COLS = 5  # columnas de la unidad que se combinan por bloque

    row = 2
    for unit_idx, unit in enumerate(pulsadores):
        uid = unit.get("id")

        cfg = button_config.get(str(uid), {})
        count = int(cfg.get("count") or 0)
        saved = cfg.get("buttons", {})
        buttons = [
            (n,
             saved.get(str(n), {}).get("usa", "") or "-",
             saved.get(str(n), {}).get("programado", "") or "-")
            for n in range(1, count + 1)
        ]

        block = max(1, len(buttons))
        alternate = unit_idx % 2 == 1

        for i in range(block):
            if buttons:
                n, usa, prog = buttons[i]
                btn_label = f"Botón {n}"
            else:
                btn_label, usa, prog = "-", "-", "-"
            values = [
                uid if i == 0 else None,
                unit.get("name", "-") if i == 0 else None,
                unit.get("type", "-") if i == 0 else None,
                unit.get("address", "-") if i == 0 else None,
                (count or "-") if i == 0 else None,
                btn_label,
                usa,
                prog,
            ]
            _write_data_row(ws, row + i, values, alternate=alternate)
            ws.row_dimensions[row + i].height = 18

        if block > 1:
            for col in range(1, N_UNIT_COLS + 1):
                col_letter = get_column_letter(col)
                ws.merge_cells(f"{col_letter}{row}:{col_letter}{row + block - 1}")
        for col in range(1, N_UNIT_COLS + 1):
            cell = ws.cell(row=row, column=col)
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)

        row += block

    if not pulsadores:
        ws.cell(row=2, column=1, value="No se encontraron pulsadores en esta red.")

    _auto_width(ws)
    _freeze(ws)


# ── Planos (galería de Casambi) ───────────────────────────────────────────────
# network['photos'] trae cada foto de la galería con sus 'controls':
# {x, y, width, height, unit} en coordenadas relativas (0-1). Las imágenes
# (fotos e iconos de unidades) deben estar descargadas en images_dir como
# <image_id>.png.

PLANO_MAX_W = 700   # px, ancho máximo de la foto en el reporte
PLANO_MAX_H = 900   # px, alto máximo
MARKER_MIN_D = 30   # px, diámetro mínimo del marcador
MARKER_MAX_D = 64   # px, diámetro máximo
ROW_PX = 20         # px que ocupa una fila de Excel con altura por defecto

_CATEGORY_MARKER_COLORS = {
    "Luminaria":            COLOR_CARD_LUMI,
    "Sensor":               COLOR_CARD_SENSOR,
    "Pulsador / Botonera":  COLOR_CARD_BUTTON,
    "Gateway":              COLOR_CARD_GATEWAY,
}


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def _marker_color(category: str) -> tuple[int, int, int]:
    return _rgb(_CATEGORY_MARKER_COLORS.get(category, COLOR_COVER_DARK))


# Se cachea porque se llamaba en cada marcador dibujado, y en una máquina sin
# las fuentes de Windows o macOS eso son cuatro OSError por marcador antes de
# acertar con DejaVu.
@functools.lru_cache(maxsize=64)
def _marker_font(size: int) -> ImageFont.ImageFont:
    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf",
                 "Helvetica.ttc", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _circular_icon(path: Path, diameter: int) -> Image.Image | None:
    """Recorta la foto del elemento como miniatura circular."""
    try:
        icon = Image.open(path)
    except OSError:
        return None
    icon = ImageOps.exif_transpose(icon).convert("RGB")
    icon = ImageOps.fit(icon, (diameter, diameter), Image.LANCZOS)
    mask = Image.new("L", (diameter * 4, diameter * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, mask.width - 1, mask.height - 1), fill=255)
    icon.putalpha(mask.resize((diameter, diameter), Image.LANCZOS))
    return icon


def _draw_marker(canvas: Image.Image, draw: ImageDraw.ImageDraw,
                 cx: int, cy: int, diameter: int, number: int,
                 color: tuple, icon_path: Path | None) -> None:
    r = diameter // 2
    icon = _circular_icon(icon_path, diameter) if icon_path else None

    if icon is not None:
        canvas.paste(icon, (cx - r, cy - r), icon)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=3)
        # Insignia con el número en la esquina superior derecha
        br = max(10, diameter // 4)
        bx, by = cx + r - br // 2, cy - r + br // 2
        draw.ellipse((bx - br, by - br, bx + br, by + br),
                     fill=color, outline=(255, 255, 255), width=2)
        font = _marker_font(int(br * 1.1))
        draw.text((bx, by), str(number), font=font, fill=(255, 255, 255), anchor="mm")
    else:
        fill = color + (200,)
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        odraw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)
        canvas.alpha_composite(overlay)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255), width=3)
        font = _marker_font(int(diameter * 0.5))
        draw.text((cx, cy), str(number), font=font, fill=(255, 255, 255), anchor="mm")


_LEYENDA_UNIDADES = ["Nº", "ID", "Nombre", "Categoría", "Grupo"]


def _fila_leyenda_unidad(number: int, unit_id, units_by_id: dict,
                         group_map: dict) -> list:
    """Fila de leyenda para un elemento de la red colocado sobre un plano."""
    unit = units_by_id.get(unit_id)
    gid = unit.get("groupId", 0) if unit else 0
    return [
        number,
        unit_id,
        unit.get("name", "-") if unit else f"Unidad {unit_id}",
        _classify_unit(unit) if unit else "Desconocido",
        group_map.get(gid, "-") if gid else "-",
    ]


def _compose_plano(photo: dict, units_by_id: dict, group_map: dict, images_dir: Path
                   ) -> tuple[io.BytesIO, int, int, list[str], list[list]] | None:
    """
    Dibuja los marcadores de la foto sobre la imagen de la galería.
    Devuelve (png_buffer, ancho_px, alto_px, cabeceras, filas) o None si no hay imagen.
    """
    img_id = photo.get("image")
    img_path = images_dir / f"{img_id}.png" if img_id else None
    if not img_path or not img_path.exists():
        return None

    try:
        base = Image.open(img_path)
    except OSError:
        return None
    base = ImageOps.exif_transpose(base).convert("RGBA")

    scale = min(1.0, PLANO_MAX_W / base.width, PLANO_MAX_H / base.height)
    if scale < 1.0:
        base = base.resize((int(base.width * scale), int(base.height * scale)),
                           Image.LANCZOS)
    W, H = base.size
    draw = ImageDraw.Draw(base)

    controls = sorted(
        (c for c in photo.get("controls") or [] if c.get("unit") is not None),
        key=lambda c: (c.get("y", 0), c.get("x", 0)),
    )

    filas: list[list] = []
    for number, ctrl in enumerate(controls, start=1):
        unit = units_by_id.get(ctrl["unit"])
        category = _classify_unit(unit) if unit else "Desconocido"

        cx = int((ctrl.get("x", 0) + ctrl.get("width", 0) / 2) * W)
        cy = int((ctrl.get("y", 0) + ctrl.get("height", 0) / 2) * H)
        cx, cy = max(0, min(W - 1, cx)), max(0, min(H - 1, cy))
        diameter = int(max(MARKER_MIN_D, min(MARKER_MAX_D, ctrl.get("width", 0) * W)))

        icon_id = unit.get("image") if unit else None
        icon_path = images_dir / f"{icon_id}.png" if icon_id else None
        if icon_path is not None and not icon_path.exists():
            icon_path = None

        _draw_marker(base, draw, cx, cy, diameter, number,
                     _marker_color(category), icon_path)
        filas.append(_fila_leyenda_unidad(number, ctrl["unit"], units_by_id, group_map))

    buffer = io.BytesIO()
    base.convert("RGB").save(buffer, format="PNG")
    buffer.seek(0)
    return buffer, W, H, _LEYENDA_UNIDADES, filas


def _manual_marker_diameter(w: int, h: int, n_markers: int) -> int:
    """
    Diámetro del marcador proporcional al plano (~5% del lado mayor),
    reducido gradualmente cuando hay muchos elementos para que no se tapen.
    """
    d = max(w, h) * 0.05
    if n_markers > 15:
        d *= 0.75
    return int(max(24, min(56, d)))


def _compose_plano_manual(plan: dict, units_by_id: dict, group_map: dict,
                          images_dir: Path | None
                          ) -> tuple[io.BytesIO, int, int, list[str], list[list]] | None:
    """
    Dibuja los marcadores de un plano manual (subido en la web).
    plan: {"name", "path", "markers": {unit_id: {"x", "y"}}}.
    Devuelve (png_buffer, ancho_px, alto_px, cabeceras, filas) o None si no hay imagen.
    """
    try:
        base = Image.open(plan["path"])
    except (OSError, KeyError):
        return None
    base = ImageOps.exif_transpose(base).convert("RGBA")

    scale = min(1.0, PLANO_MAX_W / base.width, PLANO_MAX_H / base.height)
    if scale < 1.0:
        base = base.resize((int(base.width * scale), int(base.height * scale)),
                           Image.LANCZOS)
    W, H = base.size
    draw = ImageDraw.Draw(base)

    markers = sorted(
        plan.get("markers", {}).items(),
        key=lambda kv: (kv[1].get("y", 0), kv[1].get("x", 0)),
    )
    diameter = _manual_marker_diameter(W, H, len(markers))

    filas: list[list] = []
    for number, (uid, pos) in enumerate(markers, start=1):
        try:
            unit_id = int(uid)
        except (TypeError, ValueError):
            continue
        unit = units_by_id.get(unit_id)
        category = _classify_unit(unit) if unit else "Desconocido"

        cx = max(0, min(W - 1, int(pos.get("x", 0) * W)))
        cy = max(0, min(H - 1, int(pos.get("y", 0) * H)))

        icon_id = unit.get("image") if unit else None
        icon_path = images_dir / f"{icon_id}.png" if (icon_id and images_dir) else None
        if icon_path is not None and not icon_path.exists():
            icon_path = None

        _draw_marker(base, draw, cx, cy, diameter, number,
                     _marker_color(category), icon_path)
        filas.append(_fila_leyenda_unidad(number, unit_id, units_by_id, group_map))

    buffer = io.BytesIO()
    base.convert("RGB").save(buffer, format="PNG")
    buffer.seek(0)
    return buffer, W, H, _LEYENDA_UNIDADES, filas


_LEYENDA_COBERTURA = ["Nº", "Nodo", "Unidad asociada", "Categoría", "Nota de montaje"]

_SIN_ASOCIAR = "— sin asociar —"


def _compose_plano_cobertura(plan: dict, units_by_id: dict,
                             images_dir: Path | None
                             ) -> tuple[io.BytesIO, int, int, list[str], list[list]] | None:
    """
    Dibuja un plano importado del Simulador de Cobertura: las paredes del modelo
    de propagación y los nodos simulados, numerados.

    El proyecto .casambi no guarda el mapa de calor —lo calcula el motor Swift en
    vivo y nunca se serializa—, así que aquí se documenta la *planificación*: qué
    se simuló, dónde y con qué paredes. Para el mapa de calor en sí, se exporta
    la imagen desde el simulador y se sube como un plano normal.
    """
    cobertura = plan.get("cobertura") or {}
    try:
        base = Image.open(plan["path"])
    except (OSError, KeyError):
        return None
    base = ImageOps.exif_transpose(base).convert("RGBA")

    scale = min(1.0, PLANO_MAX_W / base.width, PLANO_MAX_H / base.height)
    if scale < 1.0:
        base = base.resize((int(base.width * scale), int(base.height * scale)),
                           Image.LANCZOS)
    W, H = base.size

    # Paredes primero, para que los nodos queden por encima. Van en una capa
    # translúcida: son contexto del cálculo, no deben tapar el plano de obra.
    paredes = cobertura.get("paredes") or []
    huecos = cobertura.get("huecos") or []
    if paredes or huecos:
        grosor = max(2, round(max(W, H) / 400))
        capa = Image.new("RGBA", base.size, (0, 0, 0, 0))
        cdraw = ImageDraw.Draw(capa)
        # Huecos de losa debajo de las paredes: donde el nivel no tiene piso.
        for hueco in huecos:
            puntos = [(x * W, y * H) for x, y in hueco.get("puntos") or []]
            if len(puntos) >= 3:
                cdraw.polygon(puntos, fill=_rgb(COLOR_COB_HUECO) + (45,),
                              outline=_rgb(COLOR_COB_HUECO) + (200,))
        for pared in paredes:
            cdraw.line(
                (pared.get("x1", 0) * W, pared.get("y1", 0) * H,
                 pared.get("x2", 0) * W, pared.get("y2", 0) * H),
                fill=_rgb(COLOR_COB_PARED) + (170,), width=grosor,
            )
        base.alpha_composite(capa)

    draw = ImageDraw.Draw(base)
    nodos = sorted(cobertura.get("nodos") or [],
                   key=lambda n: (n.get("y", 0), n.get("x", 0)))
    diameter = _manual_marker_diameter(W, H, len(nodos))

    filas: list[list] = []
    for number, nodo in enumerate(nodos, start=1):
        unit = units_by_id.get(nodo.get("unit_id"))
        if unit is not None:
            category = _classify_unit(unit)
            color = _marker_color(category)
        else:
            # Sin unidad asociada el nodo sigue siendo una posición propuesta;
            # el color lo distingue de lo que ya está instalado.
            category = "-"
            color = (_marker_color("Gateway") if nodo.get("gateway")
                     else _rgb(COLOR_COB_NODO))

        cx = max(0, min(W - 1, int(nodo.get("x", 0) * W)))
        cy = max(0, min(H - 1, int(nodo.get("y", 0) * H)))

        icon_id = unit.get("image") if unit else None
        icon_path = images_dir / f"{icon_id}.png" if (icon_id and images_dir) else None
        if icon_path is not None and not icon_path.exists():
            icon_path = None

        _draw_marker(base, draw, cx, cy, diameter, number, color, icon_path)

        etiqueta = nodo.get("label") or "?"
        if nodo.get("modelo"):
            etiqueta += f" ({nodo['modelo']})"
        if nodo.get("gateway"):
            etiqueta += " · gateway"
        filas.append([
            number,
            etiqueta,
            unit.get("name", "-") if unit else _SIN_ASOCIAR,
            category,
            nodo.get("nota") or "-",
        ])

    buffer = io.BytesIO()
    base.convert("RGB").save(buffer, format="PNG")
    buffer.seek(0)
    return buffer, W, H, _LEYENDA_COBERTURA, filas


def _subtitulo_cobertura(plan: dict) -> str:
    """Una línea con lo que hace falta para leer el plano con criterio."""
    c = plan.get("cobertura") or {}
    partes = []
    if c.get("cliente"):
        partes.append(f"Cliente: {c['cliente']}")
    if c.get("nivel"):
        partes.append(f"Nivel: {c['nivel']} (de {c.get('niveles') or '?'})")
    if c.get("ancho_m") and c.get("alto_m"):
        partes.append(f"{c['ancho_m']} × {c['alto_m']} m")
    if c.get("n") is not None:
        partes.append(f"exponente n = {c['n']}")
    partes.append(f"{len(c.get('paredes') or [])} pared(es) modeladas")
    if c.get("huecos"):
        partes.append(f"{len(c['huecos'])} hueco(s) de losa")
    partes.append("simulación estimativa (±10 dB), sin mapa de calor")
    return " · ".join(partes)


def _write_plano_section(ws, row: int, title: str, composed: tuple,
                         subtitle: str = "") -> int:
    """Escribe un plano (título + imagen + leyenda) y devuelve la fila siguiente."""
    buffer, width_px, height_px, headers, filas = composed

    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
    cell = ws.cell(row=row, column=1, value=title)
    cell.font = Font(bold=True, size=13, color=COLOR_HEADER_FG, name="Calibri")
    cell.fill = _header_fill()
    cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[row].height = 26
    row += 1

    if subtitle:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
        sub = ws.cell(row=row, column=1, value=subtitle)
        sub.font = Font(italic=True, size=9, color=COLOR_COB_NOTA, name="Calibri")
        sub.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    row += 1

    img = XLImage(buffer)
    img.width, img.height = width_px, height_px
    ws.add_image(img, f"A{row}")
    row += -(-height_px // ROW_PX) + 2  # ceil + margen

    # Sin marcadores no hay nada que numerar: una cabecera sola confunde más
    # que ayuda, así que la leyenda solo se escribe si tiene filas.
    if filas:
        _write_header_row(ws, row, headers)
        row += 1
        for i, valores in enumerate(filas):
            _write_data_row(ws, row, valores, alternate=(i % 2 == 1))
            ws.row_dimensions[row].height = 18
            row += 1

    return row + 2  # separación entre planos


def _sheet_planos(wb: Workbook, network: dict, images_dir: Path | None,
                  manual_planos: list | None = None) -> None:
    """
    Una sección por cada foto de la galería de Casambi con elementos colocados,
    seguida de los planos manuales subidos en la web: la imagen con marcadores
    numerados y su leyenda debajo.

    Los planos importados del Simulador de Cobertura van al final, con su propia
    leyenda de nodos: son planificación, no inventario, y mezclarlos con lo
    instalado haría leer una propuesta como si fuera un hecho.
    """
    ws = wb.create_sheet("Planos")
    ws.sheet_view.showGridLines = False

    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 10
    ws.column_dimensions["C"].width = 40
    ws.column_dimensions["D"].width = 22
    ws.column_dimensions["E"].width = 30

    units_by_id = {u.get("id"): u for u in network.get("units", [])}
    group_map = {g["id"]: g.get("name", "-") for g in network.get("groups", [])}

    sections: list[tuple[str, tuple, str]] = []

    if images_dir is not None:
        photos = [
            p for p in network.get("photos") or []
            if p.get("controls") and p.get("image")
        ]
        for idx, photo in enumerate(photos, start=1):
            composed = _compose_plano(photo, units_by_id, group_map, images_dir)
            if composed is not None:
                nombre = photo.get("name") or f"Foto {idx}"
                sections.append((f"PLANO: {nombre}", composed, ""))

    planos = list(manual_planos or [])
    for plan in (p for p in planos if not p.get("cobertura")):
        composed = _compose_plano_manual(plan, units_by_id, group_map, images_dir)
        if composed is not None:
            sections.append((f"PLANO: {plan.get('name') or 'Plano'}", composed, ""))

    for plan in (p for p in planos if p.get("cobertura")):
        composed = _compose_plano_cobertura(plan, units_by_id, images_dir)
        if composed is not None:
            titulo = f"COBERTURA SIMULADA: {plan.get('name') or 'Simulación'}"
            sections.append((titulo, composed, _subtitulo_cobertura(plan)))

    if not sections:
        ws.cell(row=2, column=1,
                value="Esta red no tiene fotos con elementos colocados en la "
                      "galería de Casambi ni planos subidos en la web.")
        return

    row = 1
    for title, composed, subtitle in sections:
        row = _write_plano_section(ws, row, title, composed, subtitle)


def _sheet_horarios(wb: Workbook, schedules: list | None = None) -> None:
    """
    Horarios documentados por el usuario (el API de Casambi no los expone).
    """
    ws = wb.create_sheet("Horarios")
    ws.sheet_view.showGridLines = False
    ws.row_dimensions[1].height = 30

    if schedules is None:
        schedules = []

    columns = ["Nombre", "Días", "Encendido", "Apagado", "Escena", "Habilitado"]
    _write_header_row(ws, 1, columns)

    for row_idx, h in enumerate(schedules, start=2):
        values = [
            h.get("nombre", "") or "-",
            h.get("dias", "") or "-",
            h.get("encendido", "") or "-",
            h.get("apagado", "") or "-",
            h.get("escena", "") or "-",
            "Sí" if h.get("habilitado") else "No",
        ]
        _write_data_row(ws, row_idx, values, alternate=(row_idx % 2 == 0))
        ws.row_dimensions[row_idx].height = 18

    if not schedules:
        ws.cell(row=2, column=1, value="No hay horarios documentados.")

    _auto_width(ws)
    _freeze(ws)


def _sheet_bitacora(wb: Workbook, bitacora: list | None = None) -> None:
    """
    Historial de intervenciones sobre la red.

    Las demás hojas describen cómo está la instalación; esta, cómo llegó a
    estarlo. Va en el informe porque es lo que el cliente necesita ver para
    aprobar un cambio o recordar uno anterior: sin ella, el trabajo hecho se
    queda dentro de la app.

    Se ordena de más reciente a más antigua, que es como se consulta.
    """
    ws = wb.create_sheet("Bitácora")
    ws.sheet_view.showGridLines = False
    ws.row_dimensions[1].height = 30

    if bitacora is None:
        bitacora = []

    columns = ["Fecha", "Tipo", "Técnico", "Solicitado por", "Descripción",
               "Pendiente", "Origen"]
    _write_header_row(ws, 1, columns)

    def _clave(entrada: dict) -> str:
        return str(entrada.get("fecha") or entrada.get("creado") or "")

    for row_idx, b in enumerate(sorted(bitacora, key=_clave, reverse=True), start=2):
        veces = int(b.get("veces", 1) or 1)
        origen = "Automática" if b.get("origen") == "automatica" else "Manual"
        if veces > 1:
            origen += f" (×{veces})"
        if b.get("editado"):
            origen += " · editada"

        values = [
            str(b.get("fecha", "") or "-").replace("T", " "),
            b.get("tipo", "") or "-",
            b.get("tecnico", "") or "-",
            b.get("solicitado_por", "") or "-",
            b.get("descripcion", "") or "-",
            b.get("pendiente", "") or "-",
            origen,
        ]
        _write_data_row(ws, row_idx, values, alternate=(row_idx % 2 == 0))
        ws.row_dimensions[row_idx].height = 18

    if not bitacora:
        ws.cell(row=2, column=1, value="Sin intervenciones registradas.")

    _auto_width(ws)
    _freeze(ws)


# ── Public entry point ────────────────────────────────────────────────────────

# Cuántos días se conservan los informes generados. En el escritorio daba igual
# que se acumularan; en un servidor compartido, `reportes/` solo crecía.
DIAS_INFORMES = 7


def _nombre_informe(net_name: str | None) -> str:
    """
    Nombre único para el .xlsx del informe.

    Antes la marca de tiempo tenía granularidad de minuto y el nombre de la red
    se usaba tal cual: dos descargas de la misma red en el mismo minuto daban el
    mismo nombre, y la segunda sobrescribía el fichero mientras `send_file` podía
    estar leyendo la primera. El nombre de la red viene del API de Casambi y trae
    comas, barras y acentos, así que se sanea.
    """
    limpio = re.sub(r"[^\w.-]+", "_", (net_name or "red").strip()).strip("_.")
    limpio = (limpio or "red")[:60]
    marca = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"Casambi_{limpio}_{marca}_{uuid.uuid4().hex[:6]}.xlsx"


def _purgar_informes(directorio: Path, dias: int = DIAS_INFORMES) -> int:
    """Borra los informes más viejos de `dias`. Devuelve cuántos quitó."""
    limite = time.time() - dias * 86400
    quitados = 0
    try:
        candidatos = list(directorio.glob("Casambi_*.xlsx"))
    except OSError:
        return 0
    for fichero in candidatos:
        try:
            if fichero.stat().st_mtime < limite:
                fichero.unlink()
                quitados += 1
        except OSError:
            continue  # que no se caiga la descarga por no poder limpiar
    return quitados


def generate_report(network: dict, state: dict, fixtures: dict | None = None,
                    scene_levels: dict | None = None,
                    button_config: dict | None = None,
                    sensor_config: dict | None = None,
                    schedules: list | None = None,
                    bitacora: list | None = None,
                    images_dir: Path | None = None,
                    manual_planos: list | None = None,
                    output_dir: str = ".") -> Path:
    """
    Build the Excel report from raw API data and save it.
    scene_levels: {scene_id: {unit_id: nivel%}} — intensidades anotadas por el usuario.
    button_config: {unit_id: {"count", "buttons"}} — botones anotados de pulsadores.
    sensor_config: {unit_id: {"modo", "escena_presencia", "escena_ausencia"}} — sensores anotados.
    schedules: lista de horarios documentados por el usuario.
    bitacora: historial de intervenciones sobre la red.
    images_dir: carpeta con las imágenes de la red (<image_id>.png) para la hoja Planos.
    manual_planos: [{"name", "path", "markers", "cobertura"}] — planos subidos
        manualmente; "cobertura" solo lo traen los importados del simulador.
    Returns the Path of the generated file.
    """
    if fixtures is None:
        fixtures = {}

    wb = Workbook()

    _sheet_portada(wb, network, state)
    _sheet_red(wb, network, state)
    _sheet_conectividad(wb, network, state)
    _sheet_elementos(wb, network, state, fixtures)
    _sheet_luminarias(wb, network, state, fixtures)
    _sheet_pulsadores(wb, network, state, button_config=button_config)
    _sheet_sensores(wb, network, state, fixtures, sensor_config=sensor_config)
    _sheet_grupos(wb, network)
    _sheet_escenas(wb, network, scene_levels=scene_levels)
    _sheet_horarios(wb, schedules=schedules)
    _sheet_bitacora(wb, bitacora=bitacora)
    _sheet_planos(wb, network, images_dir, manual_planos=manual_planos)

    filepath = Path(output_dir) / _nombre_informe(network.get("name"))
    wb.save(filepath)
    _purgar_informes(Path(output_dir))
    return filepath
