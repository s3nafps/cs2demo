import os
import threading
import time
from pathlib import Path
from threading import Event

import pytest

from cs2hl.capture import hlae
from cs2hl.capture.base import CaptureCancelled, CaptureResult, CaptureSettings
from cs2hl.capture.hlae import (
    PRESET_NAME,
    PRESET_OPTIONS,
    HlaeBackend,
    HlaeConfig,
    build_session_cfg,
    demo_stem,
    ffmpeg_preset_line,
    hlae_launch_command,
    is_settled,
    newest_take_video,
    record_name,
    select_clips,
    split_sessions,
    stage_demo,
)
from cs2hl.models import Clip, DemoMeta, Plan

EXPECTED_TWO_CLIPS = [
    r'mirv_streams settings add ffmpeg cs2hl_h264 "-c:v libx264 -preset slow -crf 16 -pix_fmt yuv420p -movflags +faststart {QUOTE}{AFX_STREAM_PATH}\\video.mp4{QUOTE}"',
    "mirv_streams record fps 60",
    "mirv_streams record screen enabled 1",
    "mirv_streams record screen settings cs2hl_h264",
    "mirv_cmd clear",
    "playdemo cs2hl/match",
    r"mirv_cmd addAtTick 1000 mirv_streams record name ..\..\csgo\cs2hl\work\clip001_r03",
    "mirv_cmd addAtTick 1000 mirv_streams record start",
    "mirv_cmd addAtTick 2000 mirv_streams record end",
    "mirv_cmd addAtTick 2001 demo_gototick 5000",
    r"mirv_cmd addAtTick 5000 mirv_streams record name ..\..\csgo\cs2hl\work\clip002_r07",
    "mirv_cmd addAtTick 5000 mirv_streams record start",
    "mirv_cmd addAtTick 6000 mirv_streams record end",
    "mirv_cmd addAtTick 6001 quit",
]


def make_clip(clip_id: str, start: int, end: int) -> Clip:
    return Clip(
        id=clip_id,
        round_num=1,
        pov_steamid=76561198000000001,
        pov_name="player",
        start_tick=start,
        end_tick=end,
        start_s=start / 64,
        end_s=end / 64,
        score=10.0,
        labels=("3K",),
        tags=(),
    )


def make_plan(*clips: Clip) -> Plan:
    return Plan(meta=DemoMeta(demo_name="match.dem", map_name="de_mirage", tickrate=64.0), clips=list(clips))


def expand_like_hlae(options: str, stream_path: str) -> str:
    """Mirror of ReplaceAllW and the placeholder map in shared/AfxOutStreams.cpp (earliest match first)."""
    replacements = {
        "{AFX_STREAM_PATH}": stream_path,
        "{QUOTE}": '"',
        "\\{": "{",
        "\\}": "}",
        "\\\\": "\\",
    }
    text = options
    start = 0
    while True:
        best = None
        for key in sorted(replacements):
            pos = text.find(key, start)
            if pos != -1 and (best is None or pos < best[0]):
                best = (pos, key)
        if best is None:
            return text
        pos, key = best
        value = replacements[key]
        text = text[:pos] + value + text[pos + len(key):]
        start = pos + len(value)


@pytest.fixture
def cs2_dir(tmp_path: Path) -> Path:
    root = tmp_path / "Counter-Strike Global Offensive"
    (root / "game" / "csgo").mkdir(parents=True)
    return root


@pytest.fixture
def demo(tmp_path: Path) -> Path:
    path = tmp_path / "my match.dem"
    path.write_bytes(b"DEMO")
    return path


def test_session_cfg_for_two_clips_has_expected_lines_in_order():
    clips = [make_clip("clip002_r07", 5000, 6000), make_clip("clip001_r03", 1000, 2000)]
    text = build_session_cfg(clips, "match", 60)
    assert text == "\n".join(EXPECTED_TWO_CLIPS) + "\n"


