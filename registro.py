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
from pathlib import Path

# Los nombres de `sensors` en el estado. Temperatura: Eulum TRED-E-CSB-2A;
# las cuatro eléctricas: McWong PSC-WCM-450-Power-Metering.
MAGNITUDES = ("Temperature", "Voltage", "Current", "Power", "Energy")

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


def abrir(ruta: Path) -> sqlite3.Connection:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(ruta), timeout=30)
    conn.execute(_ESQUEMA)
    return conn


def extraer(state: dict) -> list[tuple]:
    """(unidad, magnitud, momento, valor) de cada lectura del estado de una red."""
    filas = []
    for u in state.get("units") or []:
        if not isinstance(u.get("id"), int):
            continue
        for s in u.get("sensors") or []:
            valor, momento = s.get("value"), s.get("timestamp")
            if (s.get("name") in MAGNITUDES
                    and isinstance(valor, (int, float)) and not isinstance(valor, bool)
                    and isinstance(momento, (int, float))):
                filas.append((u["id"], s["name"], int(momento), float(valor)))
    return filas


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
