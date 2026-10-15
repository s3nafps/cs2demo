"""Desktop UI tests. Headless: offscreen Qt with fake scan, planner, recorder and setup checks,
so no CS2, HLAE, ffmpeg or demo file is needed."""

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPointF, QSettings, QStandardPaths, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QAbstractItemView, QApplication

import cs2hl.gui.app as app_module
import cs2hl.gui.runpanel as runpanel_module
import cs2hl.gui.setuppanel as setuppanel_module
from cs2hl.capture import CaptureResult
from cs2hl.export import ExportResult
from cs2hl.gui.app import MainWindow, create_window, main
from cs2hl.gui.checks import LOAD_CHECK, SetupCheck, check_setup
from cs2hl.gui.flow import GateState, first_demo, generate_blocker, progress_fraction
from cs2hl.gui.options import GuiOptions, highlight_options, render_options, run_folder, safe_name, sanitize
from cs2hl.gui.prefs import default_output_dir, load_me, load_options, save_me, save_options
from cs2hl.gui.results import RenderOutcome, summarize_outcome
from cs2hl.gui.tables import clip_length_s, clock, span
from cs2hl.gui.workers import BackendConfig, build_hlae_backend
from cs2hl.models import Clip, DemoData, DemoMeta, Kill, Plan, Round
from cs2hl.pipeline import HighlightOptions, RenderOptions, plan_highlights
from cs2hl.scan import summarize
from cs2hl.selection import Selection

ALPHA, BRAVO, CARL, DAVE = 76561198000000001, 76561198000000002, 76561198000000011, 76561198000000012
NAMES = {ALPHA: "alpha", BRAVO: "bravo", CARL: "carl", DAVE: "dave"}
CS2_DIR = Path("C:/Games/Steam/steamapps/common/Counter-Strike Global Offensive")
HLAE_EXE = Path("C:/Program Files (x86)/HLAE/HLAE.exe")
WINDOWS_OK = SetupCheck("Windows", True, "Recording runs on this PC.")


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
        1: {ALPHA: "T", BRAVO: "T", CARL: "CT", DAVE: "CT"},
        2: {ALPHA: "CT", BRAVO: "CT", CARL: "T", DAVE: "T"},
        3: {ALPHA: "T", BRAVO: "T", CARL: "CT", DAVE: "CT"},
    }
    kills = [
        kill(100, 1, ALPHA, CARL, "T", "CT"),
        kill(200, 1, ALPHA, DAVE, "T", "CT"),
        kill(300, 1, CARL, BRAVO, "CT", "T"),
        kill(7000, 2, DAVE, BRAVO, "T", "CT"),
        kill(13000, 3, BRAVO, CARL, "T", "CT"),
    ]
    meta = DemoMeta(demo_name="match.dem", map_name="de_mirage", tickrate=64.0)
    return DemoData(meta=meta, rounds=rounds, kills=kills, rosters=rosters, player_names=NAMES)


def fake_scan(path):
    return summarize(make_demo())