def test_preset_line_matches_builtin_escaping():
    assert ffmpeg_preset_line() == f'mirv_streams settings add ffmpeg {PRESET_NAME} "{PRESET_OPTIONS}"'
    assert PRESET_OPTIONS.count("\\\\video.mp4") == 1
    assert not PRESET_NAME.lower().startswith("afx")


def test_preset_expands_to_single_backslash_output_path():
    expanded = expand_like_hlae(PRESET_OPTIONS, r"C:\cs2\game\csgo\cs2hl\work\clip001_r03\take0000")
    assert expanded.endswith(r'"C:\cs2\game\csgo\cs2hl\work\clip001_r03\take0000\video.mp4"')
    assert "-crf 16" in expanded and "-pix_fmt yuv420p" in expanded


def test_pov_line_only_when_slot_is_set():
    clips = [make_clip("a", 1000, 2000), make_clip("b", 3000, 4000)]
    assert "spec_player" not in build_session_cfg(clips, "match", 60)
    lines = build_session_cfg(clips, "match", 60, pov_slot=3).splitlines()
    pov = "mirv_cmd addAtTick 1000 spec_player 3"
    assert lines.count(pov) == 1
    assert lines.index(pov) < lines.index("mirv_cmd addAtTick 1000 mirv_streams record name ..\\..\\csgo\\cs2hl\\work\\a")


def test_seek_is_written_only_when_next_clip_starts_after_one_tick_past_end():
    adjacent = build_session_cfg([make_clip("a", 1000, 2000), make_clip("b", 2001, 3000)], "match", 60)
    assert "demo_gototick" not in adjacent
    assert "mirv_cmd addAtTick 2001 mirv_streams record start" in adjacent

    gap = build_session_cfg([make_clip("a", 1000, 2000), make_clip("b", 2002, 3000)], "match", 60)
    assert "mirv_cmd addAtTick 2001 demo_gototick 2002" in gap
    assert gap.rstrip().endswith("mirv_cmd addAtTick 3001 quit")


def test_build_rejects_overlap_and_empty_clips():
    with pytest.raises(ValueError, match="overlaps"):
        build_session_cfg([make_clip("a", 1000, 2000), make_clip("b", 1500, 2500)], "match", 60)
    with pytest.raises(ValueError, match="no length"):
        build_session_cfg([make_clip("a", 1000, 1000)], "match", 60)


def test_select_clips_keeps_first_of_overlapping_group():
    a = make_clip("a", 1000, 2000)
    b = make_clip("b", 1500, 2500)
    c = make_clip("c", 2600, 3000)
    touching = make_clip("d", 3000, 3500)
    kept, skipped = select_clips([touching, c, b, a])
    assert [clip.id for clip in kept] == ["a", "c"]
    assert [clip.id for clip in skipped] == ["b", "d"]


def test_record_name_rejects_spaces_and_quotes():
    assert record_name("clip001_r03") == r"..\..\csgo\cs2hl\work\clip001_r03"
    with pytest.raises(ValueError):
        record_name("clip 1")
    with pytest.raises(ValueError):
        record_name('clip"1')


def test_demo_stem_is_safe_for_console_paths():
    assert demo_stem(Path("My Match (1).dem")) == "My_Match__1_"
    assert demo_stem(Path("match.dem")) == "match"


def test_hlae_launch_command_uses_custom_loader(tmp_path):
    hlae_exe = tmp_path / "HLAE" / "HLAE.exe"
    cs2_exe = tmp_path / "cs2.exe"
    assert hlae_launch_command(hlae_exe, cs2_exe) == [
        str(hlae_exe),
        "-customLoader",
        "-programPath", str(cs2_exe),
        "-cmdLine", "-steam -insecure",
        "-hookDllPath", str(tmp_path / "HLAE" / "x64" / "AfxHookSource2.dll"),
        "-autoStart",
    ]


