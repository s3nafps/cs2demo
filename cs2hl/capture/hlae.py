"""HLAE backend: records each clip of a plan from CS2 with HLAE's screen recorder.

A generated console script sets up the recorder, plays the demo and schedules each
clip on demo ticks with ``mirv_cmd addAtTick``. HLAE writes every take under the CS2
folder, and each finished ``video.mp4`` is moved to ``<out_dir>/<clip id>.mp4``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable

from ..models import Clip, Plan
from .base import CaptureCancelled, CaptureResult, CaptureSettings

PRESET_NAME = "cs2hl_h264"
# {QUOTE} and {AFX_STREAM_PATH} are HLAE placeholders. The two backslashes match the
# built-in presets in shared/RecordingSettings.cpp; HLAE turns them into one.
PRESET_OPTIONS = (
    r"-c:v libx264 -preset slow -crf 16 -pix_fmt yuv420p -movflags +faststart"
    r" {QUOTE}{AFX_STREAM_PATH}\\video.mp4{QUOTE}"
)
CS2HL_DIR = "cs2hl"
WORK_DIR = "work"
SESSION_NAME = "session"
EXEC_LINE = f"exec {CS2HL_DIR}/{SESSION_NAME}"
# cs2.exe runs from game\bin\win64 and HLAE resolves relative record names from there.
RECORD_ROOT = r"..\..\csgo"
# A command at tick T runs once playback passes T, so seeks and quit run one tick after a record end.
AFTER_END_TICKS = 1
CS2_LAUNCH_OPTIONS = "-steam -insecure"


@dataclass(frozen=True)
class HlaeConfig:
    cs2_dir: Path  # the "Counter-Strike Global Offensive" folder
    hlae_exe: Path | None = None
    launch_command: list[str] | None = None  # started with subprocess when set
    pov_slot: int | None = None  # spec_player index; not verified for CS2
    session_timeout_s: float = 7200.0
    poll_interval_s: float = 2.0

    @property
    def cs2_exe(self) -> Path:
        return self.cs2_dir / "game" / "bin" / "win64" / "cs2.exe"

    @property
    def csgo_dir(self) -> Path:
        return self.cs2_dir / "game" / "csgo"


def hlae_launch_command(hlae_exe: Path, cs2_exe: Path, cmd_line: str = CS2_LAUNCH_OPTIONS) -> list[str]:
    """HLAE command line that starts cs2.exe through its custom loader with the hook DLL injected."""
    hook_dll = hlae_exe.parent / "x64" / "AfxHookSource2.dll"
    return [
        str(hlae_exe),
        "-customLoader",
        "-programPath", str(cs2_exe),
        "-cmdLine", cmd_line,
        "-hookDllPath", str(hook_dll),
        "-autoStart",
    ]


def ffmpeg_preset_line(name: str = PRESET_NAME, options: str = PRESET_OPTIONS) -> str:
    return f'mirv_streams settings add ffmpeg {name} "{options}"'


def demo_stem(demo_path: Path) -> str:
    """File name stem as CS2 will see it: only letters, digits, underscore and dash."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", demo_path.stem) or "demo"


def record_name(clip_id: str) -> str:
    """Relative record folder for a clip. Spaces are avoided because addAtTick drops quotes."""
    return f"{RECORD_ROOT}\\{CS2HL_DIR}\\{WORK_DIR}\\{_token(clip_id, 'clip id')}"


def select_clips(clips: list[Clip]) -> tuple[list[Clip], list[Clip]]:
    """Split clips in start order into those one session can record and those that overlap an earlier one."""
    kept: list[Clip] = []
    skipped: list[Clip] = []
    for clip in sorted(clips, key=lambda c: (c.start_tick, c.end_tick)):
        if kept and clip.start_tick <= kept[-1].end_tick:
            skipped.append(clip)
        else:
            kept.append(clip)
    return kept, skipped


def split_sessions(clips: list[Clip]) -> list[list[Clip]]:
    """One CS2 session per round of non-overlapping clips. Overlapping highlights need extra sessions."""
    sessions: list[list[Clip]] = []
    remaining = list(clips)
    while remaining:
        kept, remaining = select_clips(remaining)
        sessions.append(kept)
    return sessions


def timed(tick: int, command: str) -> str:
    return f"mirv_cmd addAtTick {tick} {command}"


