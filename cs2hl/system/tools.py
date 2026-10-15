"""HLAE discovery and the process helpers the recorder uses to follow CS2.

HLAE.exe exits right after it injects its hook (hlae/Loader.cs, Program.cs), so CS2 is
followed by process name, as CS Demo Manager does.
"""

from __future__ import annotations

import configparser
import os
import sys
import time
from pathlib import Path
from threading import Event
from typing import Callable, Literal, Mapping

import psutil

HLAE_EXE_NAMES = ("HLAE.exe", "hlae.exe")
HOOK_DLL = Path("x64") / "AfxHookSource2.dll"
INJECTOR = Path("x64") / "injector.exe"
# AfxHookSource2 reports hook failures in a MessageBox with this title (AfxHookSource2/Globals.cpp).
HOOK_ERROR_TITLE = "Error - AfxHookSource"

WaitOutcome = Literal["done", "timeout", "cancelled", "aborted"]


def hook_dll_path(hlae_exe: Path) -> Path:
    return hlae_exe.parent / HOOK_DLL


def is_hlae_exe(path: Path) -> bool:
    """True for an HLAE.exe with the CS2 hook DLL next to it."""
    return path.is_file() and hook_dll_path(path).is_file()


def hlae_search_dirs(env: Mapping[str, str] | None = None, app_dir: Path | None = None) -> list[Path]:
    """Folders HLAE is usually found in: the MSI's Program Files folder, CS Demo Manager's copy, zip spots."""
    env = os.environ if env is None else env
    dirs: list[Path] = []
    if app_dir is not None:
        dirs.append(app_dir / "HLAE")
    for var, sub in (
        ("LOCALAPPDATA", ("cs2hl", "HLAE")),
        ("ProgramFiles(x86)", ("HLAE",)),
        ("ProgramFiles", ("HLAE",)),
        ("USERPROFILE", (".csdm", "hlae")),
        ("USERPROFILE", ("HLAE",)),
        ("USERPROFILE", ("Desktop", "HLAE")),
        ("USERPROFILE", ("Downloads", "HLAE")),
        ("SystemDrive", ("HLAE",)),
    ):
        base = env.get(var)
        if base:
            root = Path(base.rstrip("\\/") + os.sep) if var == "SystemDrive" else Path(base)  # "C:" -> "C:\"
            dirs.append(root.joinpath(*sub))
    return dirs


def find_hlae(hint: Path | str | None = None, env: Mapping[str, str] | None = None) -> Path | None:
    """HLAE.exe from ``hint`` (the exe or its folder) or the usual install folders."""
    candidates: list[Path] = []
    if hint:
        hint = Path(hint)
        candidates += [hint] if hint.suffix.lower() == ".exe" else [hint / name for name in HLAE_EXE_NAMES]
    app_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else None
    for folder in hlae_search_dirs(env, app_dir):
        candidates += [folder / name for name in HLAE_EXE_NAMES]
    return next((c for c in candidates if is_hlae_exe(c)), None)


def hlae_ffmpeg_ini(hlae_exe: Path) -> Path:
    return hlae_exe.parent / "ffmpeg" / "ffmpeg.ini"


def hlae_ffmpeg_path(hlae_exe: Path) -> Path | None:
    """The ffmpeg.exe HLAE will run: ``[Ffmpeg] Path`` in ffmpeg\\ffmpeg.ini, else ffmpeg\\bin\\ffmpeg.exe.

    Same lookup as shared/AfxOutStreams.cpp. None when that file does not exist.
    """
    configured = _ini_value(hlae_ffmpeg_ini(hlae_exe), "Ffmpeg", "Path")
    path = Path(configured) if configured else hlae_exe.parent / "ffmpeg" / "bin" / "ffmpeg.exe"
    return path if path.is_file() else None


def _ini_value(path: Path, section: str, key: str) -> str:
    """Read one value like GetPrivateProfileString: section and key ignore case, quotes are stripped."""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")  # WritePrivateProfileStringA writes the ANSI code page
    ini = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        ini.read_string(text)
    except configparser.Error:
        return ""
    name = next((s for s in ini.sections() if s.lower() == section.lower()), None)
    if name is None:
        return ""
    return ini.get(name, key, fallback="").strip().strip('"').strip()


def hlae_has_ffmpeg(hlae_exe: Path) -> bool:
    return hlae_ffmpeg_path(hlae_exe) is not None


def register_hlae_ffmpeg(hlae_exe: Path, ffmpeg_exe: Path) -> Path:
    """Point HLAE at ``ffmpeg_exe`` by writing ffmpeg\\ffmpeg.ini, as CS Demo Manager does. Returns the ini."""
    ini = hlae_ffmpeg_ini(hlae_exe)
    ini.parent.mkdir(parents=True, exist_ok=True)
    ini.write_text(f"[Ffmpeg]\nPath={ffmpeg_exe}\n", encoding="utf-8")
    return ini


def find_processes(name: str) -> list[psutil.Process]:
    """Running processes whose image name matches ``name`` (case-insensitive)."""
    wanted = name.lower()
    found = []
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").lower() == wanted:
                found.append(proc)
        except (psutil.Error, AttributeError):
            continue
    return found


def is_process_running(name: str) -> bool:
    return bool(find_processes(name))


def kill_process(name: str, timeout_s: float = 10.0) -> int:
    """Kill every process named ``name`` and wait for them to go. Returns how many were signalled."""
    procs = find_processes(name)
    for proc in procs:
        try:
            proc.kill()
        except psutil.Error:
            pass
    if procs:
        psutil.wait_procs(procs, timeout=timeout_s)
    return len(procs)


def wait_until(
    done: Callable[[], bool],
    cancel: Event,
    timeout_s: float,
    poll_s: float,
    abort: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> WaitOutcome:
    """Poll ``done`` until it is true. ``abort`` lets the caller stop early for its own reason."""
    deadline = clock() + timeout_s
    while True:
        if done():
            return "done"
        if cancel.is_set():
            return "cancelled"
        if abort is not None and abort():
            return "aborted"
        if clock() >= deadline:
            return "timeout"
        if cancel.wait(poll_s):
            return "cancelled"


def wait_for_start(
    name: str, cancel: Event, timeout_s: float, poll_s: float, abort: Callable[[], bool] | None = None
) -> WaitOutcome:
    return wait_until(lambda: is_process_running(name), cancel, timeout_s, poll_s, abort)


def wait_for_exit(
    name: str, cancel: Event, timeout_s: float, poll_s: float, abort: Callable[[], bool] | None = None
) -> WaitOutcome:
    return wait_until(lambda: not is_process_running(name), cancel, timeout_s, poll_s, abort)


def window_title_exists(prefix: str) -> bool:
    """True when a visible top-level window title starts with ``prefix``. Always False off Windows."""
    if sys.platform != "win32":
        return False
    # Win32 EnumWindows via ctypes; written from the API docs and not run yet.
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = False

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        nonlocal found
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value.startswith(prefix):
            found = True
            return False
        return True

    user32.EnumWindows(visit, 0)
    return found
