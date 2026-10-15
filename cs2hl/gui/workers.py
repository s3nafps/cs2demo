"""Background jobs for the desktop UI.

A job is a QObject that ``JobRunner`` moves onto its own QThread. It runs one call and
reports through ``done``, ``failed`` and ``progress``. Jobs hold data and functions only,
never widgets.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from ..models import DemoData, Plan
from ..pipeline import HighlightOptions, RenderOptions
from ..scan import DemoScan
from ..selection import Selection
from .checks import FindFn, PreflightFn, SetupStatus, check_setup
from .flow import describe
from .results import RenderOutcome

PlanFn = Callable[[DemoData, Selection, HighlightOptions], Plan]
RenderFn = Callable[..., tuple[list[Any], list[Any]]]


@dataclass(frozen=True)
class BackendConfig:
    cs2_dir: Path | None
    hlae_exe: Path | None


BackendFactory = Callable[[BackendConfig], Any]


class Job(QObject):
    done = Signal(str, object)
    failed = Signal(str, str)
    progress = Signal(str, str)

    kind = ""

    def work(self) -> Any:
        raise NotImplementedError

    def say(self, text: str) -> None:
        self.progress.emit(self.kind, str(text))

    def run(self) -> None:
        try:
            result = self.work()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # native parser panics derive from BaseException, not Exception
            self.failed.emit(self.kind, describe(exc))
        else:
            self.done.emit(self.kind, result)


class ScanWorker(Job):
    def __init__(self, scan_fn: Callable[[Path], DemoScan], path: Path) -> None:
        super().__init__()
        self._scan_fn = scan_fn
        self._path = path

    def work(self) -> DemoScan:
        return self._scan_fn(self._path)


class PlanWorker(Job):
    def __init__(self, plan_fn: PlanFn, data: DemoData, selection: Selection, opts: HighlightOptions) -> None:
        super().__init__()
        self._plan_fn = plan_fn
        self._args = (data, selection, opts)

    def work(self) -> Plan:
        return self._plan_fn(*self._args)


class SetupWorker(Job):
    def __init__(
        self, cs2_override: str, hlae_override: str, find_cs2: FindFn, find_hlae: FindFn, preflight: PreflightFn
    ) -> None:
        super().__init__()
        self._args = (cs2_override, hlae_override, find_cs2, find_hlae, preflight)

    def work(self) -> SetupStatus:
        return check_setup(*self._args)


@dataclass(frozen=True)
class RenderRequest:
    plan_fn: PlanFn
    render_fn: RenderFn
    backend_factory: BackendFactory
    data: DemoData
    selection: Selection
    highlight_opts: HighlightOptions
    render_opts: RenderOptions
    backend_config: BackendConfig
    demo_path: Path
    out_dir: Path
    cancel: Event


class RenderWorker(Job):
    """Plan the chosen player's highlights, record them with the backend, export the videos."""

    def __init__(self, request: RenderRequest) -> None:
        super().__init__()
        self._req = request

    def work(self) -> RenderOutcome:
        req = self._req
        plan = req.plan_fn(req.data, req.selection, req.highlight_opts)
        self.say(f"Found {len(plan.clips)} highlight(s).")
        if not plan.clips:
            return RenderOutcome(plan=plan, out_dir=req.out_dir)
        if req.cancel.is_set():
            return RenderOutcome(plan=plan, out_dir=req.out_dir, cancelled=True)
        backend = req.backend_factory(req.backend_config)
        name = getattr(backend, "name", "the recorder")
        self.say(f"Recording with {name}. CS2 opens and plays the demo by itself; leave it alone until it closes.")
        try:
            captures, exports = req.render_fn(
                plan, req.demo_path, backend, req.out_dir, req.render_opts, self.say, req.cancel
            )
        except Exception as exc:
            if req.cancel.is_set() or type(exc).__name__ == "CaptureCancelled":
                return RenderOutcome(plan=plan, out_dir=req.out_dir, cancelled=True)
            raise
        return RenderOutcome(
            plan=plan,
            out_dir=req.out_dir,
            captures=list(captures),
            exports=list(exports),
            cancelled=req.cancel.is_set(),
        )


def build_hlae_backend(config: BackendConfig) -> Any:
    """Build the HLAE backend. Imported here so the window opens even when that module cannot load."""
    try:
        from ..capture.hlae import HlaeBackend, HlaeConfig
    except ImportError as exc:
        message = f"The HLAE recorder could not be loaded ({describe(exc)}). Reinstall CS2 Highlights."
        raise RuntimeError(message) from exc
    if config.cs2_dir is None:
        raise RuntimeError("CS2 was not found. Choose its folder in the Setup tab.")
    if config.hlae_exe is None:
        raise RuntimeError("HLAE was not found. Choose HLAE.exe in the Setup tab.")
    return HlaeBackend(HlaeConfig(cs2_dir=config.cs2_dir, hlae_exe=config.hlae_exe))
