"""HLAE backend: records every clip segment from CS2 with HLAE's screen recorder, unattended.

One CS2 session per group of clips that do not overlap in time:

1. copy the demo to game\\csgo\\cs2hl and write game\\csgo\\cfg\\cs2hl_boot.cfg;
2. start CS2 through HLAE's custom loader with ``+exec cs2hl_boot`` on its command line;
3. the cfg sets up the recorder, schedules seeks, camera, kill feed, record and quit
   commands on demo ticks with ``mirv_cmd addAtTick``, then plays the demo;
4. wait for cs2.exe to exit and move each take, with its WAV muxed in, to
   ``<out_dir>/<clip id>_s<k>.mp4``.

Checked against HLAE source (hlae/Program.cs, Loader.cs, LaunchCs2.cs,
shared/CommandSystem.*, AfxHookSource2/RenderSystemDX11Hooks.cpp) and CS Demo Manager
(create-cs2-video-json-file.ts, json-actions-file-generator.ts). Not run in CS2 yet.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Mapping

from .. import ffmpeg
from ..models import Clip, Plan
from ..system import steam, tools
from .base import (
    CaptureCancelled,
    CaptureError,
    CaptureResult,
    CaptureSettings,
    clip_segments,
    segment_file_name,
)

PRESET_NAME = "cs2hl_h264"
# {QUOTE} and {AFX_STREAM_PATH} are HLAE placeholders. The two backslashes match the
# built-in presets in shared/RecordingSettings.cpp; HLAE turns them into one.
# For the screen recorder {AFX_STREAM_PATH} is the take folder (CAfxStreams::GetStreamFolder).
PRESET_OPTIONS = (
    r"-c:v libx264 -preset slow -crf 16 -pix_fmt yuv420p -movflags +faststart"
    r" {QUOTE}{AFX_STREAM_PATH}\\video.mp4{QUOTE}"
)
CS2HL_DIR = "cs2hl"
WORK_DIR = "work"
BOOT_CFG = "cs2hl_boot"  # exec reads game\csgo\cfg\<name>.cfg
# HLAE starts cs2.exe in game\bin\win64 (Loader.cs), so relative record names resolve from there.
RECORD_ROOT = r"..\..\csgo"
CS2_PROCESS = "cs2.exe"
STEAM_PROCESSES = ("steam.exe", "steamchina.exe")
# Set by -addEnv for CS2 and removed from HLAE's own environment, which HLAE passes on first.
STEAM_ENV_KEYS = ("SteamPath", "SteamClientLaunch", "SteamGameId", "SteamAppId", "SteamOverlayGameId")
VIDEO_NAME = "video.mp4"
CLAIMED_VIDEO_NAME = "video.cs2hl.mp4"  # renamed before use; the rename fails on Windows while ffmpeg writes
AUDIO_NAME = "audio.wav"  # startMovieWav output, redirected into the take folder (AfxHookSource2/main.cpp)

# Ticks. A mirv_cmd at tick T fires on the frame where playback moves past T, and a forward
# seek fires every command it skips (CommandSystem::OnExecuteCommands).
FIRST_TICK = 96  # CS Demo Manager's floor for every action: early ticks can be skipped (its issue 1343)
SEEK_PREROLL_S = 1.0  # a seek lands this long before the recording starts
AFTER_END_TICKS = 1  # a seek runs one tick after the previous record end
QUIT_AFTER_TICKS = 64  # quit this long after the last record end (CS Demo Manager)
DEATHMSG_LIFETIME_S = 10

# Seconds.
HLAE_START_TIMEOUT_S = 90.0  # HLAE.exe exits about 2 s after injecting; longer means an error dialog
CS2_START_TIMEOUT_S = 60.0
FIRST_TAKE_TIMEOUT_S = 300.0  # no take folder this long after CS2 started: the boot cfg did not run
IDLE_AFTER_LAST_S = 60.0  # every take written and none growing, but CS2 did not quit
STALL_AFTER_S = 240.0  # takes missing and none appeared or grew this long: CS2 hangs (gaps are seconds)
SETTLE_TIMEOUT_S = 120.0
MISSING_POLLS = 3  # polls after CS2 exited before a missing or empty take counts as failed
SHORT_RATIO = 0.5  # a segment shorter than this share of its length was cut off

SESSION_LINES = (
    # CS Demo Manager's plugin unhides every hidden command too; which of ours need it is not verified.
    "mirv_cvar_unhide_all",
    "engine_no_focus_sleep 0",  # keep rendering at full speed when the window loses focus
)
# CS Demo Manager's HUD cleanup (create-cs2-video-json-file.ts mandatoryCommands).
HUD_LINES = (
    "cl_hud_telemetry_frametime_show 0",
    "cl_hud_telemetry_net_misdelivery_show 0",
    "cl_hud_telemetry_ping_show 0",
    "cl_hud_telemetry_serverrecvmargin_graph_show 0",
    "cl_trueview_show_status 0",
    "r_show_build_info 0",
    "demo_ui_mode 0",
    "cl_draw_only_deathnotices 1",
)
# Run again once the demo plays, as CS Demo Manager does; whether loading resets them is not verified.
PLAYBACK_LINES = ("sv_cheats 1", *HUD_LINES)


@dataclass(frozen=True)
class HlaeConfig:
    cs2_dir: Path  # the "Counter-Strike Global Offensive" folder
    hlae_exe: Path
    steam_dir: Path | None = None  # found from the registry when None
    session_timeout_s: float = 7200.0
    poll_interval_s: float = 1.0
    launch: bool = True  # False: write the files, log the command, and wait for the user to start CS2

    @property
    def cs2_exe(self) -> Path:
        return steam.cs2_exe_path(self.cs2_dir)

    @property
    def csgo_dir(self) -> Path:
        return self.cs2_dir / "game" / "csgo"

    @property
    def cfg_path(self) -> Path:
        return self.csgo_dir / "cfg" / f"{BOOT_CFG}.cfg"

    @property
    def stage_dir(self) -> Path:
        return self.csgo_dir / CS2HL_DIR


@dataclass(frozen=True)
class Segment:
    clip: Clip
    index: int
    start_tick: int
    end_tick: int

    @property
    def name(self) -> str:
        return f"{self.clip.id}_s{self.index}"


def cs2_command_line(width: int, height: int) -> str:
    """CS2 arguments: HLAE's own CS2 launcher line (LaunchCs2.cs) plus the boot cfg.

    AfxHookSource2 refuses to run without -insecure.
    """
    return f"-steam -insecure -sw -w {width} -h {height} -nojoy +exec {BOOT_CFG}"


def steam_env(steam_dir: Path | None) -> list[str]:
    """Environment of a Steam-client launch, as HLAE's CS2 launcher sets it (LaunchCs2.cs)."""
    env = [] if steam_dir is None else [f"SteamPath={steam_dir}"]
    return env + ["SteamClientLaunch=1", "SteamGameId=730", "SteamAppId=730", "SteamOverlayGameId=730"]


