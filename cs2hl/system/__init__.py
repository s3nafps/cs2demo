"""Steam, CS2 and HLAE discovery, process helpers and pre-flight checks for recording.

The checks themselves: ``from cs2hl.system.preflight import preflight``.
"""

from .preflight import Check, all_ok, problems
from .steam import cs2_exe_path, find_cs2_dir, library_paths, parse_vdf, steam_root
from .tools import (
    find_hlae,
    hlae_ffmpeg_path,
    hlae_has_ffmpeg,
    is_process_running,
    kill_process,
    register_hlae_ffmpeg,
    wait_for_exit,
    wait_for_start,
)

__all__ = [
    "Check",
    "all_ok",
    "cs2_exe_path",
    "find_cs2_dir",
    "find_hlae",
    "hlae_ffmpeg_path",
    "hlae_has_ffmpeg",
    "is_process_running",
    "kill_process",
    "library_paths",
    "parse_vdf",
    "problems",
    "register_hlae_ffmpeg",
    "steam_root",
    "wait_for_exit",
    "wait_for_start",
]
