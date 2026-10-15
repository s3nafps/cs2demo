"""Summaries of a parsed demo for the UI: who played, and how each round went."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .models import DemoData, Kill, Side
from .parse import Scoreboard


@dataclass(frozen=True)
class PlayerSummary:
    steamid: int
    name: str
    side: Side | None  # side in the last round this player was in
    kills: int  # as the in-game scoreboard counts them when known
    deaths: int
    rounds_played: int


@dataclass(frozen=True)
class RoundSummary:
    number: int
    start_s: float
    end_s: float
    winner_side: Side | None
    kills: int


@dataclass(frozen=True)
class DemoScan:
    demo_name: str
    map_name: str
    tickrate: float
    duration_s: float
    players: list[PlayerSummary]  # most kills first
    rounds: list[RoundSummary]
    data: DemoData  # kept so the pipeline does not parse the demo a second time

    @property
    def warnings(self) -> tuple[str, ...]:
        return self.data.warnings


def summarize(data: DemoData, scoreboard: Scoreboard | None = None) -> DemoScan:
    """Players and rounds for the UI.

    Player kills and deaths come from ``scoreboard`` when given (kills after round end and
    deaths to world damage included, as the game counts them); otherwise from the in-round
    kills. Round kill counts always use the in-round kills that highlights are made from.
    """
    tickrate = data.meta.tickrate
    kills_by_round = _count_by(data.kills, lambda k: k.round_num)
    if scoreboard is None:
        kills_by_attacker = _count_by(data.kills, lambda k: k.attacker_steamid)
        deaths_by_victim = _count_by(data.kills, lambda k: k.victim_steamid)
    else:
        kills_by_attacker, deaths_by_victim = scoreboard.kills, scoreboard.deaths

    rounds = [
        RoundSummary(
            number=r.number,
            start_s=round(r.start_tick / tickrate, 3),
            end_s=round(r.end_tick / tickrate, 3),
            winner_side=r.winner_side,
            kills=kills_by_round.get(r.number, 0),
        )
        for r in data.rounds
    ]

    rounds_played: dict[int, int] = {}
    last_side: dict[int, Side] = {}
    for number in sorted(data.rosters):
        for steamid, side in data.rosters[number].items():
            rounds_played[steamid] = rounds_played.get(steamid, 0) + 1
            last_side[steamid] = side

    players = [
        PlayerSummary(
            steamid=steamid,
            name=data.player_names.get(steamid, str(steamid)),
            side=last_side.get(steamid),
            kills=kills_by_attacker.get(steamid, 0),
            deaths=deaths_by_victim.get(steamid, 0),
            rounds_played=count,
        )
        for steamid, count in rounds_played.items()
    ]
    players.sort(key=lambda p: (-p.kills, p.name.lower()))

    duration_s = round(data.rounds[-1].end_tick / tickrate, 3) if data.rounds else 0.0
    return DemoScan(
        demo_name=data.meta.demo_name,
        map_name=data.meta.map_name,
        tickrate=tickrate,
        duration_s=duration_s,
        players=players,
        rounds=rounds,
        data=data,
    )


def scan_demo(path: Path) -> DemoScan:
    from .parse import read_demo

    try:
        parsed = read_demo(path)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # demoparser2 raises pyo3 PanicException (a BaseException) on bad input
        raise ValueError(f"could not read {path.name} as a CS2 demo: {exc}") from exc
    return summarize(parsed.data, parsed.scoreboard)


def _count_by(kills: list[Kill], key) -> dict[int, int]:
    counts: dict[int, int] = {}
    for kill in kills:
        k = key(kill)
        counts[k] = counts.get(k, 0) + 1
    return counts
