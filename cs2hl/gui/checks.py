"""The recording setup as the window shows it: where CS2 and HLAE are, and what still needs fixing.

``cs2hl.system`` is imported inside the default functions only, so the window still opens
(and says what is wrong) when that package cannot be loaded. No Qt here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .flow import describe

FindFn = Callable[[], Path | None]
PreflightFn = Callable[[Path | None, Path | None], Iterable[Any]]

LOAD_CHECK = "cs2hl recording modules"


@dataclass(frozen=True)
class SetupCheck:
    name: str
    ok: bool
    detail: str
    required: bool = True


@dataclass(frozen=True)
class SetupStatus:
    cs2_dir: Path | None
    hlae_exe: Path | None
    cs2_auto: bool  # True when found automatically rather than chosen by the user
    hlae_auto: bool
    checks: tuple[SetupCheck, ...]

    @property
    def blocker(self) -> SetupCheck | None:
        return next((c for c in self.checks if c.required and not c.ok), None)

    @property
    def problems(self) -> tuple[SetupCheck, ...]:
        return tuple(c for c in self.checks if c.required and not c.ok)

    @property
    def ready(self) -> bool:
        return self.blocker is None


def default_find_cs2() -> Path | None:
    from ..system.steam import find_cs2_dir

    return find_cs2_dir()


def default_find_hlae() -> Path | None:
    from ..system.tools import find_hlae

    return find_hlae()


def default_preflight(cs2_dir: Path | None, hlae_exe: Path | None) -> list[Any]:
    from ..system.preflight import preflight

    return preflight(cs2_dir, hlae_exe)


def check_setup(
    cs2_override: str,
    hlae_override: str,
    find_cs2: FindFn = default_find_cs2,
    find_hlae: FindFn = default_find_hlae,
    preflight: PreflightFn = default_preflight,
) -> SetupStatus:
    """Resolve the CS2 folder and HLAE exe (the user's choice wins), then run the pre-flight checks."""
    notes: list[SetupCheck] = []
    cs2 = _resolve(cs2_override, find_cs2, "Finding CS2", notes)
    hlae = _resolve(hlae_override, find_hlae, "Finding HLAE", notes)
    try:
        found = [_as_check(c) for c in preflight(cs2, hlae)]
    except ImportError as exc:
        found = []
        _note_load_error(notes, exc)
    except Exception as exc:
        found = [SetupCheck("Setup check", False, f"The setup check stopped with an error: {describe(exc)}")]
    return SetupStatus(
        cs2_dir=cs2,
        hlae_exe=hlae,
        cs2_auto=not cs2_override.strip(),
        hlae_auto=not hlae_override.strip(),
        checks=tuple(notes) + tuple(found),
    )


def _resolve(override: str, find: FindFn, label: str, notes: list[SetupCheck]) -> Path | None:
    if override.strip():
        return Path(override.strip())
    try:
        return find()
    except ImportError as exc:
        _note_load_error(notes, exc)
    except Exception as exc:
        notes.append(SetupCheck(label, False, f"Automatic search failed: {describe(exc)}", required=False))
    return None


def _note_load_error(notes: list[SetupCheck], exc: ImportError) -> None:
    if any(n.name == LOAD_CHECK for n in notes):
        return
    detail = f"Part of the app could not be loaded ({describe(exc)}). Reinstall CS2 Highlights."
    notes.append(SetupCheck(LOAD_CHECK, False, detail))


def _as_check(raw: Any) -> SetupCheck:
    """Accept ``cs2hl.system.preflight.Check`` or anything with name, ok and detail."""
    return SetupCheck(
        name=str(getattr(raw, "name", "Check")),
        ok=bool(getattr(raw, "ok", False)),
        detail=str(getattr(raw, "detail", "")),
        required=bool(getattr(raw, "required", True)),
    )
