"""Setup tab: where CS2 and HLAE are, and the checks that must pass before recording."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGridLayout, QGroupBox, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from .checks import SetupStatus
from .options import GuiOptions
from .tables import checks_model, make_view
from .widgets import bold, muted, pick_file, pick_folder, row

HLAE_FILTER = "HLAE (HLAE.exe);;Programs (*.exe);;All files (*)"
HELP = (
    "Recording plays the demo in CS2 through HLAE on this PC. Keep Steam running and CS2 closed; "
    "the app starts CS2 by itself, records each highlight and closes it again."
)


class PathChoice:
    """One path that is found automatically unless the user picks another one."""

    def __init__(self, title: str, browse: Callable[[str], str]) -> None:
        self.override = ""
        self.edit = QLineEdit()
        self.edit.setReadOnly(True)
        self.source = muted(QLabel())
        self.browse = QPushButton("Browse...")
        self.auto = QPushButton("Find automatically")
        self.title = title
        self._browse = browse

    def show(self, path: Path | None, auto: bool) -> None:
        self.edit.setText("" if path is None else str(path))
        self.edit.setPlaceholderText("Not found. Choose it with Browse.")
        if path is None:
            self.source.setText("Not found." if auto else "")
        else:
            self.source.setText("Found automatically." if auto else "Chosen by you.")
        self.auto.setEnabled(bool(self.override))

    def choose(self) -> bool:
        chosen = self._browse(self.override or self.edit.text())
        if not chosen:
            return False
        self.override = chosen
        return True


class SetupPanel(QWidget):
    paths_changed = Signal()
    check_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cs2 = PathChoice(
            "CS2 folder", lambda start: pick_folder(self, "Choose the 'Counter-Strike Global Offensive' folder", start)
        )
        self.hlae = PathChoice("HLAE", lambda start: pick_file(self, "Choose HLAE.exe", start, HLAE_FILTER))
        self.checks = checks_model(self)
        self.checks_view = make_view(self.checks, stretch=2)
        self.checks.modelReset.connect(self.checks_view.resizeRowsToContents)
        self.summary = bold(QLabel("Checking..."))
        self.check_button = QPushButton("Check again")
        self.check_button.clicked.connect(self.check_requested)
        for choice in (self.cs2, self.hlae):
            choice.browse.clicked.connect(lambda _=False, c=choice: self._choose(c))
            choice.auto.clicked.connect(lambda _=False, c=choice: self._reset(c))
        self._build()

    def apply(self, opts: GuiOptions) -> GuiOptions:
        return replace(opts, cs2_dir=self.cs2.override, hlae_exe=self.hlae.override)

    def load(self, opts: GuiOptions) -> None:
        self.cs2.override = opts.cs2_dir
        self.hlae.override = opts.hlae_exe
        self.cs2.show(Path(opts.cs2_dir) if opts.cs2_dir else None, not opts.cs2_dir)
        self.hlae.show(Path(opts.hlae_exe) if opts.hlae_exe else None, not opts.hlae_exe)

    def show_checking(self) -> None:
        self.summary.setText("Checking the setup...")
        self.check_button.setEnabled(False)

    def show_status(self, status: SetupStatus) -> None:
        self.cs2.show(status.cs2_dir, status.cs2_auto)
        self.hlae.show(status.hlae_exe, status.hlae_auto)
        self.checks.set_rows(status.checks)
        problems = len(status.problems)
        if problems == 0:
            self.summary.setText("Ready to record.")
        else:
            self.summary.setText(f"{problems} thing(s) to fix before recording.")
        self.check_button.setEnabled(True)

    def _choose(self, choice: PathChoice) -> None:
        if choice.choose():
            self.paths_changed.emit()

    def _reset(self, choice: PathChoice) -> None:
        choice.override = ""
        self.paths_changed.emit()

    def _build(self) -> None:
        paths = QGroupBox("Programs")
        grid = QGridLayout(paths)
        for line, choice in enumerate((self.cs2, self.hlae)):
            grid.addWidget(QLabel(choice.title), line * 2, 0)
            grid.addWidget(choice.edit, line * 2, 1)
            grid.addWidget(choice.browse, line * 2, 2)
            grid.addWidget(choice.auto, line * 2, 3)
            grid.addWidget(choice.source, line * 2 + 1, 1, 1, 3)
        grid.setColumnStretch(1, 1)

        checks = QGroupBox("Before recording")
        checks_layout = QVBoxLayout(checks)
        checks_layout.addLayout(row(self.summary, self.check_button, grow=0))
        checks_layout.addWidget(self.checks_view, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(muted(QLabel(HELP)))
        layout.addWidget(paths)
        layout.addWidget(checks, 1)
