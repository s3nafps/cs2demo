"""Tests for the demoparser2 adapter, using a fake parser with the shapes real demos give.

Shapes copied from demoparser2 0.42.0 on real demos: steamids in events are strings and
in parse_ticks are ints, round_end.winner is "T"/"CT", a missing event is an empty list,
parse_ticks omits props it does not know, and the header has no playback length.
tests/test_real_demos.py runs the same code on real .dem files.
"""

import math

import pandas as pd
import pytest

from cs2hl import parse
from cs2hl.models import Round

BASE = 76561198000000000
ALPHA, BRAVO, ECHO, GOLF = BASE + 1, BASE + 2, BASE + 3, BASE + 4  # T
CARL, DAVE, FOX = BASE + 11, BASE + 12, BASE + 13  # CT
SPECTATOR = BASE + 99

# steamid -> (name, team_num, user_id)
PLAYERS = {
    ALPHA: ("alpha", 2, 0),
    BRAVO: ("bravo", 2, 1),
    ECHO: ("echo", 2, 2),
    CARL: ("carl", 3, 3),
    DAVE: ("dave", 3, 4),
    FOX: ("fox", 3, 5),
    SPECTATOR: ("caster", 1, 6),
    GOLF: ("golf", 2, 7),  # joins before round 2
}

# What the game's own counters say at the win panel (tick 6200) for the fake match below.
FINAL_KILLS = {ALPHA: 3, BRAVO: 0, ECHO: 0, GOLF: 0, CARL: 1, DAVE: 1, FOX: 1, SPECTATOR: 0}
FINAL_DEATHS = {ALPHA: 1, BRAVO: 3, ECHO: 2, GOLF: 0, CARL: 1, DAVE: 1, FOX: 1, SPECTATOR: 0}

# tick -> (is_warmup_period, total_rounds_played)
TICK_STATE = {
    0: (False, 0),  # knife round start
    500: (False, 1),  # knife round end
    600: (True, 0),  # warmup
    900: (False, 0),  # match round 1 start
    3000: (False, 1),
    3400: (False, 1),
    6000: (False, 2),
    6200: (False, 2),  # cs_win_panel_match
}


class FakeParser:
    def __init__(self, events, states, header=None):
        self.events = events
        self.states = states
        self.header = header if header is not None else {"map_name": "de_mirage", "patch_version": "14068"}
        self.tick_calls = []
        self.event_calls = []

    def parse_header(self):
        return dict(self.header)

    def parse_event(self, name):
        self.event_calls.append(name)
        return self.events[name].copy() if name in self.events else []

    def parse_events(self, names):
        self.event_calls.append(tuple(names))
        return [(name, self.events[name].copy()) for name in names if name in self.events]

    def parse_ticks(self, props, ticks=None):
        self.tick_calls.append((tuple(props), tuple(ticks)))
        df = self.states[self.states["tick"].isin(ticks)]
        keep = [c for c in df.columns if c in props or c in ("tick", "steamid", "name")]
        return df[keep].reset_index(drop=True)


class PerEventParser(FakeParser):
    parse_events = None


def sid(steamid):
    return str(steamid)


def death(tick, attacker, victim, weapon="ak47", **flags):
    row = {
        "tick": tick,
        "attacker_steamid": sid(attacker) if attacker else float("nan"),
        "attacker_name": PLAYERS[attacker][0] if attacker else float("nan"),
        "user_steamid": sid(victim),
        "user_name": PLAYERS[victim][0],
        "weapon": weapon,
        "headshot": False,
        "noscope": False,
        "thrusmoke": False,
        "penetrated": 0,
    }
    row.update(flags)
    return row


