# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the cs2hl desktop app: one-file, windowed build named cs2hl-gui.

Build from the repository root:
    pyinstaller packaging/cs2hl-gui.spec --noconfirm --distpath dist --workpath build

The entry script is launch_gui.py, which calls cs2hl.gui.app.main. It is a separate file
because the GUI modules use relative imports and cannot run as a top-level script.
"""

import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.dirname(SPECPATH)
ENTRY = os.path.join(SPECPATH, "launch_gui.py")

# demoparser2 is a compiled extension (.pyd on Windows, .so on Linux). collect_all picks up
# the binary and any package data that the static analysis would miss.
parser_datas, parser_binaries, parser_imports = collect_all("demoparser2")

# Placeholder: ffmpeg.exe is not bundled by this spec. The CI workflow copies it next to the
# built exe, and cs2hl/ffmpeg.py looks for it in the folder of the frozen exe.
# Add (source, "."), tuples here only if the build should carry ffmpeg itself.
FFMPEG_BINARIES = []

# HLAE is never bundled. The user installs it separately and points the app at it.

EXCLUDES = [
    # test-only packages
    "pytest",
    "_pytest",
    "pytest_qt",
    "pluggy",
    "iniconfig",
    "numpy.tests",
    "numpy._pytesttester",
    "tkinter",
    # Qt modules the app does not use; each is large
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DAnimation",
    "PySide6.QtQuick3D",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtGraphsWidgets",
    "PySide6.QtDesigner",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtHelp",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtSerialPort",
    "PySide6.QtSerialBus",
    "PySide6.QtRemoteObjects",
    "PySide6.QtLocation",
    "PySide6.QtPositioning",
    "PySide6.QtSensors",
    "PySide6.QtTextToSpeech",
    "PySide6.QtHttpServer",
    "PySide6.QtNetworkAuth",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
]

a = Analysis(
    [ENTRY],
    pathex=[ROOT],
    binaries=parser_binaries + FFMPEG_BINARIES,
    datas=parser_datas,
    # demoparser2 converts its results with to_pandas() inside the native module, so the
    # pandas import is invisible to static analysis.
    hiddenimports=parser_imports + ["pandas"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="cs2hl-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
