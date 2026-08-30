# -*- mode: python ; coding: utf-8 -*-
# Receta de PyInstaller para construir CASAMBI.app.
# Reconstruir con:  .venv/bin/pyinstaller CASAMBI.spec --noconfirm

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("templates", "templates"),
        ("static", "static"),
        ("logos", "logos"),
        # Semilla: se copia a ~/Library/Application Support/CASAMBI en el
        # primer arranque; después la app usa esa copia.
        ("data", "data"),
        # OJO: no incluir nunca el .env aquí. Las credenciales viven en el
        # Llavero de macOS (credentials.py); si se empaquetan viajan en texto
        # plano dentro del .app y quedan expuestas a quien reciba la app.
    ],
    # PyMuPDF se importa de forma diferida dentro de _pdf_to_image(), así que
    # el análisis estático no lo ve; sin esto los planos en PDF fallan en la .app.
    hiddenimports=["pymupdf", "fitz"],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CASAMBI",
    console=False,
    icon="CASAMBI.icns",
)

coll = COLLECT(exe, a.binaries, a.datas, name="CASAMBI")

app = BUNDLE(
    coll,
    name="CASAMBI.app",
    icon="CASAMBI.icns",
    bundle_identifier="com.impelsa.casambi",
    info_plist={
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        "CFBundleShortVersionString": "1.0.0",
        "NSAppTransportSecurity": {
            # El WebView carga el servidor Flask local por http://
            "NSAllowsLocalNetworking": True,
        },
    },
)
