import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from cs2hl import ffmpeg
from cs2hl.ffmpeg import (
    EncodeSettings,
    FFmpegCancelled,
    FFmpegError,
    Recording,
    build_command,
    build_concat_command,
    build_join_command,
    build_remux_command,
    output_size,
    write_concat_list,
)

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def make_source(path: Path, size: str = "1920x1080", seconds: float = 2, audio: bool = True) -> Path:
    """A synthetic 'game capture': moving test pattern, plus a tone unless audio is False."""
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"] if audio else []
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=60", *tone,
            "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-pix_fmt", "yuv420p", *(["-c:a", "aac"] if audio else []), str(path),
        ],
        check=True,
        timeout=120,
    )
    return path


def graph_of(cmd: list[str]) -> str:
    return cmd[cmd.index("-filter_complex") + 1]


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
    assert cmd[cmd.index("-profile:v") + 1] == "high"
    assert "+faststart" in cmd
    assert cmd[-1].endswith("out.mp4")


def test_crop_offset_must_be_in_range(tmp_path):
    with pytest.raises(ValueError):
        build_command(tmp_path / "a.mp4", tmp_path / "b.mp4", "9x16", x_offset=1.5)
    with pytest.raises(ValueError):
        build_join_command([Recording(tmp_path / "a.mp4", 1.0)], tmp_path / "b.mp4", "9x16", x_offset=-2)


def test_crop_offset_is_written_into_the_filter(tmp_path):
    cmd = build_command(tmp_path / "a.mp4", tmp_path / "b.mp4", "9x16", vertical_mode="crop", x_offset=0.5)
    graph = graph_of(cmd)

    assert "crop=trunc(ih*9/32)*2:ih:" in graph
    assert "(1+(0.5000))" in graph


def test_unknown_aspect_or_mode_is_rejected(tmp_path):
    rec = [Recording(tmp_path / "a.mp4", 1.0)]
    with pytest.raises(ValueError):
        build_join_command(rec, tmp_path / "b.mp4", "4x3")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build_join_command(rec, tmp_path / "b.mp4", "9x16", vertical_mode="stretch")  # type: ignore[arg-type]


def test_join_command_concats_every_input_and_fills_missing_audio(tmp_path):
    recs = [
        Recording(tmp_path / "s0.mp4", 1.0, has_audio=True),
        Recording(tmp_path / "s1.mp4", 0.75, has_audio=False),
        Recording(tmp_path / "s2.mp4", 0.5, has_audio=True, start=0.02),
    ]
    cmd = build_join_command(recs, tmp_path / "out.mov", "9x16", audio="pcm")
    graph = graph_of(cmd)

    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"] == [str(r.path) for r in recs]
    assert graph.endswith("[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[v][a]")
    assert graph.count("anullsrc") == 1
    assert "anullsrc=channel_layout=stereo:sample_rate=48000,atrim=end=0.750000[a1]" in graph
    assert "[1:a:0]" not in graph
    assert "[2:a:0]asetpts=PTS-(0.020000)/TB," in graph
    assert "apad,atrim=end=0.500000[a2]" in graph
    assert graph.count("setpts=PTS-STARTPTS") == 3
    assert cmd[cmd.index("-map") + 1] == "[v]"
    assert cmd[cmd.index("-map", cmd.index("-map") + 1) + 1] == "[a]"
    assert cmd[cmd.index("-c:a") + 1] == "pcm_s16le"
    assert cmd[cmd.index("-video_track_timescale") + 1] == "90000"


def test_join_command_with_one_input_needs_no_concat(tmp_path):
    cmd = build_join_command([Recording(tmp_path / "s0.mp4", 2.0)], tmp_path / "out.mp4", "16x9")
    graph = graph_of(cmd)

    assert "concat" not in graph
    assert "[v]" in graph and "[a]" in graph
    assert cmd[cmd.index("-c:a") + 1] == "aac"


def test_join_fit_mode_uses_separate_labels_per_input(tmp_path):
    recs = [Recording(tmp_path / "s0.mp4", 1.0), Recording(tmp_path / "s1.mp4", 1.0)]
    graph = graph_of(build_join_command(recs, tmp_path / "o.mp4", "9x16", vertical_mode="fit"))

    assert "split=2[bg0][fg0]" in graph and "split=2[bg1][fg1]" in graph


def test_join_needs_at_least_one_recording(tmp_path):
    with pytest.raises(ValueError):
        build_join_command([], tmp_path / "o.mp4", "16x9")


def test_concat_and_remux_copy_video_and_encode_audio_once(tmp_path):
    for cmd in (
        build_concat_command(tmp_path / "list.txt", tmp_path / "m.mp4", EncodeSettings(audio_bitrate="160k")),
        build_remux_command(tmp_path / "p.mov", tmp_path / "c.mp4", EncodeSettings(audio_bitrate="160k")),
    ):
        assert cmd[cmd.index("-c:v") + 1] == "copy"
        assert cmd[cmd.index("-c:a") + 1] == "aac"
        assert cmd[cmd.index("-b:a") + 1] == "160k"
        assert "+faststart" in cmd
    concat = build_concat_command(tmp_path / "list.txt", tmp_path / "m.mp4")
    assert concat[concat.index("-f") + 1] == "concat"


