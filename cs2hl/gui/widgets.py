"""Small widget helpers shared by the panels."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QWidget


def muted(label: QLabel) -> QLabel:
    """Grey hint text that follows the light or dark palette."""
    palette = label.palette()
    palette.setColor(QPalette.ColorRole.WindowText, palette.color(QPalette.ColorRole.PlaceholderText))
    label.setPalette(palette)
    label.setWordWrap(True)
    return label


def bold(label: QLabel, scale: float = 1.0) -> QLabel:
    font = QFont(label.font())
    font.setBold(True)
    if scale != 1.0 and font.pointSizeF() > 0:
        font.setPointSizeF(font.pointSizeF() * scale)
    label.setFont(font)
    return label


def row(*widgets: QWidget, grow: int | None = None) -> QHBoxLayout:
    """Widgets side by side. ``grow`` is the index of the one that takes spare width; None pads the end."""
    layout = QHBoxLayout()
    for i, widget in enumerate(widgets):
        layout.addWidget(widget, 1 if i == grow else 0)
    if grow is None:
        layout.addStretch(1)
    return layout


def form_group(title: str, rows: Sequence[tuple[str, Any]]) -> QGroupBox:
    box = QGroupBox(title)
    form = QFormLayout(box)
    for label, field in rows:
        form.addRow(label, field)
    return box


def pick_folder(parent: QWidget, title: str, start: str) -> str:
    return QFileDialog.getExistingDirectory(parent, title, start or str(Path.home()))


def pick_file(parent: QWidget, title: str, start: str, pattern: str) -> str:
    chosen, _ = QFileDialog.getOpenFileName(parent, title, start or str(Path.home()), pattern)
    return chosen
