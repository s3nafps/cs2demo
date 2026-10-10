"""Background jobs for the desktop UI.

A job is a QObject that the window moves onto its own QThread. It runs one pipeline call and
reports through the ``finished``, ``failed`` and ``progress`` signals. Keep widgets out of here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from ..capture import CaptureBackend, CaptureCancelled, CaptureResult
from ..export import ExportResult
from ..models import DemoData, Plan
from ..pipeline import HighlightOptions, RenderOptions
from ..planner import save_plan
from ..scan import DemoScan
from ..selection import Selection
from .options import HlaeConfig

PlanFn = Callable[[DemoData, Selection, HighlightOptions], Plan]
RenderFn = Callable[..., tuple[list[CaptureResult], list[ExportResult]]]
BackendFactory = Callable[[HlaeConfig], CaptureBackend]


def describe(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


class Job(QObject):
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def work(self) -> Any:
        raise NotImplementedError

    def run(self) -> None:
        try:
            result = self.work()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # native parser panics derive from BaseException, not Exception
            self.failed.emit(describe(exc))
        else:
            self.finished.emit(result)


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


@dataclass(frozen=True)
class RenderRequest:
    plan_fn: PlanFn
    render_fn: RenderFn
    backend_factory: BackendFactory
    data: DemoData
    selection: Selection
    highlight_opts: HighlightOptions
    render_opts: RenderOptions
    hlae: HlaeConfig
    demo_path: Path
    out_dir: Path
    plan_only: bool
    cancel: Event


@dataclass
class RenderOutcome:
    plan: Plan
    plan_path: Path | None = None
    captures: list[CaptureResult] = field(default_factory=list)
    exports: list[ExportResult] = field(default_factory=list)
    cancelled: bool = False


class RenderWorker(Job):
    def __init__(self, request: RenderRequest) -> None:
        super().__init__()
        self._req = request

    def work(self) -> RenderOutcome:
        req = self._req
        plan = req.plan_fn(req.data, req.selection, req.highlight_opts)
        self.progress.emit(f"Planned {len(plan.clips)} clip(s).")
        if req.plan_only:
            return self._write_plan(plan)
        if not plan.clips:
            self.progress.emit("No clips passed the filters, so nothing was recorded.")
            return RenderOutcome(plan=plan)
        backend = req.backend_factory(req.hlae)
        name = getattr(backend, "name", "the capture backend")
        self.progress.emit(f"Recording {len(plan.clips)} clip(s) with {name}.")
        try:
            captures, exports = req.render_fn(
                plan, req.demo_path, backend, req.out_dir, req.render_opts, self.progress.emit, req.cancel
            )
        except CaptureCancelled:
            return RenderOutcome(plan=plan, cancelled=True)
        return RenderOutcome(plan=plan, captures=captures, exports=exports, cancelled=req.cancel.is_set())

    def _write_plan(self, plan: Plan) -> RenderOutcome:
        req = self._req
        req.out_dir.mkdir(parents=True, exist_ok=True)
        path = req.out_dir / f"{req.demo_path.stem}.plan.json"
        save_plan(plan, path)
        self.progress.emit(f"Plan written to {path}. Nothing was recorded.")
        return RenderOutcome(plan=plan, plan_path=path)


def build_hlae_backend(config: HlaeConfig) -> CaptureBackend:
    """Build the HLAE backend. Imported here so the window still opens when that module is absent."""
    from ..capture.hlae import HlaeBackend
    from ..capture.hlae import HlaeConfig as CaptureHlaeConfig

    if config.cs2_dir is None:
        raise ValueError("Choose the CS2 folder for HLAE.")
    capture_config = CaptureHlaeConfig(
        cs2_dir=config.cs2_dir,
        hlae_exe=config.hlae_exe,
        launch_command=list(config.launch_command) or None,
    )
    return HlaeBackend(capture_config)
