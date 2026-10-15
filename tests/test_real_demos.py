"""Parse real CS2 demos and compare with the game's own counters.

Skipped unless CS2HL_DEMO_DIR points at a folder with the two public demos:
  mm.dem      matchmaking, de_ancient, 8 rounds (https://ndownloader.figshare.com/files/52456259)
  faceit.dem  FACEIT, de_mirage, 24 rounds (https://ndownloader.figshare.com/files/52455215)
Run with -s to see the parse time of each demo.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from pathlib import Path

import pytest

from cs2hl import parse

DEMO_DIR = os.environ.get("CS2HL_DEMO_DIR")
pytestmark = pytest.mark.skipif(not DEMO_DIR, reason="set CS2HL_DEMO_DIR to a folder with mm.dem and faceit.dem")

_cache: dict[str, parse.ParsedDemo] = {}


def demo_path(name: str) -> Path:
    path = Path(DEMO_DIR or ".") / name
    if not path.is_file():
        pytest.skip(f"{path} not found")
    return path


def parsed(name: str) -> parse.ParsedDemo:
    if name not in _cache:
        path = demo_path(name)
        started = time.perf_counter()
        _cache[name] = parse.read_demo(path)
        print(f"\n{name}: parsed in {time.perf_counter() - started:.2f} s")
    return _cache[name]


def game_counters(name: str) -> dict[int, dict]:
    """steamid -> the game's own props at the match win panel."""
    from demoparser2 import DemoParser

    parser = DemoParser(str(demo_path(name)))
    tick = int(parser.parse_event("cs_win_panel_match")["tick"].iloc[0])
    df = parser.parse_ticks(["kills_total", "deaths_total", "total_rounds_played"], ticks=[tick])
    return {int(row["steamid"]): row for row in df.to_dict("records")}


def controller_slots(name: str, tick: int) -> dict[int, int]:
    """steamid -> controller entity index, read from the pawn's controller handle."""
    from demoparser2 import DemoParser

    df = DemoParser(str(demo_path(name))).parse_ticks(["CCSPlayerPawn.m_hController"], ticks=[tick])
    return {int(r["steamid"]): int(r["CCSPlayerPawn.m_hController"]) & 0x3FFF for r in df.to_dict("records")}


def team_score(data) -> list[int]:
    """Rounds won by each team, lowest first, following one player across side swaps."""
    reference = next(s for s in data.rosters[1] if all(s in data.rosters[r.number] for r in data.rounds))
    first = sum(data.rosters[r.number][reference] == r.winner_side for r in data.rounds)
    return sorted([first, len(data.rounds) - first])


def kills_by_player(kills) -> Counter:
    return Counter(k.attacker_steamid for k in kills)


class WithoutBeginNewMatch:
    """The real parser with begin_new_match hidden and one call per event."""

    def __init__(self, path: Path):
        from demoparser2 import DemoParser

        self._parser = DemoParser(str(path))

    def parse_header(self):
        return self._parser.parse_header()

    def parse_ticks(self, *args, **kwargs):
        return self._parser.parse_ticks(*args, **kwargs)

    def parse_event(self, name):
        return [] if name == "begin_new_match" else self._parser.parse_event(name)


def test_mm_rounds_and_winners():
    data = parsed("mm.dem").data

    assert data.meta.map_name == "de_ancient"
    assert data.meta.tickrate == 64.0
    assert len(data.rounds) == 8
    assert Counter(r.winner_side for r in data.rounds) == {"CT": 6, "T": 2}
    assert team_score(data) == [2, 6]
    assert (data.rounds[0].start_tick, data.rounds[0].end_tick) == (65, 3822)


def test_mm_kills_match_the_game_counters():
    result = parsed("mm.dem")
    game = game_counters("mm.dem")

    assert len(result.data.kills) == 42
    assert result.exit_kills == ()
    in_round = kills_by_player(result.data.kills)
    assert {sid: in_round.get(sid, 0) for sid in game} == {sid: int(row["kills_total"]) for sid, row in game.items()}
    assert {sid: result.scoreboard.deaths.get(sid, 0) for sid in game} == {
        sid: int(row["deaths_total"]) for sid, row in game.items()
    }
    assert result.data.warnings == ()


def test_mm_has_a_camera_slot_for_every_player():
    data = parsed("mm.dem").data

    assert len(data.player_slots) == 10
    assert data.player_slots == controller_slots("mm.dem", data.rounds[0].start_tick)


def test_faceit_skips_knife_round_and_warmup():
    data = parsed("faceit.dem").data

    assert data.meta.map_name == "de_mirage"
    assert data.meta.tickrate == 64.0
    assert len(data.rounds) == 24
    assert team_score(data) == [11, 13]
    assert Counter(r.winner_side for r in data.rounds) == {"CT": 17, "T": 7}
    assert (data.rounds[0].start_tick, data.rounds[0].end_tick) == (6085, 11616)


def test_faceit_kills_match_the_scoreboard():
    result = parsed("faceit.dem")
    game = game_counters("faceit.dem")

    assert len(result.data.kills) == 163
    assert len(result.exit_kills) == 2
    assert {sid: result.scoreboard.kills.get(sid, 0) for sid in game} == {
        sid: int(row["kills_total"]) for sid, row in game.items()
    }
    assert {sid: result.scoreboard.deaths.get(sid, 0) for sid in game} == {
        sid: int(row["deaths_total"]) for sid, row in game.items()
    }
    assert result.data.warnings == ()


def test_faceit_has_a_camera_slot_for_every_player():
    data = parsed("faceit.dem").data

    assert len(data.player_slots) == 10
    assert data.player_slots == controller_slots("faceit.dem", data.rounds[0].start_tick)


def test_faceit_rounds_are_the_same_without_begin_new_match():
    fallback = parse.parse_with(WithoutBeginNewMatch(demo_path("faceit.dem")), "faceit.dem")
    data = parsed("faceit.dem").data

    assert fallback.data.rounds == data.rounds
    assert len(fallback.data.kills) == 163
    assert fallback.data.warnings == ()


def test_scan_shows_scoreboard_kills():
    from cs2hl.scan import summarize

    result = parsed("faceit.dem")
    game = game_counters("faceit.dem")
    scan = summarize(result.data, result.scoreboard)

    assert {p.steamid: p.kills for p in scan.players} == {sid: int(row["kills_total"]) for sid, row in game.items()}
    assert sum(r.kills for r in scan.rounds) == 163
