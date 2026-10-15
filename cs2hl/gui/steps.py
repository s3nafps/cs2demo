"""The three steps on the main page: open a demo, pick the player, choose the output."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..scan import DemoScan, PlayerSummary
from .options import ASPECT_LABELS, ASPECTS, OUTPUT_HINTS, OUTPUT_LABELS, OUTPUT_MODES, GuiOptions
from .tables import PlayerTable, clock, make_view
from .widgets import bold, muted, pick_folder, row


class DemoStep(QGroupBox):
    open_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("1. Demo", parent)
        self.open_button = QPushButton("Open demo...")
        self.open_button.clicked.connect(self.open_clicked)
        self.name_label = bold(QLabel("No demo open"))
        self.info_label = muted(QLabel("Choose a .dem file, or drop one on this window."))
        text = QVBoxLayout()
        text.addWidget(self.name_label)
        text.addWidget(self.info_label)
        layout = QHBoxLayout(self)
        layout.addWidget(self.open_button)
        layout.addLayout(text, 1)

    def show_reading(self, path: Path) -> None:
        self.name_label.setText(path.name)
        self.name_label.setToolTip(str(path))
        self.info_label.setText("Reading the demo...")

    def show_scan(self, scan: DemoScan) -> None:
        self.name_label.setText(scan.demo_name)
        parts = (scan.map_name, f"{len(scan.rounds)} rounds", clock(scan.duration_s), f"{len(scan.players)} players")
        self.info_label.setText("  |  ".join(parts))

    def show_failed(self, path: Path) -> None:
        self.name_label.setText(path.name)
        self.info_label.setText("Could not read this demo. Try another file.")


class PlayerStep(QGroupBox):
    """Single-choice player list with a "This is me" box that is remembered between demos."""

    player_changed = Signal()
    me_changed = Signal(object)  # SteamID or None

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("2. Player", parent)
        self.model = PlayerTable(self)
        self.view = make_view(self.model, single=True, stretch=0)
        self.view.selectionModel().selectionChanged.connect(self._on_selection)
        self.me_box = QCheckBox("This is me (pick me automatically in other demos)")
        self.me_box.toggled.connect(self._on_me_toggled)
        self.me_box.setEnabled(False)
        self.hint = muted(QLabel("Open a demo to see who played."))
        layout = QVBoxLayout(self)
        layout.addWidget(self.hint)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.me_box)

    def set_players(self, players: Sequence[PlayerSummary], me: int | None) -> None:
        """Show the players and select ``me`` when present. Always emits ``player_changed`` once."""
        self.model.set_me(me)
        self.model.set_rows(players)  # a reset clears the selection without a signal
        self.hint.setText("Pick the player to make highlights for." if players else "Open a demo to see who played.")
        row_index = self.model.row_of(me)
        if row_index is None:
            self._on_selection()
            return
        self.view.selectRow(row_index)
        self.view.scrollTo(self.model.index(row_index, 0))

    def selected(self) -> PlayerSummary | None:
        rows = self.view.selectionModel().selectedRows()
        if not rows:
            return None
        return self.model.rows()[rows[0].row()]

    def select(self, steamid: int) -> bool:
        row_index = self.model.row_of(steamid)
        if row_index is None:
            return False
        self.view.selectRow(row_index)
        return True

    def _on_selection(self, *_: Any) -> None:
        player = self.selected()
        self.me_box.blockSignals(True)
        self.me_box.setChecked(player is not None and player.steamid == self.model.me)
        self.me_box.blockSignals(False)
        self.me_box.setEnabled(player is not None)
        self.player_changed.emit()

    def _on_me_toggled(self, checked: bool) -> None:
        player = self.selected()
        if player is None:
            return
        if checked:
            me = player.steamid
        elif self.model.me == player.steamid:
            me = None
        else:
            return
        self.model.set_me(me)
        self.me_changed.emit(me)


class OutputStep(QGroupBox):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("3. Output", parent)
        self.mode_group = QButtonGroup(self)
        self.mode_buttons: dict[str, QRadioButton] = {}
        for i, mode in enumerate(OUTPUT_MODES):
            button = QRadioButton(OUTPUT_LABELS.get(mode, mode))
            button.setToolTip(OUTPUT_HINTS.get(mode, ""))
            self.mode_group.addButton(button, i)
            self.mode_buttons[mode] = button
        self.aspect_boxes = {a: QCheckBox(ASPECT_LABELS[a]) for a in ASPECTS}
        self.aspect_boxes["16x9"].setToolTip("For YouTube and watching on a monitor.")
        self.aspect_boxes["9x16"].setToolTip("For TikTok, Shorts and Reels.")
        self.mode_hint = muted(QLabel())
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("Where the videos go")
        self.browse = QPushButton("Browse...")
        self.browse.clicked.connect(self._browse)

        layout = QVBoxLayout(self)
        layout.addLayout(row(*self.mode_buttons.values()))
        layout.addWidget(self.mode_hint)
        layout.addLayout(row(QLabel("Formats:"), *self.aspect_boxes.values()))
        layout.addLayout(row(QLabel("Save to:"), self.folder, self.browse, grow=1))

        self.mode_group.idToggled.connect(self._on_change)
        for box in self.aspect_boxes.values():
            box.toggled.connect(self._on_change)
        self.folder.textChanged.connect(self._on_change)
        self._update_hint()

    def apply(self, opts: GuiOptions) -> GuiOptions:
        mode = next((m for m, b in self.mode_buttons.items() if b.isChecked()), opts.output)
        aspects = tuple(a for a, box in self.aspect_boxes.items() if box.isChecked())
        return replace(opts, output=mode, aspects=aspects, output_dir=self.folder.text().strip())

    def load(self, opts: GuiOptions) -> None:
        widgets = [self.mode_group, *self.mode_buttons.values(), *self.aspect_boxes.values(), self.folder]
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self.mode_buttons.get(opts.output, self.mode_buttons[OUTPUT_MODES[0]]).setChecked(True)
            for aspect, box in self.aspect_boxes.items():
                box.setChecked(aspect in opts.aspects)
            self.folder.setText(opts.output_dir)
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        self._update_hint()

    def _browse(self) -> None:
        chosen = pick_folder(self, "Choose where to save the videos", self.folder.text().strip())
        if chosen:
            self.folder.setText(chosen)

    def _on_change(self, *_: Any) -> None:
        self._update_hint()
        self.changed.emit()

    def _update_hint(self) -> None:
        mode = next((m for m, b in self.mode_buttons.items() if b.isChecked()), "")
        self.mode_hint.setText(OUTPUT_HINTS.get(mode, ""))