def make_clip(index: int, round_num: int = 1, pov: int = ALPHA) -> Clip:
    start = 10.0 * index
    return Clip(
        id=f"r{round_num}_{NAMES[pov]}_{index}",
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
    def __init__(self, clips: int, gate: threading.Event | None = None) -> None:
        self.clip_count = clips
        self.gate = gate
        self.calls: list[tuple[Selection, HighlightOptions]] = []

    def __call__(self, data, selection, opts):
        if self.gate is not None:
            self.gate.wait(10)
        self.calls.append((selection, opts))
        return Plan(meta=data.meta, clips=[make_clip(i) for i in range(self.clip_count)])


class FakeRenderer:
    """Writes nothing; returns the results the real export step would for ``opts.output``."""

    def __init__(self, lines=(), wait_for_cancel=False, error: Exception | None = None) -> None:
        self.lines = list(lines)
        self.wait_for_cancel = wait_for_cancel
        self.error = error
        self.calls: list[tuple] = []
        self.threads: list[int] = []

    def __call__(self, plan, demo_path, backend, out_dir, opts, log, cancel):
        self.calls.append((plan, demo_path, backend, out_dir, opts, cancel))
        self.threads.append(threading.get_ident())
        for line in self.lines:
            log(line)
        if self.error is not None:
            raise self.error
        if self.wait_for_cancel:
            cancel.wait(10)
        captures = [CaptureResult(clip_id=c.id, path=out_dir / "recordings" / f"{c.id}_s0.mp4") for c in plan.clips]
        folder = out_dir / "highlights"
        exports = []
        for aspect in opts.aspects:
            if opts.output in ("clips", "both"):
                exports += [ExportResult(c.id, aspect, output=folder / f"{c.id}_{aspect}.mp4") for c in plan.clips]
            if opts.output in ("montage", "both"):
                exports.append(ExportResult("montage", aspect, output=folder / f"match_highlights_{aspect}.mp4"))
        return captures, exports


class FakeBackend:
    name = "fake-hlae"


def make_factory(backend, configs):
    def factory(cfg):
        configs.append(cfg)
        return backend

    return factory


class FakeSetup:
    """Stands in for Steam/HLAE discovery and the pre-flight checks."""

    def __init__(self, checks=None, cs2=CS2_DIR, hlae=HLAE_EXE) -> None:
        self.checks = list(checks) if checks is not None else [WINDOWS_OK]
        self.cs2 = cs2
        self.hlae = hlae
        self.calls: list[tuple] = []

    def find_cs2(self):
        return self.cs2

    def find_hlae(self):
        return self.hlae

    def preflight(self, cs2_dir, hlae_exe):
        self.calls.append((cs2_dir, hlae_exe))
        return list(self.checks)

    def fns(self) -> dict:
        return {"preflight_fn": self.preflight, "find_cs2_fn": self.find_cs2, "find_hlae_fn": self.find_hlae}


class FakeDesktop:
    def __init__(self) -> None:
        self.opened: list[Path] = []

    def openUrl(self, url):
        self.opened.append(Path(url.toLocalFile()))
        return True


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


def wait_idle(window: MainWindow) -> None:
    wait_until(window._runner.idle)


def load(window: MainWindow, path: str = "match.dem") -> MainWindow:
    window.open_demo(Path(path))
    wait_until(lambda: window._scan is not None and window._runner.idle())
    return window


def pick(window: MainWindow, steamid: int) -> None:
    assert window.player_step.select(steamid)
    wait_idle(window)


def can_generate(window: MainWindow) -> bool:
    wait_idle(window)
    return window.run_panel.generate.isEnabled()


def reason(window: MainWindow) -> str:
    wait_idle(window)
    return window.run_panel.reason.text()


def untick_round(window: MainWindow, number: int) -> None:
    model = window.advanced.rounds
    row = [r.number for r in model.rows()].index(number)
    model.setData(model.index(row, 0), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)


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
def make_window(qapp, settings, tmp_path):
    windows: list[MainWindow] = []

    def factory(setup: FakeSetup | None = None, **fns) -> MainWindow:
        fns.setdefault("scan_fn", fake_scan)
        window = MainWindow(settings=settings, **(setup or FakeSetup()).fns(), **fns)
        window.output_step.folder.setText(str(tmp_path / "videos"))
        windows.append(window)
        wait_idle(window)
        return window

    yield factory
    for window in windows:
        wait_idle(window)
        window.close()
        wait_idle(window)


def recording_window(make_window, renderer: FakeRenderer, clips: int = 1, **fns) -> MainWindow:
    """A window with a scanned demo whose Generate records with ``renderer`` and a fake backend."""
    fns.setdefault("plan_fn", FakePlanner(clips=clips))
    return load(make_window(render_fn=renderer, backend_factory=make_factory(FakeBackend(), []), **fns))


def generate(window: MainWindow) -> None:
    assert can_generate(window), reason(window)
    window.run_panel.generate.click()
    wait_idle(window)


# Opening a demo


def test_window_opens_titled_with_generate_disabled(make_window):
    window = make_window()

    assert window.windowTitle() == "CS2 Highlights"
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ["Highlights", "Advanced", "Setup"]
    assert not window.run_panel.generate.isEnabled()
    assert reason(window) == "Open a demo to start."
    assert window.run_panel.cancel.isHidden()


def test_demo_on_the_command_line_is_scanned_on_a_worker_thread(qapp, settings):
    seen: list[tuple[Path, int]] = []

    def scan(path):
        seen.append((path, threading.get_ident()))
        return fake_scan(path)

    window = create_window(["--verbose", "C:/demos/match.dem"], settings=settings, scan_fn=scan, **FakeSetup().fns())
    try:
        wait_until(lambda: window._scan is not None and window._runner.idle())
        assert seen[0][0] == Path("C:/demos/match.dem")
        assert seen[0][1] != threading.get_ident()
        assert window.windowTitle() == "CS2 Highlights - match.dem"
        assert "de_mirage" in window.demo_step.info_label.text()
        assert "3 rounds" in window.demo_step.info_label.text()
    finally:
        window.close()
        wait_idle(window)


def test_main_reads_the_demo_from_sys_argv(qapp, monkeypatch):
    created: list = []

    class Shown:
        def show(self):
            created.append("shown")

    def fake_create(args):
        created.append(list(args))
        return Shown()

    names = (qapp.organizationName(), qapp.applicationName())
    monkeypatch.setattr(app_module, "create_window", fake_create)
    monkeypatch.setattr(app_module, "_run", lambda app: 0)
    monkeypatch.setattr(sys, "argv", ["cs2hl-gui", "D:/demos/final.dem"])
    try:
        assert main() == 0
        assert created == [["D:/demos/final.dem"], "shown"]
        assert (qapp.organizationName(), qapp.applicationName()) == ("cs2hl", "cs2hl-gui")
    finally:
        qapp.setOrganizationName(names[0])
        qapp.setApplicationName(names[1])


def test_first_demo_picks_the_first_dem_file():
    assert first_demo(["-x", "notes.txt", "C:/a/One.DEM", "b.dem"]) == Path("C:/a/One.DEM")
    assert first_demo(["notes.txt", ""]) is None
    assert first_demo([]) is None


def test_dropping_a_demo_on_the_window_opens_it(make_window, tmp_path):
    scanned: list[Path] = []
    window = make_window(scan_fn=lambda path: scanned.append(path) or fake_scan(path))
    text_only = QMimeData()
    text_only.setUrls([QUrl.fromLocalFile(str(tmp_path / "notes.txt"))])
    with_demo = QMimeData()
    with_demo.setUrls([QUrl.fromLocalFile(str(tmp_path / name)) for name in ("notes.txt", "final.DEM")])
    buttons, keys, copy = Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.DropAction.CopyAction

    rejected = QDragEnterEvent(QPointF(5, 5), copy, text_only, buttons, keys)
    rejected.accept()  # starts accepted so the test sees the window ignore it
    window.dragEnterEvent(rejected)
    accepted = QDragEnterEvent(QPointF(5, 5), copy, with_demo, buttons, keys)
    window.dragEnterEvent(accepted)
    window.dropEvent(QDropEvent(QPointF(5, 5), copy, with_demo, buttons, keys))
    wait_until(lambda: window._scan is not None and window._runner.idle())

    assert not rejected.isAccepted()
    assert accepted.isAccepted()
    assert scanned == [tmp_path / "final.DEM"]


def test_a_demo_that_cannot_be_read_reports_and_keeps_the_window_usable(make_window, _no_modal_boxes):
    def broken(path):
        raise ValueError("could not read bad.dem as a CS2 demo")

    window = make_window(scan_fn=broken)
    window.open_demo(Path("bad.dem"))
    wait_idle(window)

    assert window._scan is None
    assert _no_modal_boxes == ["could not read bad.dem as a CS2 demo"]
    assert "Could not read" in window.demo_step.info_label.text()
    assert window.demo_step.open_button.isEnabled()
    assert not window.run_panel.generate.isEnabled()


# Choosing the player


def test_scan_lists_players_with_name_side_kills_and_deaths(make_window):
    window = load(make_window())
    model = window.player_step.model

    headers = [model.headerData(i, Qt.Orientation.Horizontal) for i in range(model.columnCount())]
    assert headers == ["Name", "Side", "Kills", "Deaths"]
    assert [p.name for p in model.rows()] == ["alpha", "bravo", "carl", "dave"]
    assert [model.data(model.index(0, c)) for c in range(4)] == ["alpha", "T", "2", "0"]
    assert window.advanced.included_rounds() == [1, 2, 3]
    assert window.player_step.selected() is None
    assert reason(window) == "Pick the player whose highlights you want."


def test_player_list_allows_one_player_at_a_time(make_window):
    window = load(make_window(plan_fn=FakePlanner(clips=1)))
    view = window.player_step.view

    pick(window, ALPHA)
    pick(window, BRAVO)

    assert view.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection
    assert len(view.selectionModel().selectedRows()) == 1
    assert window.player_step.selected().steamid == BRAVO


def test_this_is_me_is_remembered_and_preselected_in_the_next_demo(make_window, settings):
    first = load(make_window(plan_fn=FakePlanner(clips=1)))
    pick(first, BRAVO)
    assert first.player_step.me_box.isEnabled()
    first.player_step.me_box.setChecked(True)
    assert load_me(settings) == BRAVO

    second = load(make_window(plan_fn=FakePlanner(clips=1)))
    model = second.player_step.model

    assert second.player_step.selected().steamid == BRAVO
    assert second.player_step.me_box.isChecked()
    assert model.data(model.index(model.row_of(BRAVO), 0)) == "bravo  (you)"
    assert can_generate(second)

    second.player_step.me_box.setChecked(False)
    assert load_me(settings) is None
    assert model.data(model.index(model.row_of(BRAVO), 0)) == "bravo"


def test_a_remembered_player_who_is_not_in_the_demo_selects_nobody(make_window, settings):
    save_me(settings, 76561198999999999)

    window = load(make_window())

    assert window.player_step.selected() is None
    assert not window.player_step.me_box.isChecked()
    assert not window.player_step.me_box.isEnabled()


def test_preview_with_the_real_planner_matches_a_direct_call(make_window):
    window = load(make_window())

    pick(window, ALPHA)

    expected = plan_highlights(make_demo(), Selection.of(players=[ALPHA]), highlight_options(window._options()))
    assert len(expected.clips) == 1
    assert window.run_panel.clips.rowCount() == 1
    assert window.run_panel.found.text() == "Highlights of alpha: 1"
    assert can_generate(window)


def test_no_highlights_blocks_generate_until_the_options_find_some(make_window):
    window = load(make_window())

    pick(window, BRAVO)
    assert not can_generate(window)
    assert "No highlights for bravo" in reason(window)

    window.advanced.min_kills.setValue(1)
    assert can_generate(window)
    assert window.run_panel.clips.rowCount() == 1


def test_a_newer_preview_request_replaces_one_that_is_still_running(make_window):
    gate = threading.Event()
    planner = FakePlanner(clips=2, gate=gate)
    window = load(make_window(plan_fn=planner))

    assert window.player_step.select(ALPHA)
    assert window.player_step.select(BRAVO)
    gate.set()
    wait_idle(window)

    assert [call[0] for call in planner.calls] == [Selection.of(players=[ALPHA]), Selection.of(players=[BRAVO])]
    assert window.run_panel.found.text() == "Highlights of bravo: 2"


# Choosing the output


def test_output_choice_maps_to_render_options_output(make_window):
    window = make_window()
    buttons = window.output_step.mode_buttons

    assert [b.text() for b in buttons.values()] == ["One video", "Separate clips", "Both"]
    assert buttons["montage"].isChecked()
    for mode in ("clips", "both", "montage"):
        buttons[mode].click()
        assert render_options(window._options()).output == mode


def test_at_least_one_format_is_required(make_window):
    window = load(make_window(plan_fn=FakePlanner(clips=1)))
    boxes = window.output_step.aspect_boxes
    pick(window, ALPHA)
    assert can_generate(window)

    boxes["16x9"].setChecked(False)
    assert not can_generate(window)
    assert reason(window) == "Tick at least one format: 16:9 or 9:16."
    assert not window.advanced._vertical_box.isEnabled()

    boxes["9x16"].setChecked(True)
    assert can_generate(window)
    assert window.advanced._vertical_box.isEnabled()
    assert render_options(window._options()).aspects == ("9x16",)


# Setup


def test_generate_waits_for_a_player_and_a_passing_setup(make_window):
    setup = FakeSetup(checks=[WINDOWS_OK, SetupCheck("Steam running", False, "Start Steam.")])
    window = load(make_window(setup=setup, plan_fn=FakePlanner(clips=2)))
    assert not can_generate(window)
    assert reason(window) == "Pick the player whose highlights you want."

    pick(window, ALPHA)
    assert not can_generate(window)
    assert reason(window) == "Fix the setup first. Steam running: Start Steam."
    assert window.setup_label.text() == "Setup: 1 thing(s) to fix"
    statuses = [window.setup_panel.checks.data(window.setup_panel.checks.index(r, 1)) for r in range(2)]
    assert statuses == ["OK", "Needs fixing"]

    setup.checks = [SetupCheck("Steam running", True, "Steam is running."), SetupCheck("Tip", False, "x", False)]
    window.setup_panel.check_button.click()
    assert can_generate(window)
    assert window.setup_label.text() == "Setup: ready to record"
    assert window.setup_panel.summary.text() == "Ready to record."


def test_setup_uses_the_found_paths_unless_the_user_chose_one(make_window, settings, monkeypatch):
    setup = FakeSetup()
    window = make_window(setup=setup)
    choice = window.setup_panel.cs2

    assert setup.calls[-1] == (CS2_DIR, HLAE_EXE)
    assert choice.edit.text() == str(CS2_DIR)
    assert choice.source.text() == "Found automatically."
    assert not choice.auto.isEnabled()

    monkeypatch.setattr(setuppanel_module, "pick_folder", lambda *args: "E:/CS2")
    choice.browse.click()
    wait_idle(window)
    assert setup.calls[-1] == (Path("E:/CS2"), HLAE_EXE)
    assert choice.source.text() == "Chosen by you."
    assert load_options(settings).cs2_dir == "E:/CS2"

    choice.auto.click()
    wait_idle(window)
    assert setup.calls[-1] == (CS2_DIR, HLAE_EXE)
    assert load_options(settings).cs2_dir == ""


def test_a_recording_module_that_cannot_load_is_explained(make_window):
    def missing():
        raise ImportError("No module named 'cs2hl.system.steam'")

    setup = FakeSetup()
    window = MainWindow(
        settings=QSettings(f"cs2hl-test-{uuid.uuid4().hex}", "cs2hl-gui"),
        scan_fn=fake_scan,
        plan_fn=FakePlanner(clips=1),
        preflight_fn=setup.preflight,
        find_cs2_fn=missing,
        find_hlae_fn=setup.find_hlae,
    )
    try:
        load(window)
        pick(window, ALPHA)
        first = window._setup.checks[0]
        assert first.name == LOAD_CHECK and not first.ok
        assert "cs2hl.system.steam" in first.detail
        assert setup.calls[-1] == (None, HLAE_EXE)
        assert not can_generate(window)
        assert LOAD_CHECK in reason(window)
    finally:
        window._settings.clear()
        window.close()
        wait_idle(window)


def test_check_setup_turns_every_failure_into_a_row():
    def missing(*args):
        raise ImportError("cs2hl.system")

    def broken(*args):
        raise RuntimeError("psutil exploded")

    @dataclass(frozen=True)
    class Raw:
        name: str
        ok: bool
        detail: str

    both_missing = check_setup("", "", missing, missing, missing)
    assert [c.name for c in both_missing.checks] == [LOAD_CHECK]
    assert both_missing.blocker.name == LOAD_CHECK

    crashed = check_setup("D:/CS2", "", lambda: None, broken, broken)
    assert crashed.cs2_dir == Path("D:/CS2") and not crashed.cs2_auto and crashed.hlae_auto
    assert [(c.name, c.required) for c in crashed.checks] == [("Finding HLAE", False), ("Setup check", True)]
    assert "psutil exploded" in crashed.checks[-1].detail

    fine = check_setup("", "", lambda: CS2_DIR, lambda: HLAE_EXE, lambda c, h: [Raw("Windows", True, "ok")])
    assert fine.ready and fine.checks == (SetupCheck("Windows", True, "ok"),)


# Generating


def test_generate_records_exactly_the_chosen_player(make_window, tmp_path):
    planner = FakePlanner(clips=2)
    renderer = FakeRenderer(lines=["Recording r1_alpha_0 (1/2)"])
    backend = FakeBackend()
    configs: list = []
    window = load(make_window(plan_fn=planner, render_fn=renderer, backend_factory=make_factory(backend, configs)))
    pick(window, BRAVO)
    window.output_step.mode_buttons["clips"].click()

    generate(window)

    selection, opts = planner.calls[-1]
    assert selection == Selection.of(players=[BRAVO])
    assert opts == highlight_options(window._options())
    plan, demo_path, used_backend, out_dir, render_opts, cancel = renderer.calls[0]
    assert renderer.threads[0] != threading.get_ident()
    assert used_backend is backend
    assert configs == [BackendConfig(cs2_dir=CS2_DIR, hlae_exe=HLAE_EXE)]
    assert isinstance(render_opts, RenderOptions)
    assert (render_opts.output, render_opts.aspects) == ("clips", ("16x9",))
    assert demo_path == Path("match.dem")
    assert out_dir == tmp_path / "videos" / "match - bravo"
    assert not cancel.is_set()
    log = window.run_panel.log.toPlainText()
    assert "Recording r1_alpha_0 (1/2)" in log
    assert "Recorded 2 of 2 highlight(s) and made 2 video files." in log


def test_excluded_rounds_are_passed_on(make_window):
    planner = FakePlanner(clips=1)
    window = recording_window(make_window, FakeRenderer(), plan_fn=planner)
    pick(window, ALPHA)
    untick_round(window, 3)

    generate(window)

    assert planner.calls[-1][0] == Selection.of(players=[ALPHA], rounds=[1, 2])


def test_results_list_the_videos_and_open_folder_shows_them(make_window, tmp_path, monkeypatch):
    desktop = FakeDesktop()
    monkeypatch.setattr(runpanel_module, "QDesktopServices", desktop)
    window = recording_window(make_window, FakeRenderer(), clips=2)
    pick(window, ALPHA)
    window.output_step.aspect_boxes["9x16"].setChecked(True)

    generate(window)

    folder = tmp_path / "videos" / "match - alpha" / "highlights"
    names = [p.name for p in window.run_panel.listed_files()]
    assert names == ["match_highlights_16x9.mp4", "match_highlights_9x16.mp4"]
    assert not window.run_panel.result_box.isHidden()
    assert window.run_panel.result_text.text() == "Recorded 2 of 2 highlight(s) and made 2 video files."
    window.run_panel.open_folder.click()
    assert desktop.opened == [folder]
    window.run_panel.files.itemActivated.emit(window.run_panel.files.item(1))
    assert desktop.opened[-1] == folder / "match_highlights_9x16.mp4"


def test_cancel_sets_the_event_passed_to_the_recorder(make_window):
    renderer = FakeRenderer(wait_for_cancel=True)
    window = recording_window(make_window, renderer)
    pick(window, ALPHA)
    assert can_generate(window)

    window.run_panel.generate.click()
    wait_until(lambda: renderer.calls)
    QApplication.processEvents()
    assert not window.run_panel.cancel.isHidden() and window.run_panel.cancel.isEnabled()
    assert not window.run_panel.generate.isEnabled()
    assert not window.output_step.isEnabled()
    window.run_panel.cancel.click()
    wait_idle(window)

    assert renderer.calls[0][-1].is_set()
    assert window.run_panel.cancel.isHidden()
    assert window.run_panel.result_text.text().startswith("Cancelled.")
    assert "Cancelling" in window.run_panel.log.toPlainText()


def test_closing_during_a_render_cancels_it_and_closes_when_it_stops(make_window):
    renderer = FakeRenderer(wait_for_cancel=True)
    window = recording_window(make_window, renderer)
    window.show()
    pick(window, ALPHA)
    window.run_panel.generate.click()
    wait_until(lambda: renderer.calls)

    window.close()
    assert window._close_requested
    assert window.isVisible()
    assert renderer.calls[0][-1].is_set()
    wait_until(lambda: not window.isVisible())


def test_a_failed_render_shows_the_error(make_window, _no_modal_boxes):
    renderer = FakeRenderer(error=RuntimeError("CS2 is already running. Close it first."))
    window = recording_window(make_window, renderer)
    pick(window, ALPHA)

    generate(window)

    assert _no_modal_boxes == ["CS2 is already running. Close it first."]
    assert "Stopped: CS2 is already running" in window.run_panel.log.toPlainText()
    assert can_generate(window)


def test_a_backend_that_cannot_be_built_is_reported(make_window, _no_modal_boxes):
    def factory(config):
        raise RuntimeError("The HLAE recorder could not be loaded (boom). Reinstall CS2 Highlights.")

    renderer = FakeRenderer()
    window = load(make_window(plan_fn=FakePlanner(clips=1), render_fn=renderer, backend_factory=factory))
    pick(window, ALPHA)

    generate(window)

    assert renderer.calls == []
    assert "could not be loaded" in _no_modal_boxes[0]


# Settings


def test_options_persist_between_windows(make_window, tmp_path):
    first = make_window()
    first.output_step.mode_buttons["both"].click()
    first.output_step.aspect_boxes["9x16"].setChecked(True)
    first.output_step.folder.setText(str(tmp_path / "mine"))
    first.advanced.min_kills.setValue(3)
    first.advanced.crf.setValue(20)
    first.advanced.capture.setCurrentIndex(2)
    first.advanced.vertical.setCurrentIndex(1)
    expected = first._options()
    first.close()

    second = MainWindow(settings=first._settings, scan_fn=fake_scan, **FakeSetup().fns())
    try:
        assert second._options() == expected
        assert (expected.output, expected.aspects, expected.min_kills) == ("both", ("16x9", "9x16"), 3)
        assert (expected.crf, expected.capture_width, expected.vertical_mode) == (20, 3840, "fit")
    finally:
        wait_idle(second)
        second.close()


def test_settings_round_trip_with_a_temporary_organisation(settings, tmp_path):
    opts = GuiOptions(
        output_dir=str(tmp_path),
        output="both",
        aspects=("16x9", "9x16"),
        vertical_mode="fit",
        x_offset=-0.5,
        short_side=1440,
        crf=21,
        capture_width=1920,
        capture_height=1080,
        min_kills=3,
        include_clutches=False,
        pre_roll_s=4.5,
        post_roll_s=1.0,
        cs2_dir="D:/Games/CS2",
        hlae_exe="D:/HLAE/HLAE.exe",
    )

    save_options(settings, opts)
    save_me(settings, 76561198000000001)

    assert load_options(settings) == opts
    assert load_me(settings) == 76561198000000001
    assert settings.organizationName().startswith("cs2hl-test-")


def test_broken_settings_store_never_crashes(qapp):
    assert load_options(BrokenSettings()) == GuiOptions(output_dir=default_output_dir())
    save_options(BrokenSettings(), GuiOptions())
    save_me(BrokenSettings(), ALPHA)
    assert load_me(BrokenSettings()) is None
    window = MainWindow(settings=BrokenSettings(), scan_fn=fake_scan, **FakeSetup().fns())
    wait_idle(window)
    window.close()


def test_default_output_folder_is_videos_plus_cs2_highlights():
    videos = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)

    assert Path(default_output_dir()) == (Path(videos) if videos else Path.home() / "Videos") / "CS2 Highlights"


