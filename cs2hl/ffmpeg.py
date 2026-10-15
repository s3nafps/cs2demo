"""Reframe, join and encode recorded clips into deliverable aspect ratios with FFmpeg."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Literal, Sequence

Aspect = Literal["16x9", "9x16"]
VerticalMode = Literal["crop", "fit"]
AudioCodec = Literal["aac", "pcm"]  # pcm is for intermediate parts that are joined later

AUDIO_RATE = 48000
# One mp4/mov timescale for every file we write, so parts always share a time base when joined.
TIMESCALE = 90000
POLL_S = 0.1  # how often a running ffmpeg looks at its cancel event


class FFmpegError(RuntimeError):
    pass


class FFmpegCancelled(FFmpegError):
    """Raised by ``run`` when its cancel event is set. ffmpeg has been stopped by then."""


def _no_window_flags() -> int:
    """A windowed Windows build would open a console for every ffmpeg call without this."""
    if os.name == "nt" and getattr(sys, "frozen", False):
        return subprocess.CREATE_NO_WINDOW
    return 0


def find_tool(name: str) -> str | None:
    """Locate ffmpeg or ffprobe.

    Order: CS2HL_FFMPEG_DIR env var, the folder of the packaged exe (when frozen),
    the current folder, then PATH. The packaged build ships ffmpeg.exe next to the exe.
    """
    exe = f"{name}.exe" if os.name == "nt" else name
    candidates: list[Path] = []
    env_dir = os.environ.get("CS2HL_FFMPEG_DIR")
    if env_dir:
        candidates.append(Path(env_dir) / exe)
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / exe)
    candidates.append(Path.cwd() / exe)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


@dataclass(frozen=True)
class EncodeSettings:
    short_side: int = 1080  # 1080 -> 1920x1080 (16:9) or 1080x1920 (9:16)
    crf: int = 17  # visually near-lossless for H.264; lower is larger and sharper
    preset: str = "slow"
    audio_bitrate: str = "192k"


@dataclass(frozen=True)
class Recording:
    """One recorded file as the join command needs it. Build it with ``probe_recording``."""

    path: Path
    duration: float  # length of the video stream in seconds
    has_audio: bool = True
    start: float = 0.0  # start time of the video stream; audio is aligned to it


def output_size(aspect: Aspect, short_side: int) -> tuple[int, int]:
    long_side = round(short_side * 16 / 9)
    return (long_side, short_side) if aspect == "16x9" else (short_side, long_side)


def check_x_offset(x_offset: float) -> None:
    if not -1.0 <= x_offset <= 1.0:
        raise ValueError("x_offset must be between -1 and 1")


def build_command(
    src: Path,
    dst: Path,
    aspect: Aspect,
    settings: EncodeSettings = EncodeSettings(),
    vertical_mode: VerticalMode = "crop",
    x_offset: float = 0.0,
) -> list[str]:
    """Build the ffmpeg command for one input. Assumes a 16:9 game capture as the source.

    vertical_mode (9:16 only):
      crop: zoom into the centre of the 16:9 frame. Sharpest, but drops the sides
            (the kill feed sits on the right, so use x_offset to shift the window).
      fit:  keep the full frame over a blurred copy of itself. Nothing is cut.
    x_offset: -1 = crop window at far left, 0 = centred, 1 = far right.
    """
    check_x_offset(x_offset)
    w, h = output_size(aspect, settings.short_side)
    graph = _filter_graph(aspect, w, h, vertical_mode, x_offset)
    return [
        *_head(),
        "-i",
        str(src),
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        "-map",
        "0:a?",
        *_video_args(settings),
        "-movflags",
        "+faststart",
        *_audio_args("aac", settings),
        str(dst),
    ]


def build_join_command(
    recordings: Sequence[Recording],
    dst: Path,
    aspect: Aspect,
    settings: EncodeSettings = EncodeSettings(),
    vertical_mode: VerticalMode = "crop",
    x_offset: float = 0.0,
    *,
    audio: AudioCodec = "aac",
) -> list[str]:
    """Join recordings in order and reframe them in one encode.

    Each input is reframed to the output size before the concat filter, so inputs of
    different sizes still join. Every input gets an audio track of exactly its video
    length: its own audio padded or trimmed, or silence when it has none. The output
    therefore always has one video and one audio stream.
    """
    if not recordings:
        raise ValueError("nothing to join")
    check_x_offset(x_offset)
    w, h = output_size(aspect, settings.short_side)
    single = len(recordings) == 1
    inputs: list[str] = []
    chains: list[str] = []
    for i, rec in enumerate(recordings):
        inputs += ["-i", str(rec.path)]
        v_out, a_out = ("v", "a") if single else (f"v{i}", f"a{i}")
        chains.append(
            _reframe_graph(aspect, w, h, vertical_mode, x_offset, f"{i}:v:0", v_out, "setpts=PTS-STARTPTS,", str(i))
        )
        chains.append(_audio_chain(i, rec, a_out))
    if not single:
        pads = "".join(f"[v{i}][a{i}]" for i in range(len(recordings)))
        chains.append(f"{pads}concat=n={len(recordings)}:v=1:a=1[v][a]")
    return [
        *_head(),
        *inputs,
        "-filter_complex",
        ";".join(chains),
        "-map",
        "[v]",
        "-map",
        "[a]",
        *_video_args(settings),
        *_audio_args(audio, settings),
        *_container_args(),
        str(dst),
    ]


def build_remux_command(src: Path, dst: Path, settings: EncodeSettings = EncodeSettings()) -> list[str]:
    """Copy the video of a joined part into an mp4 and encode its audio to AAC."""
    return [
        *_head(),
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c:v",
        "copy",
        *_audio_args("aac", settings),
        *_container_args(),
        str(dst),
    ]


def build_concat_command(list_file: Path, dst: Path, settings: EncodeSettings = EncodeSettings()) -> list[str]:
    """Join the files named in a concat list: video is stream-copied, audio is encoded once.

    The parts must share codec settings (``build_join_command`` with the same settings and
    aspect). Their audio should be PCM: AAC parts joined by stream copy gain encoder
    priming at every cut and drift out of sync.
    """
    return [
        *_head(),
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c:v",
        "copy",
        *_audio_args("aac", settings),
        *_container_args(),
        str(dst),
    ]


def write_concat_list(paths: Sequence[Path], list_file: Path) -> Path:
    """Write a concat demuxer list. Paths inside the list's folder are written relative to it."""
    base = list_file.parent
    lines = []
    for path in paths:
        try:
            entry = path.relative_to(base).as_posix()
        except ValueError:
            entry = path.resolve().as_posix()
        lines.append("file '" + entry.replace("'", "'\\''") + "'\n")
    list_file.write_text("".join(lines), encoding="utf-8", newline="\n")
    return list_file


