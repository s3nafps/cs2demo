"""Shared steps for the CLI and the desktop UI: highlights -> selection -> plan."""

from __future__ import annotations

import shutil
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import Callable, Sequence

from .capture import CaptureBackend, CaptureCancelled, CaptureResult, CaptureSettings
from .detect import DetectConfig, detect_highlights
from .export import ExportResult, export_plan, remove_old_outputs, remove_recordings, segment_files
from .ffmpeg import Aspect, EncodeSettings, VerticalMode
from .models import Clip, DemoData, OutputMode, Plan
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
        deaths=data.deaths,
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


RECORDINGS_DIR = "recordings"


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

    Old recordings of the plan's clips are removed before recording, and the videos of an
    earlier run in ``out_dir`` before exporting, so two runs never mix. Only clips that were
    recorded are exported; nothing is exported when none was.

    If the cancel event stops the recording, the clips already on disk are still exported
    (that export cannot be stopped, the event is already set). If it is set later, the
    export stops at once, a running ffmpeg included, and the videos finished so far are
    returned. After a cancel the raw recordings are kept. The caller decides how to report
    the cancel.
    """
    recordings = out_dir / RECORDINGS_DIR
    remove_recordings(recordings, plan.clips)
    stopped = False
    try:
        captures = backend.render(plan, demo_path, recordings, opts.capture, log, cancel)
    except CaptureCancelled:
        captures, stopped = [], True
    clips = recorded_clips(plan.clips, captures, recordings)
    if stopped and clips:
        log(f"Stopped early. Exporting the {len(clips)} clip(s) already recorded.")
    if not clips:
        log("Stopped before any clip was recorded." if stopped else "No clip was recorded, so there is nothing to export.")
        with suppress(OSError):
            recordings.rmdir()  # only when empty
        return captures, []
    exports: list[ExportResult] = []
    if stopped or not cancel.is_set():
        remove_old_outputs(out_dir, plan.meta.demo_name, log)
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
            cancel=None if stopped else cancel,
        )
    if stopped or cancel.is_set():
        log(f"Cancelled. The raw recordings are kept in {recordings}.")
        return captures, exports
    all_ok = all(c.error is None for c in captures) and all(e.error is None for e in exports)
    if all_ok and not opts.keep_recordings:
        shutil.rmtree(recordings, ignore_errors=True)
        log("Removed the raw recordings; the finished videos are kept.")
    return captures, exports


def recorded_clips(clips: Sequence[Clip], captures: Sequence[CaptureResult], recordings: Path) -> list[Clip]:
    """The clips worth exporting: their capture succeeded, or, with no capture result, their files exist."""
    succeeded = {c.clip_id: c.error is None for c in captures}
    return [
        clip
        for clip in clips
        if (succeeded[clip.id] if clip.id in succeeded else segment_files(recordings, clip) is not None)
    ]
