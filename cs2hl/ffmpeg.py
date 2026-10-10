"""Reframe and encode one recorded clip into a deliverable aspect ratio with FFmpeg."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Aspect = Literal["16x9", "9x16"]
VerticalMode = Literal["crop", "fit"]


class FFmpegError(RuntimeError):
    pass


@dataclass(frozen=True)
class EncodeSettings:
    short_side: int = 1080  # 1080 -> 1920x1080 (16:9) or 1080x1920 (9:16)
    crf: int = 17  # visually near-lossless for H.264; lower is larger and sharper
    preset: str = "slow"
    audio_bitrate: str = "192k"


def output_size(aspect: Aspect, short_side: int) -> tuple[int, int]:
    long_side = round(short_side * 16 / 9)
    return (long_side, short_side) if aspect == "16x9" else (short_side, long_side)


def build_command(
    src: Path,
    dst: Path,
    aspect: Aspect,
    settings: EncodeSettings = EncodeSettings(),
    vertical_mode: VerticalMode = "crop",
    x_offset: float = 0.0,
) -> list[str]:
    """Build the ffmpeg command. Assumes a 16:9 game capture as the source.

    vertical_mode (9:16 only):
      crop: zoom into the centre of the 16:9 frame. Sharpest, but drops the sides
            (the kill feed sits on the right, so use x_offset to shift the window).
      fit:  keep the full frame over a blurred copy of itself. Nothing is cut.
    x_offset: -1 = crop window at far left, 0 = centred, 1 = far right.
    """
    if not -1.0 <= x_offset <= 1.0:
        raise ValueError("x_offset must be between -1 and 1")
    w, h = output_size(aspect, settings.short_side)
    graph = _filter_graph(aspect, w, h, vertical_mode, x_offset)
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-filter_complex",
        graph,
        "-map",
        "[v]",
        "-map",
        "0:a?",
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
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        "-b:a",
        settings.audio_bitrate,
        str(dst),
    ]


def _filter_graph(aspect: Aspect, w: int, h: int, vertical_mode: VerticalMode, x_offset: float) -> str:
    if aspect == "16x9":
        return (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[v]"
        )
    if vertical_mode == "crop":
        crop_w = "trunc(ih*9/32)*2"  # 9:16 width of a 16:9 frame, kept even for yuv420p
        crop_x = f"(iw-{crop_w})*(1+({x_offset:.4f}))/2"
        return f"[0:v]crop={crop_w}:ih:{crop_x}:0,scale={w}:{h},setsar=1,format=yuv420p[v]"
    if vertical_mode == "fit":
        return (
            "[0:v]split=2[bg][fg];"
            f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:5[bgb];"
            f"[fg]scale={w}:-2[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[v]"
        )
    raise ValueError(f"unknown vertical_mode: {vertical_mode}")


def run(cmd: list[str]) -> None:
    if shutil.which(cmd[0]) is None:
        raise FFmpegError(f"{cmd[0]} not found on PATH. Install FFmpeg and try again.")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise FFmpegError(result.stderr.strip() or f"{cmd[0]} exited with {result.returncode}")


def probe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    return {
        "width": video["width"],
        "height": video["height"],
        "codec": video["codec_name"],
        "duration": float(data["format"]["duration"]),
        "has_audio": any(s["codec_type"] == "audio" for s in data["streams"]),
    }
