"""Find Steam and the CS2 install without asking the user.

Order follows HLAE (hlae/Program.cs) and CS Demo Manager (get-steam-folder-path.ts,
get-csgo-folder-path.ts): the SteamPath registry value, Steam's InstallPath, then the
default folder; CS2 is the library whose ``libraryfolders.vdf`` entry lists app 730.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable

CS2_APP_ID = "730"
CS2_DEFAULT_INSTALLDIR = "Counter-Strike Global Offensive"
DEFAULT_STEAM_DIR = r"C:\Program Files (x86)\Steam"

# (hive, key, value). HKCU SteamPath is what the running client writes; it uses forward slashes.
STEAM_REGISTRY_VALUES = (
    ("HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath"),
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
)

RegistryReader = Callable[[str, str, str], str | None]


def read_registry(hive: str, key: str, value: str) -> str | None:
    """One string value from the Windows registry, or None (always None off Windows)."""
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(getattr(winreg, hive), key) as handle:
            data, _kind = winreg.QueryValueEx(handle, value)
    except (OSError, AttributeError):
        return None
    return data if isinstance(data, str) and data.strip() else None


def steam_root_candidates(read: RegistryReader = read_registry, fallback: str = DEFAULT_STEAM_DIR) -> list[Path]:
    """Places Steam may be installed, most trusted first, without duplicates."""
    raw = [read(hive, key, value) for hive, key, value in STEAM_REGISTRY_VALUES]
    raw.append(fallback)
    out: list[Path] = []
    seen: set[str] = set()
    for text in raw:
        if not text:
            continue
        path = Path(text.strip())
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def steam_root(read: RegistryReader = read_registry, fallback: str = DEFAULT_STEAM_DIR) -> Path | None:
    """The first candidate Steam folder that exists."""
    return next((p for p in steam_root_candidates(read, fallback) if p.is_dir()), None)


_TOKEN = re.compile(r'\s+|//[^\n]*|"((?:[^"\\]|\\.)*)"|([{}])|([^\s{}"]+)')
_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t"}


def parse_vdf(text: str) -> dict[str, Any]:
    """Parse Valve KeyValues text (libraryfolders.vdf, appmanifest_*.acf) into nested dicts.

    Handles quoted and bare tokens, ``//`` comments, ``\\`` escapes and ``[$WIN32]``
    conditionals (ignored). A repeated key keeps the last value. Raises ValueError when
    the braces do not match.
    """
    root: dict[str, Any] = {}
    stack = [root]
    key: str | None = None
    pos = 0
    if text.startswith("\ufeff"):
        pos = 1
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None:
            raise ValueError(f"unterminated string at offset {pos}")
        pos = m.end()
        quoted, brace, bare = m.groups()
        if brace == "{":
            if key is None:
                raise ValueError(f"'{{' without a key at offset {m.start()}")
            child: dict[str, Any] = {}
            stack[-1][key] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) == 1 or key is not None:
                raise ValueError(f"unexpected '}}' at offset {m.start()}")
            stack.pop()
        elif quoted is not None or bare is not None:
            if bare is not None and bare.startswith("[") and bare.endswith("]"):
                continue
            token = _unescape(quoted) if quoted is not None else bare
            if key is None:
                key = token
            else:
                stack[-1][key] = token
                key = None
    if len(stack) != 1 or key is not None:
        raise ValueError("unexpected end of file")
    return root


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), "\\" + m.group(1)), text)


def get_ci(data: dict[str, Any], key: str) -> Any:
    """Case-insensitive lookup: Steam writes both ``libraryfolders`` and ``LibraryFolders``."""
    if key in data:
        return data[key]
    lowered = key.lower()
    return next((v for k, v in data.items() if k.lower() == lowered), None)


def libraries(steam_dir: Path) -> list[tuple[Path, frozenset[str] | None]]:
    """Steam library folders with the app ids each one lists (None when the file has no list).

    The Steam folder itself always comes first. Both the current format (numbered blocks
    with ``path`` and ``apps``) and the old one (numbered keys holding a path) are read.
    """
    found: dict[str, tuple[Path, frozenset[str] | None]] = {}

    def add(path: Path, apps: frozenset[str] | None) -> None:
        key = os.path.normcase(os.path.normpath(str(path)))
        old = found.get(key)
        found[key] = (old[0] if old else path, apps if apps is not None else (old[1] if old else None))

    add(steam_dir, None)
    try:
        text = (steam_dir / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        folders = get_ci(parse_vdf(text), "libraryfolders")
    except (OSError, ValueError):
        folders = None
    if isinstance(folders, dict):
        for number, entry in folders.items():
            if not number.isdigit():
                continue
            if isinstance(entry, str):
                add(Path(entry), None)
            elif isinstance(entry, dict) and isinstance(get_ci(entry, "path"), str):
                apps = get_ci(entry, "apps")
                add(Path(get_ci(entry, "path")), frozenset(apps) if isinstance(apps, dict) else None)
    return list(found.values())


def library_paths(steam_dir: Path) -> list[Path]:
    return [path for path, _apps in libraries(steam_dir)]


def cs2_exe_path(cs2_dir: Path) -> Path:
    return cs2_dir / "game" / "bin" / "win64" / "cs2.exe"


def cs2_installdir(library: Path) -> str | None:
    """``installdir`` from the library's appmanifest_730.acf, if the file is there and readable."""
    try:
        text = (library / "steamapps" / f"appmanifest_{CS2_APP_ID}.acf").read_text(encoding="utf-8", errors="replace")
        state = get_ci(parse_vdf(text), "AppState")
    except (OSError, ValueError):
        return None
    value = get_ci(state, "installdir") if isinstance(state, dict) else None
    return value if isinstance(value, str) and value else None


def find_cs2_dir(steam_dir: Path | None = None) -> Path | None:
    """The "Counter-Strike Global Offensive" folder that holds game\\bin\\win64\\cs2.exe.

    Libraries that list app 730 are tried first, then the others, because the list in
    libraryfolders.vdf can be stale.
    """
    root = steam_dir if steam_dir is not None else steam_root()
    if root is None:
        return None
    libs = libraries(root)
    listed = [path for path, apps in libs if apps is not None and CS2_APP_ID in apps]
    others = [path for path, apps in libs if apps is None or CS2_APP_ID not in apps]
    for library in listed + others:
        names = [cs2_installdir(library), CS2_DEFAULT_INSTALLDIR]
        for name in dict.fromkeys(n for n in names if n):
            game = library / "steamapps" / "common" / name
            if cs2_exe_path(game).is_file():
                return game
    return None
