"""Export every clip in a plan to the requested aspect ratios.

Contract: the capture step writes each clip's recording as ``<clip id>.<ext>`` in
the recordings directory. Export looks for that file and writes
``<clip id>_<aspect>.mp4`` into the output directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg
from .ffmpeg import Aspect, EncodeSettings, VerticalMode
from .models import Plan


@dataclass(frozen=True)
class ExportResult:
    clip_id: str
    aspect: Aspect
    output: Path | None = None
    error: str | None = None


def export_plan(
    plan: Plan,
    recordings_dir: Path,
    out_dir: Path,
    aspects: list[Aspect],
    *,
    settings: EncodeSettings = EncodeSettings(),
    vertical_mode: VerticalMode = "crop",
    x_offset: float = 0.0,
) -> list[ExportResult]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[ExportResult] = []

    for clip in plan.clips:
        source = _find_recording(recordings_dir, clip.id)
        for aspect in aspects:
            if source is None:
                results.append(
                    ExportResult(clip.id, aspect, error=f"no recording named {clip.id}.* in {recordings_dir}")
                )
                continue
            dst = out_dir / f"{clip.id}_{aspect}.mp4"
            cmd = ffmpeg.build_command(source, dst, aspect, settings, vertical_mode, x_offset)
            try:
                ffmpeg.run(cmd)
            except ffmpeg.FFmpegError as exc:
                results.append(ExportResult(clip.id, aspect, error=str(exc)))
            else:
                results.append(ExportResult(clip.id, aspect, output=dst))
    return results


def _find_recording(recordings_dir: Path, clip_id: str) -> Path | None:
    matches = sorted(recordings_dir.glob(f"{clip_id}.*"))
    return matches[0] if matches else None
