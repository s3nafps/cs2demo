"""Right side of the main page: the highlights found, Generate, progress and the finished files."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QGroupBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .flow import progress_fraction
from .results import ResultSummary
from .tables import clip_length_s, clips_model, clock, make_view
from .widgets import bold, muted, row

LOG_LINES = 5000


def open_in_shell(path: Path) -> bool:
    """Open a file or folder with the system's default program."""
    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))


class RunPanel(QWidget):
    generate_clicked = Signal()
    cancel_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.found = bold(QLabel("Highlights"))
        self.found_hint = muted(QLabel("Open a demo and pick a player to see their highlights."))
        self.clips = clips_model(self)
        self.clips_view = make_view(self.clips, stretch=1)

        self.generate = QPushButton("Generate highlights")
        self.generate.setObjectName("primary")
        self.generate.setMinimumHeight(40)
        self.generate.clicked.connect(self.generate_clicked)
        self.cancel = QPushButton("Cancel")
        self.cancel.setMinimumHeight(40)
        self.cancel.clicked.connect(self.cancel_clicked)
        self.reason = muted(QLabel())
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        self.stage = muted(QLabel())

        self.result_box = QGroupBox("Your videos")
        self.result_text = QLabel()
        self.result_text.setWordWrap(True)
        self.files = QListWidget()
        self.files.itemActivated.connect(self._open_item)
        self.open_folder = QPushButton("Open folder")
        self.open_folder.clicked.connect(self._open_folder)
        self.folder: Path | None = None

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(LOG_LINES)
        self._build()

    def show_no_preview(self, text: str) -> None:
        self.clips.set_rows([])
        self.found.setText("Highlights")
        self.found_hint.setText(text)

    def show_preview_pending(self, player: str) -> None:
        self.found.setText(f"Highlights of {player}")
        self.found_hint.setText("Finding highlights...")

    def show_preview(self, player: str, clips: Sequence[Any]) -> None:
        self.clips.set_rows(clips)
        total = sum(clip_length_s(c) for c in clips)
        self.found.setText(f"Highlights of {player}: {len(clips)}")
        if clips:
            self.found_hint.setText(
                f"About {clock(total)} of video. Generate records them in CS2 and makes the videos."
            )
        else:
            self.found_hint.setText("Nothing matched. Lower 'Min kills per round' in the Advanced tab to find more.")

    def show_preview_failed(self, message: str) -> None:
        self.clips.set_rows([])
        self.found_hint.setText(f"Could not find highlights: {message}")

    def set_state(self, can_generate: bool, reason: str | None, running: bool, cancelling: bool) -> None:
        self.generate.setEnabled(can_generate)
        self.cancel.setVisible(running)
        self.cancel.setEnabled(running and not cancelling)
        self.reason.setText(reason or "Ready. Generate records the highlights and makes the videos.")

    def start_run(self) -> None:
        self.result_box.setVisible(False)
        self.files.clear()
        self.folder = None
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.stage.setText("Starting...")

    def add_progress(self, line: str) -> None:
        self.add_log(line)
        self.stage.setText(line)
        fraction = progress_fraction(line)
        if fraction is not None:
            done, total = fraction
            self.progress.setRange(0, total)
            self.progress.setValue(done)

    def finish_run(self, summary: ResultSummary | None, message: str = "") -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(1 if summary is not None and summary.files else 0)
        self.stage.setText(summary.message if summary is not None else message)
        if summary is None:
            return
        self.folder = summary.folder
        self.result_text.setText(summary.message)
        self.files.clear()
        for path in summary.files:
            item = QListWidgetItem(path.name)
            item.setToolTip(str(path))
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.files.addItem(item)
        self.files.setVisible(bool(summary.files))
        self.result_box.setVisible(True)

    def listed_files(self) -> list[Path]:
        return [Path(self.files.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.files.count())]

    def add_log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _open_folder(self) -> None:
        if self.folder is not None:
            open_in_shell(self.folder)

    def _open_item(self, item: QListWidgetItem) -> None:
        open_in_shell(Path(item.data(Qt.ItemDataRole.UserRole)))

    def _build(self) -> None:
        found = QGroupBox()
        found_layout = QVBoxLayout(found)
        found_layout.addWidget(self.found)
        found_layout.addWidget(self.found_hint)
        found_layout.addWidget(self.clips_view, 1)

        result_layout = QVBoxLayout(self.result_box)
        result_layout.addWidget(self.result_text)
        result_layout.addWidget(self.files)
        result_layout.addLayout(row(self.open_folder))
        self.result_box.setVisible(False)

        log_box = QGroupBox("Log")
        QVBoxLayout(log_box).addWidget(self.log)
        log_box.setMaximumHeight(170)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(found, 1)
        layout.addLayout(row(self.generate, self.cancel, grow=0))
        layout.addWidget(self.reason)
        layout.addWidget(self.progress)
        layout.addWidget(self.stage)
        layout.addWidget(self.result_box)
        layout.addWidget(log_box)
        self.cancel.setVisible(False)
