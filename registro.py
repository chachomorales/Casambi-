"""
Registro propio de temperatura y medidas eléctricas, para poder graficarlas.

La nube de Casambi no sirve para eso: el estado solo da la última lectura de
cada unidad, y el data logging guarda 60 días y, en las redes reales
(2026-11-24), ni temperatura ni potencia. Así que el servidor consulta el
estado cada pocos minutos y guarda aquí cada lectura nueva.

Se guarda la hora de la *lectura* —el `timestamp` que trae la nube—, no la de
la consulta. Una unidad cuyo gateway no sube datos repite la misma lectura en
cada consulta, y la clave primaria la descarta: no se acumulan puntos falsos
que dibujarían una línea plana donde en realidad no hubo dato.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path

# Los nombres de `sensors` en el estado. Temperatura: Eulum TRED-E-CSB-2A;
# las cuatro eléctricas: McWong PSC-WCM-450-Power-Metering.
MAGNITUDES = ("Temperature", "Voltage", "Current", "Power", "Energy")

# La luz no va en `sensors` sino en `controls` (tipo `Lux`), y sin hora: es la
# lectura viva, así que se guarda con la de la consulta. Solo de unidades en
# línea; una desconectada conserva su último valor y parecería actual.
LUZ = "Lux"

# El registro de la nube de LANCO trae unos 18.000 lux al día; se guardan como
# medias de 5 minutos, que siguen siendo más finas que la consulta propia.
TRAMO_IMPORTACION = 5 * 60 * 1000

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS lecturas (
    red       TEXT    NOT NULL,
    unidad    INTEGER NOT NULL,
    magnitud  TEXT    NOT NULL,
    momento   INTEGER NOT NULL,   -- ms desde 1970, UTC: la hora de la lectura
    valor     REAL    NOT NULL,
    PRIMARY KEY (red, unidad, magnitud, momento)
) WITHOUT ROWID
"""

# Ocupación por hora: segundos con presencia y segundos con dato. Sale del
# registro de presencia de la nube, que no guarda puntos sino tramos: cada
# datapoint `presence` es un intervalo que empieza a su hora, dura |valor|
# segundos y es «hay alguien» si el valor es positivo (comprobado en LANCO el
# 2026-11-24: el 99,6 % de 10.080 tramos empieza donde acaba el anterior, y el
# signo coincidió con el estado en vivo). El porcentaje se calcula sobre lo
# observado, no sobre la hora entera, para que un hueco del gateway no pase por
# «nadie».
_ESQUEMA_OCUPACION = """
CREATE TABLE IF NOT EXISTS ocupacion (
    red        TEXT    NOT NULL,
    unidad     INTEGER NOT NULL,
    hora       INTEGER NOT NULL,   -- ms UTC del comienzo de la hora
    ocupado    REAL    NOT NULL,   -- segundos con presencia
    observado  REAL    NOT NULL,   -- segundos con dato, con o sin presencia
    PRIMARY KEY (red, unidad, hora)
) WITHOUT ROWID
"""
PRESENCIA = "Presence"
HORA = 3_600_000

# Qué historiales ya se trajeron del registro de la nube, para no repetirlo.
_ESQUEMA_IMPORTACIONES = """
CREATE TABLE IF NOT EXISTS importaciones (
    red       TEXT    NOT NULL,
    magnitud  TEXT    NOT NULL,
    hecha     INTEGER NOT NULL,   -- ms
    lecturas  INTEGER NOT NULL,
    PRIMARY KEY (red, magnitud)
)
"""


def abrir(ruta: Path) -> sqlite3.Connection:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(ruta), timeout=30)
    conn.execute(_ESQUEMA)
    conn.execute(_ESQUEMA_IMPORTACIONES)
    conn.execute(_ESQUEMA_OCUPACION)
    return conn


