"""Desktop UI tests. Headless: offscreen Qt with fake pipeline functions, so no CS2, ffmpeg or demo file."""

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

import cs2hl.gui.app as app_module
from cs2hl.capture import CaptureResult
from cs2hl.gui.app import MainWindow
from cs2hl.gui.options import GuiOptions, highlight_options, render_options, render_problem, split_command
from cs2hl.gui.prefs import load_options, save_options
from cs2hl.gui.tables import clock, span
from cs2hl.models import Clip, DemoData, DemoMeta, Kill, Plan, Round
from cs2hl.pipeline import HighlightOptions, RenderOptions, plan_highlights
from cs2hl.planner import load_plan
from cs2hl.scan import summarize
from cs2hl.selection import Selection

NAMES = {1: "alpha", 2: "bravo", 11: "carl", 12: "dave"}


@pytest.fixture(autouse=True)
def _no_modal_boxes(monkeypatch):
    """A modal box blocks forever in offscreen mode, so record the message instead of showing it."""
    from PySide6.QtWidgets import QMessageBox

    shown: list[str] = []

    def record(*args):
        shown.append(args[-1] if len(args) > 2 else str(args))
        return QMessageBox.StandardButton.Ok

    for name in ("critical", "warning", "information", "question"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(record))
    return shown


def fake_cs2(root):
    """A CS2 folder as the app expects it: it must contain game/csgo."""
    (root / "game" / "csgo").mkdir(parents=True, exist_ok=True)
    return root


def kill(tick, rnd, attacker, victim, a_side, v_side):
    return Kill(
        tick=tick,
        round_num=rnd,
        attacker_steamid=attacker,
        attacker_name=NAMES[attacker],
        attacker_side=a_side,
        victim_steamid=victim,
        victim_side=v_side,
    )


def make_demo() -> DemoData:
    rounds = [
        Round(number=1, start_tick=0, end_tick=3000, winner_side="T"),
        Round(number=2, start_tick=6400, end_tick=9600, winner_side="CT"),
        Round(number=3, start_tick=12800, end_tick=16000, winner_side="T"),
    ]
    rosters = {
        1: {1: "T", 2: "T", 11: "CT", 12: "CT"},
        2: {1: "CT", 2: "CT", 11: "T", 12: "T"},
        3: {1: "T", 2: "T", 11: "CT", 12: "CT"},
    }
    kills = [
        kill(100, 1, 1, 11, "T", "CT"),
        kill(200, 1, 1, 12, "T", "CT"),
        kill(300, 1, 11, 2, "CT", "T"),
        kill(7000, 2, 12, 2, "T", "CT"),
        kill(13000, 3, 2, 11, "T", "CT"),
    ]
    meta = DemoMeta(demo_name="match.dem", map_name="de_mirage", tickrate=64.0)
    return DemoData(meta=meta, rounds=rounds, kills=kills, rosters=rosters, player_names=NAMES)


def fake_scan(path):
    return summarize(make_demo())


def make_clip(index: int, round_num: int = 1, pov: int = 1) -> Clip:
    start = 10.0 * index
    return Clip(
        id=f"r{round_num}_p{pov}_{index}",
        round_num=round_num,
        pov_steamid=pov,
        pov_name=NAMES[pov],
        start_tick=int(start * 64),
        end_tick=int((start + 8.0) * 64),
        start_s=start,
        end_s=start + 8.0,
        score=7.5 + index,
        labels=("3K",),
        tags=("opening",),
    )


class FakePlanner:
    def __init__(self, clips: int) -> None:
        self.clip_count = clips
        self.calls: list[tuple[Selection, HighlightOptions]] = []

    def __call__(self, data, selection, opts):
        self.calls.append((selection, opts))
        return Plan(meta=data.meta, clips=[make_clip(i) for i in range(self.clip_count)])


class FakeRenderer:
    def __init__(self, lines=(), wait_for_cancel=False) -> None:
        self.lines = list(lines)
        self.wait_for_cancel = wait_for_cancel
        self.calls: list[tuple] = []

    def __call__(self, plan, demo_path, backend, out_dir, opts, log, cancel):
        self.calls.append((plan, demo_path, backend, out_dir, opts, cancel))
        for line in self.lines:
            log(line)
        if self.wait_for_cancel:
            cancel.wait(10)
        results = [CaptureResult(clip_id=c.id, path=out_dir / "recordings" / f"{c.id}.mp4") for c in plan.clips]
        return results, []


class FakeBackend:
    name = "fake-hlae"


def make_factory(backend, configs):
    def factory(cfg):
        configs.append(cfg)
        return backend

    return factory


class FakeMessageBox:
    def __init__(self, shown: list) -> None:
        self.shown = shown

    def critical(self, parent, title, text, *args, **kwargs):
        self.shown.append((title, text))


