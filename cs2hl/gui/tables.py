"""Table models for the scan results and the planned clips."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Hashable, Iterable, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QTableView


def clock(seconds: float) -> str:
    """Format seconds as m:ss."""
    total = max(0, round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def span(start_s: float, end_s: float) -> str:
    return f"{clock(start_s)} - {clock(end_s)}"


@dataclass(frozen=True)
class Column:
    title: str
    text: Callable[[Any], str]
    right: bool = False


class RowTable(QAbstractTableModel):
    """Rows of text. With a ``key``, column 0 becomes an Include checkbox for each row."""

    checkedChanged = Signal()

    def __init__(
        self,
        columns: Sequence[Column],
        key: Callable[[Any], Hashable] | None = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._columns = tuple(columns)
        self._key = key
        self._offset = 0 if key is None else 1
        self._rows: list[Any] = []
        self._checked: set[Hashable] = set()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else self._offset + len(self._columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        if self._offset and index.column() == 0:
            if role == Qt.ItemDataRole.CheckStateRole:
                return Qt.CheckState.Checked if self._key(row) in self._checked else Qt.CheckState.Unchecked
            return None
        column = self._columns[index.column() - self._offset]
        if role == Qt.ItemDataRole.DisplayRole:
            return column.text(row)
        if role == Qt.ItemDataRole.TextAlignmentRole and column.right:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole or orientation != Qt.Orientation.Horizontal:
            return None
        if self._offset and section == 0:
            return "Include"
        return self._columns[section - self._offset].title

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if self._offset and index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not (self._offset and index.isValid() and index.column() == 0):
            return False
        if role != Qt.ItemDataRole.CheckStateRole:
            return False
        key = self._key(self._rows[index.row()])
        if _is_checked(value):
            self._checked.add(key)
        else:
            self._checked.discard(key)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.checkedChanged.emit()
        return True

    def set_rows(self, rows: Iterable[Any], checked: Iterable[Hashable] = ()) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self._checked = set(checked)
        self.endResetModel()
        self.checkedChanged.emit()

    def rows(self) -> list[Any]:
        return list(self._rows)

    def checked_keys(self) -> list[Hashable]:
        if self._key is None:
            return []
        keys = (self._key(row) for row in self._rows)
        return [key for key in keys if key in self._checked]

    def set_all_checked(self, checked: bool) -> None:
        if self._key is None or not self._rows:
            return
        self._checked = {self._key(row) for row in self._rows} if checked else set()
        last = len(self._rows) - 1
        self.dataChanged.emit(self.index(0, 0), self.index(last, 0), [Qt.ItemDataRole.CheckStateRole])
        self.checkedChanged.emit()


ROUND_COLUMNS = (
    Column("Round", lambda r: str(r.number), right=True),
    Column("Time", lambda r: span(r.start_s, r.end_s)),
    Column("Winner", lambda r: r.winner_side or "-"),
    Column("Kills", lambda r: str(r.kills), right=True),
)

PLAYER_COLUMNS = (
    Column("Name", lambda p: p.name),
    Column("Side", lambda p: p.side or "-"),
    Column("Kills", lambda p: str(p.kills), right=True),
    Column("Deaths", lambda p: str(p.deaths), right=True),
    Column("Rounds", lambda p: str(p.rounds_played), right=True),
)

CLIP_COLUMNS = (
    Column("Clip", lambda c: c.id),
    Column("Time", lambda c: span(c.start_s, c.end_s)),
    Column("POV", lambda c: c.pov_name),
    Column("Labels", lambda c: " + ".join(c.labels)),
    Column("Score", lambda c: f"{c.score:.1f}", right=True),
    Column("Tags", lambda c: ", ".join(c.tags)),
)


def rounds_model(parent: Any = None) -> RowTable:
    return RowTable(ROUND_COLUMNS, key=lambda r: r.number, parent=parent)


def players_model(parent: Any = None) -> RowTable:
    return RowTable(PLAYER_COLUMNS, key=lambda p: p.steamid, parent=parent)


def clips_model(parent: Any = None) -> RowTable:
    return RowTable(CLIP_COLUMNS, parent=parent)


def make_view(model: RowTable) -> QTableView:
    view = QTableView()
    view.setModel(model)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setAlternatingRowColors(True)
    view.verticalHeader().setVisible(False)
    view.horizontalHeader().setStretchLastSection(True)
    model.modelReset.connect(view.resizeColumnsToContents)
    return view


def _is_checked(value: Any) -> bool:
    try:
        return Qt.CheckState(value) == Qt.CheckState.Checked
    except (TypeError, ValueError):
        return bool(value)