def _numero(valor) -> bool:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def extraer(state: dict, consulta: int | None = None) -> list[tuple]:
    """
    (unidad, magnitud, momento, valor) de cada lectura del estado de una red.

    `consulta` (ms) es la hora que se les pone a los lux, que no traen la suya;
    se redondea al minuto para que dos lecturas del mismo minuto no se dupliquen.
    """
    consulta = (int(consulta if consulta is not None else time.time() * 1000)
                // 60_000 * 60_000)
    filas = []
    for u in state.get("units") or []:
        if not isinstance(u.get("id"), int):
            continue
        for s in u.get("sensors") or []:
            valor, momento = s.get("value"), s.get("timestamp")
            if s.get("name") in MAGNITUDES and _numero(valor) and _numero(momento):
                filas.append((u["id"], s["name"], int(momento), float(valor)))
        if not u.get("online"):
            continue
        for grupo in u.get("controls") or []:
            for c in grupo if isinstance(grupo, list) else [grupo]:
                if (isinstance(c, dict) and c.get("type") == LUZ
                        and _numero(c.get("value"))):
                    filas.append((u["id"], LUZ, consulta, float(c["value"])))
    return filas


def momento_datapoint(texto) -> int | None:
    """Ms UTC de un datapoint. La nube recorta los ceros de los milisegundos
    (`…26.47+00:00`), que `fromisoformat` de Python 3.9 no acepta."""
    for formato in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return int(datetime.strptime(str(texto), formato).timestamp() * 1000)
        except ValueError:
            continue
    return None


def promediar_datapoints(puntos: list, magnitud: str,
                         tramo: int = TRAMO_IMPORTACION) -> list[tuple]:
    """Datapoints de la nube como filas del registro, en medias por tramo."""
    sumas: dict[tuple, list] = {}
    for p in puntos:
        momento = momento_datapoint(p.get("time"))
        try:
            valor = float(p.get("value"))
            unidad = int(p.get("unit"))
        except (TypeError, ValueError):
            continue
        if momento is None:
            continue
        clave = (unidad, momento // tramo)
        acumulado = sumas.setdefault(clave, [0.0, 0])
        acumulado[0] += valor
        acumulado[1] += 1
    return [(unidad, magnitud, bloque * tramo + tramo // 2, round(suma / n, 1))
            for (unidad, bloque), (suma, n) in sorted(sumas.items())]


def importado(conn: sqlite3.Connection, red: str, magnitud: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM importaciones WHERE red = ? AND magnitud = ?",
        (str(red), magnitud)).fetchone() is not None


def importado_en(conn: sqlite3.Connection, red: str, magnitud: str) -> int | None:
    """Ms de la última importación de esa medida, o None si nunca se hizo."""
    fila = conn.execute(
        "SELECT hecha FROM importaciones WHERE red = ? AND magnitud = ?",
        (str(red), magnitud)).fetchone()
    return fila[0] if fila else None


def marcar_importado(conn: sqlite3.Connection, red: str, magnitud: str,
                     lecturas: int) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO importaciones VALUES (?, ?, ?, ?)",
                     (str(red), magnitud, int(time.time() * 1000), lecturas))


def guardar(conn: sqlite3.Connection, red: str, filas: list[tuple]) -> int:
    """Guarda las lecturas nuevas de una red; devuelve cuántas lo eran."""
    antes = conn.total_changes
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO lecturas VALUES (?, ?, ?, ?, ?)",
            [(str(red), *f) for f in filas],
        )
    return conn.total_changes - antes


def purgar(conn: sqlite3.Connection, dias: int, ahora: float | None = None) -> int:
    """Borra lo más viejo que `dias`. Devuelve cuántas lecturas quitó."""
    limite = int(((ahora or time.time()) - dias * 86400) * 1000)
    antes = conn.total_changes
    with conn:
        conn.execute("DELETE FROM lecturas WHERE momento < ?", (limite,))
    return conn.total_changes - antes


def contar(conn: sqlite3.Connection) -> dict:
    """Cifras para el log y para comprobar a mano que el registro avanza."""
    total, redes, ultima = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT red), MAX(momento) FROM lecturas"
    ).fetchone()
    return {"lecturas": total, "redes": redes, "ultima": ultima}


def primera_lectura(conn: sqlite3.Connection, red: str, magnitud: str) -> int | None:
    """Momento (ms) de la lectura más antigua de esa magnitud en la red."""
    (momento,) = conn.execute(
        "SELECT MIN(momento) FROM lecturas WHERE red = ? AND magnitud = ?",
        (str(red), magnitud),
    ).fetchone()
    return momento


