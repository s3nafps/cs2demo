"""Desktop window: open a demo, choose players and rounds, preview and render highlights.

Run with ``cs2hl-gui`` or ``python -m cs2hl.gui``. Pipeline calls run on worker threads
(see ``workers.py``); this module only connects widgets to those workers.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QSettings, Qt, QThread
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..pipeline import plan_highlights, render_plan
from ..scan import DemoScan, scan_demo
from ..selection import Selection
from .options import BACKEND_PLAN, highlight_options, hlae_config, render_options, render_problem
from .panel import OptionsPanel
from .prefs import APPLICATION, ORGANISATION, load_options, make_settings, save_options
from .tables import RowTable, clips_model, clock, make_view, players_model, rounds_model
from .workers import (
    BackendFactory,
    Job,
    PlanFn,
    PlanWorker,
    RenderFn,
    RenderOutcome,
    RenderRequest,
    RenderWorker,
    ScanWorker,
    build_hlae_backend,
)

TITLES = {"scan": "Scan failed", "preview": "Preview failed", "render": "Render failed"}


class MainWindow(QMainWindow):
    def __init__(
        self,
        scan_fn: Callable[[Path], DemoScan] | None = None,
        plan_fn: PlanFn | None = None,
        render_fn: RenderFn | None = None,
        backend_factory: BackendFactory | None = None,
        settings: QSettings | None = None,
    ) -> None:
        super().__init__()
        self._scan_fn = scan_fn or scan_demo
        self._plan_fn = plan_fn or plan_highlights
        self._render_fn = render_fn or render_plan
        self._backend_factory = backend_factory or build_hlae_backend
        self._settings = make_settings() if settings is None else settings
        self._demo_path: Path | None = None
        self._scan: DemoScan | None = None
        self._thread: QThread | None = None
        self._worker: QObject | None = None
        self._job_kind = ""
        self._cancel_event: threading.Event | None = None
        self._close_requested = False
        self._table_buttons: list[QPushButton] = []
        self.setWindowTitle("cs2hl")
        self.resize(1280, 800)
        self.setMinimumSize(960, 600)
        self._build_ui()
        self._panel.set_values(load_options(self._settings))
        self._refresh_actions()

    def open_demo(self, path: Path) -> None:
        """Scan a demo in the background. The window fills in once the scan finishes."""
        if self._thread is not None:
            return
        self._demo_path = path
        self._scan = None
        self._clear_results()
        self._demo_label.setText(path.name)
        self._demo_label.setToolTip(str(path))
        self._log(f"Scanning {path.name} ...")
        self._start("scan", ScanWorker(self._scan_fn, path), self._on_scan_finished)

    def _build_ui(self) -> None:
        self._panel = OptionsPanel()
        self._panel.changed.connect(self._save_settings)
        options = QScrollArea()
        options.setWidget(self._panel)
        options.setWidgetResizable(True)
        options.setMinimumWidth(300)

        self._open_btn = QPushButton("Open demo...")
        self._open_btn.clicked.connect(self._pick_demo)
        self._demo_label = QLabel("No demo open")
        self._demo_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._summary = QLabel()
        header = QHBoxLayout()
        header.addWidget(self._open_btn)
        header.addWidget(self._demo_label, 1)
        header.addWidget(self._summary)

        self._rounds = rounds_model(self)
        self._players = players_model(self)
        self._clips = clips_model(self)
        self._players.checkedChanged.connect(self._refresh_actions)
        tables = QSplitter(Qt.Orientation.Horizontal)
        tables.addWidget(self._table_box("Rounds", self._rounds))
        tables.addWidget(self._table_box("Players (tick who to make highlights for)", self._players))

        self._clip_count = QLabel("No preview yet")
        clips_box = QGroupBox("Highlights")
        clips_layout = QVBoxLayout(clips_box)
        clips_layout.addWidget(self._clip_count)
        clips_layout.addWidget(make_view(self._clips))

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(5000)
        log_box = QGroupBox("Progress log")
        QVBoxLayout(log_box).addWidget(self._log_view)

        self._preview_btn = QPushButton("Preview highlights")
        self._render_btn = QPushButton("Render")
        self._cancel_btn = QPushButton("Cancel")
        self._preview_btn.clicked.connect(self._preview)
        self._render_btn.clicked.connect(self._render)
        self._cancel_btn.clicked.connect(self._cancel_render)
        self._progress = QProgressBar()
        self._progress.setRange(0, 1)
        self._progress.setTextVisible(False)
        actions = QHBoxLayout()
        for button in (self._preview_btn, self._render_btn, self._cancel_btn):
            actions.addWidget(button)
        actions.addWidget(self._progress, 1)

        right_split = QSplitter(Qt.Orientation.Vertical)
        right_split.addWidget(tables)
        right_split.addWidget(clips_box)
        right_split.addWidget(log_box)
        right_split.setSizes([320, 230, 150])
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addLayout(header)
        right_layout.addWidget(right_split, 1)
        right_layout.addLayout(actions)

        main = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(options)
        main.addWidget(right)
        main.setStretchFactor(1, 1)
        main.setSizes([340, 940])
        self.setCentralWidget(main)
        self.statusBar().showMessage("Open a .dem file to start.")

    def _table_box(self, title: str, model: RowTable) -> QGroupBox:
        all_btn = QPushButton("All")
        none_btn = QPushButton("None")
        all_btn.clicked.connect(lambda: model.set_all_checked(True))
        none_btn.clicked.connect(lambda: model.set_all_checked(False))
        self._table_buttons += [all_btn, none_btn]
        row = QHBoxLayout()
        row.addWidget(all_btn)
        row.addWidget(none_btn)
        row.addStretch(1)
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        layout.addLayout(row)
        layout.addWidget(make_view(model))
        return box

    def _pick_demo(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open CS2 demo", str(Path.home()), "CS2 demo (*.dem);;All files (*)"
        )
        if path:
            self.open_demo(Path(path))

    def _selection(self) -> Selection:
        return Selection.of(players=self._players.checked_keys(), rounds=self._rounds.checked_keys())

    def _preview(self) -> None:
        if self._scan is None:
            return
        opts = self._panel.values()
        selection = self._selection()
        self._log(f"Previewing highlights for {len(selection.player_steamids or ())} player(s).")
        worker = PlanWorker(self._plan_fn, self._scan.data, selection, highlight_options(opts))
        self._start("preview", worker, self._on_preview_finished)

    def _render(self) -> None:
        if self._scan is None or self._demo_path is None:
            return
        opts = self._panel.values()
        problem = render_problem(opts)
        if problem is not None:
            self._show_error("Cannot render", problem)
            return
        self._save_settings()
        cancel = threading.Event()
        request = RenderRequest(
            plan_fn=self._plan_fn,
            render_fn=self._render_fn,
            backend_factory=self._backend_factory,
            data=self._scan.data,
            selection=self._selection(),
            highlight_opts=highlight_options(opts),
            render_opts=render_options(opts),
            hlae=hlae_config(opts),
            demo_path=self._demo_path,
            out_dir=Path(opts.output_dir),
            plan_only=opts.backend == BACKEND_PLAN,
            cancel=cancel,
        )
        self._cancel_event = cancel
        self._log("Render started.")
        self._start("render", RenderWorker(request), self._on_render_finished)

    def _cancel_render(self) -> None:
        if self._cancel_event is None or self._cancel_event.is_set():
            return
        self._cancel_event.set()
        self._log("Cancel requested. The clip being recorded finishes, then the run stops.")
        self._refresh_actions()

    def _on_scan_finished(self, scan: DemoScan) -> None:
        self._scan = scan
        self._rounds.set_rows(scan.rounds, checked=[r.number for r in scan.rounds])
        self._players.set_rows(scan.players)
        self._summary.setText(
            f"Map {scan.map_name}   Duration {clock(scan.duration_s)}   "
            f"Rounds {len(scan.rounds)}   Players {len(scan.players)}"
        )
        self.setWindowTitle(f"cs2hl - {scan.demo_name}")
        self._log(f"Scanned {scan.demo_name}: {len(scan.rounds)} rounds, {len(scan.players)} players.")
        self.statusBar().showMessage("Tick the players to make highlights for, then preview or render.")

    def _on_preview_finished(self, plan: Any) -> None:
        self._show_clips(plan.clips)
        self._log(f"Preview: {len(plan.clips)} clip(s).")
        self.statusBar().showMessage(f"{len(plan.clips)} clip(s) in the preview.")

    def _on_render_finished(self, outcome: RenderOutcome) -> None:
        self._show_clips(outcome.plan.clips)
        if outcome.plan_path is not None:
            self._log(f"Plan saved to {outcome.plan_path}. Nothing was recorded.")
            self.statusBar().showMessage("Plan written.")
            return
        self._report_render(outcome)

    def _report_render(self, outcome: RenderOutcome) -> None:
        for capture in outcome.captures:
            if capture.error:
                self._log(f"Recording failed for {capture.clip_id}: {capture.error}")
        for export in outcome.exports:
            if export.error:
                self._log(f"Export failed for {export.clip_id} {export.aspect}: {export.error}")
        recorded = sum(1 for c in outcome.captures if c.error is None)
        exported = sum(1 for e in outcome.exports if e.error is None)
        message = (
            f"Recorded {recorded} of {len(outcome.captures)}, exported {exported} of {len(outcome.exports)}."
        )
        if outcome.cancelled:
            message = f"Cancelled. {message}"
        self._log(message)
        self.statusBar().showMessage(message)

    def _show_clips(self, clips: list[Any]) -> None:
        self._clips.set_rows(clips)
        self._clip_count.setText(f"{len(clips)} clip(s)")

    def _clear_results(self) -> None:
        self._rounds.set_rows([])
        self._players.set_rows([])
        self._show_clips([])
        self._summary.setText("")
        self._clip_count.setText("No preview yet")

    def _start(self, kind: str, worker: Job, on_done: Callable[[Any], None]) -> None:
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(on_done)
        worker.failed.connect(self._on_failed)
        worker.progress.connect(self._log)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._thread, self._worker, self._job_kind = thread, worker, kind
        self._refresh_actions()
        thread.start()

    def _on_thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._refresh_actions()
        if self._close_requested:
            self.close()

    def _on_failed(self, message: str) -> None:
        self._log(f"Error: {message}")
        self.statusBar().showMessage(TITLES.get(self._job_kind, "Error"))
        self._show_error(TITLES.get(self._job_kind, "Error"), message)

    def _show_error(self, title: str, text: str) -> None:
        QMessageBox.critical(self, title, text)

    def _log(self, text: str) -> None:
        self._log_view.appendPlainText(text)

    def _refresh_actions(self) -> None:
        busy = self._thread is not None
        ready = (
            self._scan is not None
            and not busy
            and bool(self._players.checked_keys())
            and bool(self._rounds.checked_keys())
        )
        cancelling = self._cancel_event is not None and self._cancel_event.is_set()
        self._open_btn.setEnabled(not busy)
        self._preview_btn.setEnabled(ready)
        self._render_btn.setEnabled(ready)
        self._cancel_btn.setEnabled(busy and self._job_kind == "render" and not cancelling)
        for button in self._table_buttons:
            button.setEnabled(self._scan is not None and not busy)
        self._progress.setRange(0, 0 if busy else 1)
        self._progress.setValue(0)

    def _save_settings(self) -> None:
        save_options(self._settings, self._panel.values())

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_settings()
        if self._thread is not None:
            if self._cancel_event is not None:
                self._cancel_event.set()
            self._close_requested = True
            self._log("Waiting for the running job to stop before closing.")
            event.ignore()
            return
        event.accept()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setOrganizationName(ORGANISATION)
    app.setApplicationName(APPLICATION)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