def build_events(begin_ticks=(0, 905), win_panel=True):
    events = {
        "round_start": pd.DataFrame(
            {"round": [1, 1, 1, 2, 2], "tick": [0, 600, 900, 3400, 3400]}  # 3400 twice
        ),
        "round_end": pd.DataFrame(
            {
                "reason": ["ct_killed", "bomb_exploded", "t_killed"],
                "round": [1, 2, 3],
                "tick": [500, 3000, 6000],
                "winner": pd.array(["T", "T", "CT"], dtype="string"),
            }
        ),
        "player_death": pd.DataFrame(
            [
                death(100, ALPHA, CARL, "knife_karambit"),  # knife round
                death(700, CARL, ALPHA),  # warmup
                # round 1
                death(1000, ALPHA, CARL, headshot=True, thrusmoke=True),
                death(1100, DAVE, BRAVO, "knife", penetrated=1),
                death(1150, ALPHA, ECHO, "awp"),  # team kill
                death(1300, ALPHA, DAVE),
                death(3100, ALPHA, FOX),  # exit kill: after round_end, before the next round_start
                # round 2
                death(3500, FOX, ALPHA, "awp", noscope=True),
                death(3700, ECHO, ECHO, "world"),  # world damage: attacker is the victim
                death(3800, 0, BRAVO, "world"),  # no attacker at all
                death(6100, CARL, BRAVO),  # after the last round_end, before the win panel
                death(6500, CARL, CARL, "world"),  # disconnect after the match
            ]
        ),
    }
    if begin_ticks:
        events["begin_new_match"] = pd.DataFrame({"tick": list(begin_ticks)})
    if win_panel:
        events["cs_win_panel_match"] = pd.DataFrame({"tick": [6200]})
    return events


def build_states(ticks=TICK_STATE, kills=FINAL_KILLS, deaths=FINAL_DEATHS, played=None, score=(1, 1)):
    rows = []
    for tick, (warmup, rounds_played) in ticks.items():
        for steamid, (name, team, user_id) in PLAYERS.items():
            if steamid == GOLF and tick < 3400:
                continue
            final = tick == 6200
            rows.append(
                {
                    "tick": tick,
                    "steamid": steamid,
                    "name": name,
                    "team_num": team,
                    "health": 100 if team in (2, 3) else 0,
                    "user_id": user_id,
                    "is_warmup_period": warmup,
                    "game_time": 10.0 + tick / 64,
                    "kills_total": kills[steamid] if final else 0,
                    "deaths_total": deaths[steamid] if final else 0,
                    "total_rounds_played": played if final and played is not None else rounds_played,
                    "team_rounds_total": {2: score[0], 3: score[1]}.get(team, 0) if final else 0,
                }
            )
    return pd.DataFrame(rows)


def build_fake(cls=FakeParser, **kwargs):
    events = build_events(**{k: kwargs.pop(k) for k in ("begin_ticks", "win_panel") if k in kwargs})
    return cls(events, build_states(**kwargs))


def spans(rounds):
    return [(r.number, r.start_tick, r.end_tick, r.winner_side) for r in rounds]


MATCH_ROUNDS = [(1, 900, 3000, "T"), (2, 3400, 6000, "CT")]


def test_rounds_skip_knife_round_and_warmup_before_the_last_begin_new_match():
    rounds = parse._rounds(build_events())

    # Round 1 opens at the last round_start after the knife round's end, even though
    # begin_new_match (905) comes a few ticks later, as on the FACEIT demo.
    assert spans(rounds) == MATCH_ROUNDS


def test_begin_new_match_after_the_last_round_end_is_ignored():
    rounds = parse._rounds(build_events(begin_ticks=(0, 905, 7000)))

    assert spans(rounds) == MATCH_ROUNDS


def test_without_begin_new_match_a_round_undone_by_a_restart_is_dropped():
    # total_rounds_played is 1 at the knife round's end and again at round 1's end.
    fake = build_fake(begin_ticks=())
    assert len(parse._rounds(fake.events)) == 3

    data = parse.parse_with(fake, "match.dem").data

    assert spans(data.rounds) == MATCH_ROUNDS
    assert 100 not in {k.tick for k in data.kills}


def test_rounds_that_start_in_warmup_are_dropped_and_renumbered():
    rounds = [Round(1, 600, 800, "T"), Round(2, 900, 3000, "CT")]
    states = {
        600: [{"is_warmup_period": True}],
        800: [{"total_rounds_played": 0}],
        900: [{"is_warmup_period": False}],
        3000: [{"total_rounds_played": 1}],
    }

    assert spans(parse._live_rounds(rounds, states)) == [(1, 900, 3000, "CT")]


def test_live_rounds_keep_everything_when_counters_are_missing():
    rounds = [Round(1, 0, 10, "T"), Round(2, 20, 30, "CT")]

    assert parse._live_rounds(rounds, {}) == rounds


