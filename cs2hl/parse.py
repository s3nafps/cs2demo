"""Adapter from demoparser2 output to the models in ``cs2hl.models``.

Checked with demoparser2 0.42.0 on a matchmaking demo and a FACEIT demo (see
tests/test_real_demos.py). Shapes this code relies on: steamids in events are strings,
round_end.winner is "T"/"CT", an event the demo does not contain comes back as an empty
list, and parse_ticks leaves out a prop it does not know instead of raising.
"""

from __future__ import annotations

import bisect
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .models import DemoData, DemoMeta, Kill, Rosters, Round, Side

DEFAULT_TICKRATE = 64.0
MIN_TICKRATE, MAX_TICKRATE = 16.0, 512.0
# demoparser2 0.42.0 gives "T"/"CT"; team numbers are accepted in case another version does.
TEAM_SIDE: dict[Any, Side] = {2: "T", 3: "CT", "2": "T", "3": "CT", "T": "T", "CT": "CT"}

EVENTS = ("round_start", "round_end", "begin_new_match", "player_death", "cs_win_panel_match")
TICK_PROPS = (
    "team_num",
    "health",
    "name",
    "user_id",
    "is_warmup_period",
    "game_time",
    "kills_total",
    "deaths_total",
    "total_rounds_played",
    "team_rounds_total",
)

# tick -> one dict per player, from parse_ticks
States = dict[int, list[dict[str, Any]]]


@dataclass(frozen=True)
class Scoreboard:
    """Per-player kills and deaths counted the way the in-game scoreboard counts them."""

    kills: dict[int, int] = field(default_factory=dict)  # enemy kills in rounds and after round end
    deaths: dict[int, int] = field(default_factory=dict)  # every death, world damage included


@dataclass(frozen=True)
class ParsedDemo:
    """What ``read_demo`` returns: the DemoData for highlights plus the scoreboard for the UI."""

    data: DemoData
    scoreboard: Scoreboard
    exit_kills: tuple[Kill, ...] = ()  # after a round_end, before the next round_start


def load_demo(path: Path) -> DemoData:
    return read_demo(path).data


def read_demo(path: Path) -> ParsedDemo:
    # Imported here so the pure modules stay importable without the parser installed.
    from demoparser2 import DemoParser

    return parse_with(DemoParser(str(path)), path.name)


def parse_with(parser: Any, demo_name: str) -> ParsedDemo:
    """Parse through any object with demoparser2's parse_header/parse_event(s)/parse_ticks."""
    header = parser.parse_header() or {}
    events = _events(parser, EVENTS)
    candidates = _rounds(events)
    check_tick = _check_tick(events, candidates)
    states = _states(parser, candidates, check_tick)
    rounds = _live_rounds(candidates, states)

    rosters, names = _rosters(states, rounds)
    slots, slot_notes = _slots(states, rounds)
    tally = _tally(events["player_death"], rounds, rosters, check_tick)
    scoreboard = Scoreboard(
        kills=dict(Counter(k.attacker_steamid for k in tally.kills + tally.exit_kills)),
        deaths=tally.deaths,
    )
    final_rows = states.get(check_tick, []) if check_tick is not None else []
    warnings = slot_notes + _self_check(rounds, rosters, names, scoreboard, tally.team_kills, final_rows)

    meta = DemoMeta(
        demo_name=demo_name,
        map_name=str(header.get("map_name") or "unknown"),
        tickrate=_tickrate(states, rounds),
    )
    data = DemoData(
        meta=meta,
        rounds=rounds,
        kills=tally.kills,
        rosters=rosters,
        player_names=names,
        player_slots=slots,
        warnings=tuple(warnings),
    )
    return ParsedDemo(data=data, scoreboard=scoreboard, exit_kills=tuple(tally.exit_kills))


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value != value:  # NaN check without importing numpy
            return default
        return int(value)
    except (TypeError, ValueError):  # pd.NA, or text that is not a number
        return default


def _known(value: Any) -> bool:
    try:
        return value is not None and bool(value == value)
    except TypeError:  # pd.NA
        return False


def _flag(value: Any) -> bool:
    return _known(value) and bool(value)


