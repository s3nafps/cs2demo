# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the CS2 Highlights desktop app: windowed, one-folder build.

Build from the repository root:
    pyinstaller packaging/cs2hl.spec --noconfirm --distpath dist --workpath build

Result: dist/CS2Highlights/CS2Highlights.exe next to its _internal folder. One-folder starts
faster than one-file and trips fewer antivirus heuristics; the installer copies the folder as
is and adds ffmpeg.exe and ffprobe.exe next to the exe. HLAE is never bundled.
"""

import os
import re

import PySide6
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.dirname(SPECPATH)
ENTRY = os.path.join(SPECPATH, "launch_gui.py")
NAME = "CS2Highlights"
PRODUCT = "CS2 Highlights"


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def app_version():
    match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", read_text(os.path.join(ROOT, "cs2hl", "__init__.py")), re.M)
    if match is None:
        raise SystemExit("cs2hl/__init__.py has no __version__")
    return match.group(1)


def qt_modules_used():
    """PySide6 modules the cs2hl sources import, plus the three every widget app loads."""
    used = {"QtCore", "QtGui", "QtWidgets"}
    for folder, _dirs, files in os.walk(os.path.join(ROOT, "cs2hl")):
        for name in files:
            if name.endswith(".py"):
                text = read_text(os.path.join(folder, name))
                used.update(re.findall(r"\bPySide6\.(Qt\w+)", text))
                for names in re.findall(r"from\s+PySide6\s+import\s+\(?([\w\s,]+)", text):
                    used.update(n for n in re.findall(r"\w+", names) if n.startswith("Qt"))
    return used


def qt_excludes(used):
    """Every other PySide6.Qt* module installed: a hook or an optional import can pull them in."""
    installed = {n.split(".")[0] for n in os.listdir(os.path.dirname(PySide6.__file__)) if n.startswith("Qt") and n.endswith((".pyd", ".so", ".pyi"))}
    return sorted(f"PySide6.{n}" for n in installed - used)


def unused_file(dest):
    """Collected files the app never loads; several are among the largest in the build."""
    path = dest.replace("\\", "/")
    name = path.rsplit("/", 1)[-1]
    if path.startswith("pyarrow/"):
        # Flight RPC and the S3 filesystem. pyarrow.lib links every other arrow library.
        if re.search(r"arrow_(python_)?(flight|s3)|^_(flight|s3fs)\.", name):
            return True
        # C++ headers, Cython sources, import libraries and tests.
        return path.startswith(("pyarrow/include/", "pyarrow/src/", "pyarrow/tests/")) or name.endswith(
            (".h", ".hpp", ".cc", ".pxd", ".pyx", ".pxi", ".lib")
        )
    if path.startswith("PySide6/"):
        # The virtual-keyboard input plugin and the PDF image plugin, and the libraries only they load.
        if "/plugins/" in path and re.search(r"virtualkeyboard|qpdf", name, re.I):
            return True
        return re.match(r"(lib)?Qt6(Quick|Qml|VirtualKeyboard|Pdf)", name) is not None
    return False


def version_resource(version):
    """Windows file properties; FileDescription is also the name Task Manager and "Open with" show."""
    from PyInstaller.utils.win32 import versioninfo as vi

    numbers = tuple(([int(n) for n in re.match(r"\d+(?:\.\d+)*", version).group(0).split(".")] + [0, 0, 0])[:4])
    strings = [
        vi.StringStruct("FileDescription", PRODUCT),
        vi.StringStruct("FileVersion", version),
        vi.StringStruct("InternalName", NAME),
        vi.StringStruct("OriginalFilename", f"{NAME}.exe"),
        vi.StringStruct("ProductName", PRODUCT),
        vi.StringStruct("ProductVersion", version),
    ]
    return vi.VSVersionInfo(
        ffi=vi.FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            vi.StringFileInfo([vi.StringTable("040904B0", strings)]),
            vi.VarFileInfo([vi.VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


VERSION = app_version()
QT_USED = qt_modules_used()
if QT_USED & {"QtQml", "QtQuick", "QtQuickWidgets", "QtPdf", "QtPdfWidgets", "QtVirtualKeyboard"}:
    raise SystemExit("cs2hl now imports a Qt module that unused_file() drops; update it in packaging/cs2hl.spec")

# demoparser2 is a compiled extension; collect_all picks up the binary and its package data.
parser_datas, parser_binaries, parser_imports = collect_all("demoparser2")

# The demoparser2 extension builds a polars frame and converts it with to_pandas (through
# pyarrow) from native code, so static analysis never sees these three imports. psutil is a
# normal import in cs2hl.system and is listed so the dependency is explicit.
HIDDEN_IMPORTS = ["pandas", "polars", "pyarrow", "psutil"]

EXCLUDES = [
    "pytest",
    "_pytest",
    "pytestqt",
    "pluggy",
    "iniconfig",
    "pygments",
    "tkinter",
    "polars.testing",
    "pyarrow.tests",
    # pyarrow.fs imports _s3fs inside try/except ImportError, so leaving it out is safe.
    "pyarrow.flight",
    "pyarrow._flight",
    "pyarrow._s3fs",
] + qt_excludes(QT_USED)

a = Analysis(
    [ENTRY],
    pathex=[ROOT],
    binaries=parser_binaries,
    datas=parser_datas,
    hiddenimports=parser_imports + HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
a.binaries = [entry for entry in a.binaries if not unused_file(entry[0])]
a.datas = [entry for entry in a.datas if not unused_file(entry[0])]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_resource(VERSION) if os.name == "nt" else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=NAME,
)