def test_config_derives_cs2_paths(tmp_path):
    config = HlaeConfig(cs2_dir=tmp_path / "CS2")
    assert config.cs2_exe == tmp_path / "CS2" / "game" / "bin" / "win64" / "cs2.exe"
    assert config.csgo_dir == tmp_path / "CS2" / "game" / "csgo"


def test_stage_demo_copies_and_skips_identical_copy(tmp_path):
    src = tmp_path / "match.dem"
    src.write_bytes(b"abc")
    dest_dir = tmp_path / "cs2hl"
    dest_dir.mkdir()
    dest = dest_dir / "match.dem"
    dest.write_bytes(b"xyz")
    os.utime(dest, (src.stat().st_atime, src.stat().st_mtime))

    assert stage_demo(src, dest_dir) == dest
    assert dest.read_bytes() == b"xyz"

    os.utime(dest, (src.stat().st_atime, src.stat().st_mtime - 100))
    stage_demo(src, dest_dir)
    assert dest.read_bytes() == b"abc"


def test_newest_take_video_prefers_latest_take(tmp_path):
    old = tmp_path / "take0000"
    new = tmp_path / "take0001"
    for take in (old, new):
        take.mkdir()
        (take / "video.mp4").write_bytes(b"x")
    os.utime(old / "video.mp4", (1000, 1000))
    os.utime(new / "video.mp4", (2000, 2000))
    assert newest_take_video(tmp_path) == new / "video.mp4"
    assert newest_take_video(tmp_path / "missing") is None


def test_is_settled_needs_same_nonzero_size_twice():
    seen: dict[str, int] = {}
    assert not is_settled(seen, "a", 0)
    assert not is_settled(seen, "a", 0)
    assert not is_settled(seen, "a", 10)
    assert not is_settled(seen, "a", 12)
    assert is_settled(seen, "a", 12)


def test_render_copies_demo_and_writes_session_cfg(tmp_path, cs2_dir, demo):
    logs: list[str] = []
    backend = HlaeBackend(HlaeConfig(cs2_dir=cs2_dir, session_timeout_s=0.05, poll_interval_s=0.01))
    out_dir = tmp_path / "My Out"
    results = backend.render(
        make_plan(make_clip("clip001_r03", 1000, 2000)),
        demo,
        out_dir,
        CaptureSettings(),
        logs.append,
        Event(),
    )

    cs2hl = cs2_dir / "game" / "csgo" / "cs2hl"
    assert (cs2hl / "my_match.dem").read_bytes() == b"DEMO"
    cfg = (cs2hl / "session.cfg").read_text(encoding="utf-8")
    assert "playdemo cs2hl/my_match" in cfg
    assert "Counter-Strike" not in cfg
    assert any("exec cs2hl/session" in line for line in logs)
    assert len(results) == 1
    assert results[0].clip_id == "clip001_r03"
    assert results[0].path is None and results[0].error


def test_finished_take_is_moved_and_missing_take_reports_error(tmp_path, cs2_dir, demo):
    cs2hl = cs2_dir / "game" / "csgo" / "cs2hl"
    cfg_path = cs2hl / "session.cfg"
    take = cs2hl / "work" / "clip001_r03" / "take0000"

    def record_first_clip():
        deadline = time.monotonic() + 5
        while not cfg_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        take.mkdir(parents=True)
        (take / "video.mp4").write_bytes(b"MP4DATA")

    recorder = threading.Thread(target=record_first_clip, daemon=True)
    recorder.start()
    backend = HlaeBackend(HlaeConfig(cs2_dir=cs2_dir, session_timeout_s=1.0, poll_interval_s=0.01))
    out_dir = tmp_path / "out"
    results = backend.render(
        make_plan(make_clip("clip001_r03", 1000, 2000), make_clip("clip002_r07", 3000, 4000)),
        demo,
        out_dir,
        CaptureSettings(),
        lambda _: None,
        Event(),
    )
    recorder.join()

    assert results[0] == CaptureResult("clip001_r03", path=out_dir / "clip001_r03.mp4")
    assert (out_dir / "clip001_r03.mp4").read_bytes() == b"MP4DATA"
    assert not (take / "video.mp4").exists()
    assert results[1].clip_id == "clip002_r07"
    assert results[1].path is None
    assert "no recording" in results[1].error