def _head() -> list[str]:
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]


def _video_args(settings: EncodeSettings) -> list[str]:
    return [
        "-c:v",
        "libx264",
        "-preset",
        settings.preset,
        "-crf",
        str(settings.crf),
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
    ]


def _audio_args(audio: AudioCodec, settings: EncodeSettings) -> list[str]:
    if audio == "aac":
        return ["-c:a", "aac", "-b:a", settings.audio_bitrate]
    if audio == "pcm":
        return ["-c:a", "pcm_s16le"]
    raise ValueError(f"unknown audio codec: {audio}")


def _container_args() -> list[str]:
    return ["-video_track_timescale", str(TIMESCALE), "-movflags", "+faststart"]


def _audio_chain(i: int, rec: Recording, out: str) -> str:
    end = f"{rec.duration:.6f}"
    if not rec.has_audio:
        return f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE},atrim=end={end}[{out}]"
    # Shift by the video start so both streams keep their sync, then pad or cut the start
    # (first_pts) and the end (apad + atrim) to match the video exactly.
    return (
        f"[{i}:a:0]asetpts=PTS-({rec.start:.6f})/TB,"
        f"aresample={AUDIO_RATE}:async=1:first_pts=0,aformat=channel_layouts=stereo,"
        f"apad,atrim=end={end}[{out}]"
    )


def _filter_graph(aspect: Aspect, w: int, h: int, vertical_mode: VerticalMode, x_offset: float) -> str:
    return _reframe_graph(aspect, w, h, vertical_mode, x_offset)


