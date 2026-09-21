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

import functools
import io
import json
import os
import re
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)
from flask_wtf.csrf import CSRFProtect

import auth
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

# Pillow solo avisa a partir de ~89 Mpx, no aborta: un PNG de pocos kilobytes
# puede descomprimirse en gigabytes y tumbar el proceso, y con él la sesión de
# todo el equipo. Se fija aquí porque report.py comparte el módulo.
Image.MAX_IMAGE_PIXELS = config.MAX_IMAGE_PIXELS

# Como app de escritorio empaquetada, los datos escribibles viven fuera del
# bundle (CASAMBI_HOME → ~/Library/Application Support/CASAMBI); sin la
# variable, todo queda junto al código como siempre.
_HOME = Path(os.environ["CASAMBI_HOME"]) if os.environ.get("CASAMBI_HOME") else Path(__file__).parent

app = Flask(__name__)

# Clave de sesión, límites de subida y flags de cookie salen de config, que los
# decide según CASAMBI_MODE: en el servidor la clave tiene que ser estable
# (cada reinicio invalidaría los tokens CSRF), en el escritorio da igual.
config.aplicar(app)

# Cloudflare Access va delante, pero la app verifica su JWT igualmente: sin
# esto, alcanzar el contenedor por detrás del túnel daría acceso total. En modo
# escritorio no hace nada.
auth.proteger(app)

# CSRF en todo POST. Hasta ahora ningún formulario llevaba token, así que una
# página cualquiera podía hacer que el navegador de un técnico creara o borrara
# cuentas de Casambi, o subiera planos, sin que él se enterase.
_csrf = CSRFProtect(app)

LOGOS_DIR = Path(__file__).parent / "logos"
REPORTS_DIR = _HOME / "reportes"
DATA_DIR = _HOME / "data"

# ── Estado en memoria ─────────────────────────────────────────────────────────
# Con credenciales de empresa, compartir sesiones y caché entre todo el equipo
# es lo deseado: una sola autenticación contra Casambi Cloud y una sola descarga
# por red. Lo que hay que garantizar es que varios hilos no se pisen, de ahí el
# lock. Por eso también el servidor corre con un único worker.
_state: dict = {
    "clients": {},         # account_id → CasambiClient autenticado
    "networks": None,      # redes de todas las cuentas (None = sin cargar aún)
    "cache": {},           # network_id → {network, state, fixtures, fetched_at}
    "error": None,         # errores de autenticación, por cuenta
}
_state_lock = threading.RLock()

# Locks por red, para serializar lo que lee-modifica-escribe un mismo fichero de
# anotaciones y las activaciones de escena sobre una misma instalación.
_net_locks: dict[str, threading.RLock] = {}
_net_locks_guard = threading.Lock()


def _net_lock(network_id: str) -> threading.RLock:
    clave = str(network_id)
    with _net_locks_guard:
        lock = _net_locks.get(clave)
        if lock is None:
            lock = _net_locks[clave] = threading.RLock()
        return lock


# ── Tareas de carga ───────────────────────────────────────────────────────────
# Antes había una sola barra de progreso para todo el proceso: si alguien estaba
# cargando una red, la petición de otro se descartaba en silencio y se quedaba
# mirando el progreso —y el nombre— de una red ajena. Ahora cada carga es una
# tarea con su identificador, y dos personas que piden la misma red comparten
# una sola descarga en lugar de lanzar dos.
_tasks: dict[str, dict] = {}
_task_por_clave: dict[str, str] = {}   # "auth" | "net:<id>" → tarea activa
_tasks_lock = threading.Lock()

# Una tarea terminada se conserva un rato: la pantalla de carga todavía tiene que
# poder preguntar por ella para enterarse de que acabó, o de que falló.
_VIDA_TAREA_TERMINADA = 600


def _purgar_tareas() -> None:
    """Quita las tareas acabadas hace rato. Se llama con _tasks_lock tomado."""
    ahora = time.monotonic()
    caducadas = [
        tid for tid, t in _tasks.items()
        if not t["active"] and t["fin"] and ahora - t["fin"] > _VIDA_TAREA_TERMINADA
    ]
    for tid in caducadas:
        if _task_por_clave.get(_tasks[tid]["clave"]) == tid:
            del _task_por_clave[_tasks[tid]["clave"]]
        del _tasks[tid]


