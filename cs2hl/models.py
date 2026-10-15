"""Plain data types passed between parsing, detection, planning and export."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Side = Literal["T", "CT"]

# What the export step writes: one video with every highlight, one file per highlight, or both.
OutputMode = Literal["montage", "clips", "both"]

# round number -> steamid -> side, for players alive at the start of that round
Rosters = dict[int, dict[int, Side]]


@dataclass(frozen=True)
class DemoMeta:
    demo_name: str
    map_name: str
    tickrate: float


@dataclass(frozen=True)
class Round:
    number: int
    start_tick: int
    end_tick: int
    winner_side: Side | None


@dataclass(frozen=True)
class Kill:
    tick: int
    round_num: int
    attacker_steamid: int
    attacker_name: str
    attacker_side: Side | None
    victim_steamid: int
    victim_side: Side | None
    weapon: str = ""
    headshot: bool = False
    noscope: bool = False
    through_smoke: bool = False
    wallbang: bool = False


@dataclass(frozen=True)
class Highlight:
    kind: str  # "multikill" | "clutch"
    label: str  # "3K", "ACE", "1v2", "3K + 1v2"
    round_num: int
    player_steamid: int
    player_name: str
    start_tick: int  # first kill of the moment
    end_tick: int  # last kill of the moment
    kills: int
    won_round: bool
    score: float
    tags: tuple[str, ...] = ()
    kill_ticks: tuple[int, ...] = ()  # ticks of the player's kills that make up this moment


@dataclass(frozen=True)
class Clip:
    id: str
    round_num: int
    pov_steamid: int
    pov_name: str
    start_tick: int
    end_tick: int
    start_s: float
    end_s: float
    score: float
    labels: tuple[str, ...]
    tags: tuple[str, ...]
    # Recorded pieces of the clip, as (start_tick, end_tick), joined with cuts on export.
    # Empty means one piece from start_tick to end_tick.
    segments: tuple[tuple[int, int], ...] = ()
    pov_slot: int | None = None  # CS2 spectator slot of the POV player (demoparser2 user_id + 1)


@dataclass
class Plan:
    meta: DemoMeta
    clips: list[Clip] = field(default_factory=list)


@dataclass(frozen=True)
class DemoData:
    """Everything parsed from one demo. Returned by ``parse.load_demo``."""

    meta: DemoMeta
    rounds: list[Round]
    kills: list[Kill]
    rosters: Rosters
    player_names: dict[int, str]
    player_slots: dict[int, int] = field(default_factory=dict)  # steamid -> spectator slot
    warnings: tuple[str, ...] = ()  # where the parse disagrees with the game's own counters
