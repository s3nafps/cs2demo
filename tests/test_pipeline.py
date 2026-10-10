from pathlib import Path
from threading import Event

import pytest

from cs2hl.capture import CaptureCancelled
from cs2hl.capture.hlae import HlaeBackend
from cs2hl.gui.options import HlaeConfig as GuiHlaeConfig
from cs2hl.gui.workers import build_hlae_backend
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


def test_gui_hlae_config_maps_to_capture_config(tmp_path):
    (tmp_path / "game" / "csgo").mkdir(parents=True)
    gui = GuiHlaeConfig(cs2_dir=tmp_path, hlae_exe=None, launch_command=("HLAE.exe", "-autoStart"))

    backend = build_hlae_backend(gui)

    assert isinstance(backend, HlaeBackend)
    assert backend.config.cs2_dir == tmp_path
    assert backend.config.launch_command == ["HLAE.exe", "-autoStart"]


def test_gui_hlae_config_without_launch_command_is_manual(tmp_path):
    gui = GuiHlaeConfig(cs2_dir=tmp_path, hlae_exe=None, launch_command=())

    assert build_hlae_backend(gui).config.launch_command is None


def test_gui_hlae_config_requires_a_cs2_folder():
    with pytest.raises(ValueError, match="CS2 folder"):
        build_hlae_backend(GuiHlaeConfig(cs2_dir=None, hlae_exe=None, launch_command=()))