def hlae_process_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The app's environment without the Steam variables that -addEnv sets.

    HLAE copies its own environment and appends the -addEnv lines after it (Program.cs),
    so an inherited SteamAppId (the app started from Steam as a non-Steam game) would win
    over 730. Windows names ignore case.
    """
    base = os.environ if base is None else base
    drop = {key.lower() for key in STEAM_ENV_KEYS}
    return {key: value for key, value in base.items() if key.lower() not in drop}


def hlae_launch_args(hlae_exe: Path, cs2_exe: Path, cmd_line: str, steam_dir: Path | None = None) -> list[str]:
    """HLAE.exe arguments that start CS2 with the hook and no HLAE window (Program.cs custom loader).

    The first -addEnv seeds the game's environment from HLAE's own, the rest are appended.
    """
    args = [
        str(hlae_exe),
        "-customLoader",
        "-noGui",
        "-autoStart",
        "-noConfig",
        "-hookDllPath", str(tools.hook_dll_path(hlae_exe)),
        "-programPath", str(cs2_exe),
        "-cmdLine", cmd_line,
    ]
    for item in steam_env(steam_dir):
        args += ["-addEnv", item]
    return args


def ffmpeg_preset_line(name: str = PRESET_NAME, options: str = PRESET_OPTIONS) -> str:
    return f'mirv_streams settings add ffmpeg {name} "{options}"'


def demo_stem(demo_path: Path) -> str:
    """File name stem as CS2 will see it: only letters, digits, underscore and dash."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", demo_path.stem) or "demo"


