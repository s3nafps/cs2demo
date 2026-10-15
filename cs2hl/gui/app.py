"""Desktop window: open a demo, pick the player, choose the output, generate the highlights.

Run with ``cs2hl-gui [demo.dem]`` or ``python -m cs2hl.gui``. Scans, previews, setup checks
and renders run on worker threads through ``JobRunner``; this module connects widgets to them.
"""

from __future__ import annotations

import sys
import threading
import time
from functools import partial
from pathlib import Path
from typing import Any, Callable, Sequence

from PySide6.QtCore import QEvent, QSettings, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..models import Plan
from ..pipeline import plan_highlights, render_plan
from ..scan import DemoScan, PlayerSummary, scan_demo
from ..selection import Selection
from .advanced import AdvancedPanel
from .checks import FindFn, PreflightFn, SetupCheck, SetupStatus, default_find_cs2, default_find_hlae, default_preflight
from .flow import GateState, first_demo, generate_blocker
from .jobs import JobRunner
from .options import GuiOptions, highlight_options, render_options, run_folder
from .prefs import (
    APPLICATION,
    DEMO_DIR_KEY,
    GEOMETRY_KEY,
    ORGANISATION,
    load_bytes,
    load_me,
    load_options,
    load_text,
    make_settings,
    save_bytes,
    save_me,
    save_options,
    save_text,
)
from .results import RenderOutcome, summarize_outcome
from .runpanel import RunPanel
from .setuppanel import SetupPanel
from .steps import DemoStep, OutputStep, PlayerStep
from .workers import (
    BackendConfig,
    BackendFactory,
    PlanFn,
    PlanWorker,
    RenderFn,
    RenderRequest,
    RenderWorker,
    ScanWorker,
    SetupWorker,
    build_hlae_backend,
)

APP_TITLE = "CS2 Highlights"
TAB_HIGHLIGHTS, TAB_ADVANCED, TAB_SETUP = 0, 1, 2
RECHECK_AFTER_S = 15.0  # re-run the setup checks when the window is focused again after this long
STYLE = "QPushButton#primary { font-weight: 600; padding: 6px 18px; }"