def build_session_cfg(clips: list[Clip], demo: str, fps: int, pov_slot: int | None = None) -> str:
    """Console script for one session: recorder setup, playdemo, then timed record, seek and quit commands."""
    ordered = sorted(clips, key=lambda c: c.start_tick)
    for clip in ordered:
        if clip.end_tick <= clip.start_tick:
            raise ValueError(f"{clip.id} has no length ({clip.start_tick} to {clip.end_tick})")
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt.start_tick <= prev.end_tick:
            raise ValueError(f"{nxt.id} overlaps {prev.id}; a session records clips in order")

    lines = [
        ffmpeg_preset_line(),
        f"mirv_streams record fps {fps}",
        "mirv_streams record screen enabled 1",
        f"mirv_streams record screen settings {PRESET_NAME}",
        "mirv_cmd clear",
        f"playdemo {CS2HL_DIR}/{_token(demo, 'demo name')}",
    ]
    for index, clip in enumerate(ordered):
        if index == 0 and pov_slot is not None:
            lines.append(timed(clip.start_tick, f"spec_player {pov_slot}"))
        lines.append(timed(clip.start_tick, f"mirv_streams record name {record_name(clip.id)}"))
        lines.append(timed(clip.start_tick, "mirv_streams record start"))
        lines.append(timed(clip.end_tick, "mirv_streams record end"))
        seek_tick = clip.end_tick + AFTER_END_TICKS
        if index + 1 < len(ordered):
            nxt = ordered[index + 1]
            if nxt.start_tick > seek_tick:
                lines.append(timed(seek_tick, f"demo_gototick {nxt.start_tick}"))
        else:
            lines.append(timed(seek_tick, "quit"))
    return "\n".join(lines) + "\n"


def stage_demo(demo_path: Path, dest_dir: Path) -> Path:
    """Copy the demo into the CS2 folder unless an identical copy is already there."""
    dest = dest_dir / f"{demo_stem(demo_path)}.dem"
    src = demo_path.stat()
    if dest.exists():
        existing = dest.stat()
        if existing.st_size == src.st_size and abs(existing.st_mtime - src.st_mtime) < 2:
            return dest
    shutil.copy2(demo_path, dest)
    return dest


def newest_take_video(work_dir: Path) -> Path | None:
    takes = [p for p in work_dir.glob("take*/video.mp4") if p.is_file()]
    return max(takes, key=lambda p: p.stat().st_mtime, default=None)


def is_settled(seen: dict[str, int], key: str, size: int) -> bool:
    """True when a non-empty file has the same size at two consecutive polls."""
    previous = seen.get(key)
    seen[key] = size
    return size > 0 and previous == size


def _token(value: str, what: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"{what} {value!r} must contain only letters, digits, '_' and '-'")
    return value