class BrokenSettings:
    def contains(self, key):
        raise RuntimeError("store is broken")

    def value(self, key):
        raise RuntimeError("store is broken")

    def setValue(self, key, value):
        raise RuntimeError("store is broken")

    def sync(self):
        raise RuntimeError("store is broken")


def wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the background job")
        QApplication.processEvents()
        time.sleep(0.005)
    QApplication.processEvents()


def load(window: MainWindow) -> MainWindow:
    window.open_demo(Path("match.dem"))
    wait_until(lambda: window._scan is not None and window._thread is None)
    return window


def wait_idle(window: MainWindow) -> None:
    wait_until(lambda: window._thread is None)


def tick_player(window: MainWindow, steamid: int, checked: bool = True) -> None:
    model = window._players
    row = [p.steamid for p in model.rows()].index(steamid)
    state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
    model.setData(model.index(row, 0), state, Qt.ItemDataRole.CheckStateRole)


def untick_round(window: MainWindow, number: int) -> None:
    model = window._rounds
    row = [r.number for r in model.rows()].index(number)
    model.setData(model.index(row, 0), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)


def configure(window: MainWindow, **changes) -> None:
    window._panel.set_values(replace(window._panel.values(), **changes))


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(qapp):
    store = QSettings(f"cs2hl-test-{uuid.uuid4().hex}", "cs2hl-gui")
    yield store
    path = Path(store.fileName())
    store.clear()
    store.sync()
    try:
        path.unlink()
        path.parent.rmdir()
    except OSError:
        pass


@pytest.fixture
def make_window(qapp, settings):
    windows: list[MainWindow] = []

    def factory(**fns) -> MainWindow:
        window = MainWindow(settings=settings, **fns)
        windows.append(window)
        return window

    yield factory
    for window in windows:
        wait_idle(window)
        window.close()


def test_window_starts_with_defaults_and_nothing_loaded(make_window):
    window = make_window(scan_fn=fake_scan)

    assert window.windowTitle() == "cs2hl"
    assert not window._preview_btn.isEnabled()
    assert not window._render_btn.isEnabled()
    assert not window._cancel_btn.isEnabled()


def test_scan_defaults_all_rounds_in_and_no_players(make_window):
    window = load(make_window(scan_fn=fake_scan))

    assert window._rounds.checked_keys() == [1, 2, 3]
    assert window._players.checked_keys() == []
    assert not window._preview_btn.isEnabled()
    assert not window._render_btn.isEnabled()


def test_selecting_one_player_enables_generate_and_deselecting_disables_it(make_window):
    window = load(make_window(scan_fn=fake_scan))

    tick_player(window, 1)
    assert window._preview_btn.isEnabled() and window._render_btn.isEnabled()

    tick_player(window, 1, checked=False)
    assert not window._preview_btn.isEnabled() and not window._render_btn.isEnabled()


def test_all_and_none_buttons_change_the_player_table(make_window):
    window = load(make_window(scan_fn=fake_scan))
    players_all, players_none = window._table_buttons[2], window._table_buttons[3]

    players_all.click()
    assert len(window._players.checked_keys()) == 4
    assert window._preview_btn.isEnabled()

    players_none.click()
    assert window._players.checked_keys() == []
    assert not window._preview_btn.isEnabled()


def test_scan_runs_on_a_worker_thread_and_fills_the_tables(make_window):
    threads: list[int] = []

    def scan(path):
        threads.append(threading.get_ident())
        return summarize(make_demo())

    window = load(make_window(scan_fn=scan))

    assert threads and threads[0] != threading.get_ident()
    assert [p.name for p in window._players.rows()] == ["alpha", "bravo", "carl", "dave"]
    assert "Rounds 3" in window._summary.text() and "Players 4" in window._summary.text()
    assert "de_mirage" in window._summary.text() and "4:10" in window._summary.text()
    assert window._rounds.data(window._rounds.index(0, 2)) == "0:00 - 0:47"
    assert window._rounds.data(window._rounds.index(0, 4)) == "3"
    assert window._rounds.headerData(0, Qt.Orientation.Horizontal) == "Include"
    assert window._players.data(window._players.index(0, 3)) == "2"


def test_failed_scan_shows_an_error_box_and_leaves_the_window_usable(make_window, monkeypatch):
    shown: list = []
    monkeypatch.setattr(app_module, "QMessageBox", FakeMessageBox(shown))

    def broken(path):
        raise RuntimeError("not a demo file")

    window = make_window(scan_fn=broken)
    window.open_demo(Path("bad.dem"))
    wait_idle(window)

    assert window._scan is None
    assert shown == [("Scan failed", "not a demo file")]
    assert not window._preview_btn.isEnabled()
    assert window._open_btn.isEnabled()


