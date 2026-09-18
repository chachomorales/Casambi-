"""
Casambi Network Report — servidor interno de la app nativa.

Este módulo NO se ejecuta por sí solo: `desktop.py` lo importa y arranca el
servidor Flask en un hilo, en un puerto local aleatorio, para servir la
interfaz dentro de la ventana de macOS (WKWebView). Para correr la app:

    .venv/bin/python desktop.py

Las credenciales de Casambi se guardan en el Llavero de macOS y se editan
desde la pantalla de Ajustes de la propia app (ver credentials.py).
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)

import cobertura
import config
import credentials
from casambi_api import CasambiAPIError, CasambiClient
from report import (
    _classify_unit,
    _controls_summary,
    _fixture_controls_summary,
    diagnostico_conectividad,
    generate_report,
)

# Como app de escritorio empaquetada, los datos escribibles viven fuera del
# bundle (CASAMBI_HOME → ~/Library/Application Support/CASAMBI); sin la
# variable, todo queda junto al código como siempre.
_HOME = Path(os.environ["CASAMBI_HOME"]) if os.environ.get("CASAMBI_HOME") else Path(__file__).parent

app = Flask(__name__)

# Clave de sesión, límites de subida y flags de cookie salen de config, que los
# decide según CASAMBI_MODE: en el servidor la clave tiene que ser estable
# (cada reinicio invalidaría los tokens CSRF), en el escritorio da igual.
config.aplicar(app)

LOGOS_DIR = Path(__file__).parent / "logos"
REPORTS_DIR = _HOME / "reportes"
DATA_DIR = _HOME / "data"

# ── Estado en memoria ─────────────────────────────────────────────────────────
_state: dict = {
    "clients": {},         # account_id → CasambiClient autenticado
    "networks": None,      # redes de todas las cuentas (None = sin cargar aún)
    "cache": {},           # network_id → {network, state, fixtures, fetched_at}
    "error": None,         # errores de autenticación, por cuenta
}

# Progreso de la carga en curso. La interfaz lo consulta desde la pantalla de
# carga para no dejar la ventana en blanco mientras se habla con Casambi Cloud.
_load: dict = {
    "active": False,
    "label": "",
    "done": 0,
    "total": 0,
    "error": None,
}
_load_lock = threading.Lock()


def _progress(label: str, done: int = 0, total: int = 0) -> None:
    _load.update({"label": label, "done": done, "total": total})


def _start_loading(work) -> None:
    """Lanza `work(progress)` en segundo plano si no hay otra carga en curso."""
    with _load_lock:
        if _load["active"]:
            return
        _load.update({"active": True, "label": "Preparando…", "done": 0,
                      "total": 0, "error": None})

    def run() -> None:
        try:
            work(_progress)
        except CasambiAPIError as e:
            _load["error"] = str(e)
        except Exception as e:  # que un fallo inesperado no deje la barra colgada
            _load["error"] = f"Error inesperado: {e}"
        finally:
            _load["active"] = False

    threading.Thread(target=run, daemon=True).start()


# ── Cuentas y autenticación ───────────────────────────────────────────────────

def _authenticate(progress=None) -> None:
    """Autentica todas las cuentas del Llavero y reúne sus redes."""
    accounts = credentials.load_all(_HOME)
    clients: dict[str, CasambiClient] = {}
    networks: list[dict] = []
    errors: list[str] = []

    for i, acc in enumerate(accounts):
        if progress:
            progress(f"Conectando con {acc['label']}…", i, len(accounts))

        client = CasambiClient(acc["api_key"])
        try:
            user_session = client.create_user_session(acc["email"], acc["password"])
            networks_session = client.create_networks_session(
                acc["email"], acc["password"]
            )
        except CasambiAPIError as e:
            errors.append(f"{acc['label']}: {e}")
            continue

        clients[acc["id"]] = client
        for net in client.list_networks(user_session, networks_session):
            net["account_id"] = acc["id"]
            net["account_label"] = acc["label"]
            networks.append(net)

    networks.sort(key=lambda n: (n.get("name") or "").lower())
    if progress:
        progress("Listo", len(accounts), len(accounts))

    _state["clients"] = clients
    _state["networks"] = networks
    _state["error"] = " · ".join(errors) if errors else None


def _networks_loaded() -> bool:
    return _state["networks"] is not None


def _get_networks() -> list[dict]:
    return _state["networks"] or []


def _find_network_meta(network_id: str) -> dict | None:
    for net in _get_networks():
        if str(net.get("id")) == str(network_id):
            return net
    return None


def _client_for(network_id: str) -> CasambiClient | None:
    """Cliente de la cuenta a la que pertenece esa red."""
    meta = _find_network_meta(network_id)
    if meta is None:
        return None
    return _state["clients"].get(meta.get("account_id"))


def _fetch_network_data(network_id: str, progress=None) -> dict:
    """Descarga red, estado y modelos, informando del progreso."""
    client = _client_for(network_id)
    if client is None:
        raise CasambiAPIError(
            _state["error"] or "Esa red no pertenece a ninguna cuenta configurada."
        )

    meta = _find_network_meta(network_id) or {}

    if progress:
        progress("Descargando la configuración de la red…", 0, 3)
    network = client.get_network(network_id)
    network["site_name"] = meta.get("site_name", "")

    if progress:
        progress("Leyendo el estado de los dispositivos…", 1, 3)
    state = client.get_network_state(network_id)

    units = network.get("units", [])
    if progress:
        progress("Obteniendo los modelos de los dispositivos…", 2, 3)

    def fixture_progress(done: int, total: int) -> None:
        if progress:
            progress(f"Obteniendo los modelos ({done} de {total})…", done, total)

    try:
        fixtures = client.get_fixtures_for_units(units, progress=fixture_progress)
    except Exception:
        fixtures = {}

    data = {
        "network": network,
        "state": state,
        "fixtures": fixtures,
        "fetched_at": datetime.now(),
    }
    _state["cache"][str(network_id)] = data
    return data


def _get_network_data(network_id: str, force: bool = False) -> dict:
    """Datos de la red desde la caché, descargándolos si hace falta."""
    key = str(network_id)
    if not force and key in _state["cache"]:
        return _state["cache"][key]
    return _fetch_network_data(network_id)


# ── Imágenes de la red (galería y fotos de elementos) ────────────────────────
# La galería de Casambi (network['photos']) trae las posiciones de los
# elementos colocados sobre cada foto; las imágenes se descargan una sola vez
# y se cachean en data/images/<image_id>.png (los IDs son inmutables).

IMAGES_DIR = DATA_DIR / "images"


def _download_network_images(network_id: str, network: dict) -> Path:
    """Descarga las fotos de la galería y los iconos de las unidades colocadas."""
    client = _client_for(network_id)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    if client is None:
        return IMAGES_DIR

    photos = network.get("photos") or []
    unit_image_map = {
        u.get("id"): u.get("image")
        for u in network.get("units", []) if u.get("image")
    }

    image_ids: set[str] = set()
    for photo in photos:
        if photo.get("image"):
            image_ids.add(photo["image"])
        for ctrl in photo.get("controls") or []:
            icon = unit_image_map.get(ctrl.get("unit"))
            if icon:
                image_ids.add(icon)

    # Iconos de los elementos colocados en planos manuales (subidos en la web) y
    # de las unidades que el usuario haya asociado a un nodo de cobertura.
    for plano in _load_planos(network_id):
        uids = list(plano.get("markers", {}))
        uids += [n.get("unit_id")
                 for n in (plano.get("cobertura") or {}).get("nodos", [])]
        for uid in uids:
            try:
                icon = unit_image_map.get(int(uid))
            except (TypeError, ValueError):
                continue
            if icon:
                image_ids.add(icon)

    for img_id in image_ids:
        path = IMAGES_DIR / f"{img_id}.png"
        if path.exists():
            continue
        try:
            path.write_bytes(client.get_image(network_id, img_id))
        except (CasambiAPIError, OSError):
            pass  # la foto simplemente no aparecerá en el reporte
    return IMAGES_DIR


# ── Planos manuales (subidos por el usuario en PDF/JPG/PNG) ──────────────────
# Para redes sin galería (o con planos arquitectónicos formales) el usuario
# sube un plano y coloca los elementos haciendo clic. Se persiste en
# data/planos_<red>.json como [{"id", "name", "image", "markers"}], donde
# markers = {unit_id: {"x": 0-1, "y": 0-1}} (coordenadas relativas, igual que
# la galería de Casambi). Las imágenes viven en data/planos/ como PNG.
#
# Un plano importado del Simulador de Cobertura (.casambi) añade dos claves:
# "origen": "cobertura" y un bloque "cobertura" con los nodos y las paredes ya
# en relativas (ver cobertura.py). Sus nodos NO son `markers`: los coloca el
# simulador, no el usuario, así que ese plano no admite el flujo de clic para
# situar elementos — solo asociar cada nodo con una unidad real de la red.

PLANOS_DIR = DATA_DIR / "planos"
PLANO_ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png"}
PLANO_MAX_PX = 2200  # lado máximo de la imagen guardada


def _planos_path(network_id: str) -> Path:
    return DATA_DIR / f"planos_{network_id}.json"


def _load_planos(network_id: str) -> list:
    path = _planos_path(network_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_planos(network_id: str, items: list) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    _planos_path(network_id).write_text(
        json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _pdf_to_image(data: bytes, page_num: int) -> Image.Image:
    """Convierte una página de un PDF a imagen (PyMuPDF, sin binarios externos)."""
    import pymupdf  # import diferido: el resto de la app funciona sin PyMuPDF

    doc = pymupdf.open(stream=data, filetype="pdf")
    if not 1 <= page_num <= doc.page_count:
        raise ValueError(f"El PDF tiene {doc.page_count} página(s); pediste la {page_num}")
    pix = doc[page_num - 1].get_pixmap(dpi=150)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


# ── Intensidades de escena (editadas por el usuario) ─────────────────────────
# El API de Casambi no expone el nivel de dimming programado en cada escena,
# así que se permite anotarlo manualmente y se persiste en data/<red>.json
# con la forma {scene_id: {unit_id: nivel}}.

def _levels_path(network_id: str) -> Path:
    return DATA_DIR / f"scene_levels_{network_id}.json"


def _load_scene_levels(network_id: str) -> dict:
    path = _levels_path(network_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_scene_level(network_id: str, scene_id: str, unit_id: str, level: str) -> None:
    levels = _load_scene_levels(network_id)
    scene_levels = levels.setdefault(str(scene_id), {})
    if level == "":
        scene_levels.pop(str(unit_id), None)
    else:
        scene_levels[str(unit_id)] = level
    DATA_DIR.mkdir(exist_ok=True)
    _levels_path(network_id).write_text(
        json.dumps(levels, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── Configuración de botones de pulsadores (anotada por el usuario) ──────────
# El API no expone cuántos botones físicos tiene un pulsador ni qué hace cada
# uno, así que se anota manualmente y se persiste en data/buttons_<red>.json
# con la forma {unit_id: {"count": n, "buttons": {n: {"usa": ..., "programado": ...}}}}.

BUTTON_USA_OPTIONS = ["Luminaria", "Elemento", "Grupo", "Escena"]


def _buttons_path(network_id: str) -> Path:
    return DATA_DIR / f"buttons_{network_id}.json"


def _load_buttons(network_id: str) -> dict:
    path = _buttons_path(network_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_buttons(network_id: str, cfg: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    _buttons_path(network_id).write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── Configuración de sensores (anotada por el usuario) ───────────────────────
# El API no expone el modo de operación de un sensor (presencia/ausencia) ni
# qué escena activa; se anota manualmente y se persiste en data/sensors_<red>.json
# con la forma {unit_id: {"modo", "escena_presencia", "escena_ausencia"}}.
# Los sensores solo activan escenas: en modo "Presencia + Ausencia" llevan una
# escena para cada evento; en los otros modos solo una.

SENSOR_MODO_OPTIONS = ["Presencia", "Ausencia", "Presencia + Ausencia"]


def _sensors_path(network_id: str) -> Path:
    return DATA_DIR / f"sensors_{network_id}.json"


def _load_sensors(network_id: str) -> dict:
    path = _sensors_path(network_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_sensors(network_id: str, cfg: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    _sensors_path(network_id).write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── Horarios (anotados por el usuario) ────────────────────────────────────────
# El API de Casambi no expone los timers/horarios de la red; se documentan
# manualmente y se persisten en data/schedules_<red>.json como una lista de
# {"id", "nombre", "dias", "encendido", "apagado", "escena", "habilitado"}.

def _schedules_path(network_id: str) -> Path:
    return DATA_DIR / f"schedules_{network_id}.json"


def _load_schedules(network_id: str) -> list:
    path = _schedules_path(network_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_schedules(network_id: str, items: list) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    _schedules_path(network_id).write_text(
        json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── Clasificación y preparación de datos para las vistas ─────────────────────

SENSOR_TYPES = {"sensor", "occupancysensor", "lightsensor", "motionsensor", "multisensor"}
SENSOR_CONTROLS = {"Presence", "Lux", "Motion", "Temperature", "Humidity", "PIR",
                   "OccupancySensor", "LightSensor"}
SWITCH_TYPES = {"batteryswitch", "switch", "pushbutton"}


def _is_luminaria(u: dict) -> bool:
    return u.get("type") == "Luminaire" or (
        u.get("type") not in ("BatterySwitch", "Switch", "Sensor", "Gateway")
        and any(c.get("type", "") in ("Dimmer", "OnOff", "ColorTemperature", "RGB")
                for c in u.get("controls", []))
    )


def _is_sensor(u: dict) -> bool:
    return (u.get("type") or "").lower() in SENSOR_TYPES or any(
        c.get("type", "") in SENSOR_CONTROLS for c in u.get("controls", [])
    )


def _is_pulsador(u: dict) -> bool:
    return (u.get("type") or "").lower() in SWITCH_TYPES or "PushButton" in [
        c.get("type", "") for c in u.get("controls", [])
    ]


def _build_report_context(network_id: str, data: dict) -> dict:
    network = data["network"]
    fixtures = data["fixtures"]

    units = network.get("units", [])
    groups = network.get("groups", [])
    scenes = network.get("scenes", [])

    group_map = {g["id"]: g.get("name", "-") for g in groups}
    unit_map = {u.get("id"): u.get("name", str(u.get("id"))) for u in units}

    type_counts = Counter(_classify_unit(u) for u in units)

    def enrich(u: dict) -> dict:
        fid = u.get("fixtureId")
        fixture = fixtures.get(fid, {}) if fid else {}
        gid = u.get("groupId", 0)
        return {
            "id": u.get("id", "-"),
            "name": u.get("name", "-"),
            "category": _classify_unit(u),
            "type": u.get("type", "-"),
            "vendor": fixture.get("vendor") or fixture.get("manufacturer") or "-",
            "model": fixture.get("model") or fixture.get("name") or "-",
            "address": u.get("address", "-"),
            "firmware": u.get("firmwareVersion", "-"),
            "fixture_id": u.get("fixtureId", "-"),
            "group": group_map.get(gid, "-") if gid else "-",
            "controls": _fixture_controls_summary(fixture) or _controls_summary(u) or "-",
        }

    elementos = sorted((enrich(u) for u in units), key=lambda e: (e["category"], e["name"]))
    luminarias = sorted(
        (enrich(u) for u in units if _is_luminaria(u)),
        key=lambda e: (e["group"], e["name"]),
    )
    sensores = sorted((enrich(u) for u in units if _is_sensor(u)), key=lambda e: e["name"])
    pulsadores = sorted((enrich(u) for u in units if _is_pulsador(u)), key=lambda e: e["name"])

    # Configuración anotada de cada sensor
    sensor_cfg = _load_sensors(str(network_id))
    for s in sensores:
        cfg = sensor_cfg.get(str(s["id"]), {})
        s["modo"] = cfg.get("modo", "")
        s["escena_presencia"] = cfg.get("escena_presencia", "")
        s["escena_ausencia"] = cfg.get("escena_ausencia", "")

    # Botones anotados de cada pulsador
    button_cfg = _load_buttons(str(network_id))
    for p in pulsadores:
        cfg = button_cfg.get(str(p["id"]), {})
        count = int(cfg.get("count") or 0)
        saved = cfg.get("buttons", {})
        p["btn_count"] = count
        p["buttons"] = [
            {
                "n": n,
                "usa": saved.get(str(n), {}).get("usa", ""),
                "programado": saved.get(str(n), {}).get("programado", ""),
            }
            for n in range(1, count + 1)
        ]

    # Grupos con sus dispositivos (id + nombre, para enlazar a Elementos)
    group_units: dict[int, list[dict]] = {}
    for u in units:
        gid = u.get("groupId")
        if gid:
            group_units.setdefault(gid, []).append({
                "id": u.get("id"),
                "name": u.get("name", str(u.get("id"))),
            })

    grupos = [
        {
            "id": g.get("id"),
            "name": g.get("name", "-"),
            "count": len(group_units.get(g.get("id"), [])),
            "devices": sorted(group_units.get(g.get("id"), []), key=lambda d: d["name"]),
        }
        for g in sorted(groups, key=lambda g: g.get("name", ""))
    ]

    # Escenas con sus dispositivos e intensidades anotadas
    saved_levels = _load_scene_levels(str(network_id))
    escenas = []
    for s in sorted(scenes, key=lambda s: s.get("position", 0)):
        raw = s.get("units", {})
        ids = [v.get("id") for v in (raw.values() if isinstance(raw, dict) else raw)]
        scene_levels = saved_levels.get(str(s.get("id")), {})
        devices = sorted(
            (
                {
                    "id": i,
                    "name": unit_map.get(i, str(i)),
                    "level": scene_levels.get(str(i), ""),
                }
                for i in ids if i is not None
            ),
            key=lambda d: d["name"],
        )
        escenas.append({
            "id": s.get("id", "-"),
            "name": s.get("name", "-"),
            "type": s.get("type", "-"),
            "count": len(devices),
            "devices": devices,
        })

    # Catálogo de nombres por tipo, para el desplegable "Programado" de los
    # botones. Si un nombre se repite, se le añade el ID para distinguirlo.
    def _catalog_names(items: list[dict]) -> list[str]:
        counts = Counter(i["name"] for i in items)
        return [
            f'{i["name"]} (ID {i["id"]})' if counts[i["name"]] > 1 else i["name"]
            for i in items
        ]

    catalogo_botones = {
        "Luminaria": _catalog_names(luminarias),
        "Elemento": _catalog_names(elementos),
        "Grupo": _catalog_names(grupos),
        "Escena": _catalog_names(escenas),
    }

    return {
        "network_id": network_id,
        "network": network,
        "fetched_at": data["fetched_at"],
        "catalogo_botones": catalogo_botones,
        "conexion": diagnostico_conectividad(network, data["state"]),
        "summary": {
            "total": len(units),
            "luminarias": type_counts.get("Luminaria", 0),
            "sensores": type_counts.get("Sensor", 0),
            "pulsadores": type_counts.get("Pulsador / Botonera", 0),
            "gateways": type_counts.get("Gateway", 0),
            "grupos": len(groups),
            "escenas": len(scenes),
        },
        "elementos": elementos,
        "luminarias": luminarias,
        "sensores": sensores,
        "pulsadores": pulsadores,
        "grupos": grupos,
        "escenas": escenas,
        "horarios": _load_schedules(str(network_id)),
        "planos": _load_planos(str(network_id)),
        # Mapa unit_id → nombre/categoría para el editor de planos (JS)
        "elementos_map": {
            str(e["id"]): {
                "name": e["name"],
                "cat": e["category"].lower().replace(" ", "").replace("/", ""),
            }
            for e in elementos
        },
    }


# ── Rutas ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not credentials.is_configured(_HOME):
        return redirect(url_for("ajustes"))

    if not _networks_loaded():
        _start_loading(lambda p: _authenticate(p))
        return _pantalla_carga("Conectando con Casambi Cloud", url_for("index"))

    return render_template(
        "index.html",
        networks=_get_networks(),
        error=_state["error"],
        current_id=None,
        cuentas=credentials.list_accounts(_HOME),
    )


def _pantalla_carga(titulo: str, destino: str):
    """
    Página con barra de progreso mientras la carga corre en segundo plano.

    Cada pantalla vuelve a su propio destino: si el usuario pulsa otra red
    mientras algo se está cargando, al terminar aterriza donde pidió, y si esos
    datos aún no están, esta misma pantalla arranca la carga que falte.
    """
    return render_template(
        "cargando.html",
        networks=_get_networks(),
        error=None,
        current_id=None,
        titulo=titulo,
        destino=destino,
        cuentas=credentials.list_accounts(_HOME),
    )


@app.route("/carga/estado")
def carga_estado():
    """Progreso de la carga en curso, que consulta la pantalla de carga."""
    return jsonify({
        "active": _load["active"],
        "label": _load["label"],
        "done": _load["done"],
        "total": _load["total"],
        "error": _load["error"],
    })


def _reset_session() -> None:
    """Olvida las sesiones y la caché tras cambiar de cuentas."""
    _state.update({"clients": {}, "networks": None, "cache": {}, "error": None})


# ── Ajustes: cuentas de Casambi ───────────────────────────────────────────────

@app.route("/ajustes")
def ajustes():
    """Cuentas de Casambi configuradas (guardadas en el Llavero)."""
    # Se muestra la API key para poder revisarla y corregirla; la contraseña
    # nunca se envía a la interfaz.
    cuentas = []
    for entry in credentials.list_accounts(_HOME):
        full = credentials.get_account(entry["id"]) or {}
        cuentas.append({
            "id": entry["id"],
            "label": entry.get("label", ""),
            "email": entry.get("email", ""),
            "api_key": full.get("api_key", ""),
        })

    return render_template(
        "ajustes.html",
        networks=_get_networks(),
        error=None,
        current_id=None,
        cuentas=cuentas,
        redes_por_cuenta=Counter(
            n.get("account_id") for n in _get_networks()
        ),
        legacy_env=credentials.legacy_env_path(_HOME),
    )


@app.route("/ajustes/cuentas", methods=["POST"])
def cuenta_crear():
    """Añade una cuenta nueva."""
    try:
        credentials.add_account(
            (request.form.get("label") or "").strip(),
            (request.form.get("api_key") or "").strip(),
            (request.form.get("email") or "").strip(),
            request.form.get("password") or "",
        )
    except credentials.CredentialsError as e:
        flash(str(e), "error")
        return redirect(url_for("ajustes"))

    _reset_session()
    flash("Cuenta añadida. Sus redes aparecerán al cargar.", "ok")
    return redirect(url_for("index"))


@app.route("/ajustes/cuentas/<account_id>", methods=["POST"])
def cuenta_editar(account_id):
    """Actualiza o borra una cuenta."""
    if (request.form.get("accion") or "") == "borrar":
        credentials.delete_account(account_id)
        _reset_session()
        flash("Cuenta eliminada del Llavero.", "ok")
        return redirect(url_for("ajustes"))

    try:
        credentials.update_account(
            account_id,
            (request.form.get("label") or "").strip(),
            (request.form.get("api_key") or "").strip(),
            (request.form.get("email") or "").strip(),
            request.form.get("password") or "",
        )
    except credentials.CredentialsError as e:
        flash(str(e), "error")
        return redirect(url_for("ajustes"))

    _reset_session()
    flash("Cuenta actualizada.", "ok")
    return redirect(url_for("index"))


@app.route("/network/<network_id>")
def network_view(network_id):
    destino = url_for("network_view", network_id=network_id)

    if not _networks_loaded():
        _start_loading(lambda p: _authenticate(p))
        return _pantalla_carga("Conectando con Casambi Cloud", destino)

    if _find_network_meta(network_id) is None:
        abort(404)

    if str(network_id) not in _state["cache"]:
        _start_loading(lambda p: _fetch_network_data(network_id, p))
        meta = _find_network_meta(network_id) or {}
        return _pantalla_carga(f"Cargando {meta.get('name') or 'la red'}", destino)

    try:
        data = _get_network_data(network_id)
    except CasambiAPIError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))

    ctx = _build_report_context(network_id, data)
    return render_template(
        "network.html",
        networks=_get_networks(),
        error=_state["error"],
        current_id=str(network_id),
        cuentas=credentials.list_accounts(_HOME),
        **ctx,
    )


@app.route("/network/<network_id>/refresh")
def network_refresh(network_id):
    """Descarta la caché; network_view volverá a descargar con barra de progreso."""
    _state["cache"].pop(str(network_id), None)
    return redirect(url_for("network_view", network_id=network_id))


@app.route("/network/<network_id>/excel")
def network_excel(network_id):
    try:
        data = _get_network_data(network_id)
    except CasambiAPIError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))

    images_dir = _download_network_images(str(network_id), data["network"])

    # Planos con algo que mostrar: elementos colocados a mano, o nodos y paredes
    # importados del simulador de cobertura.
    manual_planos = [
        {"name": p.get("name", "-"), "path": PLANOS_DIR / p["image"],
         "markers": p.get("markers", {}), "cobertura": p.get("cobertura")}
        for p in _load_planos(str(network_id))
        if p.get("image") and (PLANOS_DIR / p["image"]).exists()
        and (p.get("markers") or p.get("cobertura"))
    ]

    REPORTS_DIR.mkdir(exist_ok=True)
    filepath = generate_report(
        data["network"], data["state"],
        fixtures=data["fixtures"],
        scene_levels=_load_scene_levels(str(network_id)),
        button_config=_load_buttons(str(network_id)),
        sensor_config=_load_sensors(str(network_id)),
        schedules=_load_schedules(str(network_id)),
        images_dir=images_dir,
        manual_planos=manual_planos,
        output_dir=str(REPORTS_DIR),
    )
    return send_file(filepath.resolve(), as_attachment=True, download_name=filepath.name)


@app.route("/network/<network_id>/planos/upload", methods=["POST"])
def plano_upload(network_id):
    """
    Sube un plano en PDF/JPG/PNG (los PDF se convierten a imagen) o importa un
    proyecto del Simulador de Cobertura (.casambi), del que se extraen el plano,
    los nodos y las paredes.
    """
    back = redirect(url_for("network_view", network_id=network_id) + "#planos")

    file = request.files.get("file")
    if file is None or not file.filename:
        flash("Selecciona un archivo PDF, JPG, PNG o .casambi.", "error")
        return back

    es_cobertura = cobertura.es_proyecto_cobertura(file.filename)
    ext = Path(file.filename).suffix.lower()
    if not es_cobertura and ext not in PLANO_ALLOWED_EXT:
        flash(f"Formato no soportado ({ext}). Usa PDF, JPG, PNG o .casambi.", "error")
        return back

    raw_page = (request.form.get("page") or "1").strip()
    page = int(raw_page) if raw_page.isdigit() and int(raw_page) >= 1 else 1

    # (imagen, bloque de cobertura o None). Un proyecto del simulador con varios
    # niveles da una entrada por nivel: cada uno tiene su plano y sus coordenadas.
    nuevos: list[tuple[Image.Image, dict | None]] = []
    avisos_cobertura: list[str] = []
    try:
        data = file.read()
        if es_cobertura:
            niveles, avisos_cobertura = cobertura.parse_proyecto(data)
            nuevos = [(img, datos) for img, datos in niveles]
        elif ext == ".pdf":
            nuevos = [(_pdf_to_image(data, page), None)]
        else:
            img = Image.open(io.BytesIO(data))
            nuevos = [(ImageOps.exif_transpose(img).convert("RGB"), None)]
        # Las relativas del proyecto son fracciones del plano, así que el
        # reescalado no las invalida.
        for img, _ in nuevos:
            img.thumbnail((PLANO_MAX_PX, PLANO_MAX_PX), Image.LANCZOS)
    except cobertura.CoberturaError as e:
        flash(str(e), "error")
        return back
    except Exception as e:
        flash(f"No se pudo procesar el archivo: {e}", "error")
        return back

    items = _load_planos(str(network_id))
    PLANOS_DIR.mkdir(parents=True, exist_ok=True)
    nombre_base = (request.form.get("name") or "").strip()
    nombres = []
    for img, datos in nuevos:
        new_id = max((int(p.get("id", 0)) for p in items), default=0) + 1
        filename = f"plano_{network_id}_{new_id}.png"
        img.save(PLANOS_DIR / filename)

        name = nombre_base
        if not name and datos:
            name = datos["proyecto"] or "Cobertura"
        if not name:
            name = Path(file.filename).stem
        # El nombre del nivel es lo que distingue los planos de un mismo
        # proyecto, en la pestaña y en la hoja Planos del Excel.
        if datos and datos.get("nivel"):
            name = f"{name} · {datos['nivel']}"

        plano = {"id": new_id, "name": name, "image": filename, "markers": {}}
        if datos:
            plano["origen"] = "cobertura"
            plano["cobertura"] = datos
        items.append(plano)
        nombres.append(name)
    _save_planos(str(network_id), items)

    if es_cobertura:
        total_nodos = sum(len(d["nodos"]) for _, d in nuevos)
        total_paredes = sum(len(d["paredes"]) for _, d in nuevos)
        if len(nuevos) == 1:
            flash(f'Cobertura "{nombres[0]}" importada: {total_nodos} nodo(s) y '
                  f'{total_paredes} pared(es). Asocia cada nodo con su unidad de la red.', "ok")
        else:
            flash(f'Cobertura importada en {len(nuevos)} planos, uno por nivel '
                  f'({", ".join(nombres)}): {total_nodos} nodo(s) y {total_paredes} '
                  f'pared(es) en total. Asocia cada nodo con su unidad de la red.', "ok")
        if avisos_cobertura:
            flash("Niveles sin importar: " + " ".join(avisos_cobertura), "error")
    else:
        flash(f'Plano "{nombres[0]}" subido. Elige un elemento y haz clic sobre el plano para colocarlo.', "ok")
    return back


@app.route("/network/<network_id>/planos", methods=["POST"])
def plano_config(network_id):
    """
    Coloca/quita marcadores, renombra o elimina un plano manual, y asocia los
    nodos de un plano de cobertura con las unidades reales de la red.
    """
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", "")).strip()

    items = _load_planos(str(network_id))
    try:
        pid = int(payload.get("plano_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Falta plano_id"}), 400
    plano = next((p for p in items if int(p.get("id", 0)) == pid), None)
    if plano is None:
        return jsonify({"ok": False, "error": "Plano no encontrado"}), 404

    if action == "marker_set":
        unit_id = str(payload.get("unit_id", "")).strip()
        try:
            x, y = float(payload.get("x")), float(payload.get("y"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Coordenadas inválidas"}), 400
        if not unit_id or not (0 <= x <= 1 and 0 <= y <= 1):
            return jsonify({"ok": False, "error": "Datos de marcador inválidos"}), 400
        plano.setdefault("markers", {})[unit_id] = {"x": round(x, 5), "y": round(y, 5)}

    elif action == "marker_remove":
        unit_id = str(payload.get("unit_id", "")).strip()
        plano.get("markers", {}).pop(unit_id, None)

    elif action == "nodo_unit":
        # Asociación nodo simulado → unidad instalada. El simulador etiqueta los
        # nodos N1, N2…, nombres que no se parecen a los de la red, así que este
        # emparejamiento solo puede hacerlo el usuario. Cadena vacía = desasociar.
        nodo_id = str(payload.get("nodo_id", "")).strip()
        unit_id = str(payload.get("unit_id", "")).strip()
        nodos = (plano.get("cobertura") or {}).get("nodos", [])
        nodo = next((n for n in nodos if str(n.get("id")) == nodo_id), None)
        if nodo is None:
            return jsonify({"ok": False, "error": "Nodo no encontrado"}), 404
        if unit_id:
            try:
                nodo["unit_id"] = int(unit_id)
            except ValueError:
                return jsonify({"ok": False, "error": "Unidad inválida"}), 400
        else:
            nodo["unit_id"] = None

    elif action == "rename":
        plano["name"] = str(payload.get("name", "")).strip() or plano.get("name", "")

    elif action == "delete":
        items.remove(plano)
        try:
            (PLANOS_DIR / plano.get("image", "")).unlink(missing_ok=True)
        except OSError:
            pass

    else:
        return jsonify({"ok": False, "error": "Acción inválida"}), 400

    _save_planos(str(network_id), items)
    return jsonify({"ok": True})


@app.route("/network/<network_id>/planos/img/<int:plano_id>")
def plano_image(network_id, plano_id):
    plano = next(
        (p for p in _load_planos(str(network_id)) if int(p.get("id", 0)) == plano_id),
        None,
    )
    if plano is None or not (PLANOS_DIR / plano.get("image", "")).exists():
        abort(404)
    return send_from_directory(PLANOS_DIR, plano["image"])


@app.route("/network/<network_id>/scene_levels", methods=["POST"])
def scene_levels(network_id):
    payload = request.get_json(silent=True) or {}
    scene_id = str(payload.get("scene_id", "")).strip()
    unit_id = str(payload.get("unit_id", "")).strip()
    level = str(payload.get("level", "")).strip()

    if not scene_id or not unit_id:
        return jsonify({"ok": False, "error": "Faltan scene_id o unit_id"}), 400
    if level and not (level.isdigit() and 0 <= int(level) <= 100):
        return jsonify({"ok": False, "error": "La intensidad debe ser un número de 0 a 100"}), 400

    _save_scene_level(str(network_id), scene_id, unit_id, level)
    return jsonify({"ok": True})


@app.route("/network/<network_id>/buttons", methods=["POST"])
def button_config(network_id):
    """Guarda el nº de botones de un pulsador o la anotación de un botón."""
    payload = request.get_json(silent=True) or {}
    unit_id = str(payload.get("unit_id", "")).strip()
    if not unit_id:
        return jsonify({"ok": False, "error": "Falta unit_id"}), 400

    cfg = _load_buttons(str(network_id))
    entry = cfg.setdefault(unit_id, {})

    if "count" in payload:
        raw = str(payload["count"]).strip() or "0"
        if not raw.isdigit() or int(raw) > 20:
            return jsonify({"ok": False, "error": "El número de botones debe ser 0 a 20"}), 400
        entry["count"] = int(raw)

    if "button" in payload:
        button = str(payload["button"]).strip()
        usa = str(payload.get("usa", "")).strip()
        programado = str(payload.get("programado", "")).strip()
        if usa and usa not in BUTTON_USA_OPTIONS:
            return jsonify({"ok": False, "error": "Tipo de uso inválido"}), 400
        btn = entry.setdefault("buttons", {}).setdefault(button, {})
        btn["usa"] = usa
        btn["programado"] = programado

    _save_buttons(str(network_id), cfg)
    return jsonify({"ok": True})


@app.route("/network/<network_id>/sensors", methods=["POST"])
def sensor_config(network_id):
    """Guarda el modo y las escenas que activa un sensor (anotación manual)."""
    payload = request.get_json(silent=True) or {}
    unit_id = str(payload.get("unit_id", "")).strip()
    if not unit_id:
        return jsonify({"ok": False, "error": "Falta unit_id"}), 400

    modo = str(payload.get("modo", "")).strip()
    esc_pres = str(payload.get("escena_presencia", "")).strip()
    esc_aus = str(payload.get("escena_ausencia", "")).strip()

    if modo and modo not in SENSOR_MODO_OPTIONS:
        return jsonify({"ok": False, "error": "Modo inválido"}), 400

    # Cada modo solo admite su(s) escena(s) correspondiente(s)
    if modo == "Presencia":
        esc_aus = ""
    elif modo == "Ausencia":
        esc_pres = ""
    elif not modo:
        esc_pres = esc_aus = ""

    cfg = _load_sensors(str(network_id))
    cfg[unit_id] = {
        "modo": modo,
        "escena_presencia": esc_pres,
        "escena_ausencia": esc_aus,
    }
    _save_sensors(str(network_id), cfg)
    return jsonify({"ok": True})


@app.route("/network/<network_id>/schedules", methods=["POST"])
def schedule_config(network_id):
    """Crea, actualiza o elimina horarios documentados de la red."""
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", "")).strip()
    items = _load_schedules(str(network_id))

    if action == "create":
        new_id = max((int(s.get("id", 0)) for s in items), default=0) + 1
        items.append({
            "id": new_id, "nombre": "", "dias": "", "encendido": "",
            "apagado": "", "escena": "", "habilitado": True,
        })
        _save_schedules(str(network_id), items)
        return jsonify({"ok": True, "id": new_id})

    try:
        sid = int(payload.get("id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Falta el id del horario"}), 400

    sched = next((s for s in items if int(s.get("id", 0)) == sid), None)
    if sched is None:
        return jsonify({"ok": False, "error": "Horario no encontrado"}), 404

    if action == "delete":
        items.remove(sched)
        _save_schedules(str(network_id), items)
        return jsonify({"ok": True})

    if action == "update":
        for field in ("nombre", "dias", "encendido", "apagado", "escena"):
            if field in payload:
                sched[field] = str(payload[field]).strip()
        if "habilitado" in payload:
            sched["habilitado"] = bool(payload["habilitado"])
        _save_schedules(str(network_id), items)
        return jsonify({"ok": True})

    return jsonify({"ok": False, "error": "Acción inválida"}), 400


@app.route("/network/<network_id>/scenes/<scene_id>/capture", methods=["POST"])
def scene_capture(network_id, scene_id):
    """
    Activa la escena en la red real (vía WebSocket) y captura el dimLevel
    que reportan las luminarias, guardándolo como intensidad de la escena.
    Requiere un gateway online (celular o hardware) en la red.
    """
    client = _client_for(network_id)
    if client is None:
        return jsonify({
            "ok": False,
            "error": _state["error"] or "Esa red no pertenece a ninguna cuenta configurada.",
        }), 503

    try:
        data = _get_network_data(network_id)
    except CasambiAPIError as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    scene = next(
        (s for s in data["network"].get("scenes", []) if str(s.get("id")) == str(scene_id)),
        None,
    )
    if scene is None:
        return jsonify({"ok": False, "error": "Escena no encontrada"}), 404

    raw = scene.get("units", {})
    scene_unit_ids = {
        v.get("id") for v in (raw.values() if isinstance(raw, dict) else raw)
        if v.get("id") is not None
    }
    if not scene_unit_ids:
        return jsonify({"ok": False, "error": "La escena no tiene dispositivos"}), 400

    try:
        client.activate_scene(network_id, scene_id)
    except CasambiAPIError as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    # Esperar a que las luminarias apliquen la escena y reporten su nivel.
    # Reintenta hasta que alguna unidad confirme la escena activa.
    captured: dict[str, str] = {}
    skipped: list[str] = []
    for wait in (3, 3, 4):
        time.sleep(wait)
        try:
            state = client.get_network_state(network_id)
        except CasambiAPIError as e:
            return jsonify({"ok": False, "error": str(e)}), 502

        captured, skipped = {}, []
        confirmed = False
        for u in state.get("units", []):
            if u.get("id") not in scene_unit_ids:
                continue
            if u.get("online") and u.get("dimLevel") is not None:
                captured[str(u["id"])] = str(round(u["dimLevel"] * 100))
                if str(u.get("activeSceneId")) == str(scene_id):
                    confirmed = True
            else:
                skipped.append(u.get("name", str(u.get("id"))))
        if confirmed:
            break

    for uid, level in captured.items():
        _save_scene_level(str(network_id), str(scene_id), uid, level)

    return jsonify({"ok": True, "captured": captured, "skipped": skipped})


@app.route("/logos/<path:filename>")
def logos(filename):
    return send_from_directory(LOGOS_DIR, filename)