def record_name(segment_name: str) -> str:
    """Relative record folder for a segment. No spaces: mirv_cmd addAtTick drops quotes."""
    return f"{RECORD_ROOT}\\{CS2HL_DIR}\\{WORK_DIR}\\{_token(segment_name, 'segment name')}"


def clip_span(clip: Clip) -> tuple[int, int]:
    segments = clip_segments(clip)
    return segments[0][0], segments[-1][1]


def clip_problem(clip: Clip) -> str | None:
    """Why a clip cannot be recorded, or None."""
    try:
        _token(clip.id, "clip id")
    except ValueError as exc:
        return str(exc)
    previous_end: int | None = None
    for start, end in clip_segments(clip):
        if end <= start:
            return f"{clip.id} has an empty segment ({start} to {end})"
        if previous_end is not None and start <= previous_end:
            return f"{clip.id} has segments out of order or overlapping"
        previous_end = end
    return None


def select_clips(clips: list[Clip]) -> tuple[list[Clip], list[Clip]]:
    """Split clips in start order into those one session can record and those that overlap an earlier one."""
    kept: list[Clip] = []
    skipped: list[Clip] = []
    for clip in sorted(clips, key=clip_span):
        if kept and clip_span(clip)[0] <= clip_span(kept[-1])[1]:
            skipped.append(clip)
        else:
            kept.append(clip)
    return kept, skipped


def split_sessions(clips: list[Clip]) -> list[list[Clip]]:
    """One CS2 session per round of non-overlapping clips.

    Seeks in a session only go forward: a backward seek would replay the commands it passes.
    """
    sessions: list[list[Clip]] = []
    remaining = list(clips)
    while remaining:
        kept, remaining = select_clips(remaining)
        sessions.append(kept)
    return sessions


def session_segments(clips: list[Clip]) -> list[Segment]:
    """Every segment of the session's clips in demo order. Raises ValueError if any overlap."""
    out: list[Segment] = []
    for clip in sorted(clips, key=clip_span):
        problem = clip_problem(clip)
        if problem:
            raise ValueError(problem)
        for index, (start, end) in enumerate(clip_segments(clip)):
            if out and start <= out[-1].end_tick:
                raise ValueError(f"{clip.id} overlaps {out[-1].clip.id}; a session records clips in order")
            out.append(Segment(clip, index, start, end))
    return out


def killfeed_lines(steamid: int) -> list[str]:
    """Show only the POV player's kills, highlighted like the local player's (CS Demo Manager recipe)."""
    return [
        "mirv_deathmsg clear",
        "mirv_deathmsg filter clear",
        "mirv_deathmsg filter add block=1",
        f"mirv_deathmsg filter add attackerMatch=x{steamid} attackerIsLocal=1 block=0",
    ]