def _start_loading(clave: str, work) -> str:
    """
    Lanza `work(progress)` en segundo plano y devuelve el id de la tarea.

    Si ya hay una tarea activa con esa misma clave, devuelve la suya: así dos
    personas abriendo la misma red ven avanzar la misma barra en vez de disparar
    dos descargas. Claves distintas corren en paralelo.
    """
    with _tasks_lock:
        _purgar_tareas()
        en_curso = _task_por_clave.get(clave)
        if en_curso and _tasks.get(en_curso, {}).get("active"):
            return en_curso

        task_id = uuid.uuid4().hex[:12]
        _tasks[task_id] = {"active": True, "label": "Preparando…", "done": 0,
                           "total": 0, "error": None, "clave": clave, "fin": None}
        _task_por_clave[clave] = task_id

    def progreso(label: str, done: int = 0, total: int = 0) -> None:
        with _tasks_lock:
            tarea = _tasks.get(task_id)
            if tarea is not None:
                tarea.update(label=label, done=done, total=total)

    def run() -> None:
        error = None
        try:
            work(progreso)
        except CasambiAPIError as e:
            error = str(e)
        except Exception as e:  # que un fallo inesperado no deje la barra colgada
            error = f"Error inesperado: {e}"
            app.logger.exception("Fallo en la tarea %s (%s)", task_id, clave)
        finally:
            with _tasks_lock:
                tarea = _tasks.get(task_id)
                if tarea is not None:
                    tarea.update(active=False, error=error, fin=time.monotonic())

    threading.Thread(target=run, daemon=True).start()
    return task_id


def _estado_tarea(task_id: str | None) -> dict:
    """
    Progreso de una tarea, para la pantalla de carga.

    Una tarea desconocida —purgada, o de antes de un reinicio— se responde como
    terminada: así la pantalla redirige en vez de quedarse girando para siempre.
    """
    with _tasks_lock:
        tarea = _tasks.get(task_id) if task_id else None
        if tarea is None and not task_id:
            # Sin identificador se devuelve la más reciente, por compatibilidad
            # con pestañas abiertas antes de este cambio.
            activas = [t for t in _tasks.values() if t["active"]]
            tarea = activas[-1] if activas else None
        if tarea is None:
            return {"active": False, "label": "", "done": 0, "total": 0,
                    "error": None}
        return {k: tarea[k] for k in ("active", "label", "done", "total", "error")}


# ── Ficheros de anotaciones ───────────────────────────────────────────────────

def _read_json(path: Path, vacio):
    """
    Lee un JSON de anotaciones, preservando el fichero si está corrupto.

    Antes se devolvía vacío en silencio, así que una escritura interrumpida
    hacía desaparecer todas las anotaciones de una red sin que nadie se
    enterase, y el siguiente guardado remataba la pérdida. Ahora el fichero malo
    se aparta con su fecha y queda recuperable a mano.
    """
    if not path.is_file():
        return vacio
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        respaldo = path.with_name(
            f"{path.name}.corrupto-{config.ahora():%Y%m%d-%H%M%S}"
        )
        try:
            path.rename(respaldo)
        except OSError:
            respaldo = None
        app.logger.error(
            "%s no es JSON válido (%s); apartado como %s",
            path.name, e, respaldo.name if respaldo else "no se pudo apartar",
        )
        return vacio
    except OSError as e:
        app.logger.error("No se pudo leer %s: %s", path.name, e)
        return vacio