def _move_recording(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    shutil.move(str(src), str(dst))
    return dst


class HlaeBackend:
    name = "hlae"

    def __init__(self, config: HlaeConfig) -> None:
        self.config = config

    def render(
        self,
        plan: Plan,
        demo_path: Path,
        out_dir: Path,
        settings: CaptureSettings,
        log: Callable[[str], None],
        cancel: Event,
    ) -> list[CaptureResult]:
        """Record every clip, one CS2 session per round of non-overlapping clips.

        A cancel between sessions raises CaptureCancelled. Recordings already moved into
        out_dir are kept on disk.
        """
        if not plan.clips:
            log("No clips to record.")
            return []
        sessions = split_sessions(plan.clips)
        results: dict[str, CaptureResult] = {}
        for number, batch in enumerate(sessions, start=1):
            if cancel.is_set():
                raise CaptureCancelled("HLAE capture cancelled")
            if len(sessions) > 1:
                log(f"Session {number} of {len(sessions)}: {len(batch)} clip(s). Overlapping highlights need separate CS2 runs.")
            results.update(self._record(batch, demo_path, out_dir, settings, log, cancel))
        return [results[clip.id] for clip in plan.clips]

    def _record(
        self,
        clips: list[Clip],
        demo_path: Path,
        out_dir: Path,
        settings: CaptureSettings,
        log: Callable[[str], None],
        cancel: Event,
    ) -> dict[str, CaptureResult]:
        cfg = self.config
        try:
            if not cfg.csgo_dir.is_dir():
                raise FileNotFoundError(f"CS2 folder not found: {cfg.csgo_dir}")
            cs2hl_dir = cfg.csgo_dir / CS2HL_DIR
            cs2hl_dir.mkdir(parents=True, exist_ok=True)
            demo = stage_demo(demo_path, cs2hl_dir)
            for clip in clips:
                work = self._work_dir(clip.id)
                if work.exists():
                    shutil.rmtree(work)
                work.mkdir(parents=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            text = build_session_cfg(clips, demo.stem, settings.fps, cfg.pov_slot)
            (cs2hl_dir / f"{SESSION_NAME}.cfg").write_text(text, encoding="utf-8")
        except (OSError, ValueError) as exc:
            return _fail_all(clips, str(exc))

        log(f"Recording {len(clips)} clip(s) with HLAE at {settings.fps} fps.")
        log(f"Recording size comes from CS2, not from the requested {settings.width}x{settings.height}.")
        if cfg.hlae_exe is not None:
            _warn_missing_ffmpeg(cfg.hlae_exe.parent, log)

        if cfg.launch_command:
            try:
                subprocess.Popen(cfg.launch_command)
            except OSError as exc:
                return _fail_all(clips, f"could not start {cfg.launch_command[0]}: {exc}")
            log(f"Started {subprocess.list2cmdline(cfg.launch_command)}")
        else:
            self._log_manual_steps(log)

        log(f"Waiting for {len(clips)} clip(s), up to {cfg.session_timeout_s:g} s.")
        return self._collect(clips, out_dir, log, cancel)

    def _log_manual_steps(self, log: Callable[[str], None]) -> None:
        cfg = self.config
        if cfg.hlae_exe is not None:
            command = hlae_launch_command(cfg.hlae_exe, cfg.cs2_exe)
            log("Start CS2 through HLAE: " + subprocess.list2cmdline(command))
        else:
            log(f"Start CS2 ({cfg.cs2_exe}) through HLAE.")
        # Whether CS2 resolves exec paths below csgo\cs2hl is not verified; see the report.
        log(f"When the main menu is up, type in the CS2 console: {EXEC_LINE}")

    def _collect(
        self,
        clips: list[Clip],
        out_dir: Path,
        log: Callable[[str], None],
        cancel: Event,
    ) -> dict[str, CaptureResult]:
        cfg = self.config
        pending = [clip.id for clip in clips]
        sizes: dict[str, int] = {}
        notes: dict[str, str] = {}
        done: dict[str, CaptureResult] = {}
        deadline = time.monotonic() + cfg.session_timeout_s
        while pending:
            if cancel.is_set():
                raise CaptureCancelled("HLAE capture cancelled")
            for clip_id in list(pending):
                saved = self._try_save(clip_id, out_dir, sizes, notes)
                if saved is not None:
                    done[clip_id] = CaptureResult(clip_id, path=saved)
                    pending.remove(clip_id)
                    log(f"Saved {saved.name}")
            if not pending or time.monotonic() >= deadline:
                break
            if cancel.wait(cfg.poll_interval_s):
                raise CaptureCancelled("HLAE capture cancelled")
        for clip_id in pending:
            reason = notes.get(clip_id) or f"no recording after {cfg.session_timeout_s:g} s"
            done[clip_id] = CaptureResult(clip_id, error=reason)
        return done

    def _try_save(self, clip_id: str, out_dir: Path, sizes: dict[str, int], notes: dict[str, str]) -> Path | None:
        try:
            src = newest_take_video(self._work_dir(clip_id))
            if src is None or not is_settled(sizes, clip_id, src.stat().st_size):
                return None
            return _move_recording(src, out_dir / f"{clip_id}.mp4")
        except OSError as exc:
            notes[clip_id] = str(exc)
            return None

    def _work_dir(self, clip_id: str) -> Path:
        return self.config.csgo_dir / CS2HL_DIR / WORK_DIR / clip_id


def _fail_all(clips: list[Clip], message: str) -> dict[str, CaptureResult]:
    return {clip.id: CaptureResult(clip.id, error=message) for clip in clips}


def _warn_missing_ffmpeg(hlae_dir: Path, log: Callable[[str], None]) -> None:
    ffmpeg_dir = hlae_dir / "ffmpeg"
    if not ((ffmpeg_dir / "ffmpeg.ini").is_file() or (ffmpeg_dir / "bin" / "ffmpeg.exe").is_file()):
        log(f"No ffmpeg found for HLAE under {ffmpeg_dir}. Recording will fail.")