class MainWindow(QMainWindow):
    def __init__(
        self,
        scan_fn: Callable[[Path], DemoScan] | None = None,
        plan_fn: PlanFn | None = None,
        render_fn: RenderFn | None = None,
        backend_factory: BackendFactory | None = None,
        preflight_fn: PreflightFn | None = None,
        find_cs2_fn: FindFn | None = None,
        find_hlae_fn: FindFn | None = None,
        settings: QSettings | Any | None = None,
    ) -> None:
        super().__init__()
        self._scan_fn = scan_fn or scan_demo
        self._plan_fn = plan_fn or plan_highlights
        self._render_fn = render_fn or render_plan
        self._backend_factory = backend_factory or build_hlae_backend
        self._preflight_fn = preflight_fn or default_preflight
        self._find_cs2_fn = find_cs2_fn or default_find_cs2
        self._find_hlae_fn = find_hlae_fn or default_find_hlae
        self._settings = make_settings() if settings is None else settings
        self._runner = JobRunner(self)
        self._demo_path: Path | None = None
        self._scan: DemoScan | None = None
        self._preview: Plan | None = None
        self._preview_error: str | None = None
        self._preview_dirty = False
        self._preview_token = 0
        self._setup: SetupStatus | None = None
        self._setup_dirty = False
        self._setup_token = 0
        self._setup_checked_at = 0.0
        self._cancel: threading.Event | None = None
        self._close_requested = False
        self.setWindowTitle(APP_TITLE)
        self.setAcceptDrops(True)
        self.resize(1200, 780)
        self.setMinimumSize(900, 600)
        self.setStyleSheet(STYLE)
        self._build_ui()
        self._load_settings()
        self._runner.changed.connect(self._on_jobs_changed)
        self._refresh()
        self.check_setup()

    # Public actions

    def open_demo(self, path: Path | str) -> bool:
        """Read a demo in the background. The player list fills in once the scan finishes."""
        path = Path(path)
        if self._runner.busy("scan") or self._runner.busy("render"):
            self.statusBar().showMessage("Wait for the current job to finish before opening another demo.", 5000)
            return False
        self._demo_path = path
        self._scan = None
        self.tabs.setCurrentIndex(TAB_HIGHLIGHTS)
        self.demo_step.show_reading(path)
        self.player_step.set_players([], None)
        self.advanced.set_rounds([])
        self.setWindowTitle(APP_TITLE)
        save_text(self._settings, DEMO_DIR_KEY, str(path.parent))
        self.run_panel.add_log(f"Reading {path.name} ...")
        self._runner.start("scan", ScanWorker(self._scan_fn, path), self._on_scan_done, self._on_scan_failed)
        return True

    def check_setup(self) -> None:
        """Find CS2 and HLAE and run the pre-flight checks in the background."""
        self._setup_token += 1
        if self._runner.busy("setup"):
            self._setup_dirty = True
            return
        self._setup_dirty = False
        opts = self._options()
        self._setup_checked_at = time.monotonic()
        self.setup_panel.show_checking()
        job = SetupWorker(opts.cs2_dir, opts.hlae_exe, self._find_cs2_fn, self._find_hlae_fn, self._preflight_fn)
        token = self._setup_token
        self._runner.start("setup", job, partial(self._on_setup_done, token), partial(self._on_setup_failed, token))

    def generate(self) -> bool:
        """Plan, record and export the chosen player's highlights in the background."""
        player = self.player_step.selected()
        if self._gate() is not None or self._scan is None or self._demo_path is None:
            return False
        if player is None or self._setup is None:
            return False
        opts = self._options()
        self._save_settings()
        out_dir = run_folder(Path(opts.output_dir), self._scan.demo_name, player.name)
        cancel = threading.Event()
        request = RenderRequest(
            plan_fn=self._plan_fn,
            render_fn=self._render_fn,
            backend_factory=self._backend_factory,
            data=self._scan.data,
            selection=self._selection(player),
            highlight_opts=highlight_options(opts),
            render_opts=render_options(opts),
            backend_config=BackendConfig(cs2_dir=self._setup.cs2_dir, hlae_exe=self._setup.hlae_exe),
            demo_path=self._demo_path,
            out_dir=out_dir,
            cancel=cancel,
        )
        self._cancel = cancel
        self.run_panel.start_run()
        self.run_panel.add_log(f"Making the highlights of {player.name} in {out_dir}")
        self._runner.start(
            "render", RenderWorker(request), self._on_render_done, self._on_render_failed, self.run_panel.add_progress
        )
        return True

    def cancel(self) -> None:
        if self._cancel is None or self._cancel.is_set():
            return
        self._cancel.set()
        self.run_panel.add_log("Cancelling. The run stops after the current step.")
        self._refresh()

    # Building

    def _build_ui(self) -> None:
        self.demo_step = DemoStep()
        self.player_step = PlayerStep()
        self.output_step = OutputStep()
        self.run_panel = RunPanel()
        self.advanced = AdvancedPanel()
        self.setup_panel = SetupPanel()

        steps = QWidget()
        column = QVBoxLayout(steps)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self.demo_step)
        column.addWidget(self.player_step, 1)
        column.addWidget(self.output_step)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(steps)
        split.addWidget(self.run_panel)
        split.setChildrenCollapsible(False)
        split.setSizes([520, 680])
        page = QWidget()
        QVBoxLayout(page).addWidget(split)
        advanced = QScrollArea()
        advanced.setWidget(self.advanced)
        advanced.setWidgetResizable(True)

        self.tabs = QTabWidget()
        self.tabs.addTab(page, "Highlights")
        self.tabs.addTab(advanced, "Advanced")
        self.tabs.addTab(self.setup_panel, "Setup")
        self.setCentralWidget(self.tabs)

        self.setup_label = QLabel()
        self.setup_button = QPushButton("Open Setup")
        self.setup_button.setFlat(True)
        self.setup_button.clicked.connect(lambda: self.tabs.setCurrentIndex(TAB_SETUP))
        self.statusBar().addPermanentWidget(self.setup_label)
        self.statusBar().addPermanentWidget(self.setup_button)

        self.demo_step.open_clicked.connect(self._pick_demo)
        self.player_step.player_changed.connect(self._request_preview)
        self.player_step.me_changed.connect(lambda me: save_me(self._settings, me))
        self.output_step.changed.connect(self._on_options_changed)
        self.advanced.changed.connect(self._on_highlight_options_changed)
        self.advanced.rounds_changed.connect(self._request_preview)
        self.setup_panel.paths_changed.connect(self._on_paths_changed)
        self.setup_panel.check_requested.connect(self.check_setup)
        self.run_panel.generate_clicked.connect(self.generate)
        self.run_panel.cancel_clicked.connect(self.cancel)

    # Options and settings

    def _options(self) -> GuiOptions:
        return self.setup_panel.apply(self.advanced.apply(self.output_step.apply(GuiOptions())))

    def _load_settings(self) -> None:
        opts = load_options(self._settings)
        self.output_step.load(opts)
        self.advanced.load(opts)
        self.setup_panel.load(opts)
        geometry = load_bytes(self._settings, GEOMETRY_KEY)
        if geometry is not None:
            self.restoreGeometry(geometry)

    def _save_settings(self) -> None:
        save_options(self._settings, self._options())

    def _on_options_changed(self) -> None:
        self._save_settings()
        self._refresh()

    def _on_highlight_options_changed(self) -> None:
        self._save_settings()
        self._request_preview()

    def _on_paths_changed(self) -> None:
        self._save_settings()
        self._setup = None
        self.check_setup()
        self._refresh()

    # Demo

    def _pick_demo(self) -> None:
        start = load_text(self._settings, DEMO_DIR_KEY) or self._replays_dir()
        path, _ = QFileDialog.getOpenFileName(self, "Open a CS2 demo", start, "CS2 demo (*.dem);;All files (*)")
        if path:
            self.open_demo(Path(path))

    def _replays_dir(self) -> str:
        """CS2's folder for downloaded match demos (the folder CS Demo Manager also uses)."""
        if self._setup is not None and self._setup.cs2_dir is not None:
            replays = self._setup.cs2_dir / "game" / "csgo" / "replays"
            if replays.is_dir():
                return str(replays)
        return str(Path.home())

    def _on_scan_done(self, scan: DemoScan) -> None:
        self._scan = scan
        self.demo_step.show_scan(scan)
        self.advanced.set_rounds(scan.rounds)
        self.player_step.set_players(scan.players, load_me(self._settings))
        self.setWindowTitle(f"{APP_TITLE} - {scan.demo_name}")
        self.run_panel.add_log(
            f"{scan.demo_name}: {scan.map_name}, {len(scan.rounds)} rounds, {len(scan.players)} players."
        )
        for warning in getattr(scan, "warnings", ()):
            self.run_panel.add_log(f"Note: {warning}")

    def _on_scan_failed(self, message: str) -> None:
        if self._demo_path is not None:
            self.demo_step.show_failed(self._demo_path)
        self.run_panel.add_log(f"Could not read the demo: {message}")
        self._show_error("Could not read the demo", message)

    # Preview of the highlights that Generate would make

    def _selection(self, player: PlayerSummary) -> Selection:
        if self.advanced.all_rounds_included():
            return Selection.of(players=[player.steamid])
        return Selection.of(players=[player.steamid], rounds=self.advanced.included_rounds())

    def _request_preview(self) -> None:
        self._preview = None
        self._preview_error = None
        self._preview_token += 1
        player = self.player_step.selected()
        if self._scan is None or player is None:
            hint = "Pick a player to see their highlights." if self._scan else "Open a demo and pick a player."
            self.run_panel.show_no_preview(hint)
            self._refresh()
            return
        self.run_panel.show_preview_pending(player.name)
        if self._runner.busy("preview"):
            self._preview_dirty = True
            self._refresh()
            return
        self._preview_dirty = False
        token = self._preview_token
        job = PlanWorker(self._plan_fn, self._scan.data, self._selection(player), highlight_options(self._options()))
        self._runner.start(
            "preview", job, partial(self._on_preview_done, token, player.name), partial(self._on_preview_failed, token)
        )

    def _on_preview_done(self, token: int, player_name: str, plan: Plan) -> None:
        if self._preview_dirty:
            self._request_preview()
        elif token == self._preview_token:
            self._preview = plan
            self.run_panel.show_preview(player_name, plan.clips)

    def _on_preview_failed(self, token: int, message: str) -> None:
        if self._preview_dirty:
            self._request_preview()
        elif token == self._preview_token:
            self._preview_error = message
            self.run_panel.show_preview_failed(message)

    # Setup

    def _on_setup_done(self, token: int, status: SetupStatus) -> None:
        if self._setup_dirty:
            self.check_setup()
            return
        if token != self._setup_token:
            return
        self._setup = status
        self.setup_panel.show_status(status)

    def _on_setup_failed(self, token: int, message: str) -> None:
        check = SetupCheck("Setup check", False, f"The setup check stopped with an error: {message}")
        self._on_setup_done(token, SetupStatus(None, None, True, True, (check,)))

    def _setup_problem(self) -> str | None:
        blocker = None if self._setup is None else self._setup.blocker
        return None if blocker is None else f"{blocker.name}: {blocker.detail}"

    # Render

    def _on_render_done(self, outcome: RenderOutcome) -> None:
        self._cancel = None
        summary = summarize_outcome(outcome)
        for problem in summary.problems:
            self.run_panel.add_log(problem)
        self.run_panel.add_log(summary.message)
        self.run_panel.finish_run(summary)
        self.statusBar().showMessage(summary.message, 15000)
        QApplication.alert(self)
        if not self._close_requested:
            self.check_setup()

    def _on_render_failed(self, message: str) -> None:
        self._cancel = None
        self.run_panel.add_log(f"Stopped: {message}")
        self.run_panel.finish_run(None, f"Stopped: {message}")
        if not self._close_requested:
            self._show_error("Could not make the highlights", message)
            self.check_setup()

    # State

    def _gate(self) -> str | None:
        player = self.player_step.selected()
        opts = self._options()
        return generate_blocker(
            GateState(
                demo_loaded=self._scan is not None,
                scanning=self._runner.busy("scan"),
                rendering=self._runner.busy("render"),
                player_name=None if player is None else player.name,
                rounds_included=len(self.advanced.included_rounds()),
                aspects=opts.aspects,
                output_dir=opts.output_dir,
                highlights=None if self._preview is None else len(self._preview.clips),
                preview_error=self._preview_error,
                setup_checked=self._setup is not None,
                setup_problem=self._setup_problem(),
            )
        )

    def _refresh(self) -> None:
        rendering = self._runner.busy("render")
        reason = self._gate()
        cancelling = self._cancel is not None and self._cancel.is_set()
        self.run_panel.set_state(reason is None, reason, rendering, cancelling)
        self.demo_step.open_button.setEnabled(not rendering and not self._runner.busy("scan"))
        for panel in (self.player_step, self.output_step, self.advanced, self.setup_panel):
            panel.setEnabled(not rendering)
        self.advanced.set_vertical_enabled("9x16" in self._options().aspects)
        self.setup_label.setText(self._setup_summary())

    def _setup_summary(self) -> str:
        if self._setup is None:
            return "Setup: checking..."
        problems = len(self._setup.problems)
        return "Setup: ready to record" if problems == 0 else f"Setup: {problems} thing(s) to fix"

    def _on_jobs_changed(self) -> None:
        self._refresh()
        if self._close_requested and self._runner.idle():
            QTimer.singleShot(0, self.close)

    def _show_error(self, title: str, text: str) -> None:
        QMessageBox.critical(self, title, text)

    # Qt events

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if _dropped_demo(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        self.dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        path = _dropped_demo(event)
        if path is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self.open_demo(path)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() != QEvent.Type.ActivationChange or not self.isActiveWindow():
            return
        stale = time.monotonic() - self._setup_checked_at > RECHECK_AFTER_S
        if stale and not self._runner.busy("render") and not self._close_requested:
            self.check_setup()  # Steam may have been started or CS2 closed in the meantime

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_settings()
        save_bytes(self._settings, GEOMETRY_KEY, self.saveGeometry())
        if not self._runner.idle():
            if self._cancel is not None:
                self._cancel.set()
            self._close_requested = True
            self.run_panel.add_log("Closing when the current job stops.")
            self.statusBar().showMessage("Closing when the current job stops...")
            event.ignore()
            return
        event.accept()


def _dropped_demo(event: Any) -> Path | None:
    mime = event.mimeData()
    if mime is None or not mime.hasUrls():
        return None
    return first_demo(url.toLocalFile() for url in mime.urls() if url.isLocalFile())


def create_window(args: Sequence[str] = (), **kwargs: Any) -> MainWindow:
    """The main window, opening the first ``.dem`` in ``args`` (a file association passes one)."""
    window = MainWindow(**kwargs)
    demo = first_demo(args)
    if demo is not None:
        window.open_demo(demo)
    return window


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    app = QApplication.instance() or QApplication([sys.argv[0] if sys.argv else "cs2hl-gui", *args])
    app.setOrganizationName(ORGANISATION)
    app.setApplicationName(APPLICATION)
    window = create_window(args)
    window.show()
    return _run(app)


def _run(app: QApplication) -> int:
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
