"""Shared steps for the CLI and the desktop UI: highlights -> selection -> plan."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import Callable

from .capture import CaptureBackend, CaptureCancelled, CaptureResult, CaptureSettings
from .detect import DetectConfig, detect_highlights
from .export import ExportResult, export_plan, segment_files
from .ffmpeg import Aspect, EncodeSettings, VerticalMode
from .models import DemoData, OutputMode, Plan
from .planner import PlanConfig, build_plan
from .selection import Selection, filter_highlights


@dataclass(frozen=True)
class HighlightOptions:
    min_kills: int = 2  # a round needs this many kills by the player to count
    include_clutches: bool = True  # a won clutch counts even below min_kills
    pre_roll_s: float = 3.0  # kept before each kill
    post_roll_s: float = 2.0  # kept after each kill
    merge_gap_s: float = 3.0  # kill windows closer than this become one piece
    min_score: float = 0.0  # 0 keeps every qualifying round
    max_clips: int | None = None


def plan_highlights(data: DemoData, selection: Selection, opts: HighlightOptions | None = None) -> Plan:
    """Detect highlights, keep the selected players and rounds, and build the clip plan."""
    opts = opts or HighlightOptions()
    highlights = detect_highlights(
        data.rounds,
        data.kills,
        data.rosters,
        DetectConfig(
            tickrate=data.meta.tickrate,
            min_kills=opts.min_kills,
            include_clutches=opts.include_clutches,
        ),
    )
    highlights = filter_highlights(highlights, selection)
    return build_plan(
        highlights,
        data.rounds,
        data.meta,
        PlanConfig(
            pre_roll_s=opts.pre_roll_s,
            post_roll_s=opts.post_roll_s,
            merge_gap_s=opts.merge_gap_s,
            min_score=opts.min_score,
            max_clips=opts.max_clips,
        ),
        player_slots=data.player_slots,
    )


@dataclass(frozen=True)
class RenderOptions:
    output: OutputMode = "montage"
    aspects: tuple[Aspect, ...] = ("16x9", "9x16")
    vertical_mode: VerticalMode = "crop"
    x_offset: float = 0.0
    encode: EncodeSettings = EncodeSettings()
    capture: CaptureSettings = CaptureSettings()
    keep_recordings: bool = False  # raw recordings are large; delete them once every export worked


def render_plan(
    plan: Plan,
    demo_path: Path,
    backend: CaptureBackend,
    out_dir: Path,
    opts: RenderOptions,
    log: Callable[[str], None],
    cancel: Event,
) -> tuple[list[CaptureResult], list[ExportResult]]:
    """Record every clip with ``backend`` into ``out_dir/recordings``, then export into ``out_dir``.

    If the cancel event stops the recording, the clips already on disk are still exported.
    The caller decides how to report the cancel.
    """
    recordings = out_dir / "recordings"
    cancelled = False
    try:
        captures = backend.render(plan, demo_path, recordings, opts.capture, log, cancel)
    except CaptureCancelled:
        captures, cancelled = [], True
        log("Stopped early. Exporting the clips that were already recorded.")
    clips = [c for c in plan.clips if not cancelled or segment_files(recordings, c) is not None]
    exports = export_plan(
        replace(plan, clips=clips),
        recordings,
        out_dir,
        list(opts.aspects),
        settings=opts.encode,
        vertical_mode=opts.vertical_mode,
        x_offset=opts.x_offset,
        output=opts.output,
        log=log,
    )
    all_ok = not cancelled and all(c.error is None for c in captures) and all(e.error is None for e in exports)
    if all_ok and not opts.keep_recordings:
        shutil.rmtree(recordings, ignore_errors=True)
        log("Removed the raw recordings; the finished videos are kept.")
    return captures, exports
