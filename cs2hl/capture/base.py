"""Contract between the planner and whatever records the clips from CS2.

A backend records every segment of every clip into the recordings folder as
``<clip id>_s<k>.mp4``, k = 0 .. len(segments) - 1 (a clip with no segments is one
segment, start_tick to end_tick). Game audio is muxed into that file when the recorder
produced any. A clip's files are all there or none are: a clip that misses a segment
gets an error and its other segment files are removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Protocol

from ..models import Clip, Plan


class CaptureCancelled(RuntimeError):
    """Raised by a backend when ``cancel`` is set. Clips finished so far are kept on disk."""


class CaptureError(RuntimeError):
    """Raised when nothing can be recorded at all, for example CS2 is already running."""


@dataclass(frozen=True)
class CaptureSettings:
    """The CS2 window size is the recording size and the source for the 9:16 crop.

    2560x1440 rather than 3840x2160 so the window fits common monitors; whether CS2 can
    open a window larger than the screen is not verified.
    """

    width: int = 2560
    height: int = 1440
    fps: int = 60


@dataclass(frozen=True)
class CaptureResult:
    clip_id: str
    path: Path | None = None  # first segment file, kept for callers that expect one file
    error: str | None = None
    parts: tuple[Path, ...] = ()  # every segment file in order, set when the clip succeeded


def clip_segments(clip: Clip) -> tuple[tuple[int, int], ...]:
    """The pieces to record for a clip. A clip without segments is one piece."""
    return clip.segments or ((clip.start_tick, clip.end_tick),)


def segment_file_name(clip_id: str, index: int) -> str:
    return f"{clip_id}_s{index}.mp4"


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
        """Record every clip in ``plan`` into ``out_dir``, one result per clip in plan order."""
        ...