def test_failure_handler_reports_without_crashing(make_window, monkeypatch):
    shown: list = []
    monkeypatch.setattr(app_module, "QMessageBox", FakeMessageBox(shown))
    window = make_window(scan_fn=fake_scan)

    window._job_kind = "scan"
    window._on_failed("boom")

    assert shown == [("Scan failed", "boom")]
    assert "boom" in window._log_view.toPlainText()


def test_preview_fills_the_clip_table_and_uses_the_chosen_selection(make_window):
    planner = FakePlanner(clips=3)
    window = load(make_window(scan_fn=fake_scan, plan_fn=planner))
    tick_player(window, 1)
    tick_player(window, 2)
    untick_round(window, 3)

    window._preview_btn.click()
    wait_idle(window)

    assert window._clips.rowCount() == 3
    assert window._clip_count.text() == "3 clip(s)"
    selection, _ = planner.calls[0]
    assert selection.player_steamids == frozenset({1, 2})
    assert selection.round_numbers == frozenset({1, 2})


def test_preview_with_the_real_planner_matches_a_direct_call(make_window):
    window = load(make_window(scan_fn=fake_scan))
    tick_player(window, 1)

    window._preview_btn.click()
    wait_idle(window)

    expected = plan_highlights(
        make_demo(),
        Selection.of(players=[1], rounds=[1, 2, 3]),
        highlight_options(window._panel.values()),
    )
    assert window._clips.rowCount() == len(expected.clips)


def test_render_passes_the_aspects_and_selection_the_user_picked(make_window, tmp_path):
    planner = FakePlanner(clips=2)
    renderer = FakeRenderer(lines=["recording r1_p1_0"])
    backend = FakeBackend()
    configs: list = []
    window = load(
        make_window(
            scan_fn=fake_scan,
            plan_fn=planner,
            render_fn=renderer,
            backend_factory=make_factory(backend, configs),
        )
    )
    tick_player(window, 1)
    untick_round(window, 3)
    configure(window, output_dir=str(tmp_path / "out"), backend="hlae", cs2_dir=str(fake_cs2(tmp_path)))
    window._panel._wide.setChecked(False)

    window._render_btn.click()
    wait_idle(window)

    selection, _ = planner.calls[0]
    assert selection.player_steamids == frozenset({1})
    assert selection.round_numbers == frozenset({1, 2})
    plan, demo_path, used_backend, out_dir, opts, cancel = renderer.calls[0]
    assert isinstance(opts, RenderOptions)
    assert opts.aspects == ("9x16",)
    assert used_backend is backend
    assert configs[0].cs2_dir == tmp_path
    assert demo_path == Path("match.dem")
    assert out_dir == tmp_path / "out"
    assert len(plan.clips) == 2
    assert not cancel.is_set()
    log = window._log_view.toPlainText()
    assert "recording r1_p1_0" in log
    assert "Recorded 2 of 2" in log


def test_plan_only_writes_the_plan_json_and_does_not_record(make_window, tmp_path):
    renderer = FakeRenderer()
    window = load(make_window(scan_fn=fake_scan, plan_fn=FakePlanner(clips=2), render_fn=renderer))
    tick_player(window, 2)
    configure(window, output_dir=str(tmp_path))

    window._render_btn.click()
    wait_idle(window)

    assert renderer.calls == []
    assert len(load_plan(tmp_path / "match.plan.json").clips) == 2
    assert "Nothing was recorded" in window._log_view.toPlainText()


def test_render_refuses_to_start_without_an_aspect(make_window, tmp_path, monkeypatch):
    shown: list = []
    monkeypatch.setattr(app_module, "QMessageBox", FakeMessageBox(shown))
    renderer = FakeRenderer()
    window = load(make_window(scan_fn=fake_scan, plan_fn=FakePlanner(clips=1), render_fn=renderer))
    tick_player(window, 1)
    configure(window, output_dir=str(tmp_path), aspects=())

    window._render_btn.click()

    assert renderer.calls == []
    assert window._thread is None
    assert shown and shown[0][0] == "Cannot render"


def test_cancel_sets_the_event_that_is_passed_to_render(make_window, tmp_path):
    renderer = FakeRenderer(wait_for_cancel=True)
    configs: list = []
    window = load(
        make_window(
            scan_fn=fake_scan,
            plan_fn=FakePlanner(clips=1),
            render_fn=renderer,
            backend_factory=make_factory(FakeBackend(), configs),
        )
    )
    tick_player(window, 1)
    configure(window, output_dir=str(tmp_path), backend="hlae", cs2_dir=str(fake_cs2(tmp_path)))

    window._render_btn.click()
    wait_until(lambda: renderer.calls)
    assert window._cancel_btn.isEnabled()
    window._cancel_btn.click()
    wait_idle(window)

    cancel = renderer.calls[0][-1]
    assert cancel.is_set()
    assert not window._cancel_btn.isEnabled()
    assert "Cancelled" in window._log_view.toPlainText()