def test_cancel_raises_capture_cancelled(tmp_path, cs2_dir, demo):
    cancel = Event()
    cancel.set()
    backend = HlaeBackend(HlaeConfig(cs2_dir=cs2_dir, session_timeout_s=5, poll_interval_s=0.01))
    with pytest.raises(CaptureCancelled):
        backend.render(make_plan(make_clip("clip001_r03", 1000, 2000)), demo, tmp_path / "out", CaptureSettings(), lambda _: None, cancel)


def test_launch_command_is_started_when_given(tmp_path, cs2_dir, demo, monkeypatch):
    started: list[list[str]] = []
    monkeypatch.setattr(hlae.subprocess, "Popen", lambda cmd, **kwargs: started.append(cmd))
    backend = HlaeBackend(
        HlaeConfig(cs2_dir=cs2_dir, launch_command=["hlae.exe", "-autoStart"], session_timeout_s=0.05, poll_interval_s=0.01)
    )
    backend.render(make_plan(make_clip("clip001_r03", 1000, 2000)), demo, tmp_path / "out", CaptureSettings(), lambda _: None, Event())
    assert started == [["hlae.exe", "-autoStart"]]


def test_launch_failure_gives_error_for_every_clip(tmp_path, cs2_dir, demo, monkeypatch):
    def refuse(cmd, **kwargs):
        raise FileNotFoundError("hlae.exe not found")

    monkeypatch.setattr(hlae.subprocess, "Popen", refuse)
    backend = HlaeBackend(HlaeConfig(cs2_dir=cs2_dir, launch_command=["hlae.exe"], poll_interval_s=0.01))
    results = backend.render(
        make_plan(make_clip("clip001_r03", 1000, 2000), make_clip("clip002_r07", 3000, 4000)),
        demo,
        tmp_path / "out",
        CaptureSettings(),
        lambda _: None,
        Event(),
    )
    assert [r.clip_id for r in results] == ["clip001_r03", "clip002_r07"]
    assert all(r.path is None and "could not start hlae.exe" in r.error for r in results)


def test_missing_cs2_folder_gives_errors_without_raising(tmp_path, demo):
    backend = HlaeBackend(HlaeConfig(cs2_dir=tmp_path / "nope", poll_interval_s=0.01))
    results = backend.render(make_plan(make_clip("clip001_r03", 1000, 2000)), demo, tmp_path / "out", CaptureSettings(), lambda _: None, Event())
    assert results[0].path is None
    assert "CS2 folder not found" in results[0].error


def test_empty_plan_returns_nothing(tmp_path, cs2_dir, demo):
    logs: list[str] = []
    backend = HlaeBackend(HlaeConfig(cs2_dir=cs2_dir))
    assert backend.render(make_plan(), demo, tmp_path / "out", CaptureSettings(), logs.append, Event()) == []
    assert logs == ["No clips to record."]


def test_split_sessions_puts_overlapping_clips_in_later_sessions():
    a = make_clip("a", 0, 100)
    b = make_clip("b", 50, 150)  # overlaps a
    c = make_clip("c", 200, 300)
    d = make_clip("d", 120, 220)  # overlaps b and c, free of a

    sessions = [[m.id for m in batch] for batch in split_sessions([a, b, c, d])]

    assert sessions == [["a", "d"], ["b", "c"]]  # sorted by start: a, b, d, c


def test_split_sessions_is_one_session_when_nothing_overlaps():
    sessions = split_sessions([make_clip("a", 0, 10), make_clip("b", 20, 30)])

    assert [len(batch) for batch in sessions] == [2]