def _write_json(path: Path, datos) -> None:
    """
    Escribe un JSON de anotaciones de forma atómica.

    Con write_text directo, un corte a media escritura dejaba el fichero
    truncado, y el lector lo interpretaba como «no hay anotaciones». Con
    os.replace, o está el contenido viejo o está el nuevo.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(datos, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


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

    # Una sola escritura bajo el lock: con tres asignaciones sueltas, un hilo
    # podía ver los clientes nuevos junto a la lista de redes vieja.
    with _state_lock:
        _state.update({
            "clients": clients,
            "networks": networks,
            "error": " · ".join(errors) if errors else None,
        })


def _networks_loaded() -> bool:
    with _state_lock:
        return _state["networks"] is not None


def _get_networks() -> list[dict]:
    with _state_lock:
        return _state["networks"] or []


def _error_autenticacion() -> str | None:
    with _state_lock:
        return _state["error"]


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
    with _state_lock:
        return _state["clients"].get(meta.get("account_id"))


def _fetch_network_data(network_id: str, progress=None) -> dict:
    """Descarga red, estado y modelos, informando del progreso."""
    client = _client_for(network_id)
    if client is None:
        raise CasambiAPIError(
            _error_autenticacion()
            or "Esa red no pertenece a ninguna cuenta configurada."
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
        "fetched_at": config.ahora(),
    }
    with _state_lock:
        _state["cache"][str(network_id)] = data
    return data


def _get_network_data(network_id: str, force: bool = False) -> dict:
    """Datos de la red desde la caché, descargándolos si hace falta."""
    key = str(network_id)
    if not force:
        # Comprobar y leer en el mismo paso: entre el `in` y el acceso, un
        # refresh de otra persona podía vaciar la entrada.
        with _state_lock:
            cacheado = _state["cache"].get(key)
        if cacheado is not None:
            return cacheado
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
    return _read_json(_planos_path(network_id), [])


def _save_planos(network_id: str, items: list) -> None:
    with _net_lock(network_id):
        _write_json(_planos_path(network_id), items)


def _pdf_to_image(data: bytes, page_num: int) -> Image.Image:
    """Convierte una página de un PDF a imagen (PyMuPDF, sin binarios externos)."""
    import pymupdf  # import diferido: el resto de la app funciona sin PyMuPDF

    doc = pymupdf.open(stream=data, filetype="pdf")
    if doc.page_count > config.MAX_PLANO_PAGINAS_PDF:
        raise ValueError(
            f"El PDF tiene {doc.page_count} páginas; el máximo es "
            f"{config.MAX_PLANO_PAGINAS_PDF}."
        )
    if not 1 <= page_num <= doc.page_count:
        raise ValueError(f"El PDF tiene {doc.page_count} página(s); pediste la {page_num}")

    # El dpi fijo era un riesgo: una página de plano muy grande genera un pixmap
    # enorme antes de que el thumbnail lo reduzca. Se baja el dpi lo necesario
    # para no pasar del tope de píxeles.
    pagina = doc[page_num - 1]
    dpi = 150
    ancho_pt, alto_pt = pagina.rect.width, pagina.rect.height
    pixeles = (ancho_pt / 72 * dpi) * (alto_pt / 72 * dpi)
    if pixeles > config.MAX_IMAGE_PIXELS:
        dpi = max(36, int(dpi * (config.MAX_IMAGE_PIXELS / pixeles) ** 0.5))

    pix = pagina.get_pixmap(dpi=dpi)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


# ── Intensidades de escena (editadas por el usuario) ─────────────────────────
# El API de Casambi no expone el nivel de dimming programado en cada escena,
# así que se permite anotarlo manualmente y se persiste en data/<red>.json
# con la forma {scene_id: {unit_id: nivel}}.

def _levels_path(network_id: str) -> Path:
    return DATA_DIR / f"scene_levels_{network_id}.json"


def _load_scene_levels(network_id: str) -> dict:
    return _read_json(_levels_path(network_id), {})


def _save_scene_level(network_id: str, scene_id: str, unit_id: str, level: str) -> None:
    # Lee, modifica y escribe el fichero entero, así que sin el lock dos
    # anotaciones simultáneas sobre la misma red perderían una.
    with _net_lock(network_id):
        levels = _load_scene_levels(network_id)
        scene_levels = levels.setdefault(str(scene_id), {})
        if level == "":
            scene_levels.pop(str(unit_id), None)
        else:
            scene_levels[str(unit_id)] = level
        _write_json(_levels_path(network_id), levels)


# ── Configuración de botones de pulsadores (anotada por el usuario) ──────────
# El API no expone cuántos botones físicos tiene un pulsador ni qué hace cada
# uno, así que se anota manualmente y se persiste en data/buttons_<red>.json
# con la forma {unit_id: {"count": n, "buttons": {n: {"usa": ..., "programado": ...}}}}.

BUTTON_USA_OPTIONS = ["Luminaria", "Elemento", "Grupo", "Escena"]


def _buttons_path(network_id: str) -> Path:
    return DATA_DIR / f"buttons_{network_id}.json"


def _load_buttons(network_id: str) -> dict:
    return _read_json(_buttons_path(network_id), {})


def _save_buttons(network_id: str, cfg: dict) -> None:
    with _net_lock(network_id):
        _write_json(_buttons_path(network_id), cfg)


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
    return _read_json(_sensors_path(network_id), {})


def _save_sensors(network_id: str, cfg: dict) -> None:
    with _net_lock(network_id):
        _write_json(_sensors_path(network_id), cfg)


# ── Horarios (anotados por el usuario) ────────────────────────────────────────
# El API de Casambi no expone los timers/horarios de la red; se documentan
# manualmente y se persisten en data/schedules_<red>.json como una lista de
# {"id", "nombre", "dias", "encendido", "apagado", "escena", "habilitado"}.

def _schedules_path(network_id: str) -> Path:
    return DATA_DIR / f"schedules_{network_id}.json"


def _load_schedules(network_id: str) -> list:
    return _read_json(_schedules_path(network_id), [])


def _save_schedules(network_id: str, items: list) -> None:
    with _net_lock(network_id):
        _write_json(_schedules_path(network_id), items)


# ── Bitácora de la red (anotada por el usuario y por la propia app) ──────────
# Las demás anotaciones describen cómo está la red *ahora*. Esta describe cómo
# llegó a estarlo: quién tocó qué, cuándo y a petición de quién. Es lo que
# permite sostener una versión de los hechos cuando un cliente pide un cambio
# sobre otro anterior, o discute uno que no recuerda haber pedido.
#
# Se persiste en data/bitacora_<red>.json, del más reciente al más antiguo.

BITACORA_TIPOS = [
    "Instalación",
    "Cambio de configuración",
    "Avería / reparación",
    "Mantenimiento",
    "Visita de diagnóstico",
    "Ampliación",
    "Otro",
]

# Ventana dentro de la cual varias entradas automáticas iguales se funden en
# una. Sin esto, anotar diez niveles de escena seguidos dejaría diez líneas
# idénticas y la bitácora sería ilegible justo cuando más falta hace.
BITACORA_FUSION_MINUTOS = 30

_BITACORA_FMT = "%Y-%m-%dT%H:%M"


def _bitacora_path(network_id: str) -> Path:
    return DATA_DIR / f"bitacora_{network_id}.json"


def _load_bitacora(network_id: str) -> list:
    return _read_json(_bitacora_path(network_id), [])


def _save_bitacora(network_id: str, items: list) -> None:
    with _net_lock(network_id):
        _write_json(_bitacora_path(network_id), items)


def _tecnico() -> str:
    """
    Quién está haciendo el cambio.

    En la web sale del JWT de Cloudflare Access, así que no hay que teclearlo ni
    se puede falsear. En el escritorio vale "escritorio", lo que de paso marca
    solas las entradas hechas desde el laboratorio.
    """
    return getattr(g, "user_email", "") or "desconocido"


def _ahora() -> str:
    # La hora de Guatemala, no la del servidor: ver config.ahora().
    return config.ahora().strftime(_BITACORA_FMT)


def _bitacora_anotar(network_id: str, tipo: str, descripcion: str) -> None:
    """
    Añade una entrada automática al guardar una anotación.

    Una bitácora que dependa de que alguien se acuerde de escribirla se abandona
    a los dos meses. Estas se generan solas, así que siempre queda rastro de qué
    se tocó y cuándo, aunque nadie documente el porqué.
    """
    with _net_lock(network_id):
        items = _load_bitacora(network_id)
        tecnico, ahora = _tecnico(), _ahora()

        # Diez ajustes seguidos son un trabajo, no diez trabajos: si la entrada
        # más reciente es automática, del mismo técnico y la misma descripción,
        # se actualiza en vez de apilar otra.
        if items:
            ultima = items[0]
            if (ultima.get("origen") == "automatica"
                    and ultima.get("tecnico") == tecnico
                    and ultima.get("descripcion") == descripcion):
                try:
                    creada = datetime.strptime(ultima.get("creado", ""), _BITACORA_FMT)
                except ValueError:
                    creada = None
                if creada is not None and (
                        config.ahora() - creada).total_seconds() <= BITACORA_FUSION_MINUTOS * 60:
                    ultima["veces"] = int(ultima.get("veces", 1)) + 1
                    ultima["fecha"] = ahora
                    _write_json(_bitacora_path(network_id), items)
                    return

        items.insert(0, {
            "id": max((int(i.get("id", 0)) for i in items), default=0) + 1,
            "fecha": ahora,
            "tecnico": tecnico,
            "tipo": tipo,
            "solicitado_por": "",
            "descripcion": descripcion,
            "pendiente": "",
            "origen": "automatica",
            "veces": 1,
            "creado": ahora,
            "editado": None,
        })
        _write_json(_bitacora_path(network_id), items)


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


def _clave_natural(texto) -> list:
    """
    Clave de orden que lee los números como números: «Luz 2» antes que «Luz 10».

    Casi todos los nombres de una instalación acaban en un número correlativo, y
    el orden alfabético los baraja justo donde más estorba: el desplegable con
    el que se colocan los elementos sobre el plano, que en una red grande pasa
    de cien entradas y se recorre a ojo.
    """
    return [(0, int(t)) if t.isdigit() else (1, t.casefold())
            for t in re.split(r"(\d+)", str(texto)) if t]


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

    elementos = sorted((enrich(u) for u in units),
                       key=lambda e: (e["category"], _clave_natural(e["name"])))
    luminarias = sorted(
        (enrich(u) for u in units if _is_luminaria(u)),
        key=lambda e: (_clave_natural(e["group"]), _clave_natural(e["name"])),
    )
    sensores = sorted((enrich(u) for u in units if _is_sensor(u)),
                      key=lambda e: _clave_natural(e["name"]))
    pulsadores = sorted((enrich(u) for u in units if _is_pulsador(u)),
                        key=lambda e: _clave_natural(e["name"]))

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
            "devices": sorted(group_units.get(g.get("id"), []),
                              key=lambda d: _clave_natural(d["name"])),
        }
        for g in sorted(groups, key=lambda g: _clave_natural(g.get("name", "")))
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
            key=lambda d: _clave_natural(d["name"]),
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
        "bitacora": _load_bitacora(str(network_id)),
        "bitacora_tipos": BITACORA_TIPOS,
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


# ── Autorización por red ──────────────────────────────────────────────────────

# El network_id llega en la URL y se interpola en nombres de fichero
# (planos_<id>.json, plano_<id>_<n>.png), así que restringirlo a este alfabeto
# es lo que impide que una ruta inventada escriba donde no debe. Flask ya
# descarta las barras, pero no los puntos.
_RE_NETWORK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def require_network(vista):
    """
    Comprueba que el network_id es plausible y que la red es de una cuenta
    nuestra, antes de que la vista toque disco o la nube.

    Solo `network_view` lo comprobaba, así que cualquier otra ruta atendía un id
    inventado. Cuando las redes aún no están cargadas no se puede verificar la
    pertenencia; se deja pasar, porque sin cliente autenticado ninguna vista
    llega a servir datos, y cada una ya resuelve ese caso a su manera (pantalla
    de carga o error).
    """
    @functools.wraps(vista)
    def envoltorio(network_id, *args, **kwargs):
        if not _RE_NETWORK_ID.match(str(network_id)):
            abort(404)
        if _networks_loaded() and _find_network_meta(network_id) is None:
            abort(404)
        return vista(network_id, *args, **kwargs)

    return envoltorio


def con_lock_de_red(vista):
    """
    Serializa la vista por red.

    Las anotaciones se guardan leyendo el fichero entero, modificándolo y
    reescribiéndolo, así que dos peticiones simultáneas sobre la misma red
    perdían una de las dos. En `scene_capture` hay además una razón física: dos
    activaciones a la vez sobre la misma instalación se estorban.

    Va siempre **después** de require_network: si se tomara el lock antes de
    validar, cada id inventado dejaría una entrada en _net_locks y la memoria
    crecería sin tope.
    """
    @functools.wraps(vista)
    def envoltorio(network_id, *args, **kwargs):
        with _net_lock(network_id):
            return vista(network_id, *args, **kwargs)

    return envoltorio


# ── Errores ───────────────────────────────────────────────────────────────────

@app.errorhandler(credentials.CredentialsError)
def _error_credenciales(e):
    """
    Un fallo del almacén de credenciales, explicado en pantalla.

    Cubre de una vez los sitios que llaman a `credentials` sin capturar
    (`index`, `ajustes`, `_pantalla_carga`, `network_view`). El caso típico en
    el servidor es arrancar con una CASAMBI_SECRET_KEY distinta de la que cifró
    el fichero: sin esto, la respuesta era un 500 sin pista alguna.
    """
    app.logger.error("Fallo de credenciales: %s", e)
    return render_template(
        "error.html",
        titulo="No se pudo acceder a las credenciales",
        mensaje=str(e),
        ayuda="Revisa la configuración del servidor. Si el mensaje habla de "
              "descifrado, la clave del servidor no es la misma con la que se "
              "guardaron las cuentas.",
        enlace_ajustes=True,
    ), 500


@app.errorhandler(413)
def _error_demasiado_grande(e):
    """Subida por encima de MAX_CONTENT_LENGTH, explicada en vez de cortada."""
    limite = config.MAX_CONTENT_LENGTH // (1024 * 1024)
    mensaje = f"El archivo supera el límite de {limite} MB."
    if request.is_json or request.accept_mimetypes.best == "application/json":
        return jsonify({"ok": False, "error": mensaje}), 413
    flash(mensaje, "error")
    destino = request.referrer or url_for("index")
    return redirect(destino), 302


@app.after_request
def _cabeceras_de_seguridad(resp):
    """
    Cabeceras que no cuestan nada y cierran clases enteras de problema.

    La CSP permite estilos y scripts en línea porque network.html los trae
    embebidos; no hay ni un onclick, así que pasar a nonce más adelante es
    trivial. HSTS solo en modo web: en el escritorio se sirve por http en
    127.0.0.1 y forzaría al navegador a rechazar la propia app.
    """
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Content-Security-Policy", (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ))
    if not config.ES_ESCRITORIO:
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return resp


# ── Rutas ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not credentials.is_configured(_HOME):
        return redirect(url_for("ajustes"))

    if not _networks_loaded():
        tarea = _start_loading("auth", lambda p: _authenticate(p))
        return _pantalla_carga("Conectando con Casambi Cloud", url_for("index"),
                               tarea)

    return render_template(
        "index.html",
        networks=_get_networks(),
        error=_error_autenticacion(),
        current_id=None,
        cuentas=credentials.list_accounts(_HOME),
    )


@app.route("/redes/refresh", methods=["POST"])
def networks_refresh():
    """
    Olvida la lista de redes; index() vuelve a autenticar con barra de progreso.

    Solo la lista: las descargas de cada red (`cache`) son lo caro, y para eso
    está el Actualizar de la red. Este botón responde a otra pregunta —«¿hay
    una red nueva?»—, y hasta ahora solo se podía contestar reiniciando.

    POST por lo mismo que `network_refresh`: como GET, una imagen remota en un
    correo obligaría a toda la oficina a autenticarse de nuevo.
    """
    with _state_lock:
        _state["networks"] = None
    return redirect(url_for("index"))


def _pantalla_carga(titulo: str, destino: str, task_id: str):
    """
    Página con barra de progreso mientras la carga corre en segundo plano.

    Cada pantalla vuelve a su propio destino: si el usuario pulsa otra red
    mientras algo se está cargando, al terminar aterriza donde pidió, y si esos
    datos aún no están, esta misma pantalla arranca la carga que falte. El
    `task_id` es lo que hace que cada pestaña siga su propia carga y no la de
    otra persona.
    """
    return render_template(
        "cargando.html",
        networks=_get_networks(),
        error=None,
        current_id=None,
        titulo=titulo,
        destino=destino,
        task_id=task_id,
        cuentas=credentials.list_accounts(_HOME),
    )


@app.route("/carga/estado")
def carga_estado():
    """Progreso de una tarea de carga, que consulta la pantalla de carga."""
    return jsonify(_estado_tarea(request.args.get("task")))


def _reset_session() -> None:
    """Olvida las sesiones y la caché tras cambiar de cuentas."""
    with _state_lock:
        _state.update({"clients": {}, "networks": None, "cache": {},
                       "error": None})


# ── Ajustes: cuentas de Casambi ───────────────────────────────────────────────

def _enmascarar(valor: str, visibles: int = 4) -> str:
    """«abcd…wxyz»: lo justo para reconocer la clave sin revelarla."""
    if not valor:
        return ""
    if len(valor) <= visibles * 2:
        return "•" * len(valor)
    return f"{valor[:visibles]}…{valor[-visibles:]}"

@app.route("/ajustes")
def ajustes():
    """Cuentas de Casambi configuradas."""
    # Ni la contraseña ni la API key se envían a la interfaz: se muestra una
    # máscara que basta para reconocer cuál está puesta. Antes la clave viajaba
    # en claro en el HTML, y queda en la caché del navegador y en el historial.
    cuentas = []
    for entry in credentials.list_accounts(_HOME):
        full = credentials.get_account(entry["id"]) or {}
        cuentas.append({
            "id": entry["id"],
            "label": entry.get("label", ""),
            "email": entry.get("email", ""),
            "api_key_mask": _enmascarar(full.get("api_key", "")),
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

    # La API key ya no se envía a la interfaz, así que vacío significa
    # «déjala como está», igual que la contraseña. Si no, guardar sin tocarla la
    # borraría.
    api_key = (request.form.get("api_key") or "").strip()
    if not api_key:
        actual = credentials.get_account(account_id) or {}
        api_key = actual.get("api_key", "")

    try:
        credentials.update_account(
            account_id,
            (request.form.get("label") or "").strip(),
            api_key,
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
@require_network
def network_view(network_id):
    destino = url_for("network_view", network_id=network_id)

    if not _networks_loaded():
        tarea = _start_loading("auth", lambda p: _authenticate(p))
        return _pantalla_carga("Conectando con Casambi Cloud", destino, tarea)

    if str(network_id) not in _state["cache"]:
        # La clave es la red, así que dos personas abriéndola a la vez comparten
        # una sola descarga, y quien abra otra red no espera a esta.
        tarea = _start_loading(f"net:{network_id}",
                               lambda p: _fetch_network_data(network_id, p))
        meta = _find_network_meta(network_id) or {}
        return _pantalla_carga(f"Cargando {meta.get('name') or 'la red'}", destino,
                               tarea)

    try:
        data = _get_network_data(network_id)
    except CasambiAPIError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))

    ctx = _build_report_context(network_id, data)
    return render_template(
        "network.html",
        networks=_get_networks(),
        error=_error_autenticacion(),
        current_id=str(network_id),
        cuentas=credentials.list_accounts(_HOME),
        **ctx,
    )


@app.route("/network/<network_id>/refresh", methods=["POST"])
@require_network
def network_refresh(network_id):
    """
    Descarta la caché; network_view volverá a descargar con barra de progreso.

    Es POST porque muta estado: como GET, bastaba una <img src> en un correo
    para invalidar la caché de toda la oficina.
    """
    with _state_lock:
        _state["cache"].pop(str(network_id), None)
    return redirect(url_for("network_view", network_id=network_id))


@app.route("/network/<network_id>/excel")
@require_network
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
        bitacora=_load_bitacora(str(network_id)),
        images_dir=images_dir,
        manual_planos=manual_planos,
        output_dir=str(REPORTS_DIR),
    )
    return send_file(filepath.resolve(), as_attachment=True, download_name=filepath.name)


@app.route("/network/<network_id>/planos/upload", methods=["POST"])
@require_network
@con_lock_de_red
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
            if len(niveles) > config.MAX_NIVELES_COBERTURA:
                raise ValueError(
                    f"El proyecto trae {len(niveles)} niveles; el máximo es "
                    f"{config.MAX_NIVELES_COBERTURA}."
                )
            nuevos = [(img, datos) for img, datos in niveles]
        elif ext == ".pdf":
            nuevos = [(_pdf_to_image(data, page), None)]
        else:
            img = Image.open(io.BytesIO(data))
            # La extensión la elige quien sube; el formato real lo dice Pillow.
            if img.format not in ("PNG", "JPEG"):
                raise ValueError(
                    f"El archivo dice ser {ext} pero su contenido es "
                    f"{img.format or 'desconocido'}."
                )
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

    PLANOS_DIR.mkdir(parents=True, exist_ok=True)
    nombre_base = (request.form.get("name") or "").strip()
    nombres = []
    # Leer el índice, numerar y guardar, todo bajo el mismo lock: el id sale de
    # un max() sobre lo leído, así que dos subidas simultáneas calculaban el
    # mismo número y una pisaba el PNG de la otra.
    with _net_lock(network_id):
        items = _load_planos(str(network_id))
        for img, datos in nuevos:
            new_id = max((int(p.get("id", 0)) for p in items), default=0) + 1
            # El sufijo aleatorio hace imposible por diseño que dos planos
            # compartan fichero, incluso si el lock fallara algún día.
            filename = f"plano_{network_id}_{new_id}_{uuid.uuid4().hex[:8]}.png"
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
        _bitacora_anotar(str(network_id), "Cambio de configuración",
                         "Se actualizaron los planos")

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
@require_network
@con_lock_de_red
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
    _bitacora_anotar(str(network_id), "Cambio de configuración",
                     "Se actualizaron los planos")
    return jsonify({"ok": True})


@app.route("/network/<network_id>/planos/img/<int:plano_id>")
@require_network
def plano_image(network_id, plano_id):
    plano = next(
        (p for p in _load_planos(str(network_id)) if int(p.get("id", 0)) == plano_id),
        None,
    )
    if plano is None or not (PLANOS_DIR / plano.get("image", "")).exists():
        abort(404)
    return send_from_directory(PLANOS_DIR, plano["image"])


@app.route("/network/<network_id>/scene_levels", methods=["POST"])
@require_network
@con_lock_de_red
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
    _bitacora_anotar(str(network_id), "Cambio de configuración",
                     "Se anotaron intensidades de escena")
    return jsonify({"ok": True})


@app.route("/network/<network_id>/buttons", methods=["POST"])
@require_network
@con_lock_de_red
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
    _bitacora_anotar(str(network_id), "Cambio de configuración",
                     "Se anotó la programación de pulsadores")
    return jsonify({"ok": True})


@app.route("/network/<network_id>/sensors", methods=["POST"])
@require_network
@con_lock_de_red
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
    _bitacora_anotar(str(network_id), "Cambio de configuración",
                     "Se anotó la configuración de sensores")
    return jsonify({"ok": True})


@app.route("/network/<network_id>/schedules", methods=["POST"])
@require_network
@con_lock_de_red
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
        _bitacora_anotar(str(network_id), "Cambio de configuración",
                         "Se modificaron los horarios")
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
        _bitacora_anotar(str(network_id), "Cambio de configuración",
                         "Se modificaron los horarios")
        return jsonify({"ok": True})

    if action == "update":
        for field in ("nombre", "dias", "encendido", "apagado", "escena"):
            if field in payload:
                sched[field] = str(payload[field]).strip()
        if "habilitado" in payload:
            sched["habilitado"] = bool(payload["habilitado"])
        _save_schedules(str(network_id), items)
        _bitacora_anotar(str(network_id), "Cambio de configuración",
                         "Se modificaron los horarios")
        return jsonify({"ok": True})

    return jsonify({"ok": False, "error": "Acción inválida"}), 400


@app.route("/network/<network_id>/bitacora", methods=["POST"])
@require_network
@con_lock_de_red
def bitacora_config(network_id):
    """Crea, actualiza o elimina entradas de la bitácora de la red."""
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", "")).strip()
    items = _load_bitacora(str(network_id))

    if action == "create":
        ahora = _ahora()
        new_id = max((int(i.get("id", 0)) for i in items), default=0) + 1
        items.insert(0, {
            "id": new_id, "fecha": ahora, "tecnico": _tecnico(),
            "tipo": "Cambio de configuración", "solicitado_por": "",
            "descripcion": "", "pendiente": "", "origen": "manual",
            "veces": 1, "creado": ahora, "editado": None,
        })
        _save_bitacora(str(network_id), items)
        return jsonify({"ok": True, "id": new_id})

    try:
        eid = int(payload.get("id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Falta el id de la entrada"}), 400

    entrada = next((i for i in items if int(i.get("id", 0)) == eid), None)
    if entrada is None:
        return jsonify({"ok": False, "error": "Entrada no encontrada"}), 404

    if action == "delete":
        items.remove(entrada)
        _save_bitacora(str(network_id), items)
        return jsonify({"ok": True})

    if action == "update":
        if "tipo" in payload and str(payload["tipo"]).strip() not in BITACORA_TIPOS:
            return jsonify({"ok": False, "error": "Tipo de intervención inválido"}), 400
        for campo in ("fecha", "tipo", "solicitado_por", "descripcion", "pendiente"):
            if campo in payload:
                entrada[campo] = str(payload[campo]).strip()
        # `tecnico` y `creado` no se tocan nunca. Una bitácora que se pueda
        # reescribir sin dejar rastro no sostiene nada frente a un cliente, así
        # que el sello de quién y cuándo queda fuera de lo editable, y cualquier
        # cambio posterior deja su marca en `editado`.
        entrada["editado"] = _ahora()
        _save_bitacora(str(network_id), items)
        return jsonify({"ok": True})

    return jsonify({"ok": False, "error": "Acción inválida"}), 400


@app.route("/network/<network_id>/scenes/<scene_id>/capture", methods=["POST"])
@require_network
@con_lock_de_red
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
            "error": _error_autenticacion()
                     or "Esa red no pertenece a ninguna cuenta configurada.",
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
