import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from cs2hl import ffmpeg
from cs2hl.export import (
    MONTAGE_ID,
    PARTS_DIR,
    export_plan,
    montage_file_name,
    recording_for,
    segment_files,
)
from cs2hl.ffmpeg import EncodeSettings, FFmpegError, Recording
from cs2hl.models import Clip, DemoMeta, Plan

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)

# superfast keeps B-frames and the High profile, like the real presets, and is still quick at 360p.
FAST = EncodeSettings(short_side=360, preset="superfast", crf=30, audio_bitrate="96k")
META = DemoMeta(demo_name="match.dem", map_name="de_mirage", tickrate=64.0)


def make_source(path: Path, seconds: float, audio: bool = True) -> Path:
    """A synthetic 16:9 recording: moving test pattern, plus a tone unless audio is False."""
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"] if audio else []
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=60", *tone,
            "-t", f"{seconds}", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
            "-pix_fmt", "yuv420p", *(["-c:a", "aac"] if audio else []), str(path),
        ],
        check=True,
        timeout=120,
    )
    return path


def streams(path: Path) -> dict[str, dict]:
    """codec_type -> stream info, plus 'format'."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True, timeout=60,
    )
    data = json.loads(out.stdout)
    info = {s["codec_type"]: s for s in data["streams"]}
    info["format"] = data["format"]
    return info


def durations(path: Path) -> tuple[float, float]:
    info = streams(path)
    return float(info["video"]["duration"]), float(info["audio"]["duration"])


def max_volume(path: Path) -> float:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:a", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, check=True, timeout=60,
    )
    return float(re.search(r"max_volume: (-?[\d.]+) dB", out.stderr).group(1))


def clip(clip_id: str, pieces: int = 0) -> Clip:
    """A clip with ``pieces`` segments (0 = no segments, one implicit piece)."""
    segments = tuple((k * 1000, k * 1000 + 500) for k in range(pieces))
    return Clip(clip_id, 1, 1, "alpha", 0, 128, 0.0, 2.0, 8.0, ("3K",), (), segments=segments)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    return {
        "1.0": make_source(d / "a_1.0.mp4", 1.0),
        "0.5": make_source(d / "a_0.5.mp4", 0.5),
        "0.8": make_source(d / "a_0.8.mp4", 0.8),
        "silent0.75": make_source(d / "n_0.75.mp4", 0.75, audio=False),
    }


@pytest.fixture
def recordings(tmp_path):
    path = tmp_path / "recordings"
    path.mkdir()
    return path


def place(src: Path, recordings: Path, name: str) -> Path:
    return Path(shutil.copy(src, recordings / name))


def by_key(results):
    return {(r.clip_id, r.aspect): r for r in results}


@needs_ffmpeg
def test_multi_segment_clip_joins_pieces_with_and_without_audio(media, recordings, tmp_path):
    place(media["1.0"], recordings, "clip001_r01_s0.mp4")
    place(media["silent0.75"], recordings, "clip001_r01_s1.mp4")
    place(media["0.5"], recordings, "clip001_r01_s2.mp4")
    plan = Plan(META, [clip("clip001_r01", pieces=3)])

    results = export_plan(plan, recordings, tmp_path / "out", ["16x9", "9x16"], settings=FAST)

    got = by_key(results)
    assert all(r.error is None for r in results)
    for aspect, size in (("16x9", (640, 360)), ("9x16", (360, 640))):
        out = got[("clip001_r01", aspect)].output
        assert out == tmp_path / "out" / f"clip001_r01_{aspect}.mp4"
        info = streams(out)
        assert (info["video"]["width"], info["video"]["height"]) == size
        assert info["video"]["codec_name"] == "h264"
        assert info["video"]["profile"] == "High"
        assert info["video"]["pix_fmt"] == "yuv420p"
        assert info["audio"]["codec_name"] == "aac"
        video, audio = durations(out)
        assert video == pytest.approx(2.25, abs=0.2)
        assert audio == pytest.approx(video, abs=0.05)
    assert max_volume(got[("clip001_r01", "16x9")].output) > -30
    assert not (tmp_path / "out" / PARTS_DIR).exists()


@needs_ffmpeg
def test_vertical_fit_mode_and_a_clip_without_any_audio(media, recordings, tmp_path):
    place(media["silent0.75"], recordings, "clip001_r01_s0.mp4")
    place(media["silent0.75"], recordings, "clip001_r01_s1.mp4")
    plan = Plan(META, [clip("clip001_r01", pieces=2)])

    [result] = export_plan(plan, recordings, tmp_path / "out", ["9x16"], settings=FAST, vertical_mode="fit")

    info = streams(result.output)
    assert (info["video"]["width"], info["video"]["height"]) == (360, 640)
    video, audio = durations(result.output)
    assert video == pytest.approx(1.5, abs=0.2)
    assert audio == pytest.approx(video, abs=0.05)  # silence, so the montage can still join it
    assert max_volume(result.output) < -80


@needs_ffmpeg
def test_montage_is_every_clip_joined_and_nothing_else_is_left(media, recordings, tmp_path):
    place(media["1.0"], recordings, "clip001_r01_s0.mp4")
    place(media["0.5"], recordings, "clip002_r03_s0.mp4")
    place(media["silent0.75"], recordings, "clip002_r03_s1.mp4")
    place(media["0.8"], recordings, "clip003_r05.mp4")  # legacy one-file layout
    plan = Plan(META, [clip("clip001_r01"), clip("clip002_r03", pieces=2), clip("clip003_r05", pieces=1)])
    out = tmp_path / "out"

    results = export_plan(plan, recordings, out, ["16x9", "9x16"], settings=FAST, output="montage")

    assert [(r.clip_id, r.aspect, r.error) for r in results] == [
        (MONTAGE_ID, "16x9", None),
        (MONTAGE_ID, "9x16", None),
    ]
    assert sorted(p.name for p in out.iterdir()) == ["match_highlights_16x9.mp4", "match_highlights_9x16.mp4"]
    for r in results:
        video, audio = durations(r.output)
        assert video == pytest.approx(1.0 + 0.5 + 0.75 + 0.8, abs=0.2)
        assert audio == pytest.approx(video, abs=0.05)
    assert streams(results[1].output)["video"]["height"] == 640


@needs_ffmpeg
def test_both_mode_writes_clip_files_and_montage(media, recordings, tmp_path):
    place(media["1.0"], recordings, "clip001_r01_s0.mp4")
    place(media["0.5"], recordings, "clip002_r02_s0.mp4")
    plan = Plan(META, [clip("clip001_r01"), clip("clip002_r02")])
    out = tmp_path / "out"

    results = export_plan(plan, recordings, out, ["16x9"], settings=FAST, output="both")

    assert [(r.clip_id, r.error) for r in results] == [("clip001_r01", None), ("clip002_r02", None), (MONTAGE_ID, None)]
    assert sorted(p.name for p in out.iterdir()) == [
        "clip001_r01_16x9.mp4",
        "clip002_r02_16x9.mp4",
        "match_highlights_16x9.mp4",
    ]
    clip_lengths = [durations(r.output)[0] for r in results[:2]]
    assert clip_lengths[0] == pytest.approx(1.0, abs=0.05)
    assert streams(results[0].output)["audio"]["codec_name"] == "aac"
    assert durations(results[2].output)[0] == pytest.approx(sum(clip_lengths), abs=0.2)


@needs_ffmpeg
def test_missing_piece_is_reported_and_montage_uses_the_rest(media, recordings, tmp_path):
    place(media["1.0"], recordings, "clip001_r01_s0.mp4")
    place(media["0.5"], recordings, "clip002_r02_s0.mp4")  # its _s1 was never recorded
    place(media["0.8"], recordings, "clip003_r04_s0.mp4")
    plan = Plan(META, [clip("clip001_r01"), clip("clip002_r02", pieces=2), clip("clip003_r04")])

    results = export_plan(plan, recordings, tmp_path / "out", ["16x9", "9x16"], settings=FAST, output="montage")

    got = by_key(results)
    for aspect in ("16x9", "9x16"):
        error = got[("clip002_r02", aspect)].error
        assert "no recording" in error and "clip002_r02_s1" in error
        montage = got[(MONTAGE_ID, aspect)]
        assert montage.error is None
        assert durations(montage.output)[0] == pytest.approx(1.8, abs=0.2)
    assert segment_files(recordings, plan.clips[1]) is None


@needs_ffmpeg
def test_legacy_single_recordings_still_export(media, recordings, tmp_path):
    place(media["1.0"], recordings, "clip001_r01.mp4")
    plan = Plan(
        META,
        [clip("clip001_r01"), clip("clip002_r02")],
    )

    results = export_plan(plan, recordings, tmp_path / "out", ["16x9", "9x16"], settings=FAST)

    got = by_key(results)
    assert got[("clip001_r01", "16x9")].error is None
    assert streams(got[("clip001_r01", "16x9")].output)["video"]["width"] == 640
    assert streams(got[("clip001_r01", "9x16")].output)["video"]["height"] == 640
    assert "no recording" in got[("clip002_r02", "16x9")].error


def make_marked_source(path: Path, seconds: float, at: float, audio_offset: float | None = 0.0) -> Path:
    """Black video that flashes white at ``at``, with a beep at the same moment.

    audio_offset delays the audio stream inside the file (the beep still lines up with
    the flash); None leaves the file without audio.
    """
    video = f"color=black:s=640x360:r=60,drawbox=c=white:t=fill:enable='between(t,{at},{at}+0.04)'"
    audio = []
    if audio_offset is not None:
        beep = at - audio_offset
        audio = [
            "-itsoffset", f"{audio_offset}",
            "-f", "lavfi", "-i", f"aevalsrc='sin(2*PI*1000*t)*between(t,{beep},{beep}+0.05)':s=48000:d={seconds}",
            "-map", "0:v", "-map", "1:a", "-c:a", "aac",
        ]
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi", "-i", video, *audio,
            "-t", f"{seconds}", "-c:v", "libx264", "-preset", "superfast", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
        timeout=120,
    )
    return path


def flashes_and_beeps(path: Path) -> tuple[list[float], list[float]]:
    out = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-i", str(path),
            "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG",
            "-af", "silencedetect=n=-30dB:d=0.01", "-f", "null", "-",
        ],
        capture_output=True, text=True, check=True, timeout=120,
    ).stderr
    flashes: list[float] = []
    t = 0.0
    for line in out.splitlines():
        if m := re.search(r"pts_time:([\d.]+)", line):
            t = float(m.group(1))
        if (m := re.search(r"YAVG=([\d.]+)", line)) and float(m.group(1)) > 100:
            if not flashes or t - flashes[-1] > 0.2:
                flashes.append(t)
    beeps = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out)]
    return flashes, beeps


@needs_ffmpeg
def test_sound_stays_in_sync_across_cuts(recordings, tmp_path):
    make_marked_source(recordings / "a_s0.mp4", 1.5, at=0.8, audio_offset=0.3)  # audio starts late
    make_marked_source(recordings / "a_s1.mp4", 0.5, at=0.2, audio_offset=None)
    make_marked_source(recordings / "b_s0.mp4", 1.0, at=0.4, audio_offset=-0.2)  # audio starts early
    plan = Plan(META, [clip("a", pieces=2), clip("b")])

    [montage] = export_plan(plan, recordings, tmp_path / "out", ["16x9"], settings=FAST, output="montage")

    flashes, beeps = flashes_and_beeps(montage.output)
    assert flashes == pytest.approx([0.8, 1.7, 2.4], abs=0.02)
    # the last silence_end is the end of the file
    assert beeps[:2] == pytest.approx([0.8, 2.4], abs=0.03)


@needs_ffmpeg
def test_unreadable_recording_is_an_error_not_a_crash(recordings, tmp_path):
    (recordings / "clip001_r01_s0.mp4").write_bytes(b"truncated")

    [result] = export_plan(Plan(META, [clip("clip001_r01")]), recordings, tmp_path / "out", ["16x9"])

    assert result.output is None and "unreadable recording" in result.error


def test_segment_files_finds_pieces_in_order(recordings):
    for name in ("c_s1.mp4", "c_s0.mkv", "c_s0.mp4", "c_s0.mp4.part", "c_s2.wav", "c_s2.mov", "c.mp4"):
        (recordings / name).write_bytes(b"")

    assert segment_files(recordings, clip("c", pieces=3)) == [
        recordings / "c_s0.mp4",
        recordings / "c_s1.mp4",
        recordings / "c_s2.mov",
    ]
    assert segment_files(recordings, clip("c", pieces=4)) is None
    assert segment_files(recordings, clip("other")) is None
    assert segment_files(recordings / "missing", clip("c")) is None


def test_legacy_file_only_counts_for_one_piece_clips(recordings):
    (recordings / "c.mp4").write_bytes(b"")

    assert segment_files(recordings, clip("c")) == [recordings / "c.mp4"]
    assert segment_files(recordings, clip("c", pieces=1)) == [recordings / "c.mp4"]
    assert segment_files(recordings, clip("c", pieces=2)) is None
    assert recording_for(recordings, "c") == recordings / "c.mp4"
    (recordings / "c_s0.mp4").write_bytes(b"")
    assert segment_files(recordings, clip("c")) == [recordings / "c_s0.mp4"]


def test_montage_name_uses_the_demo_stem():
    assert montage_file_name("faceit match.dem", "9x16") == "faceit match_highlights_9x16.mp4"
    assert montage_file_name("", "16x9") == "demo_highlights_16x9.mp4"


def test_bad_arguments_are_rejected_before_any_work(recordings, tmp_path):
    plan = Plan(META, [clip("c")])
    with pytest.raises(ValueError):
        export_plan(plan, recordings, tmp_path / "out", ["16x9"], output="gif")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        export_plan(plan, recordings, tmp_path / "out", ["9x16"], x_offset=3.0)


class FakeFFmpeg:
    """Stands in for ffmpeg: records each command, writes its output, can fail on demand."""

    def __init__(self, fail_on: str | None = None):
        self.commands: list[list[str]] = []
        self.lists: list[str] = []
        self.fail_on = fail_on

    def __call__(self, cmd: list[str]) -> None:
        self.commands.append(cmd)
        dst = Path(cmd[-1])
        dst.write_bytes(b"partial")
        if "concat" in cmd:
            self.lists.append(Path(cmd[cmd.index("-i") + 1]).read_text(encoding="utf-8"))
        if self.fail_on and self.fail_on in dst.name:
            raise FFmpegError("encoder exploded")


@pytest.fixture
def fake(monkeypatch, recordings):
    def install(fail_on: str | None = None) -> FakeFFmpeg:
        runner = FakeFFmpeg(fail_on)
        monkeypatch.setattr(ffmpeg, "run", runner)
        monkeypatch.setattr(ffmpeg, "probe_recording", lambda path: Recording(path, 1.0, True))
        return runner

    return install


def test_progress_lines_and_plan_order(fake, recordings, tmp_path):
    runner = fake()
    for name in ("b_s0.mp4", "a_s0.mp4", "a_s1.mp4"):
        (recordings / name).write_bytes(b"")
    plan = Plan(META, [clip("b"), clip("a", pieces=2)])
    lines: list[str] = []

    export_plan(plan, recordings, tmp_path / "out", ["16x9", "9x16"], output="montage", log=lines.append)

    assert lines == [
        "Exporting b 16x9 (1/4)",
        "Exporting a 16x9 (2/4)",
        "Joining 2 clips into match_highlights_16x9.mp4",
        "Exporting b 9x16 (3/4)",
        "Exporting a 9x16 (4/4)",
        "Joining 2 clips into match_highlights_9x16.mp4",
    ]
    assert runner.lists[0] == "file 'b_16x9.mov'\nfile 'a_16x9.mov'\n"
    joined_a = runner.commands[1]
    assert [joined_a[i + 1] for i, x in enumerate(joined_a) if x == "-i"] == [
        str(recordings / "a_s0.mp4"),
        str(recordings / "a_s1.mp4"),
    ]
    assert not (tmp_path / "out" / PARTS_DIR).exists()


def test_failed_encode_is_reported_without_a_partial_file(fake, recordings, tmp_path):
    fake(fail_on="b_16x9")
    for name in ("a_s0.mp4", "b_s0.mp4"):
        (recordings / name).write_bytes(b"")
    out = tmp_path / "out"

    results = export_plan(Plan(META, [clip("a"), clip("b")]), recordings, out, ["16x9"], output="both")

    assert [(r.clip_id, r.error) for r in results] == [
        ("a", None),
        ("b", "encoder exploded"),
        (MONTAGE_ID, None),
    ]
    assert sorted(p.name for p in out.iterdir()) == ["a_16x9.mp4", "match_highlights_16x9.mp4"]


def test_montage_with_nothing_to_join_is_an_error(fake, recordings, tmp_path):
    runner = fake()

    results = export_plan(Plan(META, [clip("a")]), recordings, tmp_path / "out", ["16x9"], output="montage")

    assert [(r.clip_id, r.output) for r in results] == [("a", None), (MONTAGE_ID, None)]
    assert "no montage" in results[1].error
    assert runner.commands == []
    assert not (tmp_path / "out" / PARTS_DIR).exists()


def test_clips_mode_encodes_straight_to_mp4(fake, recordings, tmp_path):
    runner = fake()
    (recordings / "a_s0.mp4").write_bytes(b"")

    [result] = export_plan(Plan(META, [clip("a")]), recordings, tmp_path / "out", ["9x16"])

    assert result.output == tmp_path / "out" / "a_9x16.mp4"
    [cmd] = runner.commands
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert not (tmp_path / "out" / PARTS_DIR).exists()
