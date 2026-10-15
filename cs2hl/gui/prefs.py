"""Remember the desktop UI options between runs. Every QSettings access is guarded."""

from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QSettings, QStandardPaths

from .options import OUTPUT_FOLDER_NAME, GuiOptions, sanitize

ORGANISATION = "cs2hl"
APPLICATION = "cs2hl-gui"
PREFIX = "options/"
ME_KEY = "player/me_steamid"
DEMO_DIR_KEY = "paths/demo_dir"
GEOMETRY_KEY = "window/geometry"


def make_settings() -> QSettings:
    return QSettings(ORGANISATION, APPLICATION)


def default_output_dir() -> str:
    """The user's Videos folder plus "CS2 Highlights"."""
    try:
        videos = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
    except Exception:
        videos = ""
    root = Path(videos) if videos else Path.home() / "Videos"
    return str(root / OUTPUT_FOLDER_NAME)


def load_options(settings: Any) -> GuiOptions:
    """Read the saved options. Anything missing or unreadable falls back to the defaults."""
    defaults = GuiOptions()
    values = {f.name: _read(settings, PREFIX + f.name, getattr(defaults, f.name)) for f in fields(GuiOptions)}
    try:
        opts = sanitize(GuiOptions(**values))
    except Exception:
        opts = defaults
    return opts if opts.output_dir.strip() else replace(opts, output_dir=default_output_dir())


def save_options(settings: Any, opts: GuiOptions) -> None:
    """Write the options. A broken store is ignored so the app keeps running."""
    for f in fields(GuiOptions):
        _write(settings, PREFIX + f.name, _to_setting(getattr(opts, f.name)))
    _sync(settings)


def load_me(settings: Any) -> int | None:
    """The SteamID marked "This is me", or None."""
    raw = _read(settings, ME_KEY, "")
    try:
        steamid = int(str(raw).strip())
    except ValueError:
        return None
    return steamid if steamid > 0 else None


def save_me(settings: Any, steamid: int | None) -> None:
    # Stored as text: a SteamID64 does not survive a round trip through a float.
    _write(settings, ME_KEY, "" if steamid is None else str(steamid))
    _sync(settings)


def load_text(settings: Any, key: str) -> str:
    return _read(settings, key, "")


def save_text(settings: Any, key: str, value: str) -> None:
    _write(settings, key, value)


def load_bytes(settings: Any, key: str) -> QByteArray | None:
    try:
        raw = settings.value(key) if settings.contains(key) else None
    except Exception:
        return None
    return raw if isinstance(raw, QByteArray) and not raw.isEmpty() else None


def save_bytes(settings: Any, key: str, value: QByteArray) -> None:
    _write(settings, key, value)
    _sync(settings)


def _read(settings: Any, key: str, default: Any) -> Any:
    try:
        if not settings.contains(key):
            return default
        return _from_setting(settings.value(key), default)
    except Exception:
        return default


def _write(settings: Any, key: str, value: Any) -> None:
    try:
        settings.setValue(key, value)
    except Exception:
        return


def _sync(settings: Any) -> None:
    try:
        settings.sync()
    except Exception:
        return


def _from_setting(raw: Any, default: Any) -> Any:
    if isinstance(default, bool):
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, tuple):
        text = ",".join(str(item) for item in raw) if isinstance(raw, (list, tuple)) else str(raw)
        return tuple(part.strip() for part in text.split(",") if part.strip())
    if isinstance(default, int):
        text = str(raw).strip()
        try:
            return int(text)
        except ValueError:
            return int(float(text))
    if isinstance(default, float):
        return float(raw)
    return "" if raw is None else str(raw)


def _to_setting(value: Any) -> Any:
    if isinstance(value, tuple):
        return ",".join(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return value
