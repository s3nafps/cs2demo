"""What a Generate run produced, summarised for the result view. No Qt here."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..models import Plan


@dataclass
class RenderOutcome:
    plan: Plan
    out_dir: Path
    captures: list[Any] = field(default_factory=list)  # capture.CaptureResult
    exports: list[Any] = field(default_factory=list)  # export.ExportResult
    cancelled: bool = False


@dataclass(frozen=True)
class ResultSummary:
    message: str
    files: tuple[Path, ...]
    problems: tuple[str, ...]
    folder: Path


def summarize_outcome(outcome: RenderOutcome) -> ResultSummary:
    files = tuple(e.output for e in outcome.exports if e.error is None and e.output is not None)
    problems = tuple(
        [f"Recording failed for {c.clip_id}: {c.error}" for c in outcome.captures if c.error]
        + [f"Export failed for {e.clip_id} {e.aspect}: {e.error}" for e in outcome.exports if e.error]
    )
    total = len(outcome.plan.clips)
    if total == 0:
        message = "No highlights matched, so nothing was recorded."
    elif outcome.captures:
        recorded = sum(1 for c in outcome.captures if c.error is None)
        message = f"Recorded {recorded} of {total} highlight(s) and made {_files(len(files))}."
    else:  # a cancelled render_plan returns no capture results but still exports what was recorded
        message = f"Made {_files(len(files))}."
    if problems:
        message += f" {len(problems)} problem(s), see the log."
    if outcome.cancelled:
        message = f"Cancelled. {message}"
    return ResultSummary(message, files, problems, output_folder(files, outcome.out_dir))


def output_folder(files: Iterable[Path], fallback: Path) -> Path:
    """The folder that holds every output file, or ``fallback`` when they are spread out or absent."""
    parents = {f.parent for f in files}
    return parents.pop() if len(parents) == 1 else fallback


def _files(count: int) -> str:
    return "1 video file" if count == 1 else f"{count} video files"
