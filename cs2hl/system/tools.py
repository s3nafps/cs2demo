"""HLAE discovery and the process helpers the recorder uses to follow CS2.

HLAE.exe exits right after it injects its hook (hlae/Loader.cs, Program.cs), so CS2 is
followed by process name, as CS Demo Manager does.
"""

from __future__ import annotations

import codecs
import configparser
import functools
import os
import sys
import tempfile
import time
from pathlib import Path
from threading import Event
from typing import Any, Callable, Literal, Mapping

import psutil

HLAE_EXE_NAMES = ("HLAE.exe", "hlae.exe")
HOOK_DLL = Path("x64") / "AfxHookSource2.dll"
INJECTOR = Path("x64") / "injector.exe"
# AfxHookSource2 reports hook failures in a MessageBox with this title (AfxHookSource2/Globals.cpp).
HOOK_ERROR_TITLE = "Error - AfxHookSource"

# GetPrivateProfileStringW reads a file without a UTF-16 BOM in the ANSI code page.
ANSI_CODEC = "mbcs" if sys.platform == "win32" else "cp1252"

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
    """Read one value like GetPrivateProfileStringW: section and key ignore case, quotes are stripped."""
    try:
        text = decode_ini(path.read_bytes())
    except OSError:
        return ""
    ini = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        ini.read_string(text)
    except configparser.Error:
        return ""
    name = next((s for s in ini.sections() if s.lower() == section.lower()), None)
    if name is None:
        return ""
    return ini.get(name, key, fallback="").strip().strip('"').strip()


def decode_ini(raw: bytes) -> str:
    """Text of an ini file as Windows reads it: UTF-16 or UTF-8 with a BOM, else the ANSI code page.

    A UTF-8 file without a BOM (what older cs2hl and CS Demo Manager write) therefore reads
    a non-ASCII path the way HLAE does: garbled, so it is not taken as a working FFmpeg.
    """
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(codecs.BOM_UTF8):
        return raw[len(codecs.BOM_UTF8):].decode("utf-8", errors="replace")
    return raw.decode(ANSI_CODEC, errors="replace")


def hlae_has_ffmpeg(hlae_exe: Path) -> bool:
    return hlae_ffmpeg_path(hlae_exe) is not None


def register_hlae_ffmpeg(hlae_exe: Path, ffmpeg_exe: Path) -> Path:
    """Point HLAE at ``ffmpeg_exe`` by writing ffmpeg\\ffmpeg.ini, as CS Demo Manager does. Returns the ini.

    Written as UTF-16 LE with a BOM: the only encoding in which GetPrivateProfileStringW
    reads a non-ASCII path (a user folder like C:\\Users\\José) correctly.
    """
    ini = hlae_ffmpeg_ini(hlae_exe)
    ini.parent.mkdir(parents=True, exist_ok=True)
    ini.write_bytes(codecs.BOM_UTF16_LE + f"[Ffmpeg]\r\nPath={ffmpeg_exe}\r\n".encode("utf-16-le"))
    return ini


def can_register_hlae_ffmpeg(hlae_exe: Path) -> bool:
    """True when ffmpeg\\ffmpeg.ini next to HLAE can be written (the HLAE installer uses Program Files).

    Probes with a temporary file and removes whatever it created.
    """
    ini = hlae_ffmpeg_ini(hlae_exe)
    folder = ini.parent
    created = False
    try:
        if not folder.is_dir():
            folder.mkdir()
            created = True
        if ini.exists():
            with open(ini, "ab"):
                pass
        else:
            with tempfile.TemporaryFile(dir=folder):
                pass
        return True
    except OSError:
        return False
    finally:
        if created:
            try:
                folder.rmdir()
            except OSError:
                pass


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
    """True when a visible top-level window title starts with ``prefix``. Always False off Windows.

    Written from the Win32 docs; not run on Windows yet.
    """
    if sys.platform != "win32":
        return False
    user32, proc_type = _user32()
    return any_window_title(user32, proc_type, prefix)


def any_window_title(user32: Any, proc_type: Callable[[Callable[[Any, Any], bool]], Any], prefix: str) -> bool:
    """Walk every top-level window with ``user32.EnumWindows`` and look for a visible title."""
    found: list[bool] = []

    def visit(hwnd: Any, _lparam: Any) -> bool:
        try:
            title = _visible_title(user32, hwnd)
            if title is not None and title.startswith(prefix):
                found.append(True)
        except Exception:
            pass
        return True  # False, or an exception escaping into ctypes, would end EnumWindows early

    callback = proc_type(visit)
    user32.EnumWindows(callback, 0)
    return bool(found)


def _visible_title(user32: Any, hwnd: Any) -> str | None:
    import ctypes

    if not user32.IsWindowVisible(hwnd):
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return None
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


@functools.cache
def _user32() -> tuple[Any, Any]:
    """user32 with argtypes and restype declared, so 64-bit handles are not cut to C ints."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)  # its own copy, not the shared ctypes.windll one
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    declare_user32(user32, proc_type)
    return user32, proc_type


def declare_user32(user32: Any, proc_type: Any) -> None:
    """Signatures of EnumWindows, IsWindowVisible, GetWindowTextLengthW and GetWindowTextW."""
    import ctypes
    from ctypes import wintypes

    user32.EnumWindows.argtypes = (proc_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int
