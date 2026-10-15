import shutil
import subprocess
import threading
import time
from pathlib import Path
from threading import Event

import pytest

import cs2hl.pipeline as pipeline
from cs2hl import ffmpeg
from cs2hl.capture import CaptureCancelled, CaptureResult
from cs2hl.capture.hlae import HlaeBackend
from cs2hl.export import ExportResult
from cs2hl.ffmpeg import EncodeSettings, Recording
from cs2hl.gui.workers import BackendConfig, build_hlae_backend
from cs2hl.models import Clip, DemoMeta, Plan
from cs2hl.pipeline import RenderOptions, render_plan

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def clip(clip_id: str) -> Clip:
    return Clip(
        id=clip_id,
        round_num=1,
        pov_steamid=1,
        pov_name="alpha",
        start_tick=0,
        end_tick=64,
        start_s=0.0,
        end_s=1.0,
        score=6.0,
        labels=("2K",),
        tags=(),
    )


PLAN = Plan(meta=DemoMeta(demo_name="m.dem", map_name="de_dust2", tickrate=64.0), clips=[clip("clip001_r01"), clip("clip002_r01")])


class CancelAfterFirst:
    """Writes the first recording, then behaves like a backend stopped by Cancel."""

    name = "fake"

    def render(self, plan, demo_path, out_dir, settings, log, cancel):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "clip001_r01.mp4").write_bytes(b"not really video")
        raise CaptureCancelled("stopped")


class FakeExport:
    """Stands in for export_plan: remembers what it was asked to export."""

    def __init__(self, results=None):
        self.calls: list[tuple[list[str], dict]] = []
        self.results = results or []

    def __call__(self, plan, recordings, out_dir, aspects, **kwargs):
        self.calls.append(([c.id for c in plan.clips], kwargs))
        return self.results


@pytest.fixture
def fake_export(monkeypatch):
    def install(results=None) -> FakeExport:
        export = FakeExport(results)
        monkeypatch.setattr(pipeline, "export_plan", export)
        return export

    return install


def test_cancel_still_exports_clips_already_on_disk(tmp_path, fake_export):
    export = fake_export()
    cancel = Event()
    cancel.set()  # a backend raises CaptureCancelled because the event is set
    log_lines: list[str] = []
    captures, exports = render_plan(
        PLAN,
        Path("m.dem"),
        CancelAfterFirst(),
        tmp_path,
        RenderOptions(aspects=("16x9",)),
        log_lines.append,
        cancel,
    )

    assert captures == []
    [(clips, kwargs)] = export.calls
    assert clips == ["clip001_r01"]  # only the clip that exists on disk
    assert kwargs["cancel"] is None  # the event is already set, so it cannot stop this export
    assert any("Stopped early" in line for line in log_lines)
    assert (tmp_path / "recordings" / "clip001_r01.mp4").exists()


def test_stale_recordings_of_the_plan_are_removed_before_recording(tmp_path, fake_export):
    export = fake_export()
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    for name in ("clip001_r01_s0.mp4", "clip002_r01_s0.mp4", "clip002_r01_s1.mkv", "clip009_r09_s0.mp4", "notes.txt"):
        (recordings / name).write_bytes(b"from an earlier run")
    seen: list[list[str]] = []

    class Records(CancelAfterFirst):
        def render(self, plan, demo_path, out_dir, settings, log, cancel):
            seen.append(sorted(p.name for p in out_dir.iterdir()))
            super().render(plan, demo_path, out_dir, settings, log, cancel)

    render_plan(PLAN, Path("m.dem"), Records(), tmp_path, RenderOptions(), lambda _: None, Event())

    assert seen == [["clip009_r09_s0.mp4", "notes.txt"]]
    [(clips, _)] = export.calls
    assert clips == ["clip001_r01"]  # the old clip002 files did not sneak into the export
    assert sorted(p.name for p in recordings.iterdir()) == ["clip001_r01.mp4", "clip009_r09_s0.mp4", "notes.txt"]


def test_old_outputs_are_removed_before_export_and_other_files_stay(tmp_path, fake_export):
    export = fake_export()
    (tmp_path / ".parts").mkdir()
    (tmp_path / ".parts" / "clip004_r09_16x9.mov").write_bytes(b"")
    old = ["clip001_r01_16x9.mp4", "clip004_r09_9x16.mp4", "m_highlights_16x9.mp4"]
    kept = ["notes.txt", "my_edit.mp4", "clip004_r09_16x9.mp4.bak", "other_highlights_16x9.mp4"]
    for name in old + kept:
        (tmp_path / name).write_bytes(b"old")

    class Records:
        name = "fake"

        def render(self, plan, demo_path, out_dir, settings, log, cancel):
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "clip001_r01_s0.mp4").write_bytes(b"x")
            return [CaptureResult("clip001_r01", path=out_dir / "clip001_r01_s0.mp4")]

    plan = Plan(meta=PLAN.meta, clips=[clip("clip001_r01")])
    render_plan(plan, Path("m.dem"), Records(), tmp_path, RenderOptions(), lambda _: None, Event())

    assert len(export.calls) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(kept)  # recordings removed: nothing failed