def _text(value: Any) -> str:
    return str(value) if _known(value) and value else ""


def _side(value: Any) -> Side | None:
    if not _known(value):
        return None
    if isinstance(value, float):
        value = int(value)
    if isinstance(value, str):
        value = value.strip().upper()
    return TEAM_SIDE.get(value)


def _frame(value: Any):
    import pandas as pd

    return value if isinstance(value, pd.DataFrame) else pd.DataFrame()


def _events(parser: Any, names: tuple[str, ...]) -> dict[str, Any]:
    """Each event as a DataFrame (empty when the demo has none), in one pass when possible."""
    found: dict[str, Any] | None = None
    parse_events = getattr(parser, "parse_events", None)
    if callable(parse_events):
        try:
            result = parse_events(list(names))
            if isinstance(result, (list, tuple)):
                found = {name: df for name, df in result}
        except Exception:
            found = None
    if found is None:
        found = {name: parser.parse_event(name) for name in names}
    return {name: _frame(found.get(name)) for name in names}


def _column(df: Any, name: str) -> list[Any]:
    df = _frame(df)
    return [] if df.empty or name not in df.columns else df[name].tolist()


def _round_ends(df: Any) -> list[tuple[int, Side | None]]:
    ticks = _column(df, "tick")
    winners = _column(df, "winner") or [None] * len(ticks)
    ends: dict[int, Side | None] = {}
    for tick, winner in sorted(zip((_int(t) for t in ticks), winners), key=lambda e: e[0]):
        ends.setdefault(tick, _side(winner))
    return sorted(ends.items())


def _match_start(begin_ticks: list[Any], last_end: int) -> int | None:
    """Tick of the last begin_new_match that still has a round_end after it."""
    ticks = [_int(t) for t in begin_ticks if _int(t, -1) <= last_end]
    return max(ticks) if ticks else None


def _rounds(events: dict[str, Any]) -> list[Round]:
    """Pair each round_end with the last round_start after the previous round_end.

    Rounds that end before the last begin_new_match (warmup, a knife round) are dropped.
    """
    starts = sorted({_int(t) for t in _column(events.get("round_start"), "tick")})
    ends = _round_ends(events.get("round_end"))
    if not ends:
        return []
    match_start = _match_start(_column(events.get("begin_new_match"), "tick"), ends[-1][0])

    rounds: list[Round] = []
    prev_end: int | None = None
    for tick, winner in ends:
        if match_start is not None and tick < match_start:
            prev_end = tick
            continue
        lo = bisect.bisect_right(starts, prev_end) if prev_end is not None else 0
        hi = bisect.bisect_left(starts, tick)
        start = starts[hi - 1] if hi > lo else (prev_end if prev_end is not None else 0)
        rounds.append(Round(number=len(rounds) + 1, start_tick=start, end_tick=tick, winner_side=winner))
        prev_end = tick
    return rounds


def _check_tick(events: dict[str, Any], rounds: list[Round]) -> int | None:
    """Where to read the game's final counters: the match win panel, else the last round end."""
    if not rounds:
        return None
    last_end = rounds[-1].end_tick
    panels = sorted(_int(t) for t in _column(events.get("cs_win_panel_match"), "tick") if _int(t) >= last_end)
    return panels[0] if panels else last_end


def _states(parser: Any, rounds: list[Round], check_tick: int | None) -> States:
    """Player props at every round start and end and at the check tick, in one parse_ticks call."""
    ticks = {r.start_tick for r in rounds} | {r.end_tick for r in rounds}
    if check_tick is not None:
        ticks.add(check_tick)
    if not ticks:
        return {}
    df = _frame(parser.parse_ticks(list(TICK_PROPS), ticks=sorted(ticks)))
    if df.empty or "tick" not in df.columns:
        return {}
    return {int(t): rows.to_dict("records") for t, rows in df.groupby("tick")}


def _first(rows: list[dict[str, Any]] | None, key: str) -> Any:
    for row in rows or []:
        if _known(row.get(key)):
            return row[key]
    return None


