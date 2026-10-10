"""Adapter from demoparser2 output to the models in ``cs2hl.models``.

Status: NOT YET VERIFIED against a real CS2 demo. The column names (``tick``,
``attacker_steamid``, ``user_steamid``, ``thrusmoke``, ``team_num``, ``health``,
``winner``) follow demoparser2's CS2 output as documented, but the first real
demo may show small differences. Fix them here, not in the detection code.
"""

from __future__ import annotations

import bisect
from pathlib import Path
from typing import Any

from .models import DemoData, DemoMeta, Kill, Rosters, Round, Side

DEFAULT_TICKRATE = 64.0
TEAM_SIDE: dict[int, Side] = {2: "T", 3: "CT"}


def load_demo(path: Path) -> DemoData:
    # Imported here so the pure modules stay importable without the parser installed.
    from demoparser2 import DemoParser

    parser = DemoParser(str(path))
    header = parser.parse_header()
    meta = DemoMeta(
        demo_name=path.name,
        map_name=str(header.get("map_name", "unknown")),
        tickrate=_tickrate(header),
    )

    rounds = _rounds(parser)
    rosters, names = _rosters(parser, rounds)
    kills = _kills(parser, rounds, rosters)
    return DemoData(meta=meta, rounds=rounds, kills=kills, rosters=rosters, player_names=names)


def _tickrate(header: dict[str, Any]) -> float:
    try:
        ticks = float(header["playback_ticks"])
        seconds = float(header["playback_time"])
    except (KeyError, TypeError, ValueError):
        return DEFAULT_TICKRATE
    return ticks / seconds if seconds > 0 else DEFAULT_TICKRATE


def _int(value: Any, default: int = 0) -> int:
    if value is None or value != value:  # NaN check without importing numpy
        return default
    return int(value)


def _flag(value: Any) -> bool:
    return value is not None and value == value and bool(value)


def _rounds(parser: Any) -> list[Round]:
    start_df = parser.parse_event("round_start")
    end_df = parser.parse_event("round_end")

    starts = sorted(_int(t) for t in start_df["tick"]) if not start_df.empty else []
    ends: list[tuple[int, Side | None]] = []
    if not end_df.empty:
        ends = sorted(
            (_int(t), TEAM_SIDE.get(_int(w))) for t, w in zip(end_df["tick"], end_df["winner"])
        )

    rounds: list[Round] = []
    end_index = 0
    for start in starts:
        # Ignore a duplicate round_start that falls inside the round we already opened.
        if rounds and start <= rounds[-1].end_tick:
            continue
        while end_index < len(ends) and ends[end_index][0] < start:
            end_index += 1
        if end_index == len(ends):
            break
        end_tick, winner = ends[end_index]
        end_index += 1
        rounds.append(
            Round(number=len(rounds) + 1, start_tick=start, end_tick=end_tick, winner_side=winner)
        )
    return rounds


def _rosters(parser: Any, rounds: list[Round]) -> tuple[Rosters, dict[int, str]]:
    if not rounds:
        return {}, {}
    ticks_df = parser.parse_ticks(
        ["team_num", "health", "name"], ticks=[r.start_tick for r in rounds]
    )
    by_tick = {int(t): rows for t, rows in ticks_df.groupby("tick")}

    rosters: Rosters = {}
    names: dict[int, str] = {}
    for rnd in rounds:
        roster: dict[int, Side] = {}
        rows = by_tick.get(rnd.start_tick)
        for row in rows.to_dict("records") if rows is not None else []:
            steamid = _int(row.get("steamid"))
            if row.get("name"):
                names[steamid] = str(row["name"])
            side = TEAM_SIDE.get(_int(row.get("team_num")))
            if side and _int(row.get("health")) > 0:
                roster[steamid] = side
        rosters[rnd.number] = roster
    return rosters, names


def _kills(parser: Any, rounds: list[Round], rosters: Rosters) -> list[Kill]:
    death_df = parser.parse_event("player_death")
    if death_df.empty or not rounds:
        return []

    round_starts = [r.start_tick for r in rounds]
    kills: list[Kill] = []
    for row in death_df.sort_values("tick").to_dict("records"):
        tick = _int(row.get("tick"))
        index = bisect.bisect_right(round_starts, tick) - 1
        if index < 0 or tick > rounds[index].end_tick:
            continue  # warmup, or between rounds

        attacker = _int(row.get("attacker_steamid"))
        victim = _int(row.get("user_steamid"))
        if attacker == 0 or attacker == victim:
            continue  # world damage or suicide

        rnd = rounds[index]
        roster = rosters.get(rnd.number, {})
        attacker_side = roster.get(attacker)
        victim_side = roster.get(victim)
        if attacker_side and attacker_side == victim_side:
            continue  # team kill

        kills.append(
            Kill(
                tick=tick,
                round_num=rnd.number,
                attacker_steamid=attacker,
                attacker_name=str(row.get("attacker_name") or ""),
                attacker_side=attacker_side,
                victim_steamid=victim,
                victim_side=victim_side,
                weapon=str(row.get("weapon") or ""),
                headshot=_flag(row.get("headshot")),
                noscope=_flag(row.get("noscope")),
                through_smoke=_flag(row.get("thrusmoke")),
                wallbang=_int(row.get("penetrated")) > 0,
            )
        )
    return kills
