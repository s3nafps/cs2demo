"""Options the desktop UI collects, and how they map onto the pipeline.

Nothing here imports Qt, so the mapping can be tested without a display. Defaults come
from the pipeline's own option classes so the two cannot drift apart.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import get_args

from ..models import OutputMode
from ..pipeline import HighlightOptions, RenderOptions

OUTPUT_MODES: tuple[str, ...] = get_args(OutputMode)
OUTPUT_LABELS = {"montage": "One video", "clips": "Separate clips", "both": "Both"}
OUTPUT_HINTS = {
    "montage": "Every highlight joined into one video.",
    "clips": "One video file per highlight.",
    "both": "The joined video and one file per highlight.",
}
ASPECTS: tuple[str, ...] = ("16x9", "9x16")
ASPECT_LABELS = {"16x9": "16:9 landscape", "9x16": "9:16 vertical"}
VERTICAL_MODES: tuple[str, ...] = ("crop", "fit")
VERTICAL_LABELS = {"crop": "Crop to fill", "fit": "Fit with bars"}
SHORT_SIDES: tuple[int, ...] = (1080, 1440, 2160)
CAPTURE_SIZES: tuple[tuple[int, int], ...] = ((1920, 1080), (2560, 1440), (3840, 2160))
CRF_RANGE = (10, 35)
MIN_KILLS_RANGE = (1, 5)
ROLL_RANGE_S = (0.0, 30.0)
OUTPUT_FOLDER_NAME = "CS2 Highlights"

_HIGHLIGHT = HighlightOptions()
_RENDER = RenderOptions()


@dataclass(frozen=True)
class GuiOptions:
    output_dir: str = ""
    output: str = _RENDER.output
    aspects: tuple[str, ...] = ("16x9",)  # one landscape video unless the user ticks 9:16 too
    vertical_mode: str = _RENDER.vertical_mode
    x_offset: float = _RENDER.x_offset
    short_side: int = _RENDER.encode.short_side
    crf: int = _RENDER.encode.crf
    capture_width: int = _RENDER.capture.width
    capture_height: int = _RENDER.capture.height
    min_kills: int = _HIGHLIGHT.min_kills
    include_clutches: bool = _HIGHLIGHT.include_clutches
    pre_roll_s: float = _HIGHLIGHT.pre_roll_s
    post_roll_s: float = _HIGHLIGHT.post_roll_s
    cs2_dir: str = ""  # empty: found automatically
    hlae_exe: str = ""  # empty: found automatically


def highlight_options(opts: GuiOptions) -> HighlightOptions:
    return replace(
        _HIGHLIGHT,
        min_kills=opts.min_kills,
        include_clutches=opts.include_clutches,
        pre_roll_s=opts.pre_roll_s,
        post_roll_s=opts.post_roll_s,
    )


def render_options(opts: GuiOptions) -> RenderOptions:
    return replace(
        _RENDER,
        output=opts.output,
        aspects=opts.aspects,
        vertical_mode=opts.vertical_mode,
        x_offset=opts.x_offset,
        encode=replace(_RENDER.encode, short_side=opts.short_side, crf=opts.crf),
        capture=replace(_RENDER.capture, width=opts.capture_width, height=opts.capture_height),
    )


def sanitize(opts: GuiOptions) -> GuiOptions:
    """Replace unknown or out-of-range values, for example from an old settings file."""
    d = GuiOptions()
    size = (opts.capture_width, opts.capture_height)
    width, height = size if size in CAPTURE_SIZES else (d.capture_width, d.capture_height)
    aspects = tuple(a for a in ASPECTS if a in opts.aspects)
    return replace(
        opts,
        output=opts.output if opts.output in OUTPUT_MODES else d.output,
        aspects=aspects or d.aspects,
        vertical_mode=opts.vertical_mode if opts.vertical_mode in VERTICAL_MODES else d.vertical_mode,
        x_offset=_clamp(opts.x_offset, -1.0, 1.0, d.x_offset),
        short_side=opts.short_side if opts.short_side in SHORT_SIDES else d.short_side,
        crf=int(_clamp(opts.crf, *CRF_RANGE, d.crf)),
        capture_width=width,
        capture_height=height,
        min_kills=int(_clamp(opts.min_kills, *MIN_KILLS_RANGE, d.min_kills)),
        pre_roll_s=_clamp(opts.pre_roll_s, *ROLL_RANGE_S, d.pre_roll_s),
        post_roll_s=_clamp(opts.post_roll_s, *ROLL_RANGE_S, d.post_roll_s),
    )


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def safe_name(text: str, fallback: str = "highlights") -> str:
    """A folder name Windows accepts: no reserved characters, no trailing dot or space."""
    cleaned = _UNSAFE.sub("_", text).strip()[:120].rstrip(". ")
    return cleaned or fallback


def run_folder(root: Path, demo_name: str, player_name: str) -> Path:
    """One folder per demo and player, so two players of the same demo never overwrite each other."""
    stem = Path(demo_name).stem or "demo"
    return root / safe_name(f"{stem} - {player_name}")


def _clamp(value: float, low: float, high: float, fallback: float) -> float:
    if not math.isfinite(value):
        return fallback
    return min(max(value, low), high)