def _live_rounds(rounds: list[Round], states: States) -> list[Round]:
    """Drop rounds that start in warmup or that a later restart undid, then renumber from 1.

    A round is undone when a later round ends with the same or a lower total_rounds_played.
    This is what drops FACEIT's knife round when a demo has no begin_new_match (checked by
    hiding that event); a backup restore mid-match should look the same (not seen in a demo).
    """
    keep: list[Round] = []
    later_count: int | None = None
    for rnd in reversed(rounds):
        count = _first(states.get(rnd.end_tick), "total_rounds_played")
        if count is not None:
            if later_count is not None and _int(count) >= later_count:
                continue
            later_count = _int(count)
        if _flag(_first(states.get(rnd.start_tick), "is_warmup_period")):
            continue
        keep.append(rnd)
    keep.reverse()
    return [replace(r, number=i) for i, r in enumerate(keep, 1)]


def _rosters(states: States, rounds: list[Round]) -> tuple[Rosters, dict[int, str]]:
    rosters: Rosters = {}
    names: dict[int, str] = {}
    for rnd in rounds:
        roster: dict[int, Side] = {}
        for row in states.get(rnd.start_tick, []):
            steamid = _int(row.get("steamid"))
            if _text(row.get("name")):
                names[steamid] = _text(row["name"])
            side = _side(row.get("team_num"))
            if side and _int(row.get("health")) > 0:
                roster[steamid] = side
        rosters[rnd.number] = roster
    return rosters, names


def _slots(states: States, rounds: list[Round]) -> tuple[dict[int, int], list[str]]:
    """steamid -> spectator slot (user_id + 1), from the first round start each player is in.

    user_id + 1 equals CCSPlayerPawn.m_hController & 0x3FFF on both checked demos.
    """
    slots: dict[int, int] = {}
    notes: list[str] = []
    for rnd in rounds:
        for row in states.get(rnd.start_tick, []):
            steamid = _int(row.get("steamid"))
            if steamid == 0 or not _known(row.get("user_id")) or _side(row.get("team_num")) is None:
                continue
            slot = _int(row["user_id"]) + 1
            first = slots.setdefault(steamid, slot)
            if first != slot:
                name = _text(row.get("name")) or steamid
                note = f"{name}: camera slot changed from {first} to {slot} in round {rnd.number}; using {first}."
                if note not in notes:
                    notes.append(note)
    return slots, notes


@dataclass
class _Tally:
    kills: list[Kill] = field(default_factory=list)  # enemy kills inside rounds
    exit_kills: list[Kill] = field(default_factory=list)  # enemy kills after round_end
    deaths: dict[int, int] = field(default_factory=dict)
    team_kills: dict[int, int] = field(default_factory=dict)


def _tally(death_df: Any, rounds: list[Round], rosters: Rosters, last_tick: int | None) -> _Tally:
    """Sort player_death rows into in-round kills, exit kills and scoreboard deaths.

    Deaths before the first round are warmup. After the last round, deaths count only up
    to ``last_tick`` (the match win panel), as the game's counters do.
    """
    tally = _Tally()
    death_df = _frame(death_df)
    if death_df.empty or "tick" not in death_df.columns or not rounds:
        return tally
    starts = [r.start_tick for r in rounds]
    match_end = max(last_tick or 0, rounds[-1].end_tick)

    for row in death_df.sort_values("tick", kind="stable").to_dict("records"):
        tick = _int(row.get("tick"))
        index = bisect.bisect_right(starts, tick) - 1
        if index < 0 or tick > match_end:
            continue
        rnd = rounds[index]
        in_round = tick <= rnd.end_tick

        attacker = _int(row.get("attacker_steamid"))
        victim = _int(row.get("user_steamid"))
        if victim:
            tally.deaths[victim] = tally.deaths.get(victim, 0) + 1
        if attacker == 0 or attacker == victim:
            continue  # world damage, suicide or a disconnect

        roster = rosters.get(rnd.number, {})
        attacker_side = roster.get(attacker)
        victim_side = roster.get(victim)
        if attacker_side and attacker_side == victim_side:
            tally.team_kills[attacker] = tally.team_kills.get(attacker, 0) + 1
            continue

        kill = Kill(
            tick=tick,
            round_num=rnd.number,
            attacker_steamid=attacker,
            attacker_name=_text(row.get("attacker_name")),
            attacker_side=attacker_side,
            victim_steamid=victim,
            victim_side=victim_side,
            weapon=_text(row.get("weapon")),
            headshot=_flag(row.get("headshot")),
            noscope=_flag(row.get("noscope")),
            through_smoke=_flag(row.get("thrusmoke")),
            wallbang=_int(row.get("penetrated")) > 0,
        )
        (tally.kills if in_round else tally.exit_kills).append(kill)
    return tally


