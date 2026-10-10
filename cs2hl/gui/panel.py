"""Options panel: every setting the user can change before a preview or a render."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .options import BACKEND_HLAE, BACKENDS, CAPTURE_SIZES, SHORT_SIDES, VERTICAL_MODES, GuiOptions

VERTICAL_LABELS = {"crop": "Crop to 9:16", "fit": "Fit with bars"}
BACKEND_LABELS = {"plan": "Plan only (no recording)", "hlae": "HLAE (records in CS2)"}


class OptionsPanel(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False
        self._output = QLineEdit()
        self._output_browse = QPushButton("Browse...")
        self._wide = QCheckBox("16:9")
        self._tall = QCheckBox("9:16")
        self._vertical = QComboBox()
        self._vertical.addItems([VERTICAL_LABELS[m] for m in VERTICAL_MODES])
        self._x_offset = QSlider(Qt.Orientation.Horizontal)
        self._x_offset.setRange(-100, 100)
        self._x_label = QLabel()
        self._short_side = QComboBox()
        self._short_side.addItems([f"{side}p" for side in SHORT_SIDES])
        self._min_score = QDoubleSpinBox()
        self._min_score.setRange(0.0, 100.0)
        self._min_score.setSingleStep(0.5)
        self._pre_roll = QDoubleSpinBox()
        self._pre_roll.setRange(0.0, 60.0)
        self._pre_roll.setSuffix(" s")
        self._post_roll = QDoubleSpinBox()
        self._post_roll.setRange(0.0, 60.0)
        self._post_roll.setSuffix(" s")
        self._max_clips = QSpinBox()
        self._max_clips.setRange(0, 10000)
        self._max_clips.setSpecialValueText("No limit")
        self._backend = QComboBox()
        self._backend.addItems([BACKEND_LABELS[b] for b in BACKENDS])
        self._capture = QComboBox()
        self._capture.addItems([f"{w} x {h}" for w, h in CAPTURE_SIZES])
        self._cs2_dir = QLineEdit()
        self._cs2_browse = QPushButton("Browse...")
        self._hlae_exe = QLineEdit()
        self._hlae_browse = QPushButton("Browse...")
        self._launch = QLineEdit()
        self._launch.setPlaceholderText("Optional, one line, e.g. -start -csgoLaunchArgs ...")
        self._build()
        self._connect()
        self._refresh_widgets()

    def values(self) -> GuiOptions:
        width, height = CAPTURE_SIZES[self._capture.currentIndex()]
        return GuiOptions(
            output_dir=self._output.text().strip(),
            aspects=tuple(a for a, box in (("16x9", self._wide), ("9x16", self._tall)) if box.isChecked()),
            vertical_mode=VERTICAL_MODES[self._vertical.currentIndex()],
            x_offset=self._x_offset.value() / 100.0,
            short_side=SHORT_SIDES[self._short_side.currentIndex()],
            min_score=self._min_score.value(),
            pre_roll_s=self._pre_roll.value(),
            post_roll_s=self._post_roll.value(),
            max_clips=self._max_clips.value(),
            backend=BACKENDS[self._backend.currentIndex()],
            capture_width=width,
            capture_height=height,
            cs2_dir=self._cs2_dir.text().strip(),
            hlae_exe=self._hlae_exe.text().strip(),
            launch_command=self._launch.text().strip(),
        )

    def set_values(self, opts: GuiOptions) -> None:
        self._loading = True
        try:
            self._output.setText(opts.output_dir)
            self._wide.setChecked("16x9" in opts.aspects)
            self._tall.setChecked("9x16" in opts.aspects)
            self._vertical.setCurrentIndex(_index(VERTICAL_MODES, opts.vertical_mode))
            self._x_offset.setValue(round(opts.x_offset * 100))
            self._short_side.setCurrentIndex(_index(SHORT_SIDES, opts.short_side))
            self._min_score.setValue(opts.min_score)
            self._pre_roll.setValue(opts.pre_roll_s)
            self._post_roll.setValue(opts.post_roll_s)
            self._max_clips.setValue(opts.max_clips)
            self._backend.setCurrentIndex(_index(BACKENDS, opts.backend))
            self._capture.setCurrentIndex(_index(CAPTURE_SIZES, (opts.capture_width, opts.capture_height)))
            self._cs2_dir.setText(opts.cs2_dir)
            self._hlae_exe.setText(opts.hlae_exe)
            self._launch.setText(opts.launch_command)
        finally:
            self._loading = False
        self._refresh_widgets()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.addWidget(_group("Output", [("Folder", _with_button(self._output, self._output_browse))]))

        aspects = QHBoxLayout()
        aspects.addWidget(self._wide)
        aspects.addWidget(self._tall)
        aspects.addStretch(1)
        x_row = QHBoxLayout()
        x_row.addWidget(self._x_offset, 1)
        x_row.addWidget(self._x_label)
        root.addWidget(
            _group(
                "Clips",
                [
                    ("Aspects", aspects),
                    ("Vertical", self._vertical),
                    ("9:16 position", x_row),
                    ("Short side", self._short_side),
                ],
            )
        )

        root.addWidget(
            _group(
                "Highlights",
                [
                    ("Min score", self._min_score),
                    ("Pre-roll", self._pre_roll),
                    ("Post-roll", self._post_roll),
                    ("Max clips", self._max_clips),
                ],
            )
        )

        root.addWidget(
            _group(
                "Capture",
                [
                    ("Backend", self._backend),
                    ("Capture size", self._capture),
                    ("CS2 folder", _with_button(self._cs2_dir, self._cs2_browse)),
                    ("HLAE exe", _with_button(self._hlae_exe, self._hlae_browse)),
                    ("Launch command", self._launch),
                ],
            )
        )
        root.addStretch(1)

    def _connect(self) -> None:
        self._output_browse.clicked.connect(lambda: _pick_folder(self, self._output, "Choose output folder"))
        self._cs2_browse.clicked.connect(lambda: _pick_folder(self, self._cs2_dir, "Choose the CS2 folder"))
        self._hlae_browse.clicked.connect(
            lambda: _pick_file(self, self._hlae_exe, "Choose the HLAE executable", "Executables (*.exe);;All files (*)")
        )
        for widget in (self._output, self._cs2_dir, self._hlae_exe, self._launch):
            widget.textChanged.connect(self._on_change)
        for widget in (self._wide, self._tall):
            widget.toggled.connect(self._on_change)
        for combo in (self._vertical, self._short_side, self._backend, self._capture):
            combo.currentIndexChanged.connect(self._on_change)
        self._x_offset.valueChanged.connect(self._on_change)
        for spin in (self._min_score, self._pre_roll, self._post_roll):
            spin.valueChanged.connect(self._on_change)
        self._max_clips.valueChanged.connect(self._on_change)

    def _on_change(self, *_: Any) -> None:
        self._refresh_widgets()
        if not self._loading:
            self.changed.emit()

    def _refresh_widgets(self) -> None:
        crop = VERTICAL_MODES[self._vertical.currentIndex()] == "crop"
        self._x_offset.setEnabled(crop)
        self._x_label.setText(f"{self._x_offset.value() / 100:.2f}")
        self._x_label.setEnabled(crop)
        hlae = BACKENDS[self._backend.currentIndex()] == BACKEND_HLAE
        for widget in (self._capture, self._cs2_dir, self._cs2_browse, self._hlae_exe, self._hlae_browse, self._launch):
            widget.setEnabled(hlae)


def _group(title: str, rows: Sequence[tuple[str, Any]]) -> QGroupBox:
    box = QGroupBox(title)
    form = QFormLayout(box)
    for label, field in rows:
        form.addRow(label, field)
    return box


def _with_button(edit: QLineEdit, button: QPushButton) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addWidget(edit, 1)
    row.addWidget(button)
    return row


def _pick_folder(parent: QWidget, edit: QLineEdit, title: str) -> None:
    start = edit.text().strip() or str(Path.home())
    chosen = QFileDialog.getExistingDirectory(parent, title, start)
    if chosen:
        edit.setText(chosen)


def _pick_file(parent: QWidget, edit: QLineEdit, title: str, pattern: str) -> None:
    start = edit.text().strip() or str(Path.home())
    chosen, _ = QFileDialog.getOpenFileName(parent, title, start, pattern)
    if chosen:
        edit.setText(chosen)


def _index(options: Sequence[Any], value: Any) -> int:
    return options.index(value) if value in options else 0
