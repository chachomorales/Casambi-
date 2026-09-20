# CASAMBI — Generador de informes de redes Casambi

Lee una red de iluminación Casambi desde la nube, la muestra en una interfaz de
pestañas y exporta un informe Excel con formato corporativo.

**Esta rama (`web`) sirve las dos versiones desde la misma base de código.** La de
escritorio es la de siempre: `desktop.py` arranca Flask en un hilo, en un puerto
local aleatorio, dentro de una ventana WKWebView. La web corre con gunicorn en un
contenedor, detrás de Cloudflare Access. `config.py` decide cuál es cuál según
`CASAMBI_MODE`, y `app.py` nunca se ejecuta por sí solo en ninguna de las dos.

La rama `main` (en `~/Developer/CASAMBI`) sigue siendo solo la de macOS. Un arreglo
que valga para ambas se pasa con `git cherry-pick`.

## Arranque

```bash
.venv/bin/python desktop.py          # escritorio, en desarrollo

# servidor, en desarrollo (sin Access: CASAMBI_MODE=desktop lo desactiva)
CASAMBI_MODE=desktop .venv/bin/gunicorn -w 1 -k gthread --threads 8 \
  -b 127.0.0.1:8000 wsgi:application

.venv/bin/python -m pytest tests/ -q   # 225 tests, ~7 s

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

## La versión web

Los detalles de puesta en marcha están en `despliegue/README.md`. Lo que conviene
saber al tocar el código:

- **Un solo worker, a propósito.** `gunicorn -w 1 -k gthread --threads 8`. Las
  sesiones de Casambi y la caché de redes viven en memoria del proceso y se
  comparten entre todo el equipo, que es lo que queremos con credenciales de
  empresa. Con varios workers habría una autenticación y una caché por proceso, y
  `/carga/estado` respondería desde un worker distinto al que está descargando.
  `--timeout 300` tampoco es adorno: `scene_capture` duerme diez segundos y un
  Excel con planos puede pasar de treinta.
- **Todo POST necesita token CSRF.** Los formularios llevan `{{ csrf_token() }}`;
  el JavaScript de `network.html` lo toma del `<meta name="csrf-token">` de
  `base.html` y lo manda en `X-CSRFToken`. Al añadir un `fetch`, no olvidarlo.
- **Las rutas de red van decoradas, y el orden importa:** `@require_network`
  primero y `@con_lock_de_red` después. Al revés, cada `network_id` inventado
  dejaría una entrada en `_net_locks` y la memoria crecería sin tope.
- **Las anotaciones se escriben con `_write_json`**, nunca con `write_text`: es
  atómico, y un corte a media escritura dejaba el fichero truncado, que los
  loaders interpretaban como «no hay anotaciones». Un JSON ilegible ahora se
  aparta como `<nombre>.corrupto-<fecha>` en vez de desaparecer.
- **El progreso es por tarea.** `_start_loading(clave, work)` devuelve un id; dos
  personas en la misma red comparten una sola descarga porque comparten clave.
- **Ante la duda, `auth.py` cierra.** Si no se puede consultar el JWKS de
  Cloudflare, la respuesta es 503, nunca un 200.

## Estructura

| Archivo | Rol |
|---|---|
| `desktop.py` | Lanzador de escritorio: puerto libre, hilo Flask, ventana pywebview. Prepara `CASAMBI_HOME` y fija `CASAMBI_MODE=desktop` |
| `wsgi.py` | Lanzador web: valida la configuración al arrancar y aplica `ProxyFix` |
| `config.py` | Lo que difiere entre las dos versiones: clave, backend de credenciales, límites, cookies |
| `auth.py` | Verificación del JWT de Cloudflare Access; puebla `g.user_email` |
| `tests/` | 225 tests. Cliente de Casambi y Llavero sustituidos: nunca salen a la red |
| `despliegue/` | Dockerfile y compose en la raíz; aquí el README de operación y los scripts de copia |
| `app.py` | Servidor interno: ~20 rutas, caché en memoria por red, pantalla de progreso, anotaciones del usuario |
| `casambi_api.py` | Cliente de `door.casambi.com` + bridge WebSocket para activar escenas |
| `report.py` | Excel con openpyxl: 12 hojas, portada con tarjetas, planos compuestos con Pillow. También `diagnostico_conectividad()`, que usan la hoja Conectividad y la interfaz |
| `credentials.py` | Cuentas múltiples en el Llavero de macOS vía `/usr/bin/security` |
| `cobertura.py` | Importa proyectos `.casambi` del Simulador de Cobertura: plano, nodos y paredes |
| `templates/`, `static/` | Interfaz: `network.html` (9 pestañas) es el grueso |

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
(`buttons_`, `sensors_`, `schedules_`, `scene_levels_`, `planos_`, `bitacora_`). Todo eso acaba en
el Excel. **Al tocar esas hojas del informe, recuerda que su origen es el JSON local,
no la API.**

Los planos se pueden subir a mano (PDF vía PyMuPDF, o imagen) y colocar marcadores;
la galería de Casambi (`network['photos']`) ya trae posiciones y se compone aparte.
Las imágenes se cachean en `data/images/<image_id>.png` — los IDs son inmutables.

### Planos importados del Simulador de Cobertura

El proyecto hermano `~/Developer/casambi-wifi-coverage` (apps Swift de preventa) guarda
sus proyectos como `.casambi`: JSON plano con el plano en un data URL, los nodos y las
paredes en **coordenadas del mundo, en metros**. `cobertura.py` los traduce a relativas
0-1 —las mismas que `markers`— y `plano_upload()` las persiste en el mismo
`planos_<red>.json` con `"origen": "cobertura"` y un bloque `"cobertura"`.

La conversión sale de `SceneRenderer.swift`: la esquina superior izquierda de la imagen
está en `(imageOriginX, imageOriginY)`, así que
`px = (x - imageOriginX) / metersPerPixel`, y la relativa es `px / anchoPx`. Como son
fracciones, el reescalado a `PLANO_MAX_PX` no las invalida.

**Proyectos de varios niveles (formato 4).** Desde el 2026-09-13 el simulador admite
pisos: cada nivel guarda su plano, escala, origen, nodos, paredes y huecos de losa
dentro de `levels`, y los campos de la raíz van vacíos. Leído como un proyecto de un
nivel, eso daba «no tiene plano cargado». `parse_proyecto()` devuelve un plano por
nivel, convirtiendo cada uno con su propio origen y escala, y `plano_upload()` los
guarda por separado con el nombre del nivel. Un nivel sin plano, sin escala o vacío
se salta con aviso; el archivo solo se rechaza si no queda ninguno. Un proyecto de un
nivel sigue escribiéndose plano (v1-v3), igual que antes: la lista `levels` solo
existe con dos o más. Una versión mayor a 4 se rechaza a propósito, porque el
simulador sube la versión justo cuando un lector anterior leería mal el archivo.

**El `.casambi` no guarda el mapa de calor.** El `image` embebido es el plano
arquitectónico desnudo; el heatmap y las paredes son vectores que el motor Swift calcula
en vivo y nunca se serializan. Por eso se importa la *planificación* (qué se simuló,
dónde, con qué paredes) y el plano se redibuja con marcadores propios. Para llevar el
mapa de calor al informe hay que exportarlo aparte desde el simulador (⇧⌘E) y subirlo
como una imagen más. Recalcularlo en Python exigiría portar ~700 líneas de Swift
(`Propagation`, `Raycast`, `WallIndex`, `Heatmap`, `CasambiProfile`) y dejaría dos
implementaciones de la misma física; se descartó por eso.

Dos consecuencias de diseño que conviene respetar:

- **Los nodos no son `markers`.** Los coloca el simulador, no el usuario, así que ese
  plano no admite el flujo de clic para situar elementos (`esCobertura` lo corta en el
  JS y `plano-cobertura` cambia el cursor). Lo único que se edita es la asociación
  nodo → unidad real, acción `nodo_unit`.
- **Esa asociación solo puede hacerla el usuario.** El simulador etiqueta los nodos
  `N1`, `N2`… — nombres que no se parecen a los de las unidades de la red, de modo que
  no hay emparejamiento automático posible. Sin asociar, el nodo es una posición
  propuesta y se dibuja en cian; asociado toma el color de su categoría.

En la hoja Planos van al final, con leyenda propia (`Nº · Nodo · Unidad asociada ·
Categoría · Nota de montaje`) y bajo el título `COBERTURA SIMULADA:`: son planificación,
no inventario, y mezclarlos con lo instalado haría leer una propuesta como un hecho.

## Datos y credenciales

- Empaquetada, la app escribe en `~/Library/Application Support/CASAMBI`
  (`CASAMBI_HOME`); en desarrollo, junto al código. `data/` del bundle es solo semilla
  del primer arranque.
- Las credenciales viven en el Llavero de macOS, servicio `com.impelsa.casambi`, y se
  editan desde la pantalla de Ajustes. **En el servidor no hay Llavero**: la misma
  API de `credentials.py` escribe en `data/credentials.enc`, cifrado con Fernet y
  una clave derivada de `CASAMBI_SECRET_KEY` con PBKDF2. El backend se elige solo
  (`config.backend_credenciales()`), y de la capa de índice hacia arriba el módulo
  no sabe cuál usa. Perder esa clave es perder las credenciales, también las de las
  copias de seguridad. **Nunca añadir `.env` a `datas` en
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
- `controls` viene **plano** en la config de la red (`[{...}]`) y **anidado** en el
  estado (`[[{...}]]`). `_classify_unit()` y `_controls_summary()` esperan el plano:
  pásales unidades de `network`, nunca de `state`, o revientan con
  `'list' object has no attribute 'get'`.

## Conectividad: dos falsos positivos que hay que respetar

La hoja Conectividad y la pestaña del mismo nombre salen de
`report.diagnostico_conectividad(network, state)`. Su lógica no es «listar lo que está
offline», y hay dos razones medidas sobre las redes reales:

- **`online` depende del gateway.** Si la pasarela no responde, las unidades salen
  todas offline a la vez: en el barrido de las 39 redes, 35 daban 0 online. Un listado
  ingenuo acusaría a 130 luminarias sanas. Por eso solo se marca `No responde` cuando
  *alguna* unidad responde (`fiable`); si no, el estado es `Sin lectura` y el veredicto
  apunta al gateway.
- **Los `BatterySwitch` duermen.** De 180 en total, ninguno estaba online, y son los
  únicos 153 sin `firmwareVersion`. Solo despiertan al pulsarlos, así que se cuentan
  aparte como `En reposo`, nunca como avería (`_TIPOS_EN_REPOSO`).

`condition` es el otro dato aprovechable: la API no lo documenta, valía 0 en 1.544 de
1.550 unidades y 128 en seis luminarias por lo demás sanas (`status: "ok"`, online).
Se muestra como aviso sin traducirlo a una causa, porque no sabemos cuál es.

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