@pytest.mark.parametrize(
    "value, side",
    [("T", "T"), ("CT", "CT"), ("ct", "CT"), (2, "T"), (3, "CT"), ("2", "T"), ("3", "CT"), (3.0, "CT"),
     (float("nan"), None), (None, None), (pd.NA, None), ("", None), (1, None)],
)
def test_winner_accepts_side_names_and_team_numbers(value, side):
    assert parse._side(value) == side


def test_round_end_rows_at_the_same_tick_count_once():
    df = pd.DataFrame({"tick": [3000, 3000, 500], "winner": ["T", "CT", "CT"]})

    assert parse._round_ends(df) == [(500, "CT"), (3000, "T")]


def test_missing_events_come_back_as_lists_and_are_treated_as_empty():
    fake = build_fake()
    del fake.events["player_death"], fake.events["begin_new_match"], fake.events["cs_win_panel_match"]
    assert fake.parse_event("player_death") == []

    data = parse.parse_with(fake, "match.dem").data

    assert len(data.rounds) == 2
    assert data.kills == []


def test_demo_without_round_end_has_no_rounds_and_says_so():
    fake = build_fake()
    del fake.events["round_end"]

    data = parse.parse_with(fake, "match.dem").data

    assert data.rounds == [] and data.kills == []
    assert data.meta.tickrate == parse.DEFAULT_TICKRATE
    assert data.warnings == ("No rounds were found in this demo.",)


def test_events_are_read_in_one_pass_when_the_parser_can():
    fake = build_fake()
    parse.parse_with(fake, "match.dem")

    assert fake.event_calls == [parse.EVENTS]


def test_events_fall_back_to_one_call_per_event():
    fake = build_fake(PerEventParser)
    data = parse.parse_with(fake, "match.dem").data

    assert fake.event_calls == list(parse.EVENTS)
    assert spans(data.rounds) == MATCH_ROUNDS


def test_player_props_come_from_one_parse_ticks_call():
    fake = build_fake()
    parse.parse_with(fake, "match.dem")

    assert len(fake.tick_calls) == 1
    props, ticks = fake.tick_calls[0]
    assert set(ticks) == {900, 3000, 3400, 6000, 6200}
    assert "user_id" in props and "kills_total" in props


def test_rosters_only_include_living_players_with_a_team():
    fake = build_fake()
    data = parse.parse_with(fake, "match.dem").data

    assert data.rosters[1] == {ALPHA: "T", BRAVO: "T", ECHO: "T", CARL: "CT", DAVE: "CT", FOX: "CT"}
    assert data.rosters[2][GOLF] == "T"
    assert data.player_names[GOLF] == "golf"
    assert data.player_names[SPECTATOR] == "caster"


def test_kills_inside_rounds_drop_warmup_knife_team_kills_and_world_damage():
    parsed = parse.parse_with(build_fake(), "match.dem")
    kills = parsed.data.kills

    assert [(k.tick, k.round_num) for k in kills] == [(1000, 1), (1100, 1), (1300, 1), (3500, 2)]
    first, second, _, fourth = kills
    assert first.attacker_steamid == ALPHA and first.victim_steamid == CARL
    assert first.attacker_name == "alpha"
    assert first.attacker_side == "T" and first.victim_side == "CT"
    assert first.headshot and first.through_smoke and not first.wallbang
    assert second.weapon == "knife" and second.wallbang
    assert fourth.noscope and fourth.attacker_side == "CT"


def test_exit_kills_are_kept_apart_from_round_kills():
    parsed = parse.parse_with(build_fake(), "match.dem")

    # 3100 is between round 1's end and round 2's start; 6100 is before the win panel.
    assert [(k.tick, k.round_num, k.attacker_steamid) for k in parsed.exit_kills] == [
        (3100, 1, ALPHA),
        (6100, 2, CARL),
    ]


def test_scoreboard_counts_exit_kills_and_every_death():
    board = parse.parse_with(build_fake(), "match.dem").scoreboard

    assert board.kills == {k: v for k, v in FINAL_KILLS.items() if v}
    assert board.deaths == {k: v for k, v in FINAL_DEATHS.items() if v}
    assert board.deaths[CARL] == 1  # the knife round and the disconnect after the match do not count


def test_without_a_win_panel_nothing_after_the_last_round_counts():
    parsed = parse.parse_with(build_fake(win_panel=False), "match.dem")

    assert [k.tick for k in parsed.exit_kills] == [3100]
    assert parsed.scoreboard.deaths[BRAVO] == 2


