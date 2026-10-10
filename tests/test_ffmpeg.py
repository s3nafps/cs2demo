import shutil
import subprocess
from pathlib import Path

import pytest

from cs2hl import ffmpeg
from cs2hl.ffmpeg import EncodeSettings, build_command, output_size
from cs2hl.export import export_plan
from cs2hl.models import Clip, DemoMeta, Plan

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def make_source(path: Path, size: str = "1920x1080", seconds: int = 2) -> Path:
    """A synthetic 'game capture': moving test pattern plus a tone, so both streams exist."""
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=60",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", str(seconds), "-c:v", "libx264", "-crf", "28", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )
    return path


def test_output_sizes_for_both_aspects():
    assert output_size("16x9", 1080) == (1920, 1080)
    assert output_size("9x16", 1080) == (1080, 1920)
    assert output_size("16x9", 1440) == (2560, 1440)
    assert output_size("9x16", 2160) == (2160, 3840)


def test_command_uses_high_quality_h264_settings(tmp_path):
    cmd = build_command(tmp_path / "in.mp4", tmp_path / "out.mp4", "9x16", EncodeSettings(crf=16))

    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-crf") + 1] == "16"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert "+faststart" in cmd
    assert cmd[-1].endswith("out.mp4")


def test_crop_offset_must_be_in_range(tmp_path):
    with pytest.raises(ValueError):
        build_command(tmp_path / "a.mp4", tmp_path / "b.mp4", "9x16", x_offset=1.5)


def test_crop_offset_is_written_into_the_filter(tmp_path):
    cmd = build_command(tmp_path / "a.mp4", tmp_path / "b.mp4", "9x16", vertical_mode="crop", x_offset=0.5)
    graph = cmd[cmd.index("-filter_complex") + 1]

    assert "crop=trunc(ih*9/32)*2:ih:" in graph
    assert "(1+(0.5000))" in graph


@needs_ffmpeg
@pytest.mark.parametrize(
    ("aspect", "mode", "expected"),
    [
        ("16x9", "crop", (1280, 720)),
        ("9x16", "crop", (720, 1280)),
        ("9x16", "fit", (720, 1280)),
    ],
)
def test_rendered_clip_has_expected_dimensions(tmp_path, aspect, mode, expected):
    src = make_source(tmp_path / "src.mp4")
    dst = tmp_path / f"out_{aspect}_{mode}.mp4"
    settings = EncodeSettings(short_side=720, preset="ultrafast", crf=23)

    ffmpeg.run(build_command(src, dst, aspect, settings, vertical_mode=mode))

    info = ffmpeg.probe(dst)
    assert (info["width"], info["height"]) == expected
    assert info["codec"] == "h264"
    assert info["has_audio"] is True
    assert info["duration"] == pytest.approx(2.0, abs=0.2)


@needs_ffmpeg
def test_export_plan_writes_both_aspects_and_reports_missing_recordings(tmp_path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    make_source(recordings / "clip001_r01.mp4")
    plan = Plan(
        meta=DemoMeta(demo_name="m.dem", map_name="de_dust2", tickrate=64.0),
        clips=[
            Clip("clip001_r01", 1, 1, "alpha", 0, 128, 0.0, 2.0, 8.0, ("3K",), ()),
            Clip("clip002_r02", 2, 2, "bravo", 0, 128, 0.0, 2.0, 6.0, ("2K",), ()),
        ],
    )
    settings = EncodeSettings(short_side=360, preset="ultrafast", crf=30)

    results = export_plan(plan, recordings, tmp_path / "out", ["16x9", "9x16"], settings=settings)

    by_key = {(r.clip_id, r.aspect): r for r in results}
    assert by_key[("clip001_r01", "16x9")].error is None
    assert ffmpeg.probe(by_key[("clip001_r01", "16x9")].output)["width"] == 640
    assert ffmpeg.probe(by_key[("clip001_r01", "9x16")].output)["height"] == 640
    assert "no recording" in by_key[("clip002_r02", "16x9")].error