def test_sanitize_replaces_unknown_values():
    bad = GuiOptions(
        output="dvd",
        aspects=("4x3",),
        vertical_mode="stretch",
        x_offset=float("nan"),
        short_side=720,
        crf=99,
        capture_width=800,
        capture_height=600,
        min_kills=0,
        pre_roll_s=-1.0,
        post_roll_s=1e9,
    )
    d = GuiOptions()

    clean = sanitize(bad)

    assert (clean.output, clean.aspects, clean.vertical_mode) == (d.output, d.aspects, d.vertical_mode)
    assert (clean.x_offset, clean.short_side, clean.crf) == (d.x_offset, d.short_side, 35)
    assert (clean.capture_width, clean.capture_height) == (d.capture_width, d.capture_height)
    assert (clean.min_kills, clean.pre_roll_s, clean.post_roll_s) == (1, 0.0, 30.0)
    assert sanitize(replace(d, aspects=("9x16", "16x9", "9x16"))).aspects == ("16x9", "9x16")


# Pure helpers


def test_mapping_form_values_onto_pipeline_options():
    opts = GuiOptions(
        output="clips",
        aspects=("9x16",),
        vertical_mode="fit",
        x_offset=0.5,
        short_side=1440,
        crf=20,
        capture_width=1920,
        capture_height=1080,
        min_kills=3,
        include_clutches=False,
        pre_roll_s=4.0,
        post_roll_s=1.5,
    )

    highlights = highlight_options(opts)
    assert (highlights.min_kills, highlights.include_clutches) == (3, False)
    assert (highlights.pre_roll_s, highlights.post_roll_s) == (4.0, 1.5)
    assert highlights.merge_gap_s == HighlightOptions().merge_gap_s
    assert highlights.min_score == HighlightOptions().min_score
    rendered = render_options(opts)
    assert (rendered.output, rendered.aspects) == ("clips", ("9x16",))
    assert (rendered.vertical_mode, rendered.x_offset) == ("fit", 0.5)
    assert (rendered.encode.short_side, rendered.encode.crf) == (1440, 20)
    assert rendered.encode.preset == RenderOptions().encode.preset
    assert (rendered.capture.width, rendered.capture.height) == (1920, 1080)
    assert rendered.capture.fps == RenderOptions().capture.fps


