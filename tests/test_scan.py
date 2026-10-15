from dataclasses import replace

from cs2hl.models import DemoData, DemoMeta, Kill, Round
from cs2hl.parse import ParsedDemo, Scoreboard
from cs2hl.scan import summarize


def kill(tick, rnd, attacker, victim, a_side, v_side):
    return Kill(
        tick=tick,
        round_num=rnd,
        attacker_steamid=attacker,
        attacker_name=f"p{attacker}",
        attacker_side=a_side,
        victim_steamid=victim,
        victim_side=v_side,
    )


def make_data():
    # Sides swap between rounds, as in a real match.
    rounds = [
        Round(number=1, start_tick=0, end_tick=3000, winner_side="T"),
        Round(number=2, start_tick=6400, end_tick=9600, winner_side="CT"),
    ]
    rosters = {
        1: {1: "T", 2: "T", 11: "CT", 12: "CT"},
        2: {1: "CT", 2: "CT", 11: "T", 12: "T"},
    }
    kills = [
        kill(100, 1, 1, 11, "T", "CT"),
        kill(200, 1, 1, 12, "T", "CT"),
        kill(300, 1, 11, 2, "CT", "T"),
        kill(7000, 2, 12, 2, "T", "CT"),
    ]
    meta = DemoMeta(demo_name="match.dem", map_name="de_mirage", tickrate=64.0)
    return DemoData(
        meta=meta,
        rounds=rounds,
        kills=kills,
        rosters=rosters,
        player_names={1: "alpha", 2: "bravo", 11: "carl", 12: "dave"},
    )


def test_players_sorted_by_kills_with_counts_and_last_side():
    scan = summarize(make_data())
    by_id = {p.steamid: p for p in scan.players}

    assert [p.steamid for p in scan.players][0] == 1  # two kills
    assert by_id[1].kills == 2 and by_id[1].deaths == 0
    assert by_id[2].kills == 0 and by_id[2].deaths == 2
    assert by_id[12].kills == 1 and by_id[12].deaths == 1  # killed by 1 in round 1
    assert by_id[1].rounds_played == 2
    assert by_id[1].side == "CT"  # last round they were in
    assert by_id[11].side == "T"
    assert by_id[1].name == "alpha"


def test_rounds_have_times_winner_and_kill_counts():
    scan = summarize(make_data())

    r1, r2 = scan.rounds
    assert (r1.number, r1.start_s, r1.end_s, r1.winner_side, r1.kills) == (1, 0.0, 46.875, "T", 3)
    assert (r2.number, r2.start_s, r2.end_s, r2.winner_side, r2.kills) == (2, 100.0, 150.0, "CT", 1)


def test_duration_and_metadata():
    scan = summarize(make_data())

    assert scan.map_name == "de_mirage"
    assert scan.demo_name == "match.dem"
    assert scan.duration_s == 150.0
    assert scan.data.rounds[0].number == 1  # raw data kept for the pipeline


def test_player_without_known_name_falls_back_to_steamid():
    data = make_data()
    data = DemoData(meta=data.meta, rounds=data.rounds, kills=data.kills, rosters=data.rosters, player_names={})
    scan = summarize(data)

    assert {p.name for p in scan.players} >= {"1", "11", "12"}


def test_players_who_never_joined_a_round_are_excluded():
    data = make_data()
    kills = data.kills + [kill(400, 1, 99, 11, None, "CT")]  # 99 is not on any roster
    data = DemoData(meta=data.meta, rounds=data.rounds, kills=kills, rosters=data.rosters, player_names=data.player_names)

    assert 99 not in {p.steamid for p in summarize(data).players}


def test_scan_demo_turns_parser_panics_into_value_error(tmp_path, monkeypatch):
    import cs2hl.parse as parse
    from cs2hl.scan import scan_demo

    class Panic(BaseException):
        pass

    def boom(path):
        raise Panic("range end index 16 out of range")

    monkeypatch.setattr(parse, "read_demo", boom)
    bogus = tmp_path / "not_a_demo.dem"
    bogus.write_bytes(b"junk")

    import pytest

    with pytest.raises(ValueError, match="not_a_demo.dem"):
        scan_demo(bogus)


def test_scoreboard_counts_replace_in_round_counts_for_players():
    # Player 11 also got a kill after round 1 ended, and player 2 died to world damage.
    board = Scoreboard(kills={1: 2, 11: 2, 12: 1}, deaths={2: 3, 11: 1, 12: 1})
    scan = summarize(make_data(), board)
    by_id = {p.steamid: p for p in scan.players}

    assert (by_id[11].kills, by_id[11].deaths) == (2, 1)
    assert (by_id[2].kills, by_id[2].deaths) == (0, 3)
    assert (by_id[1].kills, by_id[1].deaths) == (2, 0)
    assert [p.steamid for p in scan.players][:2] == [1, 11]  # 2 kills each, then by name
    assert [r.kills for r in scan.rounds] == [3, 1]  # rounds still count in-round kills only


def test_scan_exposes_parse_warnings():
    data = replace(make_data(), warnings=("Found 2 rounds, but the game counted 3.",))

    assert summarize(data).warnings == ("Found 2 rounds, but the game counted 3.",)
    assert summarize(make_data()).warnings == ()


def test_scan_demo_uses_the_scoreboard_counts(tmp_path, monkeypatch):
    import cs2hl.parse as parse
    from cs2hl.scan import scan_demo

    board = Scoreboard(kills={1: 5}, deaths={1: 4})
    monkeypatch.setattr(parse, "read_demo", lambda path: ParsedDemo(data=make_data(), scoreboard=board))

    scan = scan_demo(tmp_path / "match.dem")
    by_id = {p.steamid: p for p in scan.players}

    assert (by_id[1].kills, by_id[1].deaths) == (5, 4)
    assert by_id[11].kills == 0
    assert len(scan.data.kills) == 4  # the in-round kills the highlights are made from