def _reframe_graph(
    aspect: Aspect,
    w: int,
    h: int,
    vertical_mode: VerticalMode,
    x_offset: float,
    src: str = "0:v",
    out: str = "v",
    pre: str = "",
    tag: str = "",
) -> str:
    """Filter chain from input label ``src`` to output label ``out``; ``pre`` runs first."""
    head = f"[{src}]{pre}"
    if aspect == "16x9":
        return (
            f"{head}scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[{out}]"
        )
    if aspect != "9x16":
        raise ValueError(f"unknown aspect: {aspect}")
    if vertical_mode == "crop":
        crop_w = "trunc(ih*9/32)*2"  # 9:16 width of a 16:9 frame, kept even for yuv420p
        crop_x = f"(iw-{crop_w})*(1+({x_offset:.4f}))/2"
        return f"{head}crop={crop_w}:ih:{crop_x}:0,scale={w}:{h},setsar=1,format=yuv420p[{out}]"
    if vertical_mode == "fit":
        return (
            f"{head}split=2[bg{tag}][fg{tag}];"
            f"[bg{tag}]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:5[bgb{tag}];"
            f"[fg{tag}]scale={w}:-2[fgs{tag}];"
            f"[bgb{tag}][fgs{tag}]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[{out}]"
        )
    raise ValueError(f"unknown vertical_mode: {vertical_mode}")


def run(cmd: list[str], cancel: Event | None = None) -> None:
    """Run an ffmpeg command and wait for it.

    A set ``cancel`` stops ffmpeg within about POLL_S seconds and raises FFmpegCancelled;
    a set event before the start runs nothing.
    """
    tool = find_tool(cmd[0])
    if tool is None:
        raise FFmpegError(f"{cmd[0]} not found. Install FFmpeg, or place {cmd[0]}.exe next to the app.")
    if cancel is not None and cancel.is_set():
        raise FFmpegCancelled(f"{cmd[0]} was not started: cancelled")
    with subprocess.Popen(
        [tool, *cmd[1:]],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_no_window_flags(),
    ) as proc:
        try:
            stderr = _wait(proc, cancel, cmd[0])
        except BaseException:
            _stop(proc)
            raise
    if proc.returncode != 0:
        message = stderr.strip()[-2000:]
        raise FFmpegError(message or f"{cmd[0]} exited with {proc.returncode}")


def run_to(cmd: list[str], dst: Path, cancel: Event | None = None) -> Path:
    """Run a command that writes ``dst``. A failed or cancelled run leaves no partial file behind."""
    try:
        run(cmd, cancel=cancel)
    except FFmpegError:
        dst.unlink(missing_ok=True)
        raise
    return dst


def _wait(proc: subprocess.Popen[str], cancel: Event | None, name: str) -> str:
    """Wait for ``proc`` while reading its output, so a full pipe never blocks it. Returns stderr."""
    while True:
        try:
            return proc.communicate(timeout=None if cancel is None else POLL_S)[1] or ""
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                raise FFmpegCancelled(f"{name} was stopped: cancelled") from None


def _stop(proc: subprocess.Popen[str]) -> None:
    """Kill a running process at once; what it was writing is thrown away anyway."""
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def probe(path: Path) -> dict:
    ffprobe = find_tool("ffprobe")
    if ffprobe is None:
        raise FFmpegError("ffprobe not found. Install FFmpeg, or place ffprobe.exe next to the app.")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_no_window_flags(),
    )
    if result.returncode != 0:
        raise FFmpegError(f"cannot read {path.name}: {result.stderr.strip() or 'ffprobe failed'}")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    if video is None:
        raise FFmpegError(f"{path.name} has no video stream")
    fmt_duration = _seconds(data.get("format", {}).get("duration"))
    video_duration = _seconds(video.get("duration")) or fmt_duration or 0.0
    return {
        "width": video["width"],
        "height": video["height"],
        "codec": video["codec_name"],
        "duration": fmt_duration or video_duration,
        "video_duration": video_duration,
        "video_start": _seconds(video.get("start_time")) or 0.0,
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def probe_recording(path: Path) -> Recording:
    info = probe(path)
    if info["video_duration"] <= 0:
        raise FFmpegError(f"{path.name} has no video frames")
    return Recording(path, info["video_duration"], info["has_audio"], info["video_start"])


def _seconds(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