def test_nothing_is_exported_when_nothing_was_recorded(tmp_path, fake_export):
    export = fake_export()
    (tmp_path / "clip001_r01_16x9.mp4").write_bytes(b"from an earlier run")

    class StopsAtOnce:
        name = "fake"

        def render(self, plan, demo_path, out_dir, settings, log, cancel):
            out_dir.mkdir(parents=True, exist_ok=True)
            raise CaptureCancelled("stopped")

    lines: list[str] = []
    assert render_plan(PLAN, Path("m.dem"), StopsAtOnce(), tmp_path, RenderOptions(), lines.append, Event()) == ([], [])

    assert export.calls == []
    assert lines == ["Stopped before any clip was recorded."]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["clip001_r01_16x9.mp4"]  # empty recordings folder removed


def test_all_captures_failing_skips_the_export(tmp_path, fake_export):
    export = fake_export()
    failed = [CaptureResult("clip001_r01", error="CS2 crashed"), CaptureResult("clip002_r01", error="CS2 crashed")]

    class Fails:
        name = "fake"

        def render(self, plan, demo_path, out_dir, settings, log, cancel):
            return failed

    lines: list[str] = []
    captures, exports = render_plan(PLAN, Path("m.dem"), Fails(), tmp_path, RenderOptions(), lines.append, Event())

    assert (captures, exports) == (failed, [])
    assert export.calls == []
    assert lines == ["No clip was recorded, so there is nothing to export."]


def test_a_failed_capture_is_reported_once(tmp_path, monkeypatch):
    def fake_run(cmd, cancel=None):
        Path(cmd[-1]).write_bytes(b"video")

    monkeypatch.setattr(ffmpeg, "run", fake_run)
    monkeypatch.setattr(ffmpeg, "probe_recording", lambda path: Recording(path, 1.0, True))

    class HalfWorks:
        name = "fake"

        def render(self, plan, demo_path, out_dir, settings, log, cancel):
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "clip002_r01_s0.mp4").write_bytes(b"x")
            return [
                CaptureResult("clip001_r01", error="recording stopped early"),
                CaptureResult("clip002_r01", path=out_dir / "clip002_r01_s0.mp4"),
            ]

    captures, exports = render_plan(
        PLAN, Path("m.dem"), HalfWorks(), tmp_path, RenderOptions(output="both", aspects=("16x9",)), lambda _: None, Event()
    )

    assert [(c.clip_id, c.error) for c in captures if c.error] == [("clip001_r01", "recording stopped early")]
    assert [(e.clip_id, e.error) for e in exports] == [("clip002_r01", None), ("montage", None)]


def test_a_cancel_after_recording_skips_the_export_and_keeps_everything(tmp_path, fake_export):
    export = fake_export()
    (tmp_path / "m_highlights_16x9.mp4").write_bytes(b"from an earlier run")

    class CancelledAtTheEnd:
        name = "fake"

        def render(self, plan, demo_path, out_dir, settings, log, cancel):
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "clip001_r01_s0.mp4").write_bytes(b"x")
            cancel.set()
            return [CaptureResult("clip001_r01", path=out_dir / "clip001_r01_s0.mp4")]

    plan = Plan(meta=PLAN.meta, clips=[clip("clip001_r01")])
    captures, exports = render_plan(plan, Path("m.dem"), CancelledAtTheEnd(), tmp_path, RenderOptions(), lambda _: None, Event())

    assert [c.clip_id for c in captures] == ["clip001_r01"] and exports == []
    assert export.calls == []
    assert (tmp_path / "m_highlights_16x9.mp4").exists()
    assert (tmp_path / "recordings" / "clip001_r01_s0.mp4").exists()


def make_recording(path: Path, seconds: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=60", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", f"{seconds}", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
            "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
        ],
        check=True,
        timeout=120,
    )
    return path


@needs_ffmpeg
def test_cancel_during_export_stops_ffmpeg_and_keeps_the_recordings(tmp_path):
    source = make_recording(tmp_path / "source.mp4", 10)

    class Records:
        name = "fake"

        def render(self, plan, demo_path, out_dir, settings, log, cancel):
            out_dir.mkdir(parents=True, exist_ok=True)
            results = []
            for c in plan.clips:
                part = Path(shutil.copy(source, out_dir / f"{c.id}_s0.mp4"))
                results.append(CaptureResult(c.id, path=part, parts=(part,)))
            return results

    cancel = Event()
    timers: list[threading.Timer] = []
    pressed: list[float] = []
    lines: list[str] = []

    def press_cancel() -> None:
        pressed.append(time.monotonic())
        cancel.set()

    def log(line: str) -> None:
        lines.append(line)
        if line.startswith("Exporting") and not timers:
            timers.append(threading.Timer(1.0, press_cancel))
            timers[0].start()

    # Upscaling 10 s to 1080p with the slowest preset takes about a minute per clip.
    opts = RenderOptions(output="both", aspects=("16x9", "9x16"), encode=EncodeSettings(preset="veryslow"))
    out = tmp_path / "out"
    try:
        captures, exports = render_plan(PLAN, Path("m.dem"), Records(), out, opts, log, cancel)
    finally:
        for timer in timers:
            timer.cancel()
    stopped_after = time.monotonic() - pressed[0]

    assert stopped_after < 5.0
    assert [c.clip_id for c in captures] == ["clip001_r01", "clip002_r01"]
    assert exports == []  # nothing had finished
    assert [line for line in lines if line.startswith("Exporting")] == ["Exporting clip001_r01 16x9 (1/4)"]
    assert any(line.startswith("Cancelled. The raw recordings are kept") for line in lines)
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*")) == [
        "recordings",
        "recordings/clip001_r01_s0.mp4",
        "recordings/clip002_r01_s0.mp4",
    ]


def test_gui_backend_config_builds_the_hlae_backend(tmp_path):
    hlae = tmp_path / "HLAE" / "HLAE.exe"

    backend = build_hlae_backend(BackendConfig(cs2_dir=tmp_path, hlae_exe=hlae))

    assert isinstance(backend, HlaeBackend)
    assert backend.config.cs2_dir == tmp_path
    assert backend.config.hlae_exe == hlae


@pytest.mark.parametrize(("cs2", "hlae", "message"), [(None, "x.exe", "CS2 was not found"), ("cs2", None, "HLAE was not found")])
def test_gui_backend_config_needs_cs2_and_hlae(tmp_path, cs2, hlae, message):
    config = BackendConfig(cs2_dir=cs2 and tmp_path, hlae_exe=hlae and tmp_path / hlae)

    with pytest.raises(RuntimeError, match=message):
        build_hlae_backend(config)


def test_raw_recordings_are_removed_only_when_everything_worked(tmp_path, monkeypatch):
    class Records:
        name = "fake"

        def render(self, plan, demo_path, out_dir, settings, log, cancel):
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "clip001_r01_s0.mp4").write_bytes(b"x")
            return [CaptureResult("clip001_r01", path=out_dir / "clip001_r01_s0.mp4")]

    plan = Plan(meta=PLAN.meta, clips=[clip("clip001_r01")])
    results = {"export": [ExportResult("clip001_r01", "16x9", output=tmp_path / "a.mp4")]}
    monkeypatch.setattr(pipeline, "export_plan", lambda *a, **k: results["export"])

    render_plan(plan, Path("m.dem"), Records(), tmp_path / "ok", RenderOptions(), lambda _: None, Event())
    assert not (tmp_path / "ok" / "recordings").exists()

    results["export"] = [ExportResult("clip001_r01", "16x9", error="ffmpeg failed")]
    render_plan(plan, Path("m.dem"), Records(), tmp_path / "bad", RenderOptions(), lambda _: None, Event())
    assert (tmp_path / "bad" / "recordings" / "clip001_r01_s0.mp4").exists()

    results["export"] = [ExportResult("clip001_r01", "16x9", output=tmp_path / "a.mp4")]
    render_plan(plan, Path("m.dem"), Records(), tmp_path / "keep", RenderOptions(keep_recordings=True), lambda _: None, Event())
    assert (tmp_path / "keep" / "recordings").exists()


def test_plan_highlights_uses_every_death_for_clutches():
    from cs2hl.models import DemoData, Kill, Round
    from cs2hl.pipeline import HighlightOptions, plan_highlights
    from cs2hl.selection import Selection

    def kill(tick, attacker, victim, a_side, v_side):
        return Kill(tick=tick, round_num=1, attacker_steamid=attacker, attacker_name=f"p{attacker}",
                    attacker_side=a_side, victim_steamid=victim, victim_side=v_side)

    # T: 1, 2, 3 against CT: 11, 12. CT 11 kills T2, T3 dies to fall damage, T1 alone wins with two kills.
    data = DemoData(
        meta=PLAN.meta,
        rounds=[Round(number=1, start_tick=0, end_tick=5000, winner_side="T")],
        kills=[kill(500, 11, 2, "CT", "T"), kill(900, 1, 11, "T", "CT"), kill(1000, 1, 12, "T", "CT")],
        rosters={1: {1: "T", 2: "T", 3: "T", 11: "CT", 12: "CT"}},
        player_names={},
        deaths=((500, 1, 2), (700, 1, 3), (900, 1, 11), (1000, 1, 12)),
    )

    plan = plan_highlights(data, Selection.of(players=[1]), HighlightOptions())

    assert [c.labels for c in plan.clips] == [("2K + 1v2",)]