def build_schedule(clips: list[Clip], tickrate: float) -> list[tuple[int, str]]:
    """(tick, command) pairs for one session, sorted by tick.

    Per segment: a seek to one second before its start (only when that skips at least
    that much), the camera half a second before the start (never on a seek tick), the
    kill-feed filter at a clip's first segment, then record name and start at the start
    and record end at the end. ``quit`` follows the last segment. Commands go in tick
    order because HLAE's tick tree only visits every command when they were added that way
    (CommandSystem.h Insert and OverlapExecute).
    """
    segments = session_segments(clips)
    if not segments:
        return []
    preroll = max(2, round(SEEK_PREROLL_S * tickrate))
    lead = preroll // 2
    events: list[tuple[int, str]] = [(FIRST_TICK, line) for line in PLAYBACK_LINES]
    cursor = FIRST_TICK
    for seg in segments:
        target = seg.start_tick - preroll
        if target - cursor >= preroll:
            events.append((cursor, f"demo_gototick {target}"))
        camera = max(cursor, seg.start_tick - lead)
        if seg.clip.pov_slot is not None:
            events += [(camera, "spec_mode 1"), (camera, f"spec_player {seg.clip.pov_slot}")]
        if seg.index == 0:
            events += [(camera, line) for line in killfeed_lines(seg.clip.pov_steamid)]
        events += [
            (seg.start_tick, f"mirv_streams record name {record_name(seg.name)}"),
            (seg.start_tick, "mirv_streams record start"),
            (seg.end_tick, "mirv_streams record end"),
        ]
        cursor = seg.end_tick + AFTER_END_TICKS
    events.append((segments[-1].end_tick + QUIT_AFTER_TICKS, "quit"))
    return sorted(((max(FIRST_TICK, tick), cmd) for tick, cmd in events), key=lambda e: e[0])


def timed(tick: int, command: str) -> str:
    return f"mirv_cmd addAtTick {tick} {command}"


def build_boot_cfg(clips: list[Clip], demo: str, fps: int, tickrate: float) -> str:
    """The whole session script: recorder and HUD setup, the tick schedule, then playdemo."""
    lines = [
        "// Written by cs2hl for one recording session. Safe to delete.",
        *SESSION_LINES,
        ffmpeg_preset_line(),
        f"mirv_streams record fps {fps}",
        "mirv_streams record screen enabled 1",
        f"mirv_streams record screen settings {PRESET_NAME}",
        "mirv_streams record startMovieWav 1",
        *HUD_LINES,
        f"mirv_deathmsg lifetime {DEATHMSG_LIFETIME_S}",
        "mirv_cmd clear",
        *(timed(tick, cmd) for tick, cmd in build_schedule(clips, tickrate)),
        f"playdemo {CS2HL_DIR}/{_token(demo, 'demo name')}",
    ]
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


def take_files(work_dir: Path) -> tuple[Path | None, Path | None]:
    """Video and WAV of the newest take (HLAE numbers them take0000, take0001, ...)."""
    takes = sorted((p for p in work_dir.glob("take*") if p.is_dir()), reverse=True)
    for take in takes:
        for name in (VIDEO_NAME, CLAIMED_VIDEO_NAME):
            video = take / name
            if video.is_file():
                audio = take / AUDIO_NAME
                return video, (audio if audio.is_file() else None)
    return None, None


def is_settled(seen: dict[str, int], key: str, size: int) -> bool:
    """True when a non-empty file has the same size at two consecutive polls."""
    previous = seen.get(key)
    seen[key] = size
    return size > 0 and previous == size


def mux_command(video: Path, audio: Path, dst: Path) -> list[str]:
    """Copy the video stream and add the game's WAV as AAC (CS Demo Manager muxes the same way)."""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dst),
    ]


def too_short(duration_s: float, expected_s: float) -> bool:
    return expected_s > 0 and duration_s < expected_s * SHORT_RATIO


def _token(value: str, what: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"{what} {value!r} must contain only letters, digits, '_' and '-'")
    return value


