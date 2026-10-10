"""Tests for the demoparser2 adapter's logic, using a fake parser.

These do not prove the column names match real demoparser2 output. That needs a
real .dem file; see README "Status".
"""

import pandas as pd

from cs2hl import parse


class FakeParser:
    def __init__(self, events, ticks):
        self.events = events
        self.ticks = ticks

    def parse_event(self, name):
        return self.events.get(name, pd.DataFrame())

    def parse_ticks(self, props, ticks=None):
        return self.ticks[self.ticks["tick"].isin(ticks)]


def player_rows(tick, players):
    return [
        {"tick": tick, "steamid": steamid, "team_num": team, "health": health, "name": name}
        for steamid, team, health, name in players
    ]


def build_fake():
    events = {
        "round_start": pd.DataFrame({"tick": [0, 50, 6400]}),  # 50 is a duplicate start
        "round_end": pd.DataFrame({"tick": [3000, 9000], "winner": [2, 3]}),  # 2 = T, 3 = CT
        "player_death": pd.DataFrame(
            [
                # round 1
                {"tick": 100, "attacker_steamid": 1, "attacker_name": "alpha", "user_steamid": 11,
                 "weapon": "ak47", "headshot": True, "noscope": False, "thrusmoke": True, "penetrated": 0},
                {"tick": 200, "attacker_steamid": 11, "attacker_name": "carl", "user_steamid": 2,
                 "weapon": "knife", "headshot": False, "noscope": False, "thrusmoke": False, "penetrated": 1},
                {"tick": 250, "attacker_steamid": 1, "attacker_name": "alpha", "user_steamid": 2,
                 "weapon": "awp", "headshot": False, "noscope": False, "thrusmoke": False, "penetrated": 0},  # team kill
                {"tick": 260, "attacker_steamid": 0, "attacker_name": "", "user_steamid": 1,
                 "weapon": "world", "headshot": False, "noscope": False, "thrusmoke": False, "penetrated": 0},  # world
                # between rounds
                {"tick": 3500, "attacker_steamid": 1, "attacker_name": "alpha", "user_steamid": 11,
                 "weapon": "ak47", "headshot": False, "noscope": False, "thrusmoke": False, "penetrated": 0},
                # round 2
                {"tick": 7000, "attacker_steamid": 11, "attacker_name": "carl", "user_steamid": 1,
                 "weapon": "ak47", "headshot": False, "noscope": True, "thrusmoke": False, "penetrated": 0},
            ]
        ),
    }
    ticks = pd.DataFrame(
        player_rows(0, [(1, 2, 100, "alpha"), (2, 2, 100, "bravo"), (11, 3, 100, "carl"), (12, 3, 0, "dead")])
        + player_rows(6400, [(1, 2, 100, "alpha"), (11, 3, 100, "carl")])
    )
    return FakeParser(events, ticks)


def test_rounds_skip_duplicate_starts_and_pair_with_the_next_end():
    rounds = parse._rounds(build_fake())

    assert [(r.number, r.start_tick, r.end_tick, r.winner_side) for r in rounds] == [
        (1, 0, 3000, "T"),
        (2, 6400, 9000, "CT"),
    ]


def test_rosters_only_include_living_players_with_a_team():
    fake = build_fake()
    rounds = parse._rounds(fake)
    rosters = parse._rosters(fake, rounds)

    assert rosters[1] == {1: "T", 2: "T", 11: "CT"}  # 12 is dead at round start
    assert rosters[2] == {1: "T", 11: "CT"}


def test_kills_drop_warmup_gaps_team_kills_and_world_damage():
    fake = build_fake()
    rounds = parse._rounds(fake)
    rosters = parse._rosters(fake, rounds)
    kills = parse._kills(fake, rounds, rosters)

    assert [(k.tick, k.round_num) for k in kills] == [(100, 1), (200, 1), (7000, 2)]

    first, second, third = kills
    assert first.attacker_side == "T" and first.victim_side == "CT"
    assert first.headshot and first.through_smoke and not first.wallbang
    assert second.weapon == "knife" and second.wallbang
    assert third.noscope and third.attacker_side == "CT"


def test_tickrate_comes_from_header_when_present():
    assert parse._tickrate({"playback_ticks": "6400", "playback_time": "100"}) == 64.0
    assert parse._tickrate({}) == parse.DEFAULT_TICKRATE
    assert parse._tickrate({"playback_ticks": "0", "playback_time": "0"}) == parse.DEFAULT_TICKRATE
