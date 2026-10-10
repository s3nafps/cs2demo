"""What the user picked in the UI: which players and rounds to make highlights for."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import Highlight


@dataclass(frozen=True)
class Selection:
    """``None`` means "no filter" for that field, so everything is included."""

    player_steamids: frozenset[int] | None = None
    round_numbers: frozenset[int] | None = None

    @classmethod
    def of(cls, players: Iterable[int] | None = None, rounds: Iterable[int] | None = None) -> "Selection":
        return cls(
            player_steamids=None if players is None else frozenset(players),
            round_numbers=None if rounds is None else frozenset(rounds),
        )


def filter_highlights(highlights: list[Highlight], selection: Selection) -> list[Highlight]:
    """Keep highlights whose POV player and round are both selected."""
    return [
        h
        for h in highlights
        if (selection.player_steamids is None or h.player_steamid in selection.player_steamids)
        and (selection.round_numbers is None or h.round_num in selection.round_numbers)
    ]