def serie(conn: sqlite3.Connection, red: str, magnitud: str,
          desde: int, tramo: int) -> dict:
    """
    Lecturas de cada unidad desde `desde` (ms), promediadas por tramos de `tramo` ms.

    Con 41 unidades y una lectura cada 15 min, seis meses son más de 700.000
    puntos: ni el navegador los dibuja ni hacen falta, porque la gráfica no
    tiene tantos píxeles. Las cifras de la tabla (mínimo, máximo, media y
    última) salen de las lecturas sin promediar, para que un pico no se diluya.
    """
    red = str(red)
    puntos: dict[int, list] = {}
    for unidad, bloque, media in conn.execute(
        "SELECT unidad, momento / ? AS bloque, AVG(valor) FROM lecturas"
        " WHERE red = ? AND magnitud = ? AND momento >= ?"
        " GROUP BY unidad, bloque ORDER BY unidad, bloque",
        (tramo, red, magnitud, desde),
    ):
        # El punto va en el centro de su tramo: al principio, una media de
        # varias horas aparecería adelantada respecto a lo que resume.
        puntos.setdefault(unidad, []).append(
            [bloque * tramo + tramo // 2, round(media, 1)])

    cifras = {}
    for unidad, minimo, maximo, media, ultima_momento in conn.execute(
        "SELECT unidad, MIN(valor), MAX(valor), AVG(valor), MAX(momento)"
        " FROM lecturas WHERE red = ? AND magnitud = ? AND momento >= ?"
        " GROUP BY unidad",
        (red, magnitud, desde),
    ):
        (ultima,) = conn.execute(
            "SELECT valor FROM lecturas WHERE red = ? AND magnitud = ?"
            " AND unidad = ? AND momento = ?",
            (red, magnitud, unidad, ultima_momento),
        ).fetchone()
        cifras[unidad] = {"min": minimo, "max": maximo, "media": round(media, 1),
                          "ultima": ultima, "ultima_momento": ultima_momento}
    return {u: {"puntos": puntos.get(u, []), **c} for u, c in cifras.items()}


def tiene_presencia(state: dict) -> bool:
    """Si alguna unidad de la red tiene control de presencia."""
    for u in state.get("units") or []:
        for grupo in u.get("controls") or []:
            for c in grupo if isinstance(grupo, list) else [grupo]:
                if isinstance(c, dict) and c.get("type") == "Presence":
                    return True
    return False


def sumar_ocupacion(puntos: list, horas: dict | None = None) -> dict:
    """{(unidad, hora): [ocupado, observado]} a partir de los tramos de presencia.

    Un tramo que cruza el cambio de hora se reparte entre las dos. Con `horas`
    se acumula sobre lo ya sumado: la importación va por trozos y así no hace
    falta tener en memoria los 600.000 tramos de 60 días de LANCO.
    """
    horas = {} if horas is None else horas
    for p in puntos:
        inicio = momento_datapoint(p.get("time"))
        try:
            valor = float(p.get("value"))
            unidad = int(p.get("unit"))
        except (TypeError, ValueError):
            continue
        if inicio is None or valor == 0:
            continue
        fin = inicio + int(abs(valor) * 1000)
        t = inicio
        while t < fin:
            hora = t // HORA * HORA
            corte = min(fin, hora + HORA)
            acumulado = horas.setdefault((unidad, hora), [0.0, 0.0])
            segundos = (corte - t) / 1000
            acumulado[1] += segundos
            if valor > 0:
                acumulado[0] += segundos
            t = corte
    return horas


def guardar_ocupacion(conn: sqlite3.Connection, red: str, horas: dict,
                      desde: int) -> int:
    """
    Escribe las horas desde `desde` (ms), sustituyendo lo que hubiera.

    Se sustituye y no se suma porque cada importación vuelve a pedir el último
    día: un tramo largo («nadie» toda la noche) solo se registra al acabar, así
    que las últimas horas llegan incompletas y se completan en la siguiente.
    """
    filas = [(str(red), unidad, hora, round(o, 1), round(v, 1))
             for (unidad, hora), (o, v) in horas.items() if hora >= desde]
    with conn:
        conn.executemany("INSERT OR REPLACE INTO ocupacion VALUES (?, ?, ?, ?, ?)",
                         filas)
    return len(filas)


def ultima_hora_ocupacion(conn: sqlite3.Connection, red: str) -> int | None:
    (hora,) = conn.execute("SELECT MAX(hora) FROM ocupacion WHERE red = ?",
                           (str(red),)).fetchone()
    return hora


def ocupacion(conn: sqlite3.Connection, red: str, desde: int) -> list[tuple]:
    """(unidad, hora, ocupado, observado) de la red desde `desde` (ms)."""
    return conn.execute(
        "SELECT unidad, hora, ocupado, observado FROM ocupacion"
        " WHERE red = ? AND hora >= ? ORDER BY unidad, hora",
        (str(red), desde)).fetchall()
