"""Run background jobs on QThreads, at most one per kind (scan, preview, setup, render)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .workers import Job

# A job emits done or failed as its last action, so its thread stops within moments of quit().
THREAD_STOP_TIMEOUT_MS = 5000


@dataclass
class _Running:
    thread: QThread
    job: Job  # kept referenced until its thread has stopped
    on_done: Callable[[Any], None]
    on_failed: Callable[[str], None]
    on_progress: Callable[[str], None] | None


class JobRunner(QObject):
    """Callbacks run on the thread that owns the runner (the GUI thread)."""

    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._running: dict[str, _Running] = {}

    def busy(self, kind: str) -> bool:
        return kind in self._running

    def idle(self) -> bool:
        return not self._running

    def start(
        self,
        kind: str,
        job: Job,
        on_done: Callable[[Any], None],
        on_failed: Callable[[str], None],
        on_progress: Callable[[str], None] | None = None,
    ) -> bool:
        if kind in self._running:
            return False
        thread = QThread(self)
        job.kind = kind
        job.moveToThread(thread)
        thread.started.connect(job.run)
        job.done.connect(self._on_done)
        job.failed.connect(self._on_failed)
        job.progress.connect(self._on_progress)
        self._running[kind] = _Running(thread, job, on_done, on_failed, on_progress)
        thread.start()
        self.changed.emit()
        return True

    @Slot(str, object)
    def _on_done(self, kind: str, result: object) -> None:
        entry = self._stop(kind)
        if entry is not None:
            entry.on_done(result)
        self.changed.emit()

    @Slot(str, str)
    def _on_failed(self, kind: str, message: str) -> None:
        entry = self._stop(kind)
        if entry is not None:
            entry.on_failed(message)
        self.changed.emit()

    @Slot(str, str)
    def _on_progress(self, kind: str, text: str) -> None:
        entry = self._running.get(kind)
        if entry is not None and entry.on_progress is not None:
            entry.on_progress(text)

    def _stop(self, kind: str) -> _Running | None:
        entry = self._running.pop(kind, None)
        if entry is None:
            return None
        entry.thread.quit()
        entry.thread.wait(THREAD_STOP_TIMEOUT_MS)
        entry.thread.deleteLater()
        return entry
