"""Small pure rules behind the window: which demo to open, and when Generate may run.

No Qt here, so every rule is tested directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEMO_SUFFIX = ".dem"
_COUNT = re.compile(r"\((\d+)\s*/\s*(\d+)\)")


def describe(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def is_demo(path: str | Path) -> bool:
    return Path(path).suffix.lower() == DEMO_SUFFIX


def first_demo(paths: Iterable[str | Path]) -> Path | None:
    """The first ``.dem`` among command-line arguments or dropped files."""
    return next((Path(p) for p in paths if p and is_demo(p)), None)


@dataclass(frozen=True)
class GateState:
    """What the window knows when it decides whether Generate is allowed."""

    demo_loaded: bool = False
    scanning: bool = False
    rendering: bool = False
    player_name: str | None = None
    rounds_included: int = 0
    aspects: tuple[str, ...] = ()
    output_dir: str = ""
    highlights: int | None = None  # None until the preview has counted them
    preview_error: str | None = None
    setup_checked: bool = False
    setup_problem: str | None = None  # first required check that failed


def generate_blocker(state: GateState) -> str | None:
    """Why Generate is not available, in the order the user meets each step; None when it is."""
    s = state
    if s.rendering:
        return "Making your highlights. Cancel stops after the current step."
    if s.scanning:
        return "Reading the demo..."
    if not s.demo_loaded:
        return "Open a demo to start."
    if s.player_name is None:
        return "Pick the player whose highlights you want."
    if s.rounds_included == 0:
        return "Include at least one round (Advanced tab)."
    if not s.aspects:
        return "Tick at least one format: 16:9 or 9:16."
    if not s.output_dir.strip():
        return "Choose an output folder."
    if s.preview_error is not None:
        return f"Could not find highlights: {s.preview_error}"
    if s.highlights is None:
        return "Finding highlights..."
    if s.highlights == 0:
        return (
            f"No highlights for {s.player_name} with these settings. "
            "Lower 'Min kills per round' in the Advanced tab."
        )
    if not s.setup_checked:
        return "Checking the recording setup..."
    if s.setup_problem is not None:
        return f"Fix the setup first. {s.setup_problem}"
    return None


def progress_fraction(line: str) -> tuple[int, int] | None:
    """``(done, total)`` from a log line that ends in a count such as ``(3/10)``."""
    matches = _COUNT.findall(line)
    if not matches:
        return None
    done, total = (int(n) for n in matches[-1])
    return (done, total) if 0 <= done <= total and total > 0 else None