def test_concat_list_is_relative_inside_its_folder_and_escapes_quotes(tmp_path):
    inside = tmp_path / "parts" / "clip001_r01_16x9.mov"
    outside = tmp_path / "other" / "it's.mov"
    (tmp_path / "parts").mkdir()
    list_file = write_concat_list([inside, outside], tmp_path / "parts" / "list.txt")

    lines = list_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "file 'clip001_r01_16x9.mov'"
    assert lines[1] == "file '" + outside.resolve().as_posix().replace("'", "'\\''") + "'"
    assert lines[1].endswith("it'\\''s.mov'")


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
def test_probe_reports_video_length_and_missing_audio(tmp_path):
    src = make_source(tmp_path / "silent.mp4", size="320x180", seconds=0.5, audio=False)

    rec = ffmpeg.probe_recording(src)

    assert rec.path == src
    assert rec.has_audio is False
    assert rec.duration == pytest.approx(0.5, abs=0.02)
    assert rec.start == pytest.approx(0.0, abs=0.05)


@needs_ffmpeg
def test_probe_of_a_broken_file_raises_ffmpeg_error(tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video at all")

    with pytest.raises(FFmpegError):
        ffmpeg.probe(bad)


@needs_ffmpeg
def test_failed_run_leaves_no_partial_output(tmp_path):
    dst = tmp_path / "out.mp4"
    dst.write_bytes(b"stale")

    with pytest.raises(FFmpegError):
        ffmpeg.run_to(build_command(tmp_path / "missing.mp4", dst, "16x9"), dst)
    assert not dst.exists()


def lavfi_command(dst: Path, seconds: int, realtime: bool) -> list[str]:
    """Encode a test pattern. With ``realtime`` (-re) it takes ``seconds`` of wall time."""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        *(["-re"] if realtime else []), "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=30:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-f", "mpegts", str(dst),
    ]


@pytest.fixture
def started(monkeypatch):
    """Every process ffmpeg.run starts, so a test can check it is gone afterwards."""
    procs: list[subprocess.Popen] = []

    class Tracked(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            procs.append(self)

    monkeypatch.setattr(subprocess, "Popen", Tracked)
    return procs


@needs_ffmpeg
def test_cancel_kills_a_running_ffmpeg_quickly_and_removes_its_output(tmp_path, started):
    dst = tmp_path / "slow.ts"
    cancel = threading.Event()
    seen: list[bool] = []

    def press_cancel():
        seen.append(dst.exists() and started[0].poll() is None)  # ffmpeg is running and writing
        cancel.set()

    timer = threading.Timer(1.5, press_cancel)
    timer.start()
    begin = time.monotonic()
    try:
        with pytest.raises(FFmpegCancelled, match="cancelled"):
            ffmpeg.run_to(lavfi_command(dst, 60, realtime=True), dst, cancel)
    finally:
        timer.cancel()
    stopped_after = time.monotonic() - begin - 1.5

    assert seen == [True]
    assert stopped_after < 3.0  # the encode alone would take 60 s
    assert started[0].poll() is not None
    assert not dst.exists()
    assert isinstance(FFmpegCancelled("x"), FFmpegError)  # callers that catch FFmpegError still do


@needs_ffmpeg
def test_an_unset_cancel_event_lets_ffmpeg_finish(tmp_path):
    dst = tmp_path / "short.ts"

    ffmpeg.run_to(lavfi_command(dst, 1, realtime=False), dst, threading.Event())

    assert dst.stat().st_size > 0


def test_a_set_cancel_event_starts_nothing(tmp_path, monkeypatch):
    def no_start(*args, **kwargs):
        raise AssertionError("ffmpeg must not start")

    monkeypatch.setattr(ffmpeg, "find_tool", lambda name: "/opt/ffmpeg")
    monkeypatch.setattr(subprocess, "Popen", no_start)
    dst = tmp_path / "out.mp4"
    dst.write_bytes(b"old")
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(FFmpegCancelled):
        ffmpeg.run_to(["ffmpeg", "-i", "x"], dst, cancel)
    assert not dst.exists()


class FakeProc:
    """A finished ffmpeg as Popen returns it."""

    def __init__(self, args: list[str], kwargs: dict, returncode: int, stderr: str):
        self.args, self.kwargs = args, kwargs
        self.returncode = returncode
        self.stderr_text = stderr

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def communicate(self, timeout=None):
        return "", self.stderr_text

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


@pytest.mark.parametrize("cancel", [None, threading.Event()])
@pytest.mark.parametrize(
    ("returncode", "stderr", "message"),
    [(1, "  in.mp4: No such file or directory\n", "in.mp4: No such file or directory"), (3, "", "ffmpeg exited with 3")],
)
def test_failed_run_reports_ffmpeg_errors_and_hides_the_console(monkeypatch, cancel, returncode, stderr, message):
    procs: list[FakeProc] = []

    def popen(args, **kwargs):
        procs.append(FakeProc(args, kwargs, returncode, stderr))
        return procs[-1]

    monkeypatch.setattr(ffmpeg, "find_tool", lambda name: "/opt/ffmpeg")
    monkeypatch.setattr(ffmpeg, "_no_window_flags", lambda: 0x08000000)
    monkeypatch.setattr(subprocess, "Popen", popen)

    with pytest.raises(FFmpegError) as raised:
        ffmpeg.run(["ffmpeg", "-i", "in.mp4", "out.mp4"], cancel=cancel)

    assert str(raised.value) == message
    assert not isinstance(raised.value, FFmpegCancelled)
    [proc] = procs
    assert proc.args == ["/opt/ffmpeg", "-i", "in.mp4", "out.mp4"]
    assert proc.kwargs["creationflags"] == 0x08000000
    assert proc.kwargs["stdin"] is subprocess.DEVNULL
