"""Checks to run before recording, so a run fails up front with a clear message.

Every check reports instead of raising, so the list also renders on Linux (all recording
checks then come back not-ok).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .. import ffmpeg
from . import tools
from .steam import cs2_exe_path

STEAM_PROCESSES = ("steam.exe", "steamchina.exe")
CS2_PROCESS = "cs2.exe"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def preflight(cs2_dir: Path | None, hlae_exe: Path | None) -> list[Check]:
    """Windows, Steam, CS2, HLAE, FFmpeg and FFprobe checks, in the order a user should fix them."""
    return [
        _windows(),
        _steam_running(),
        _cs2_found(cs2_dir),
        _cs2_closed(),
        _hlae_found(hlae_exe),
        _hlae_ffmpeg(hlae_exe),
        _app_ffmpeg(),
        _app_ffprobe(),
    ]


def all_ok(checks: list[Check]) -> bool:
    return all(c.ok for c in checks)


def problems(checks: list[Check]) -> list[Check]:
    return [c for c in checks if not c.ok]


def is_windows() -> bool:
    return sys.platform == "win32"


def _windows() -> Check:
    if is_windows():
        return Check("Windows", True, "Recording runs on this PC.")
    return Check("Windows", False, f"Recording needs Windows; HLAE does not run on {sys.platform}.")


def _steam_running() -> Check:
    if any(tools.is_process_running(name) for name in STEAM_PROCESSES):
        return Check("Steam running", True, "Steam is running.")
    return Check("Steam running", False, "Start Steam and log in. CS2 is started directly and needs the Steam client.")


def _cs2_found(cs2_dir: Path | None) -> Check:
    if cs2_dir is None:
        return Check("CS2 installed", False, "CS2 was not found in any Steam library. Choose its folder in the Setup tab.")
    exe = cs2_exe_path(cs2_dir)
    if exe.is_file():
        return Check("CS2 installed", True, str(cs2_dir))
    return Check("CS2 installed", False, f"{exe} is missing. Choose the 'Counter-Strike Global Offensive' folder.")


def _cs2_closed() -> Check:
    if tools.is_process_running(CS2_PROCESS):
        return Check("CS2 closed", False, "Close CS2 first. Recording starts its own CS2 with -insecure and HLAE.")
    return Check("CS2 closed", True, "CS2 is not running.")


def _hlae_found(hlae_exe: Path | None) -> Check:
    if hlae_exe is None:
        return Check("HLAE installed", False, "HLAE was not found. Install HLAE or choose HLAE.exe in the Setup tab.")
    if not hlae_exe.is_file():
        return Check("HLAE installed", False, f"{hlae_exe} does not exist.")
    missing = [p for p in (tools.HOOK_DLL, tools.INJECTOR) if not (hlae_exe.parent / p).is_file()]
    if missing:
        names = ", ".join(str(p) for p in missing)
        return Check("HLAE installed", False, f"{names} missing next to {hlae_exe.name}. Reinstall HLAE.")
    return Check("HLAE installed", True, str(hlae_exe))


def _hlae_ffmpeg(hlae_exe: Path | None) -> Check:
    if hlae_exe is None or not hlae_exe.is_file():
        return Check("HLAE FFmpeg", False, "Needs HLAE first.")
    own = tools.hlae_ffmpeg_path(hlae_exe)
    if own is not None:
        return Check("HLAE FFmpeg", True, str(own))
    folder = hlae_exe.parent / "ffmpeg" / "bin"
    app = ffmpeg.find_tool("ffmpeg")
    if app is None:
        return Check("HLAE FFmpeg", False, f"HLAE records through FFmpeg. Put ffmpeg.exe in {folder}.")
    if not tools.can_register_hlae_ffmpeg(hlae_exe):
        return Check(
            "HLAE FFmpeg",
            False,
            f"HLAE has no FFmpeg and cs2hl cannot write to {folder.parent} (a protected folder such as "
            f"Program Files). Copy ffmpeg.exe into {folder} as administrator, or install HLAE in a folder "
            "you own and choose it in the Setup tab.",
        )
    return Check("HLAE FFmpeg", True, f"HLAE has none; cs2hl will point it at {app} when recording.")


def _app_ffmpeg() -> Check:
    found = ffmpeg.find_tool("ffmpeg")
    if found is not None:
        return Check("FFmpeg", True, found)
    return Check("FFmpeg", False, "ffmpeg.exe was not found next to the app or on PATH.")


def _app_ffprobe() -> Check:
    found = ffmpeg.find_tool("ffprobe")
    if found is not None:
        return Check("FFprobe", True, found)
    return Check("FFprobe", False, "ffprobe.exe was not found next to the app or on PATH. Export needs it; reinstall the app.")
