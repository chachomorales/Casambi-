# CASAMBI — Generador de informes de redes Casambi

Aplicación de escritorio nativa de macOS (Impelsa) que lee una red de iluminación
Casambi desde la nube, la muestra en una interfaz de pestañas y exporta un informe
Excel con formato corporativo.

No es una aplicación web: `desktop.py` arranca un servidor Flask en un hilo, en un
puerto local aleatorio, y lo sirve dentro de una ventana WKWebView (pywebview).

## Requisitos

- macOS 11 o superior
- Python 3.9 (el de las Command Line Tools de Apple sirve)
- Una cuenta de Casambi con acceso a las redes que se quieran documentar

## Puesta en marcha

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python desktop.py
```

Las credenciales no se guardan en el repositorio: se introducen desde la pantalla
de Ajustes de la propia aplicación y quedan en el Llavero de macOS, bajo el
servicio `com.impelsa.casambi`.

## Empaquetado

PyInstaller debe escribir fuera de cualquier carpeta sincronizada en la nube: los
clientes de sincronización aplanan los enlaces simbólicos del bundle y la `.app`
resultante no arranca.

```bash
.venv/bin/pyinstaller CASAMBI.spec --noconfirm \
  --distpath /tmp/casambi-dist --workpath /tmp/casambi-build
rm -rf /Applications/CASAMBI.app
ditto /tmp/casambi-dist/CASAMBI.app /Applications/CASAMBI.app
```

## Estructura

| Archivo | Rol |
|---|---|
| `desktop.py` | Lanzador: puerto libre, hilo Flask, ventana pywebview |
| `app.py` | Servidor interno: rutas, caché por red, anotaciones del usuario |
| `casambi_api.py` | Cliente de `door.casambi.com` + bridge WebSocket para escenas |
| `report.py` | Generación del Excel con openpyxl y composición de planos con Pillow |
| `credentials.py` | Cuentas múltiples en el Llavero de macOS |
| `templates/`, `static/` | Interfaz web servida dentro de la ventana |

`main.py` y los scripts `run*.sh` / `run*.bat` son de una etapa anterior en línea
de comandos; el flujo vigente es `desktop.py`.

## Datos locales

Lo que la API de Casambi no expone —programación de pulsadores, modo de los
sensores, horarios, niveles por escena y planos— se anota en la interfaz y se
guarda como JSON en `data/`. Empaquetada, la aplicación escribe en
`~/Library/Application Support/CASAMBI`. Ese contenido corresponde a
instalaciones reales y queda fuera del control de versiones.

En `CLAUDE.md` están las notas de mantenimiento detalladas y las trampas conocidas.