def test_defaults_follow_the_pipeline_defaults():
    d = GuiOptions()

    assert highlight_options(d) == HighlightOptions()
    assert render_options(d) == replace(RenderOptions(), aspects=("16x9",))


def test_generate_blocker_names_the_first_missing_step():
    ready = GateState(
        demo_loaded=True,
        player_name="alpha",
        rounds_included=3,
        aspects=("16x9",),
        output_dir="V:/out",
        highlights=2,
        setup_checked=True,
    )

    assert generate_blocker(ready) is None
    assert generate_blocker(GateState()) == "Open a demo to start."
    assert generate_blocker(replace(ready, scanning=True)) == "Reading the demo..."
    assert "Cancel" in generate_blocker(replace(ready, rendering=True))
    assert generate_blocker(replace(ready, player_name=None)).startswith("Pick the player")
    assert "round" in generate_blocker(replace(ready, rounds_included=0))
    assert "16:9 or 9:16" in generate_blocker(replace(ready, aspects=()))
    assert "output folder" in generate_blocker(replace(ready, output_dir=" "))
    assert generate_blocker(replace(ready, highlights=None)) == "Finding highlights..."
    assert "No highlights for alpha" in generate_blocker(replace(ready, highlights=0))
    assert "boom" in generate_blocker(replace(ready, preview_error="boom"))
    assert "Checking" in generate_blocker(replace(ready, setup_checked=False))
    blocked = generate_blocker(replace(ready, setup_problem="Windows: needs Windows"))
    assert blocked == "Fix the setup first. Windows: needs Windows"
    assert generate_blocker(replace(ready, player_name=None, aspects=())).startswith("Pick the player")


