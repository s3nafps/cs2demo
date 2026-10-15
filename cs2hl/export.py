"""Turn recorded clip segments into finished videos: separate clips, one montage, or both.

Recording contract: the capture step writes each piece of a clip as
``<clip id>_s<k>.<ext>`` in the recordings directory, k = 0 .. len(clip.segments) - 1
(a clip without segments has one piece). A one-piece clip may instead be a single
``<clip id>.<ext>``, the layout older backends wrote.

Outputs in ``out_dir``:
  clips:   ``<clip id>_<aspect>.mp4`` per clip and aspect
  montage: ``<demo stem>_highlights_<aspect>.mp4`` with every clip in plan order
  both:    all of the above
"""

from __future__ import annotations

import glob
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence, get_args

from . import ffmpeg
from .ffmpeg import Aspect, EncodeSettings, Recording, VerticalMode
from .models import Clip, OutputMode, Plan

MONTAGE_ID = "montage"
PARTS_DIR = ".parts"
# Recognised recording containers, most preferred first when several share a name.
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v")


@dataclass(frozen=True)
class ExportResult:
    clip_id: str  # MONTAGE_ID for the joined video
    aspect: Aspect
    output: Path | None = None
    error: str | None = None


def export_plan(
    plan: Plan,
    recordings_dir: Path,
    out_dir: Path,
    aspects: Sequence[Aspect],
    *,
    settings: EncodeSettings = EncodeSettings(),
    vertical_mode: VerticalMode = "crop",
    x_offset: float = 0.0,
    output: OutputMode = "clips",
    log: Callable[[str], None] | None = None,
) -> list[ExportResult]:
    """Export every clip of ``plan`` to each aspect, as clips, a montage or both.

    A clip whose recordings are missing or unreadable gets an error result per aspect
    and is left out of the montage; the montage is still made from the other clips.
    Each clip is encoded once per aspect. For a montage the clip is encoded into
    ``out_dir/.parts`` with PCM audio, the parts are joined with a video stream copy,
    and in "both" mode each clip file is remuxed from its part. ``.parts`` is removed
    at the end.
    """
    if output not in get_args(OutputMode):
        raise ValueError(f"unknown output mode: {output}")
    ffmpeg.check_x_offset(x_offset)
    say = log or _quiet
    want_clips = output in ("clips", "both")
    want_montage = output in ("montage", "both")
    out_dir.mkdir(parents=True, exist_ok=True)
    parts_dir = out_dir / PARTS_DIR

    sources = {clip.id: _load_recordings(recordings_dir, clip) for clip in plan.clips}
    results: list[ExportResult] = []
    total = len(plan.clips) * len(aspects)
    done = 0
    for aspect in aspects:
        parts: list[Path] = []
        for clip in plan.clips:
            done += 1
            source = sources[clip.id]
            if isinstance(source, str):
                results.append(ExportResult(clip.id, aspect, error=source))
                continue
            say(f"Exporting {clip.id} {aspect} ({done}/{total})")
            final = out_dir / clip_file_name(clip.id, aspect)
            try:
                if want_montage:
                    parts_dir.mkdir(exist_ok=True)
                    part = parts_dir / f"{clip.id}_{aspect}.mov"
                    _encode(source, part, aspect, settings, vertical_mode, x_offset, "pcm")
                    parts.append(part)
                    if want_clips:
                        ffmpeg.run_to(ffmpeg.build_remux_command(part, final, settings), final)
                else:
                    _encode(source, final, aspect, settings, vertical_mode, x_offset, "aac")
            except ffmpeg.FFmpegError as exc:
                results.append(ExportResult(clip.id, aspect, error=str(exc)))
                continue
            if want_clips:
                results.append(ExportResult(clip.id, aspect, output=final))
        if want_montage:
            dst = out_dir / montage_file_name(plan.meta.demo_name, aspect)
            try:
                results.append(_join(parts, dst, aspect, settings, say))
            finally:
                for part in parts:
                    part.unlink(missing_ok=True)
    _remove_empty_dir(parts_dir)
    return results


def segment_count(clip: Clip) -> int:
    return max(1, len(clip.segments))


def segment_files(recordings_dir: Path, clip: Clip) -> list[Path] | None:
    """The recorded pieces of ``clip`` in order, or None when any piece is missing."""
    found, missing = _locate(recordings_dir, clip)
    return None if missing else found


def clip_file_name(clip_id: str, aspect: Aspect) -> str:
    return f"{clip_id}_{aspect}.mp4"


def montage_file_name(demo_name: str, aspect: Aspect) -> str:
    stem = Path(demo_name).stem or "demo"
    return f"{stem}_highlights_{aspect}.mp4"


def recording_for(recordings_dir: Path, clip_id: str) -> Path | None:
    """The legacy single recording ``<clip id>.<ext>``, if present."""
    return _find_file(recordings_dir, clip_id)


_find_recording = recording_for


def _quiet(_message: str) -> None:
    pass


def _encode(
    recordings: list[Recording],
    dst: Path,
    aspect: Aspect,
    settings: EncodeSettings,
    vertical_mode: VerticalMode,
    x_offset: float,
    audio: ffmpeg.AudioCodec,
) -> None:
    cmd = ffmpeg.build_join_command(recordings, dst, aspect, settings, vertical_mode, x_offset, audio=audio)
    ffmpeg.run_to(cmd, dst)


def _join(
    parts: list[Path], dst: Path, aspect: Aspect, settings: EncodeSettings, say: Callable[[str], None]
) -> ExportResult:
    if not parts:
        return ExportResult(MONTAGE_ID, aspect, error="no clip could be exported, so there is no montage")
    say(f"Joining {len(parts)} clip{'s' if len(parts) != 1 else ''} into {dst.name}")
    list_file = ffmpeg.write_concat_list(parts, parts[0].parent / f"montage_{aspect}.txt")
    try:
        ffmpeg.run_to(ffmpeg.build_concat_command(list_file, dst, settings), dst)
    except ffmpeg.FFmpegError as exc:
        return ExportResult(MONTAGE_ID, aspect, error=str(exc))
    finally:
        list_file.unlink(missing_ok=True)
    return ExportResult(MONTAGE_ID, aspect, output=dst)


def _load_recordings(recordings_dir: Path, clip: Clip) -> list[Recording] | str:
    """Probe every piece of ``clip``. Returns an error message instead when that fails."""
    found, missing = _locate(recordings_dir, clip)
    if missing:
        return f"no recording for {clip.id}: missing {', '.join(missing)} in {recordings_dir}"
    try:
        return [ffmpeg.probe_recording(path) for path in found]
    except ffmpeg.FFmpegError as exc:
        return f"unreadable recording for {clip.id}: {exc}"


def _locate(recordings_dir: Path, clip: Clip) -> tuple[list[Path], list[str]]:
    count = segment_count(clip)
    found: list[Path] = []
    missing: list[str] = []
    for k in range(count):
        path = _find_file(recordings_dir, f"{clip.id}_s{k}")
        if path is None and count == 1:
            path = _find_file(recordings_dir, clip.id)
        if path is None:
            missing.append(f"{clip.id}_s{k}.mp4")
        else:
            found.append(path)
    return found, missing


def _find_file(directory: Path, stem: str) -> Path | None:
    matches = [
        p
        for p in directory.glob(f"{glob.escape(stem)}.*")
        if p.stem == stem and p.suffix.lower() in VIDEO_EXTS and p.is_file()
    ]
    if not matches:
        return None
    return min(matches, key=lambda p: (VIDEO_EXTS.index(p.suffix.lower()), p.name))


def _remove_empty_dir(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass
