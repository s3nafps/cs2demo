"""Contract between the planner and whatever records the clips from CS2.

A backend turns a plan into one recording per clip, named ``<clip id>.<ext>`` in
the output folder. The export step (``cs2hl.export``) then reframes those files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Protocol

from ..models import Plan


class CaptureCancelled(RuntimeError):
    """Raised by a backend when ``cancel`` is set. Clips finished so far are kept."""


@dataclass(frozen=True)
class CaptureSettings:
    """Recording resolution is the source for the 9:16 crop, so keep it high."""

    width: int = 3840
    height: int = 2160
    fps: int = 60


@dataclass(frozen=True)
class CaptureResult:
    clip_id: str
    path: Path | None = None
    error: str | None = None


class CaptureBackend(Protocol):
    name: str

    def render(
        self,
        plan: Plan,
        demo_path: Path,
        out_dir: Path,
        settings: CaptureSettings,
        log: Callable[[str], None],
        cancel: Event,
    ) -> list[CaptureResult]:
        """Record every clip in ``plan`` into ``out_dir``. Must check ``cancel`` between clips."""
        ...