def _tickrate(states: States, rounds: list[Round]) -> float:
    """Ticks per second from game_time between the first round start and the last round end.

    CS2 demo headers carry no playback length, so this is the only source; 64 if it fails.
    """
    if not rounds:
        return DEFAULT_TICKRATE
    t1, t2 = rounds[0].start_tick, rounds[-1].end_tick
    try:
        rate = (t2 - t1) / (float(_first(states.get(t2), "game_time")) - float(_first(states.get(t1), "game_time")))
    except (TypeError, ValueError, ZeroDivisionError):
        return DEFAULT_TICKRATE
    if not MIN_TICKRATE <= rate <= MAX_TICKRATE:
        return DEFAULT_TICKRATE
    nearest = round(rate)
    return float(nearest) if abs(rate - nearest) <= 0.001 * nearest else rate


def _self_check(
    rounds: list[Round],
    rosters: Rosters,
    names: dict[int, str],
    scoreboard: Scoreboard,
    team_kills: dict[int, int],
    final_rows: list[dict[str, Any]],
) -> list[str]:
    """Compare the parse with the game's own counters; one readable note per mismatch."""
    if not rounds:
        return ["No rounds were found in this demo."]
    if _first(final_rows, "kills_total") is None:
        return ["The game's own scoreboard could not be read, so rounds and kills were not checked."]

    notes: list[str] = []
    played = _first(final_rows, "total_rounds_played")
    if played is not None and _int(played) != len(rounds):
        notes.append(f"Found {_plural(len(rounds), 'round')}, but the game counted {_int(played)}.")

    for row in final_rows:
        steamid = _int(row.get("steamid"))
        if steamid == 0:
            continue
        name = names.get(steamid) or _text(row.get("name")) or str(steamid)
        for what, ours, key in (
            ("kill", scoreboard.kills.get(steamid, 0), "kills_total"),
            ("death", scoreboard.deaths.get(steamid, 0), "deaths_total"),
        ):
            if not _known(row.get(key)) or _int(row[key]) == ours:
                continue
            note = f"{name}: found {_plural(ours, what)}, but the game counted {_int(row[key])}."
            if what == "kill" and team_kills.get(steamid):
                # Not verified: whether CS2 takes a kill away for a team kill.
                note += f" They have {_plural(team_kills[steamid], 'team kill')}, which the game may subtract."
            notes.append(note)

    score = _score_note(rounds, rosters, final_rows)
    if score:
        notes.append(score)
    return notes


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _score_note(rounds: list[Round], rosters: Rosters, final_rows: list[dict[str, Any]]) -> str | None:
    """Round wins per team against team_rounds_total, teams named by the side they finish on."""
    final_side = {_int(r.get("steamid")): _side(r.get("team_num")) for r in final_rows}
    final_side = {sid: side for sid, side in final_side.items() if sid and side}
    game: dict[Side, int] = {}
    for row in final_rows:
        side = _side(row.get("team_num"))
        if side and _known(row.get("team_rounds_total")):
            game[side] = _int(row["team_rounds_total"])
    if set(game) != {"T", "CT"}:
        return None

    found: dict[Side, int] = {"T": 0, "CT": 0}
    for rnd in rounds:
        if rnd.winner_side is None:
            continue
        winners = rosters.get(rnd.number, {})
        votes = Counter(final_side[s] for s, side in winners.items() if side == rnd.winner_side and s in final_side)
        if not votes:
            return None  # cannot tell which team won this round
        found[votes.most_common(1)[0][0]] += 1
    if found == game:
        return None
    return (
        f"Score found {found['CT']}-{found['T']}, but the game says {game['CT']}-{game['T']} "
        "(the team that finished on CT first)."
    )
