"""
Importador de proyectos del Simulador de Cobertura Casambi (~/Developer/casambi-wifi-coverage).

Un archivo `.casambi` (o `.casambi.json`, el formato antiguo) es JSON con el
plano embebido como data URL y los nodos situados en **coordenadas del mundo,
en metros**. Aquí se traduce todo a lo que la app de informes ya sabe manejar:
una imagen PNG y coordenadas relativas 0-1, igual que `markers` y que la galería
de Casambi.

**Un proyecto puede tener varios niveles** (formato 4, desde el 2026-09-13). En
ese caso cada nivel lleva su propio plano, escala, origen, nodos, paredes y
huecos de losa dentro de `levels`, y los campos de la raíz van vacíos: leídos
como antes, el proyecto daba «no tiene plano cargado». Cada nivel se importa
como un plano aparte, porque cada uno tiene su imagen y sus coordenadas.

**Un plano puede llevar varias redes Casambi** (formato 5, desde el
2026-09-25). Cada nodo puede traer su propia `network`; el que no la trae es de
la red de su nivel (`network` del nivel, 1 si no hay). Los nombres que el
usuario les puso van en `networkNames` en la raíz. Aquí cada nodo sale con su
`red` ya resuelta, y el bloque del plano con `redes`, número → nombre.

**Cada red puede traer su modo de radio** (formato 6, desde el 2026-09-28): en
la misma entrada de `networkNames`, `mode` es `performance`, `balanced` o
`longRange`. Una red con modo y sin nombre trae `name` vacío, y aquí sigue sin
título propio. El modo solo le importa al cálculo de enlaces del simulador, que
aquí no se hace.

**Los sensores locales no son Casambi** (desde el 2026-10-03). El simulador
coloca en el plano sensores de línea sin radio —bodegas, baños— por su carga y
su zona de detección; los guarda como nodos con `"radio": false`. Aquí no
existen: no se importan, no se cuentan en ninguna red y no se ofrecen para
asociar a una unidad real.

Cada red del simulador corresponde a una red de la nube distinta, así que un
proyecto con varias se importa **una red a la vez**: `parse_proyecto(..., red=N)`
deja solo los nodos de la red N y salta los niveles que no tienen ninguno. El
mismo archivo se importa otra vez en la página de la otra red eligiendo la otra.
`redes_del_proyecto` dice cuáles trae, para ofrecerlas.

**El proyecto no guarda el mapa de calor.** El `image` embebido es el plano
arquitectónico desnudo; el heatmap y las paredes son vectores que el motor Swift
calcula y pinta en vivo, y nunca se serializan. Por eso aquí se importa la
estructura —nodos, paredes, huecos, escala, gateway, modelos y notas de
montaje— y el plano se redibuja con marcadores propios. Si hace falta el mapa de
calor en el informe, se exporta aparte desde la app (⇧⌘E) y se sube como un
plano normal.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import re

from PIL import Image, ImageOps

# El simulador guarda el plano como data URL. Formato y payload por separado
# porque el tipo MIME decide poco: Pillow reconoce el contenido igualmente.
_DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;,]*);base64,(?P<payload>.*)$", re.DOTALL)

# Extensiones que produce el simulador. `.casambi` es la actual; `.casambi.json`
# son proyectos anteriores a que la extensión quedara asociada a la app.
EXTENSIONES = (".casambi", ".casambi.json")

# Formatos que este importador sabe leer: 1 base, 2 con cuadro de cargas, 3 con
# mediciones, 4 con varios niveles, 5 con redes Casambi por equipo, 6 con el
# modo de radio de cada red y 7 con líneas dibujadas sobre el plano (`lines`,
# que aquí no se usan: son dibujo, no equipo). El
# simulador sube la versión justo cuando un lector anterior leería mal el
# archivo —la 4 vació los campos de la raíz—, así que una versión desconocida se
# rechaza en vez de adivinarla.
VERSIONES_CONOCIDAS = range(1, 8)


class CoberturaError(Exception):
    """El archivo no es un proyecto de cobertura utilizable."""


def es_proyecto_cobertura(filename: str) -> bool:
    return filename.lower().endswith(EXTENSIONES)


def _decodifica_plano(image_field: str, sujeto: str) -> Image.Image:
    match = _DATA_URL_RE.match(image_field.strip())
    if match is None:
        raise CoberturaError(f"El plano de {sujeto} no está en un formato reconocible.")
    try:
        raw = base64.b64decode(match.group("payload"), validate=True)
    except (binascii.Error, ValueError):
        raise CoberturaError(f"El plano embebido en {sujeto} está corrupto.")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except OSError:
        raise CoberturaError(f"No se pudo abrir el plano embebido en {sujeto}.")
    # El simulador guarda RGBA con transparencia; el informe compone sobre blanco.
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        fondo = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(fondo, img)
    return img.convert("RGB")


def _niveles(proyecto: dict) -> list[dict]:
    """
    Los niveles del proyecto, de abajo hacia arriba. Un proyecto de un solo
    nivel no guarda la lista —se escribe igual que antes de que existieran—, así
    que su único nivel son los campos de la raíz. Es la misma regla que
    `ProjectData.allLevels` en el simulador.
    """
    niveles = [n for n in proyecto.get("levels") or [] if isinstance(n, dict)]
    return niveles if len(niveles) > 1 else [proyecto]


def _red(valor) -> int | None:
    """Un número de red Casambi, o None si no lo es."""
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 1:
        return None
    return valor


def _nombres_de_red(proyecto: dict) -> dict[str, str]:
    """Los nombres que el usuario les puso a las redes, por número. Con la clave
    en texto, que es como queda al guardarse en JSON."""
    nombres = {}
    for entrada in proyecto.get("networkNames") or []:
        if not isinstance(entrada, dict):
            continue
        red = _red(entrada.get("network"))
        nombre = (entrada.get("name") or "").strip()
        if red and nombre:
            nombres[str(red)] = nombre
    return nombres


def _red_del_nodo(nodo: dict, red_del_nivel: int) -> int:
    """La red de un nodo: la suya, o la de su nivel si nadie se la asignó."""
    return _red(nodo.get("network")) or red_del_nivel


def _es_casambi(nodo) -> bool:
    """Un nodo de la red. El simulador marca con `"radio": false` lo que pone
    en el plano sin ser Casambi —un sensor de línea—; sin la marca, es nodo."""
    return isinstance(nodo, dict) and nodo.get("radio") is not False


def _nodos_por_red(nivel: dict) -> dict[int, int]:
    """Cuántos nodos de cada red hay en un nivel."""
    red_del_nivel = _red(nivel.get("network")) or 1
    cuenta: dict[int, int] = {}
    for nodo in nivel.get("nodes") or []:
        if _es_casambi(nodo):
            red = _red_del_nodo(nodo, red_del_nivel)
            cuenta[red] = cuenta.get(red, 0) + 1
    return cuenta


def _carga(data: bytes) -> dict:
    """El JSON del proyecto, o CoberturaError si no lo es."""
    try:
        proyecto = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CoberturaError("El archivo no es un proyecto de cobertura válido (JSON).")
    if not isinstance(proyecto, dict):
        raise CoberturaError("El archivo no es un proyecto de cobertura válido.")
    return proyecto


def titulo_de_red(red: int, nombres: dict[str, str]) -> str:
    """«Red 2», o «Red 2 · Pasillos» si tiene nombre. Igual que en el simulador."""
    nombre = nombres.get(str(red))
    return f"Red {red} · {nombre}" if nombre else f"Red {red}"


def redes_del_proyecto(data: bytes) -> list[dict]:
    """
    Las redes Casambi que trae un proyecto, de menor a mayor: número, nombre y
    cuántos nodos tiene en todos los niveles. Un proyecto anterior a las redes
    por equipo trae la de cada nivel.
    """
    proyecto = _carga(data)
    nombres = _nombres_de_red(proyecto)
    total: dict[int, int] = {}
    for nivel in _niveles(proyecto):
        for red, cuenta in _nodos_por_red(nivel).items():
            total[red] = total.get(red, 0) + cuenta
    return [
        {"red": red, "nombre": nombres.get(str(red)), "titulo": titulo_de_red(red, nombres),
         "nodos": total[red]}
        for red in sorted(total)
    ]


def _importa_nivel(nivel: dict, perdidas: dict, nombre: str | None,
                   red: int | None = None) -> tuple[Image.Image, dict]:
    """
    Un nivel → (plano, datos del nivel en relativas). `nombre` es None en un
    proyecto de un solo nivel, y entonces los mensajes hablan del proyecto.
    `red` deja solo los nodos de esa red; None, todos.
    """
    sujeto = f"el nivel «{nombre}»" if nombre else "el proyecto"

    imagen = nivel.get("image")
    if not imagen:
        raise CoberturaError(
            f"{sujeto[0].upper()}{sujeto[1:]} no tiene plano cargado. Ábrelo en el "
            "simulador, sube el plano y vuelve a guardarlo."
        )
    plano = _decodifica_plano(imagen, sujeto)

    try:
        m_por_px = float(nivel.get("metersPerPixel") or 0)
    except (TypeError, ValueError):
        m_por_px = 0.0
    if m_por_px <= 0:
        raise CoberturaError(
            f"{sujeto[0].upper()}{sujeto[1:]} no tiene escala calibrada; sin ella no se "
            "pueden situar los nodos sobre el plano."
        )

    red_del_nivel = _red(nivel.get("network")) or 1

    origen_x = float(nivel.get("imageOriginX") or 0)
    origen_y = float(nivel.get("imageOriginY") or 0)
    ancho, alto = plano.size

    def a_relativas(x_m, y_m) -> tuple[float, float] | None:
        """Metros del mundo → fracción del plano. La esquina superior izquierda
        de la imagen está en (imageOriginX, imageOriginY) — ver SceneRenderer.
        Cada nivel tiene su propio origen: por eso se convierte nivel a nivel."""
        try:
            px = (float(x_m) - origen_x) / m_por_px
            py = (float(y_m) - origen_y) / m_por_px
        except (TypeError, ValueError):
            return None
        return round(px / ancho, 5), round(py / alto, 5)

    nodos = []
    for nodo in nivel.get("nodes") or []:
        if not _es_casambi(nodo):
            continue
        if red is not None and _red_del_nodo(nodo, red_del_nivel) != red:
            continue
        rel = a_relativas(nodo.get("x"), nodo.get("y"))
        if rel is None:
            continue
        nota = (nodo.get("note") or "").strip()
        nodos.append({
            "id":       str(nodo.get("id") or ""),
            "label":    str(nodo.get("label") or "?"),
            "x":        rel[0],
            "y":        rel[1],
            "gateway":  bool(nodo.get("isGateway")),
            "modelo":   nodo.get("modelId") or "",
            "nota":     nota,
            "ptx_dbm":  nodo.get("ptxDbm"),
            "fijo":     bool(nodo.get("locked")),
            # La suya, o la de su nivel si nadie se la asignó.
            "red":      _red_del_nodo(nodo, red_del_nivel),
            # Lo rellena el usuario desde la pestaña Planos.
            "unit_id":  None,
        })

    paredes = []
    for pared in nivel.get("walls") or []:
        if not isinstance(pared, dict):
            continue
        p1 = a_relativas(pared.get("x1"), pared.get("y1"))
        p2 = a_relativas(pared.get("x2"), pared.get("y2"))
        if p1 is None or p2 is None:
            continue
        paredes.append({
            "x1": p1[0], "y1": p1[1], "x2": p2[0], "y2": p2[1],
            "perdida_db": perdidas.get(str(pared.get("materialId"))),
        })

    # Huecos de losa: dobles alturas y cubos de escalera, donde este nivel no
    # tiene piso. Explican por qué un nodo de otro piso alcanza aquí.
    huecos = []
    for hueco in nivel.get("openings") or []:
        if not isinstance(hueco, dict):
            continue
        puntos = [
            a_relativas(p.get("x"), p.get("y"))
            for p in hueco.get("points") or [] if isinstance(p, dict)
        ]
        if len(puntos) < 3 or any(p is None for p in puntos):
            continue
        huecos.append({
            "nombre": (hueco.get("name") or "Hueco").strip(),
            "puntos": [list(p) for p in puntos],
        })

    if not nodos and not paredes:
        raise CoberturaError(
            f"{sujeto[0].upper()}{sujeto[1:]} no tiene nodos ni paredes; no hay nada "
            "que importar."
        )

    return plano, {
        "escala_m_px": m_por_px,
        # Dimensiones reales del plano, útiles para reportar la superficie.
        "ancho_m":     round(ancho * m_por_px, 2),
        "alto_m":      round(alto * m_por_px, 2),
        "nodos":       nodos,
        "paredes":     paredes,
        "huecos":      huecos,
    }


def parse_proyecto(data: bytes, red: int | None = None
                   ) -> tuple[list[tuple[Image.Image, dict]], list[str]]:
    """
    Lee un `.casambi` y devuelve ([(plano, cobertura), …], avisos).

    Hay un par por nivel con algo que importar, de abajo hacia arriba; un
    proyecto de un solo nivel da uno. `cobertura` es el bloque que se persiste en
    `planos_<red>.json`: metadatos del proyecto y del nivel, `nodos`, `paredes` y
    `huecos` ya en coordenadas relativas 0-1 sobre su plano. `unit_id` nace en
    None — el simulador etiqueta los nodos N1, N2… y esos nombres no se parecen a
    los de las unidades reales, así que la asociación con la red la hace el
    usuario desde la interfaz.

    `avisos` dice qué niveles se dejaron fuera y por qué. Solo si no queda ninguno
    el proyecto entero se rechaza.

    `red` importa solo esa red Casambi del simulador: sus nodos, en los niveles
    donde tiene alguno. None importa todo, como antes de que hubiera redes por
    equipo.
    """
    proyecto = _carga(data)

    version = proyecto.get("version", 1)
    if not isinstance(version, int) or version not in VERSIONES_CONOCIDAS:
        raise CoberturaError(
            f"El proyecto está en el formato v{version} del simulador, más nuevo que el "
            f"que esta app sabe leer (hasta v{VERSIONES_CONOCIDAS[-1]}). Actualiza CASAMBI "
            "para importarlo."
        )

    # Las paredes referencian su material; la pérdida se resuelve aquí para que
    # el informe no tenga que arrastrar la tabla de materiales. La tabla es del
    # proyecto, compartida por todos los niveles.
    perdidas = {
        str(m.get("id")): m.get("lossDb")
        for m in proyecto.get("materials") or [] if isinstance(m, dict)
    }

    niveles = _niveles(proyecto)
    varios = len(niveles) > 1
    nombres_de_red = _nombres_de_red(proyecto)
    if red is not None:
        presentes = {r for nivel in niveles for r in _nodos_por_red(nivel)}
        if red not in presentes:
            disponibles = ", ".join(titulo_de_red(r, nombres_de_red) for r in sorted(presentes))
            raise CoberturaError(
                f"El proyecto no tiene nodos en la Red {red}. Trae: "
                f"{disponibles or 'ninguna'}."
            )
    planos: list[tuple[Image.Image, dict]] = []
    avisos: list[str] = []
    for indice, nivel in enumerate(niveles):
        nombre = ((nivel.get("name") or "").strip() or f"Nivel {indice}") if varios else None
        # Un nivel sin nodos de la red pedida no es de esa red: su plano iría
        # vacío al informe de una red a la que no pertenece.
        if red is not None and red not in _nodos_por_red(nivel):
            continue
        try:
            plano, datos = _importa_nivel(nivel, perdidas, nombre, red)
        except CoberturaError as e:
            if not varios:
                raise
            avisos.append(str(e))
            continue
        planos.append((plano, {
            "proyecto":  (proyecto.get("projectName") or "").strip(),
            "cliente":   (proyecto.get("clientName") or "").strip(),
            "perfil":    (proyecto.get("profileId") or "").strip(),
            "n":         proyecto.get("nExponent"),
            # None en un proyecto de un solo nivel.
            "nivel":     nombre,
            "niveles":   len(niveles),
            # Sólo las redes con nombre; las demás son «Red N».
            "redes":     nombres_de_red,
            # La red importada, o None si entraron todas.
            "red":       red,
            "red_titulo": titulo_de_red(red, nombres_de_red) if red is not None else None,
            **datos,
        }))

    if not planos:
        raise CoberturaError(
            "Ningún nivel del proyecto se pudo importar. " + " ".join(avisos)
        )
    return planos, avisos
