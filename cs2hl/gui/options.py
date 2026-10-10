"""Options the desktop UI collects, and how they map onto the pipeline.

Nothing here imports Qt, so the mapping can be tested without a display.
"""

from __future__ import annotations

import math
import shlex
from dataclasses import dataclass
from pathlib import Path

from ..capture import CaptureSettings
from ..ffmpeg import Aspect, EncodeSettings, VerticalMode
from ..pipeline import HighlightOptions, RenderOptions

ASPECTS: tuple[Aspect, ...] = ("16x9", "9x16")
VERTICAL_MODES: tuple[VerticalMode, ...] = ("crop", "fit")
SHORT_SIDES = (1080, 1440, 2160)
BACKEND_PLAN = "plan"
BACKEND_HLAE = "hlae"
BACKENDS = (BACKEND_PLAN, BACKEND_HLAE)
CAPTURE_SIZES = ((3840, 2160), (2560, 1440), (1920, 1080))


@dataclass(frozen=True)
class HlaeConfig:
    cs2_dir: Path | None
    hlae_exe: Path | None
    launch_command: tuple[str, ...]


@dataclass(frozen=True)
class GuiOptions:
    output_dir: str = ""
    aspects: tuple[Aspect, ...] = ASPECTS
    vertical_mode: VerticalMode = "crop"
    x_offset: float = 0.0
    short_side: int = 1080
    min_score: float = 5.0
    pre_roll_s: float = 4.0
    post_roll_s: float = 2.0
    max_clips: int = 0  # 0 means no limit
    backend: str = BACKEND_PLAN
    capture_width: int = 3840
    capture_height: int = 2160
    cs2_dir: str = ""
    hlae_exe: str = ""
    launch_command: str = ""


def highlight_options(opts: GuiOptions) -> HighlightOptions:
    return HighlightOptions(
        pre_roll_s=opts.pre_roll_s,
        post_roll_s=opts.post_roll_s,
        min_score=opts.min_score,
        max_clips=opts.max_clips or None,
    )


def render_options(opts: GuiOptions) -> RenderOptions:
    return RenderOptions(
        aspects=opts.aspects,
        vertical_mode=opts.vertical_mode,
        x_offset=opts.x_offset,
        encode=EncodeSettings(short_side=opts.short_side),
        capture=CaptureSettings(width=opts.capture_width, height=opts.capture_height),
    )


def hlae_config(opts: GuiOptions) -> HlaeConfig:
    """Only used for HLAE runs, which are checked by ``render_problem`` first."""
    return HlaeConfig(
        cs2_dir=_path_or_none(opts.cs2_dir),
        hlae_exe=_path_or_none(opts.hlae_exe),
        launch_command=_split_or_empty(opts.launch_command),
    )


def split_command(text: str) -> tuple[str, ...]:
    """Split a one-line command like shlex does, but keep backslashes so Windows paths survive."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    return tuple(lexer)


def render_problem(opts: GuiOptions) -> str | None:
    """Return a message for the first setting that would stop a render, or None if all is well."""
    if not opts.aspects:
        return "Pick at least one aspect: 16:9 or 9:16."
    if not opts.output_dir.strip():
        return "Choose an output folder."
    if opts.backend != BACKEND_HLAE:
        return None
    if not opts.cs2_dir.strip():
        return "Choose the CS2 folder for HLAE."
    if not (Path(opts.cs2_dir) / "game" / "csgo").is_dir():
        return f"Not a CS2 folder (no game\\csgo inside): {opts.cs2_dir}"
    if opts.hlae_exe.strip() and not Path(opts.hlae_exe).is_file():
        return f"HLAE executable not found: {opts.hlae_exe}"
    try:
        split_command(opts.launch_command)
    except ValueError as exc:
        return f"Launch command could not be parsed: {exc}"
    return None


def sanitize(opts: GuiOptions) -> GuiOptions:
    """Replace unknown or out-of-range values, for example from an old settings file."""
    d = GuiOptions()
    size = (opts.capture_width, opts.capture_height)
    width, height = size if size in CAPTURE_SIZES else (d.capture_width, d.capture_height)
    return GuiOptions(
        output_dir=opts.output_dir,
        aspects=tuple(a for a in opts.aspects if a in ASPECTS),
        vertical_mode=opts.vertical_mode if opts.vertical_mode in VERTICAL_MODES else d.vertical_mode,
        x_offset=_clamp(opts.x_offset, -1.0, 1.0, d.x_offset),
        short_side=opts.short_side if opts.short_side in SHORT_SIDES else d.short_side,
        min_score=_clamp(opts.min_score, 0.0, 100.0, d.min_score),
        pre_roll_s=_clamp(opts.pre_roll_s, 0.0, 60.0, d.pre_roll_s),
        post_roll_s=_clamp(opts.post_roll_s, 0.0, 60.0, d.post_roll_s),
        max_clips=int(_clamp(opts.max_clips, 0, 10000, d.max_clips)),
        backend=opts.backend if opts.backend in BACKENDS else d.backend,
        capture_width=width,
        capture_height=height,
        cs2_dir=opts.cs2_dir,
        hlae_exe=opts.hlae_exe,
        launch_command=opts.launch_command,
    )


def _clamp(value: float, low: float, high: float, fallback: float) -> float:
    if not math.isfinite(value):
        return fallback
    return min(max(value, low), high)


def _path_or_none(text: str) -> Path | None:
    text = text.strip()
    return Path(text) if text else None


def _split_or_empty(text: str) -> tuple[str, ...]:
    try:
        return split_command(text)
    except ValueError:
        return ()
