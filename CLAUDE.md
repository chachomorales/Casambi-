# CASAMBI — Generador de informes de redes Casambi

App de escritorio nativa de macOS (Impelsa) que lee una red de iluminación Casambi
desde la nube, la muestra en una interfaz de pestañas y exporta un informe Excel
con formato corporativo.

No es una web: `desktop.py` arranca Flask en un hilo, en un puerto local aleatorio,
y lo sirve dentro de una ventana WKWebView (pywebview). `app.py` nunca se ejecuta
por sí solo.

## Arranque

```bash
.venv/bin/python desktop.py          # desarrollo

# empaquetar — SIEMPRE fuera del proyecto (ver más abajo), y luego instalar
.venv/bin/pyinstaller CASAMBI.spec --noconfirm --distpath /tmp/casambi-dist --workpath /tmp/casambi-build
rm -rf /Applications/CASAMBI.app && ditto /tmp/casambi-dist/CASAMBI.app /Applications/CASAMBI.app
```

Usa `ditto`, no `cp -R`: es lo correcto para bundles (preserva metadatos y firma).

### Historia: el proyecto estuvo en OneDrive

Desde el 2026-08-29 el proyecto vive en `~/Developer/CASAMBI`, fuera de cualquier
carpeta sincronizada. Lo que sigue es historia, pero merece quedarse: explica el
`--distpath` de más abajo y sirve de diagnóstico si el código vuelve a una carpeta
sincronizada.

Mientras estuvo bajo OneDrive, el `.venv` se rompió: OneDrive aplanó los enlaces
simbólicos —`.venv/bin/python` acabó siendo un fichero de texto de 7 bytes con la
palabra `python3` dentro— y además quedaron mezclados el `bin/` de macOS con el
`Scripts/`, `Lib/` y los `.exe` de la etapa Windows.

**El síntoma es traicionero: no da error.** Ejecuta `python3` sin argumentos, no
imprime nada y sale con código 0, así que cualquier comando parece colgarse o
devolver vacío. Si ves eso, comprueba el intérprete antes de depurar nada más:

```bash
file .venv/bin/python     # debe ser un Mach-O executable, no "ASCII text"
```

Para rehacerlo (Apple no admite `--copies`, pero su build copia el binario igual):

