"""Advanced tab: which rounds count, what makes a highlight, and how the videos are made."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..scan import RoundSummary
from .options import (
    CAPTURE_SIZES,
    CRF_RANGE,
    MIN_KILLS_RANGE,
    ROLL_RANGE_S,
    SHORT_SIDES,
    VERTICAL_LABELS,
    VERTICAL_MODES,
    GuiOptions,
)
from .tables import make_view, rounds_model
from .widgets import form_group, muted, row


class AdvancedPanel(QWidget):
    changed = Signal()  # an option changed
    rounds_changed = Signal()  # the round filter changed

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False
        self.rounds = rounds_model(self)
        self.rounds_view = make_view(self.rounds)
        self.rounds.checkedChanged.connect(self.rounds_changed)
        self.all_rounds = QPushButton("All")
        self.no_rounds = QPushButton("None")
        self.all_rounds.clicked.connect(lambda: self.rounds.set_all_checked(True))
        self.no_rounds.clicked.connect(lambda: self.rounds.set_all_checked(False))

        self.min_kills = QSpinBox()
        self.min_kills.setRange(*MIN_KILLS_RANGE)
        self.min_kills.setSuffix(" kills")
        self.min_kills.setToolTip("A round becomes a highlight when the player gets at least this many kills.")
        self.clutches = QCheckBox("Include won clutches (1vX) with fewer kills")
        self.pre_roll = _seconds("Seconds kept before each kill.")
        self.post_roll = _seconds("Seconds kept after each kill.")

        self.short_side = QComboBox()
        self.short_side.addItems([f"{side}p" for side in SHORT_SIDES])
        self.crf = QSpinBox()
        self.crf.setRange(*CRF_RANGE)
        self.crf.setToolTip("Lower is sharper and larger. 17 looks close to the original.")
        self.capture = QComboBox()
        self.capture.addItems([f"{w} x {h}" for w, h in CAPTURE_SIZES])
        self.capture.setToolTip("The CS2 window size while recording. Larger is sharper and slower.")

        self.vertical = QComboBox()
        self.vertical.addItems([VERTICAL_LABELS[m] for m in VERTICAL_MODES])
        self.x_offset = QSlider(Qt.Orientation.Horizontal)
        self.x_offset.setRange(-100, 100)
        self.x_offset.setToolTip("Which part of the wide picture the 9:16 crop keeps.")
        self.x_label = QLabel()
        self.x_label.setMinimumWidth(80)
        self.reset = QPushButton("Restore defaults")
        self.reset.clicked.connect(self._restore_defaults)
        self._build()
        self._connect()
        self._refresh()

    def apply(self, opts: GuiOptions) -> GuiOptions:
        width, height = CAPTURE_SIZES[self.capture.currentIndex()]
        return replace(
            opts,
            min_kills=self.min_kills.value(),
            include_clutches=self.clutches.isChecked(),
            pre_roll_s=self.pre_roll.value(),
            post_roll_s=self.post_roll.value(),
            short_side=SHORT_SIDES[self.short_side.currentIndex()],
            crf=self.crf.value(),
            capture_width=width,
            capture_height=height,
            vertical_mode=VERTICAL_MODES[self.vertical.currentIndex()],
            x_offset=self.x_offset.value() / 100.0,
        )

    def load(self, opts: GuiOptions) -> None:
        self._loading = True
        try:
            self.min_kills.setValue(opts.min_kills)
            self.clutches.setChecked(opts.include_clutches)
            self.pre_roll.setValue(opts.pre_roll_s)
            self.post_roll.setValue(opts.post_roll_s)
            self.short_side.setCurrentIndex(_index(SHORT_SIDES, opts.short_side))
            self.crf.setValue(opts.crf)
            self.capture.setCurrentIndex(_index(CAPTURE_SIZES, (opts.capture_width, opts.capture_height)))
            self.vertical.setCurrentIndex(_index(VERTICAL_MODES, opts.vertical_mode))
            self.x_offset.setValue(round(opts.x_offset * 100))
        finally:
            self._loading = False
        self._refresh()

    def set_rounds(self, rounds: Sequence[RoundSummary]) -> None:
        """Show the demo's rounds, all included."""
        self.rounds.set_rows(rounds, checked=[r.number for r in rounds])
        for button in (self.all_rounds, self.no_rounds):
            button.setEnabled(bool(rounds))

    def included_rounds(self) -> list[int]:
        return [int(n) for n in self.rounds.checked_keys()]

    def all_rounds_included(self) -> bool:
        return len(self.rounds.checked_keys()) == len(self.rounds.rows())

    def set_vertical_enabled(self, enabled: bool) -> None:
        """The 9:16 settings only matter when 9:16 is ticked on the main page."""
        self._vertical_box.setEnabled(enabled)

    def _build(self) -> None:
        rounds_box = QGroupBox("Rounds")
        rounds_layout = QVBoxLayout(rounds_box)
        rounds_layout.addWidget(muted(QLabel("Untick rounds to leave them out. All rounds count by default.")))
        rounds_layout.addLayout(row(self.all_rounds, self.no_rounds))
        rounds_layout.addWidget(self.rounds_view, 1)

        highlights = form_group(
            "Highlights",
            [
                ("Min kills per round", self.min_kills),
                ("", self.clutches),
                ("Before each kill", self.pre_roll),
                ("After each kill", self.post_roll),
            ],
        )
        video = form_group(
            "Video",
            [("Resolution", self.short_side), ("Quality (CRF)", self.crf), ("Recording size", self.capture)],
        )
        x_row = QHBoxLayout()
        x_row.addWidget(self.x_offset, 1)
        x_row.addWidget(self.x_label)
        self._vertical_box = form_group("9:16 vertical", [("Mode", self.vertical), ("Crop position", x_row)])

        options = QVBoxLayout()
        for box in (highlights, video, self._vertical_box):
            options.addWidget(box)
        options.addLayout(row(self.reset))
        options.addStretch(1)
        layout = QHBoxLayout(self)
        layout.addWidget(rounds_box, 1)
        layout.addLayout(options, 1)

    def _connect(self) -> None:
        for spin in (self.min_kills, self.crf, self.pre_roll, self.post_roll, self.x_offset):
            spin.valueChanged.connect(self._on_change)
        for combo in (self.short_side, self.capture, self.vertical):
            combo.currentIndexChanged.connect(self._on_change)
        self.clutches.toggled.connect(self._on_change)

    def _restore_defaults(self) -> None:
        self.load(GuiOptions())
        self.changed.emit()

    def _on_change(self, *_: Any) -> None:
        self._refresh()
        if not self._loading:
            self.changed.emit()

    def _refresh(self) -> None:
        crop = VERTICAL_MODES[self.vertical.currentIndex()] == "crop"
        self.x_offset.setEnabled(crop)
        value = self.x_offset.value()
        where = "center" if value == 0 else ("left" if value < 0 else "right")
        self.x_label.setText(f"{where} ({value / 100:+.2f})")
        self.x_label.setEnabled(crop)


def _seconds(tip: str) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(*ROLL_RANGE_S)
    spin.setSingleStep(0.5)
    spin.setDecimals(1)
    spin.setSuffix(" s")
    spin.setToolTip(tip)
    return spin


def _index(options: Sequence[Any], value: Any) -> int:
    return options.index(value) if value in options else 0
