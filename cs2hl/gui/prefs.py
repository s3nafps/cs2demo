"""Remember the desktop UI options between runs. Every QSettings access is guarded."""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from PySide6.QtCore import QSettings

from .options import GuiOptions, sanitize

ORGANISATION = "cs2hl"
APPLICATION = "cs2hl-gui"
PREFIX = "options/"


def make_settings() -> QSettings:
    return QSettings(ORGANISATION, APPLICATION)


def load_options(settings: Any) -> GuiOptions:
    """Read the saved options. Anything missing or unreadable falls back to the defaults."""
    defaults = GuiOptions()
    values = {f.name: _read(settings, PREFIX + f.name, getattr(defaults, f.name)) for f in fields(GuiOptions)}
    return sanitize(GuiOptions(**values))


def save_options(settings: Any, opts: GuiOptions) -> None:
    """Write the options. A broken store is ignored so the app keeps running."""
    try:
        for f in fields(GuiOptions):
            settings.setValue(PREFIX + f.name, _to_setting(getattr(opts, f.name)))
        settings.sync()
    except Exception:
        return


def _read(settings: Any, key: str, default: Any) -> Any:
    try:
        if not settings.contains(key):
            return default
        return _from_setting(settings.value(key), default)
    except Exception:
        return default


def _from_setting(raw: Any, default: Any) -> Any:
    if isinstance(default, bool):
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, tuple):
        text = ",".join(str(item) for item in raw) if isinstance(raw, (list, tuple)) else str(raw)
        return tuple(part.strip() for part in text.split(",") if part.strip())
    if isinstance(default, int):
        return int(float(raw))
    if isinstance(default, float):
        return float(raw)
    return "" if raw is None else str(raw)


def _to_setting(value: Any) -> Any:
    if isinstance(value, tuple):
        return ",".join(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return value