class _SessionWatch:
    """Ends the wait for cs2.exe early: a hook error box, no take at all, a stall, or every take done but no quit."""

    def __init__(
        self,
        work_dirs: list[Path],
        clock: Callable[[], float] = time.monotonic,
        first_take_s: float | None = None,
        idle_s: float | None = None,
        stall_s: float | None = None,
    ) -> None:
        self.work_dirs = work_dirs
        self.clock = clock
        self.first_take_s = FIRST_TAKE_TIMEOUT_S if first_take_s is None else first_take_s
        self.idle_s = IDLE_AFTER_LAST_S if idle_s is None else idle_s
        self.stall_s = STALL_AFTER_S if stall_s is None else stall_s
        self.started = clock()
        self.last_change = self.started
        self.state: tuple[tuple[int, int], ...] = ()
        self.reason: str | None = None

    def __call__(self) -> bool:
        self.reason = self.check()
        return self.reason is not None

    def check(self) -> str | None:
        now = self.clock()
        if tools.window_title_exists(tools.HOOK_ERROR_TITLE):
            return "hook-error"
        takes = [sum(1 for p in d.glob("take*") if p.is_dir()) for d in self.work_dirs]
        if not any(takes):
            return "no-start" if now - self.started > self.first_take_s else None
        videos = [take_files(d)[0] for d in self.work_dirs]
        state = tuple(zip(takes, (_size(v) for v in videos)))
        if state != self.state:
            self.state, self.last_change = state, now  # a take appeared or grew
        idle = now - self.last_change
        if all(v is not None for v in videos):
            return "finished" if idle >= self.idle_s else None
        return "stalled" if idle >= self.stall_s else None

    def message(self) -> str:
        return WATCH_MESSAGES[self.reason or "no-start"].format(first_take_s=self.first_take_s, stall_s=self.stall_s)


def _size(path: Path | None) -> int:
    try:
        return path.stat().st_size if path is not None else -1
    except OSError:
        return -1