def test_closing_during_a_render_cancels_it_and_closes_when_it_stops(make_window, tmp_path):
    renderer = FakeRenderer(wait_for_cancel=True)
    window = load(
        make_window(
            scan_fn=fake_scan,
            plan_fn=FakePlanner(clips=1),
            render_fn=renderer,
            backend_factory=make_factory(FakeBackend(), []),
        )
    )
    tick_player(window, 1)
    configure(window, output_dir=str(tmp_path), backend="hlae", cs2_dir=str(fake_cs2(tmp_path)))
    window._render_btn.click()
    wait_until(lambda: renderer.calls)

    window.close()
    assert window._close_requested
    assert renderer.calls[0][-1].is_set()
    wait_idle(window)


def test_output_folder_is_restored_from_settings(make_window, settings, tmp_path):
    first = make_window(scan_fn=fake_scan)
    configure(first, output_dir=str(tmp_path))
    first.close()

    second = make_window(scan_fn=fake_scan)

    assert second._panel.values().output_dir == str(tmp_path)


def test_options_round_trip_through_settings(settings, tmp_path):
    opts = GuiOptions(
        output_dir=str(tmp_path),
        aspects=("9x16",),
        vertical_mode="fit",
        x_offset=-0.5,
        short_side=1440,
        min_score=7.5,
        pre_roll_s=3.0,
        post_roll_s=1.0,
        max_clips=12,
        backend="hlae",
        capture_width=2560,
        capture_height=1440,
        cs2_dir=str(fake_cs2(tmp_path)),
        hlae_exe="",
        launch_command="-start -insecure",
    )

    save_options(settings, opts)

    assert load_options(settings) == opts


def test_broken_settings_store_never_crashes(qapp):
    assert load_options(BrokenSettings()) == GuiOptions()
    save_options(BrokenSettings(), GuiOptions())
    window = MainWindow(settings=BrokenSettings())
    window.close()


def test_mapping_form_values_onto_pipeline_options():
    opts = GuiOptions(
        output_dir="x",
        aspects=("9x16",),
        vertical_mode="fit",
        x_offset=0.5,
        short_side=1440,
        min_score=6.0,
        pre_roll_s=3.0,
        post_roll_s=1.5,
        max_clips=0,
        capture_width=2560,
        capture_height=1440,
    )

    assert highlight_options(opts) == HighlightOptions(pre_roll_s=3.0, post_roll_s=1.5, min_score=6.0, max_clips=None)
    assert highlight_options(replace(opts, max_clips=3)).max_clips == 3
    rendered = render_options(opts)
    assert rendered.aspects == ("9x16",) and rendered.vertical_mode == "fit" and rendered.x_offset == 0.5
    assert rendered.encode.short_side == 1440
    assert (rendered.capture.width, rendered.capture.height) == (2560, 1440)


def test_render_problem_names_the_first_missing_setting(tmp_path):
    base = GuiOptions(output_dir=str(tmp_path))
    hlae = replace(base, backend="hlae")

    assert render_problem(base) is None
    assert "aspect" in render_problem(replace(base, aspects=())).lower()
    assert "output" in render_problem(replace(base, output_dir="")).lower()
    assert "CS2 folder" in render_problem(hlae)
    assert render_problem(replace(hlae, cs2_dir=str(fake_cs2(tmp_path)))) is None
    assert "could not be parsed" in render_problem(replace(hlae, cs2_dir=str(fake_cs2(tmp_path)), launch_command='"open'))


def test_launch_command_keeps_windows_paths_intact():
    assert split_command('"C:\\Program Files\\HLAE\\HLAE.exe" -start') == (
        "C:\\Program Files\\HLAE\\HLAE.exe",
        "-start",
    )


def test_clock_formatting():
    assert clock(250) == "4:10"
    assert span(0, 65) == "0:00 - 1:05"


@pytest.mark.skipif(not os.environ.get("CS2HL_SHOT"), reason="set CS2HL_SHOT=<png path> to write a screenshot")
def test_screenshot_of_the_populated_window(make_window, tmp_path):
    window = make_window(scan_fn=fake_scan, plan_fn=FakePlanner(clips=4))
    window.resize(1280, 800)
    window.show()
    configure(window, output_dir=str(tmp_path / "out"), backend="hlae", cs2_dir=str(fake_cs2(tmp_path)))
    load(window)
    tick_player(window, 1)
    tick_player(window, 2)
    window._preview_btn.click()
    wait_idle(window)
    for _ in range(5):
        QApplication.processEvents()

    path = Path(os.environ["CS2HL_SHOT"])
    path.parent.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(path))
    assert path.stat().st_size > 0
