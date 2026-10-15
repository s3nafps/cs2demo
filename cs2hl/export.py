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
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Iterable, Sequence, get_args

from . import ffmpeg
from .ffmpeg import Aspect, EncodeSettings, Recording, VerticalMode
from .models import Clip, OutputMode, Plan

MONTAGE_ID = "montage"
PARTS_DIR = ".parts"
# Recognised recording containers, most preferred first when several share a name.
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v")
# A clip file written for a planner clip id (clipNNN_rNN), as clip_file_name names it.
CLIP_FILE = re.compile(r"clip\d{3,}_r\d{2,}_(?:%s)\.mp4" % "|".join(map(re.escape, get_args(Aspect))))
PIECE_SUFFIX = re.compile(r"_s\d+$")


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
    cancel: Event | None = None,
) -> list[ExportResult]:
    """Export every clip of ``plan`` to each aspect, as clips, a montage or both.

    A clip whose recordings are missing or unreadable gets an error result per aspect
    and is left out of the montage; the montage is still made from the other clips.
    Each clip is encoded once per aspect. For a montage the clip is encoded into
    ``out_dir/.parts`` with PCM audio, the parts are joined with a video stream copy,
    and in "both" mode each clip file is remuxed from its part. ``.parts`` is removed
    at the end.

    A set ``cancel`` stops the export before the next encode or join and stops a running
    ffmpeg. The videos finished by then are returned; partial files are removed.
    """
    if output not in get_args(OutputMode):
        raise ValueError(f"unknown output mode: {output}")
    ffmpeg.check_x_offset(x_offset)
    say = log or _quiet
    want_clips = output in ("clips", "both")
    want_montage = output in ("montage", "both")
    out_dir.mkdir(parents=True, exist_ok=True)
    parts_dir = out_dir / PARTS_DIR

    results: list[ExportResult] = []
    parts: list[Path] = []
    total = len(plan.clips) * len(aspects)
    done = 0
    try:
        _check(cancel)
        sources = {clip.id: _load_recordings(recordings_dir, clip) for clip in plan.clips}
        for aspect in aspects:
            for clip in plan.clips:
                _check(cancel)
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
                        parts.append(_encode(source, part, aspect, settings, vertical_mode, x_offset, "pcm", cancel))
                        if want_clips:
                            ffmpeg.run_to(ffmpeg.build_remux_command(part, final, settings), final, cancel)
                    else:
                        _encode(source, final, aspect, settings, vertical_mode, x_offset, "aac", cancel)
                except ffmpeg.FFmpegCancelled:
                    raise
                except ffmpeg.FFmpegError as exc:
                    results.append(ExportResult(clip.id, aspect, error=str(exc)))
                    continue
                if want_clips:
                    results.append(ExportResult(clip.id, aspect, output=final))
            if want_montage:
                _check(cancel)
                dst = out_dir / montage_file_name(plan.meta.demo_name, aspect)
                try:
                    results.append(_join(parts, dst, aspect, settings, say, cancel))
                finally:
                    _remove_files(parts)
    except ffmpeg.FFmpegCancelled:
        say("Export stopped. The videos finished so far are kept.")
    finally:
        _remove_files(parts)
        _remove_empty_dir(parts_dir)
    return results


def remove_recordings(recordings_dir: Path, clips: Iterable[Clip]) -> None:
    """Delete every recorded piece of ``clips``, so a new capture never mixes with an old one."""
    if not recordings_dir.is_dir():
        return
    ids = {clip.id for clip in clips}
    for path in recordings_dir.iterdir():
        owner = PIECE_SUFFIX.sub("", path.stem)
        if (path.stem in ids or owner in ids) and path.suffix.lower() in VIDEO_EXTS and path.is_file():
            path.unlink()


def remove_old_outputs(out_dir: Path, demo_name: str, log: Callable[[str], None] | None = None) -> list[Path]:
    """Delete the videos an earlier export left in ``out_dir``, and a leftover ``.parts`` folder.

    Only clip files of planner clip ids (``clipNNN_rNN_<aspect>.mp4``) and the montages
    of ``demo_name`` are removed; every other file stays. Returns the removed files.
    """
    if not out_dir.is_dir():
        return []
    say = log or _quiet
    montages = {montage_file_name(demo_name, aspect) for aspect in get_args(Aspect)}
    removed: list[Path] = []
    for path in sorted(out_dir.iterdir()):
        if not (path.name in montages or CLIP_FILE.fullmatch(path.name)) or not path.is_file():
            continue
        try:
            path.unlink()
        except OSError as exc:
            say(f"Could not remove {path.name} from an earlier run: {exc}")
            continue
        removed.append(path)
    shutil.rmtree(out_dir / PARTS_DIR, ignore_errors=True)
    return removed


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


def _check(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ffmpeg.FFmpegCancelled("export cancelled")


def _encode(
    recordings: list[Recording],
    dst: Path,
    aspect: Aspect,
    settings: EncodeSettings,
    vertical_mode: VerticalMode,
    x_offset: float,
    audio: ffmpeg.AudioCodec,
    cancel: Event | None = None,
) -> Path:
    cmd = ffmpeg.build_join_command(recordings, dst, aspect, settings, vertical_mode, x_offset, audio=audio)
    return ffmpeg.run_to(cmd, dst, cancel)


def _join(
    parts: list[Path],
    dst: Path,
    aspect: Aspect,
    settings: EncodeSettings,
    say: Callable[[str], None],
    cancel: Event | None = None,
) -> ExportResult:
    if not parts:
        return ExportResult(MONTAGE_ID, aspect, error="no clip could be exported, so there is no montage")
    say(f"Joining {len(parts)} clip{'s' if len(parts) != 1 else ''} into {dst.name}")
    list_file = ffmpeg.write_concat_list(parts, parts[0].parent / f"montage_{aspect}.txt")
    try:
        ffmpeg.run_to(ffmpeg.build_concat_command(list_file, dst, settings), dst, cancel)
    except ffmpeg.FFmpegCancelled:
        raise
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


def _remove_files(paths: list[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
    paths.clear()


def _remove_empty_dir(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass
