from pathlib import Path
from threading import Event

import pytest

from cs2hl.capture import CaptureCancelled
from cs2hl.capture.hlae import HlaeBackend
from cs2hl.gui.workers import BackendConfig, build_hlae_backend
from cs2hl.models import Clip, DemoMeta, Plan
from cs2hl.pipeline import RenderOptions, render_plan


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


def test_cancel_still_exports_clips_already_on_disk(tmp_path, monkeypatch):
    import cs2hl.pipeline as pipeline

    seen = []

    def fake_export(plan, recordings, out_dir, aspects, **kwargs):
        seen.append([c.id for c in plan.clips])
        return []

    monkeypatch.setattr(pipeline, "export_plan", fake_export)
    log_lines: list[str] = []
    captures, exports = render_plan(
        PLAN,
        Path("m.dem"),
        CancelAfterFirst(),
        tmp_path,
        RenderOptions(aspects=("16x9",)),
        log_lines.append,
        Event(),
    )

    assert captures == []
    assert seen == [["clip001_r01"]]  # only the clip that exists on disk
    assert any("Stopped early" in line for line in log_lines)


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
    import cs2hl.pipeline as pipeline
    from cs2hl.capture import CaptureResult
    from cs2hl.export import ExportResult

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