def test_run_folder_is_one_safe_folder_per_demo_and_player():
    assert run_folder(Path("V"), "match.dem", "alpha") == Path("V") / "match - alpha"
    assert run_folder(Path("V"), "a:b?.dem", 'x/y*z<>"|') == Path("V") / "a_b_ - x_y_z_"
    assert safe_name("name. . ") == "name"
    assert safe_name("...") == "highlights"
    assert len(safe_name("x" * 500)) == 120


def test_summarize_outcome_lists_files_and_problems(tmp_path):
    plan = Plan(meta=make_demo().meta, clips=[make_clip(0), make_clip(1)])
    folder = tmp_path / "highlights"
    outcome = RenderOutcome(
        plan=plan,
        out_dir=tmp_path,
        captures=[
            CaptureResult("r1_alpha_0", path=tmp_path / "a.mp4"),
            CaptureResult("r1_alpha_1", error="CS2 crashed"),
        ],
        exports=[
            ExportResult("montage", "16x9", output=folder / "m.mp4"),
            ExportResult("r1_alpha_1", "16x9", error="missing"),
        ],
        cancelled=True,
    )

    summary = summarize_outcome(outcome)

    assert summary.files == (folder / "m.mp4",)
    assert summary.folder == folder
    assert summary.problems == (
        "Recording failed for r1_alpha_1: CS2 crashed",
        "Export failed for r1_alpha_1 16x9: missing",
    )
    assert summary.message == (
        "Cancelled. Recorded 1 of 2 highlight(s) and made 1 video file. 2 problem(s), see the log."
    )
    stopped = summarize_outcome(replace(outcome, captures=[], exports=outcome.exports[:1]))
    assert stopped.message == "Cancelled. Made 1 video file."
    empty = summarize_outcome(RenderOutcome(plan=Plan(meta=plan.meta), out_dir=tmp_path))
    assert empty.message == "No highlights matched, so nothing was recorded." and empty.folder == tmp_path