def test_player_slots_are_user_id_plus_one_including_late_joiners():
    data = parse.parse_with(build_fake(), "match.dem").data

    assert data.player_slots == {ALPHA: 1, BRAVO: 2, ECHO: 3, CARL: 4, DAVE: 5, FOX: 6, GOLF: 8}


def test_a_changed_slot_keeps_the_first_and_is_reported():
    fake = build_fake()
    fake.states.loc[(fake.states["steamid"] == FOX) & (fake.states["tick"] >= 3400), "user_id"] = 9

    data = parse.parse_with(fake, "match.dem").data

    assert data.player_slots[FOX] == 6
    assert data.warnings == ("fox: camera slot changed from 6 to 10 in round 2; using 6.",)


def test_slots_skip_players_without_user_id():
    fake = build_fake()
    fake.states = fake.states.drop(columns=["user_id"])

    assert parse.parse_with(fake, "match.dem").data.player_slots == {}


def test_tickrate_comes_from_game_time():
    data = parse.parse_with(build_fake(), "match.dem").data

    assert data.meta.tickrate == 64.0
    assert data.meta.map_name == "de_mirage"
    assert data.meta.demo_name == "match.dem"


def states_with_game_time(start, end):
    return {900: [{"game_time": start}], 6000: [{"game_time": end}]}


ROUNDS = [Round(1, 900, 3000, "T"), Round(2, 3400, 6000, "CT")]


def test_tickrate_snaps_to_a_whole_rate_and_keeps_odd_ones():
    assert parse._tickrate(states_with_game_time(10.0, 10.0 + 5100 / 64.0002), ROUNDS) == 64.0
    assert parse._tickrate(states_with_game_time(10.0, 10.0 + 5100 / 128), ROUNDS) == 128.0
    assert math.isclose(parse._tickrate(states_with_game_time(0.0, 5100 / 102.4), ROUNDS), 102.4)


@pytest.mark.parametrize(
    "states",
    [{}, states_with_game_time(10.0, 10.0), states_with_game_time(10.0, 9.0),
     states_with_game_time(0.0, 1.0), states_with_game_time(float("nan"), 20.0)],
)
def test_tickrate_falls_back_to_64(states):
    assert parse._tickrate(states, ROUNDS) == parse.DEFAULT_TICKRATE
    assert parse._tickrate(states, []) == parse.DEFAULT_TICKRATE


def test_a_parse_that_matches_the_game_has_no_warnings():
    assert parse.parse_with(build_fake(), "match.dem").data.warnings == ()


def test_kill_and_death_mismatches_with_the_game_are_reported():
    kills = {**FINAL_KILLS, FOX: 2, ALPHA: 2}
    deaths = {**FINAL_DEATHS, DAVE: 3}

    data = parse.parse_with(build_fake(kills=kills, deaths=deaths), "match.dem").data

    assert set(data.warnings) == {
        "fox: found 1 kill, but the game counted 2.",
        "alpha: found 3 kills, but the game counted 2. They have 1 team kill, which the game may subtract.",
        "dave: found 1 death, but the game counted 3.",
    }


def test_round_count_mismatch_is_reported():
    data = parse.parse_with(build_fake(played=3), "match.dem").data

    assert data.warnings == ("Found 2 rounds, but the game counted 3.",)


def test_score_mismatch_is_reported():
    data = parse.parse_with(build_fake(score=(2, 0)), "match.dem").data

    assert data.warnings == ("Score found 1-1, but the game says 0-2 (the team that finished on CT first).",)


def test_missing_game_counters_are_reported_once():
    fake = build_fake()
    fake.states = fake.states.drop(columns=["kills_total", "deaths_total"])

    assert parse.parse_with(fake, "match.dem").data.warnings == (
        "The game's own scoreboard could not be read, so rounds and kills were not checked.",
    )


def test_load_demo_uses_demoparser2(monkeypatch, tmp_path):
    import demoparser2

    fake = build_fake()
    monkeypatch.setattr(demoparser2, "DemoParser", lambda path: fake)

    data = parse.load_demo(tmp_path / "match.dem")

    assert data.meta.demo_name == "match.dem"
    assert spans(data.rounds) == MATCH_ROUNDS