```bash
rm -rf .venv __pycache__ && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Mudar el proyecto de carpeta no daña el binario, pero **deja inservibles los scripts de
consola** del venv: `pip`, `pyinstaller`, `flask`, `dotenv` y los `activate*` llevan la
ruta absoluta del intérprete en el shebang y pasan a fallar con
`bad interpreter: ... no such file or directory`. Ocurrió con la mudanza del 2026-08-29
y se resolvió reescribiendo la ruta en los ~22 ficheros afectados:

```bash
grep -rl "<ruta vieja>" .venv/bin | xargs sed -i '' 's|<ruta vieja>|<ruta nueva>|g'
```

Rehacer el venv también vale. Y como atajo, `.venv/bin/python -m pip` y
`-m PyInstaller` funcionan aunque el shebang esté roto: no pasan por él.

Solo hay Python 3.9.6 (el de CommandLineTools) en la máquina; es con el que se creó.
`requirements.txt` no fija versiones, así que un `pip install` limpio trae lo último
compatible con 3.9 — hoy Flask 3.1.3, pywebview 6.2.1, PyMuPDF 1.26.5, Pillow 11.3.
La API que usa `desktop.py` (`webview.settings["ALLOW_DOWNLOADS"]`, `min_size`) sigue
existiendo en pywebview 6, pero es un salto de versión mayor: verifícalo si algo de la
ventana se comporta raro. `urllib3` avisa de `NotOpenSSLWarning` (LibreSSL, propio del
Python de Apple); es cosmético.

### La sincronización también corrompía el `.app` — compila siempre fuera del proyecto

El mismo aplanado de symlinks arruina los bundles de PyInstaller, y ahí es **fatal**:
`Contents/Frameworks/Python3` y `Contents/Resources/Python3` son symlinks al binario
real dentro de `Python3.framework/`, y el cliente de sincronización los convierte en
ficheros de texto de 38 bytes con la ruta dentro. La app entonces no arranca:

```
Failed to load Python shared library '.../Contents/Frameworks/Python3':
(slice is not valid mach-o file)
```

Pasó de verdad: el `dist/CASAMBI.app` del 28-08-2026 conservaba 6 symlinks de los 70
que tiene un bundle sano, y se cerraba nada más abrirla. Se borró junto con su hermana
`dist/CASAMBI` (2026-08-29); ya no existe ninguna de las dos en el proyecto.

**Por eso el `--distpath` fuera del proyecto del comando de arriba.** La regla se
mantiene aunque la carpeta ya no se sincronice: no cuesta nada y evita reintroducir el
problema si el código vuelve a la nube.

Y ojo con cómo se verifica un bundle: `find -type l ! -exec test -e {} \;` **no**
detecta este daño, porque los symlinks aplanados ya no son symlinks sino normales. La
comprobación buena es contarlos y mirar el framework:

```bash
find <bundle> -type l | wc -l                        # un bundle sano ronda los 70
file <bundle>/Contents/Frameworks/Python3            # debe ser Mach-O, no ASCII text
```

Una vez instalada en `/Applications`, la app es autónoma: no depende del `.venv` ni
de la carpeta del proyecto.

`main.py` es una CLI independiente y anterior (pregunta credenciales por consola,
elige red, genera Excel). `run.sh` / `run_web.sh` / `run.bat` / `run_web.bat` son de
esa etapa previa; el flujo vigente es `desktop.py`.

## Estructura

| Archivo | Rol |
|---|---|
| `desktop.py` | Lanzador: puerto libre, hilo Flask, ventana pywebview. Prepara `CASAMBI_HOME` |
| `app.py` | Servidor interno: ~20 rutas, caché en memoria por red, pantalla de progreso, anotaciones del usuario |
| `casambi_api.py` | Cliente de `door.casambi.com` + bridge WebSocket para activar escenas |
| `report.py` | Excel con openpyxl: 10 hojas, portada con tarjetas, planos compuestos con Pillow |
| `credentials.py` | Cuentas múltiples en el Llavero de macOS vía `/usr/bin/security` |
| `templates/`, `static/` | Interfaz: `network.html` (8 pestañas) es el grueso |

## Cómo fluyen los datos

1. `credentials.load_all()` → una `CasambiClient` autenticada por cuenta.
2. Cada cuenta autentica dos veces: `/v1/users/session/` (sesión global) y
   `/v1/networks/session` (una `sessionId` por red). Las peticiones a una red usan
   su sesión propia — `_headers_for(network_id)`.
3. `_fetch_network_data()` descarga config + estado + modelos y cachea en memoria.
   Los modelos (`/v1/fixtures/{id}`, una petición por modelo distinto) son la parte
   lenta: por eso todo el flujo lleva callbacks de progreso hasta `cargando.html`.
4. `generate_report()` mezcla los datos de la API con lo anotado por el usuario.

## Lo que la API no da, y el usuario anota

La nube de Casambi no expone la programación de pulsadores, el modo de los sensores,
los horarios ni los niveles por escena. La app los captura en la interfaz y los
guarda como JSON en `data/<tipo>_<network_id>.json`
(`buttons_`, `sensors_`, `schedules_`, `scene_levels_`, `planos_`). Todo eso acaba en
el Excel. **Al tocar esas hojas del informe, recuerda que su origen es el JSON local,
no la API.**

Los planos se pueden subir a mano (PDF vía PyMuPDF, o imagen) y colocar marcadores;
la galería de Casambi (`network['photos']`) ya trae posiciones y se compone aparte.
Las imágenes se cachean en `data/images/<image_id>.png` — los IDs son inmutables.

## Datos y credenciales

- Empaquetada, la app escribe en `~/Library/Application Support/CASAMBI`
  (`CASAMBI_HOME`); en desarrollo, junto al código. `data/` del bundle es solo semilla
  del primer arranque.
- Las credenciales viven en el Llavero de macOS, servicio `com.impelsa.casambi`, y se
  editan desde la pantalla de Ajustes. **Nunca añadir `.env` a `datas` en
  `CASAMBI.spec`**: viajaría en texto plano dentro del `.app`.
- Dos detalles del Llavero que ya costaron una depuración: los valores se guardan en
  **base64** (`security -w` imprime hex cuando hay bytes no ASCII, y ese hex es
  indistinguible de un valor ASCII hexadecimal), y se **trocean en fragmentos de 120
  caracteres** (`clave#0`, `clave#1`, …) porque el prompt de `security -w` trunca a 128
  en silencio. Pasar el valor como argumento evitaría el límite pero lo dejaría visible
  en `ps`.

## Trampas conocidas

- `webview.settings["ALLOW_DOWNLOADS"] = True` en `desktop.py`: sin eso WKWebView
  cancela la descarga sin avisar y "Descargar Excel" genera el archivo pero no lo entrega.
- `hiddenimports=["pymupdf", "fitz"]` en el `.spec`: PyMuPDF se importa de forma
  diferida dentro de `_pdf_to_image()`, así que PyInstaller no lo ve solo.
- `activate_scene()` necesita un gateway online (móvil o hardware) en la red; sin él
  el bridge no abre el canal.
- `CasambiClient._normalize_network()` convierte a lista las colecciones que la API
  devuelve como diccionario (`units`, `groups`, `scenes`). Trabaja siempre con listas
  aguas abajo.

## Convenciones

- Comentarios, docstrings, interfaz y nombres de hoja en **español**. Los comentarios
  explican *por qué*, no *qué* — mantén ese registro.
- La paleta del Excel está centralizada arriba de `report.py` (`COLOR_*`); no metas
  colores literales en las funciones de hoja.
- Repositorio git privado en `git@github.com:chachomorales/Casambi-.git`, rama `main`
  (ojo al guion final del nombre). `.gitignore` deja fuera `.venv/`, `reportes/`, `.env`
  y el contenido de `data/`: son informes, planos y anotaciones de instalaciones reales
  de clientes y no deben salir de la máquina. `data/.gitkeep` existe solo para que
  `CASAMBI.spec` siga encontrando la carpeta al empaquetar desde un clon limpio.
- `reportes/` acumula informes reales de clientes.