def test_progress_fraction_reads_counts_from_log_lines():
    assert progress_fraction("Exporting r3_x 16x9 (3/10)") == (3, 10)
    assert progress_fraction("clip (1/4) segment (2/3)") == (2, 3)
    assert progress_fraction("Recording") is None
    assert progress_fraction("odd (5/2)") is None
    assert progress_fraction("odd (0/0)") is None


def test_clip_length_counts_only_recorded_segments():
    whole = make_clip(0)
    cut = replace(whole, segments=((0, 128), (384, 512)))

    assert clip_length_s(whole) == 8.0
    assert clip_length_s(cut) == 4.0
    assert clock(250) == "4:10"
    assert span(0, 65) == "0:00 - 1:05"


# HLAE backend factory


def test_hlae_backend_that_cannot_be_imported_gives_a_readable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "cs2hl.capture.hlae", None)

    with pytest.raises(RuntimeError, match="HLAE recorder could not be loaded"):
        build_hlae_backend(BackendConfig(cs2_dir=CS2_DIR, hlae_exe=HLAE_EXE))


def test_hlae_backend_gets_the_setup_paths():
    from cs2hl.capture.hlae import HlaeBackend

    backend = build_hlae_backend(BackendConfig(CS2_DIR, HLAE_EXE))

    assert isinstance(backend, HlaeBackend)
    assert (backend.config.cs2_dir, backend.config.hlae_exe) == (CS2_DIR, HLAE_EXE)
    with pytest.raises(RuntimeError, match="CS2 was not found"):
        build_hlae_backend(BackendConfig(None, HLAE_EXE))
    with pytest.raises(RuntimeError, match="HLAE was not found"):
        build_hlae_backend(BackendConfig(CS2_DIR, None))


@pytest.mark.skipif(not os.environ.get("CS2HL_SHOT"), reason="set CS2HL_SHOT=<png path> to write a screenshot")
def test_screenshot_of_the_populated_window(make_window, settings):
    save_me(settings, ALPHA)
    window = make_window(plan_fn=FakePlanner(clips=4))
    window.resize(1200, 780)
    window.show()
    load(window)
    wait_idle(window)
    for _ in range(5):
        QApplication.processEvents()

    path = Path(os.environ["CS2HL_SHOT"])
    path.parent.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(path))
    assert path.stat().st_size > 0
