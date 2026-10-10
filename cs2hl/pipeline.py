"""Shared steps for the CLI and the desktop UI: highlights -> selection -> plan."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import Callable

from .capture import CaptureBackend, CaptureCancelled, CaptureResult, CaptureSettings
from .detect import DetectConfig, detect_highlights
from .export import ExportResult, export_plan, recording_for
from .ffmpeg import Aspect, EncodeSettings, VerticalMode
from .models import DemoData, Plan
from .planner import PlanConfig, build_plan
from .selection import Selection, filter_highlights


@dataclass(frozen=True)
class HighlightOptions:
    streak_gap_s: float = 10.0
    pre_roll_s: float = 4.0
    post_roll_s: float = 2.0
    min_score: float = 5.0
    max_clips: int | None = None


def plan_highlights(data: DemoData, selection: Selection, opts: HighlightOptions | None = None) -> Plan:
    """Detect highlights, keep the selected players and rounds, and build the clip plan."""
    opts = opts or HighlightOptions()
    highlights = detect_highlights(
        data.rounds,
        data.kills,
        data.rosters,
        DetectConfig(tickrate=data.meta.tickrate, streak_gap_s=opts.streak_gap_s),
    )
    highlights = filter_highlights(highlights, selection)
    return build_plan(
        highlights,
        data.rounds,
        data.meta,
        PlanConfig(
            pre_roll_s=opts.pre_roll_s,
            post_roll_s=opts.post_roll_s,
            min_score=opts.min_score,
            max_clips=opts.max_clips,
        ),
    )


@dataclass(frozen=True)
class RenderOptions:
    aspects: tuple[Aspect, ...] = ("16x9", "9x16")
    vertical_mode: VerticalMode = "crop"
    x_offset: float = 0.0
    encode: EncodeSettings = EncodeSettings()
    capture: CaptureSettings = CaptureSettings()


def render_plan(
    plan: Plan,
    demo_path: Path,
    backend: CaptureBackend,
    out_dir: Path,
    opts: RenderOptions,
    log: Callable[[str], None],
    cancel: Event,
) -> tuple[list[CaptureResult], list[ExportResult]]:
    """Record every clip with ``backend`` into ``out_dir/recordings``, then export the aspects.

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
    clips = [c for c in plan.clips if not cancelled or recording_for(recordings, c.id) is not None]
    exports = export_plan(
        replace(plan, clips=clips),
        recordings,
        out_dir / "highlights",
        list(opts.aspects),
        settings=opts.encode,
        vertical_mode=opts.vertical_mode,
        x_offset=opts.x_offset,
    )
    return captures, exports