WATCH_MESSAGES = {
    "hook-error": "HLAE reported an error inside CS2 (an 'Error - AfxHookSource2' window). "
    "HLAE may need an update after a CS2 update.",
    "no-start": "CS2 started but nothing was recorded within {first_take_s:g} s. "
    "The start script probably did not run; check the CS2 console.",
    "stalled": "CS2 stopped recording: nothing new was written for {stall_s:g} s while some highlights "
    "were still to come. CS2 probably froze or shows an error; it was closed.",
}


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

        Raises CaptureError when nothing can be recorded (CS2 already running, HLAE or
        CS2 missing) and CaptureCancelled on cancel; segment files already saved stay.
        """
        if not plan.clips:
            log("No clips to record.")
            return []
        results: dict[str, CaptureResult] = {}
        valid: list[Clip] = []
        for clip in plan.clips:
            problem = clip_problem(clip)
            if problem:
                results[clip.id] = CaptureResult(clip.id, error=problem)
            else:
                valid.append(clip)
        if not valid:
            return [results[clip.id] for clip in plan.clips]
        self._check_ready(log)
        try:
            self.config.stage_dir.mkdir(parents=True, exist_ok=True)
            demo = stage_demo(demo_path, self.config.stage_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CaptureError(f"could not copy the demo into the CS2 folder: {exc}") from exc
        for clip in valid:
            if clip.pov_slot is None:
                log(f"No camera slot for {clip.pov_name} in {clip.id}; it follows the demo's own camera.")

        tickrate = plan.meta.tickrate or 64.0
        sessions = split_sessions(valid)
        for number, batch in enumerate(sessions, start=1):
            if cancel.is_set():
                raise CaptureCancelled("HLAE capture cancelled")
            if len(sessions) > 1:
                log(f"CS2 run {number} of {len(sessions)}: {len(batch)} clip(s). Overlapping highlights need separate runs.")
            done, stop = self._record_session(batch, demo.stem, out_dir, settings, tickrate, log, cancel)
            results.update(done)
            if stop:
                for clip in (c for later in sessions[number:] for c in later):
                    results[clip.id] = CaptureResult(clip.id, error=f"not recorded: {stop}")
                break
        return [results[clip.id] for clip in plan.clips]

    def _check_ready(self, log: Callable[[str], None]) -> None:
        cfg = self.config
        if cfg.cs2_dir is None:
            raise CaptureError("CS2 was not found. Choose its folder in the Setup tab.")
        if cfg.hlae_exe is None:
            raise CaptureError("HLAE was not found. Install HLAE or choose HLAE.exe in the Setup tab.")
        if not cfg.csgo_dir.is_dir():
            raise CaptureError(f"CS2 folder not found: {cfg.csgo_dir}")
        if not cfg.cs2_exe.is_file():
            raise CaptureError(f"cs2.exe not found: {cfg.cs2_exe}")
        if not tools.is_hlae_exe(cfg.hlae_exe):
            raise CaptureError(f"HLAE not found: {cfg.hlae_exe} with x64\\AfxHookSource2.dll next to it")
        if tools.is_process_running(CS2_PROCESS):
            raise CaptureError(
                "CS2 is already running. Close it and start again: cs2hl starts its own CS2 "
                "with -insecure and HLAE's hook."
            )
        if cfg.launch and not any(tools.is_process_running(name) for name in STEAM_PROCESSES):
            raise CaptureError("Steam is not running. Start Steam and log in, then try again.")
        if tools.hlae_has_ffmpeg(cfg.hlae_exe):
            return
        app_ffmpeg = ffmpeg.find_tool("ffmpeg")
        if app_ffmpeg is None:
            raise CaptureError(f"HLAE needs FFmpeg: put ffmpeg.exe in {cfg.hlae_exe.parent / 'ffmpeg' / 'bin'}.")
        try:
            ini = tools.register_hlae_ffmpeg(cfg.hlae_exe, Path(app_ffmpeg))
        except OSError as exc:
            raise CaptureError(
                f"HLAE has no FFmpeg and cs2hl cannot write to its folder ({exc}). Copy ffmpeg.exe into "
                f"{cfg.hlae_exe.parent / 'ffmpeg' / 'bin'} as administrator, or install HLAE in a folder you own."
            ) from exc
        log(f"HLAE had no FFmpeg; pointed it at {app_ffmpeg} ({ini}).")

    def _record_session(
        self,
        clips: list[Clip],
        demo: str,
        out_dir: Path,
        settings: CaptureSettings,
        tickrate: float,
        log: Callable[[str], None],
        cancel: Event,
    ) -> tuple[dict[str, CaptureResult], str | None]:
        """Record one session. Returns the results and, when later sessions should not run, why."""
        cfg = self.config
        if tools.is_process_running(CS2_PROCESS):
            outcome = tools.wait_for_exit(CS2_PROCESS, cancel, CS2_START_TIMEOUT_S, cfg.poll_interval_s)
            if outcome == "cancelled":
                raise CaptureCancelled("HLAE capture cancelled")
            if outcome != "done":
                message = "CS2 from the previous run is still open"
                return _fail_all(clips, message), message
        try:
            segments = session_segments(clips)
            self._prepare(clips, segments, out_dir)
            text = build_boot_cfg(clips, demo, settings.fps, tickrate)
            cfg.cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.cfg_path.write_text(text, encoding="utf-8")
        except (OSError, ValueError) as exc:
            return _fail_all(clips, str(exc)), str(exc)
        log(f"Recording {len(segments)} segment(s) of {len(clips)} clip(s) at {settings.width}x{settings.height}, {settings.fps} fps.")

        stop = self._start_cs2(settings, log, cancel)
        if stop:
            return _fail_all(clips, stop), stop

        watch = _SessionWatch([self._work_dir(s.name) for s in segments])
        log("CS2 is recording on its own and closes when it is done. Leave its window open.")
        outcome = tools.wait_for_exit(CS2_PROCESS, cancel, cfg.session_timeout_s, cfg.poll_interval_s, abort=watch)
        reason: str | None = None
        stop = None
        if outcome == "cancelled":
            tools.kill_process(CS2_PROCESS)
            log("Stopped. Keeping the segments CS2 finished.")
            self._collect(clips, segments, out_dir, tickrate, log, "cancelled")
            raise CaptureCancelled("HLAE capture cancelled")
        if outcome == "timeout":
            tools.kill_process(CS2_PROCESS)
            reason = stop = f"CS2 did not finish within {cfg.session_timeout_s:g} s"
            log(reason + "; closed it.")
        elif outcome == "aborted":
            tools.kill_process(CS2_PROCESS)
            if watch.reason == "finished":
                log("Every segment is written but CS2 did not quit; closed it.")
            else:
                reason = stop = watch.message()
                log(reason)
        else:
            log("CS2 closed. Collecting the recordings.")
        return self._collect(clips, segments, out_dir, tickrate, log, reason), stop

    def _prepare(self, clips: list[Clip], segments: list[Segment], out_dir: Path) -> None:
        for seg in segments:
            work = self._work_dir(seg.name)
            if work.exists():
                shutil.rmtree(work)
            work.mkdir(parents=True)
        for clip in clips:
            for old in out_dir.glob(f"{clip.id}_s*.mp4"):
                old.unlink()

    def _start_cs2(self, settings: CaptureSettings, log: Callable[[str], None], cancel: Event) -> str | None:
        """Start CS2 through HLAE (or wait for the user to). Returns an error message, or None."""
        cfg = self.config
        steam_dir = cfg.steam_dir or steam.steam_root()
        args = hlae_launch_args(cfg.hlae_exe, cfg.cs2_exe, cs2_command_line(settings.width, settings.height), steam_dir)
        if not cfg.launch:
            log("Debug mode: start CS2 yourself with: " + subprocess.list2cmdline(args))
            log(f"or from HLAE's CS2 launcher with the launch option +exec {BOOT_CFG}")
            outcome = tools.wait_for_start(CS2_PROCESS, cancel, cfg.session_timeout_s, cfg.poll_interval_s)
            if outcome == "cancelled":
                raise CaptureCancelled("HLAE capture cancelled")
            return None if outcome == "done" else f"CS2 was not started within {cfg.session_timeout_s:g} s"

        log("Starting CS2 through HLAE.")
        try:
            proc = start_process(args, hlae_process_env())
        except OSError as exc:
            return f"could not start {cfg.hlae_exe.name}: {exc}"
        outcome = tools.wait_until(lambda: proc.poll() is not None, cancel, HLAE_START_TIMEOUT_S, cfg.poll_interval_s)
        if outcome != "done":
            _kill(proc)
            tools.kill_process(CS2_PROCESS)
            if outcome == "cancelled":
                raise CaptureCancelled("HLAE capture cancelled")
            # HLAE shows a modal ErrorDialogue on launch failures (Loader.cs), which keeps it running.
            return f"HLAE did not finish starting CS2 within {HLAE_START_TIMEOUT_S:g} s; it may be showing an error"
        if proc.returncode != 0:
            tools.kill_process(CS2_PROCESS)  # Loader.cs resumes cs2.exe even when injection failed
            return f"HLAE could not start CS2 (exit code {proc.returncode})"
        outcome = tools.wait_for_start(CS2_PROCESS, cancel, CS2_START_TIMEOUT_S, cfg.poll_interval_s)
        if outcome == "cancelled":
            tools.kill_process(CS2_PROCESS)
            raise CaptureCancelled("HLAE capture cancelled")
        if outcome != "done":
            return "HLAE finished but cs2.exe is not running"
        return None

    def _collect(
        self,
        clips: list[Clip],
        segments: list[Segment],
        out_dir: Path,
        tickrate: float,
        log: Callable[[str], None],
        reason: str | None = None,
    ) -> dict[str, CaptureResult]:
        """Move each finished take to out_dir, muxing its WAV in. A clip missing any segment fails."""
        placed: dict[str, Path] = {}
        errors: dict[str, str] = {}
        pending = {seg.name: seg for seg in segments}
        sizes: dict[str, int] = {}
        missing: dict[str, int] = {}
        deadline = time.monotonic() + SETTLE_TIMEOUT_S
        while pending:
            for name, seg in list(pending.items()):
                video, audio = take_files(self._work_dir(name))
                size = _size(video)
                if size <= 0:
                    missing[name] = missing.get(name, 0) + 1
                    if missing[name] >= MISSING_POLLS:
                        errors[name] = reason or ("HLAE wrote no video" if video is None else "HLAE wrote an empty video")
                        del pending[name]
                    continue
                if not is_settled(sizes, name, size):
                    continue
                try:
                    placed[name] = self._place(video, audio, out_dir / segment_file_name(seg.clip.id, seg.index), log)
                except OSError as exc:
                    errors[name] = str(exc)  # on Windows: ffmpeg still has the file open; retried next poll
                    continue
                errors.pop(name, None)
                del pending[name]
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(self.config.poll_interval_s)
        for name in pending:
            errors.setdefault(name, reason or "the recording never finished writing")

        for seg in segments:
            path = placed.get(seg.name)
            if path is None:
                continue
            problem = _length_problem(path, (seg.end_tick - seg.start_tick) / tickrate, log)
            if problem:
                errors[seg.name] = problem
                path.unlink(missing_ok=True)
                del placed[seg.name]

        results: dict[str, CaptureResult] = {}
        for clip in clips:
            names = [seg.name for seg in segments if seg.clip.id == clip.id]
            failed = [n for n in names if n not in placed]
            if failed:
                for n in names:
                    if n in placed:
                        placed[n].unlink(missing_ok=True)
                detail = "; ".join(f"{n}: {errors.get(n, reason or 'not recorded')}" for n in failed)
                results[clip.id] = CaptureResult(clip.id, error=detail)
                continue
            parts = tuple(placed[n] for n in names)
            for n in names:
                shutil.rmtree(self._work_dir(n), ignore_errors=True)
            results[clip.id] = CaptureResult(clip.id, path=parts[0], parts=parts)
            log(f"Saved {clip.id}: {len(parts)} segment(s).")
        return results

    def _place(self, video: Path, audio: Path | None, dst: Path, log: Callable[[str], None]) -> Path:
        """Mux or move one take into ``dst``. Raises OSError while the take is still locked."""
        claimed = video if video.name == CLAIMED_VIDEO_NAME else video.with_name(CLAIMED_VIDEO_NAME)
        if claimed != video:
            os.replace(video, claimed)
        if dst.exists():
            dst.unlink()
        if audio is not None:
            if ffmpeg.find_tool("ffmpeg") is None:
                log(f"No FFmpeg found, so {dst.name} has no sound.")
            else:
                part = dst.with_name(dst.stem + "-muxing.mp4")  # not "<stem>.*", which export would pick up
                try:
                    ffmpeg.run(mux_command(claimed, audio, part))
                    os.replace(part, dst)
                    claimed.unlink(missing_ok=True)
                    return dst
                except (ffmpeg.FFmpegError, OSError) as exc:
                    part.unlink(missing_ok=True)
                    log(f"Could not add sound to {dst.name}, keeping it silent: {exc}")
        shutil.move(str(claimed), str(dst))
        return dst

    def _work_dir(self, segment_name: str) -> Path:
        return self.config.stage_dir / WORK_DIR / segment_name


def _length_problem(path: Path, expected_s: float, log: Callable[[str], None]) -> str | None:
    """A cut-off or unreadable segment, checked with ffprobe when it is available."""
    if ffmpeg.find_tool("ffprobe") is None:
        return None
    try:
        duration = float(ffmpeg.probe(path)["duration"])
    except (ffmpeg.FFmpegError, OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as exc:
        return f"unreadable recording: {exc}"
    if too_short(duration, expected_s):
        return f"recording stopped early: {duration:.1f} s of {expected_s:.1f} s"
    if duration < expected_s * 0.9:
        log(f"{path.name} is {duration:.1f} s, expected {expected_s:.1f} s.")
    return None


def start_process(args: list[str], env: Mapping[str, str] | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env
    )


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError:
        pass


def _fail_all(clips: list[Clip], message: str) -> dict[str, CaptureResult]:
    return {clip.id: CaptureResult(clip.id, error=message) for clip in clips}
