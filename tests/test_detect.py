from cs2hl.detect import DetectConfig, detect_highlights
from cs2hl.models import Kill, Round

TICKRATE = 64.0
T_PLAYERS = [1, 2, 3, 4, 5]
CT_PLAYERS = [11, 12, 13, 14, 15]
ROSTER = {**{p: "T" for p in T_PLAYERS}, **{p: "CT" for p in CT_PLAYERS}}


def kill(tick, attacker, victim, *, weapon="ak47", headshot=False, noscope=False,
         through_smoke=False, wallbang=False, rnd=1):
    return Kill(
        tick=tick,
        round_num=rnd,
        attacker_steamid=attacker,
        attacker_name=f"p{attacker}",
        attacker_side=ROSTER[attacker],
        victim_steamid=victim,
        victim_side=ROSTER[victim],
        weapon=weapon,
        headshot=headshot,
        noscope=noscope,
        through_smoke=through_smoke,
        wallbang=wallbang,
    )


def run(kills, winner="T", roster=ROSTER, rnd_end=20000):
    rounds = [Round(number=1, start_tick=0, end_tick=rnd_end, winner_side=winner)]
    return detect_highlights(rounds, kills, {1: roster}, DetectConfig(tickrate=TICKRATE))


def by_kind(highlights, kind):
    return [h for h in highlights if h.kind == kind]


def test_three_kill_streak_is_a_3k_and_scores_win_bonus():
    found = run([kill(1000, 1, 11), kill(1200, 1, 12), kill(1500, 1, 13)], winner="T")

    multis = by_kind(found, "multikill")
    assert len(multis) == 1
    h = multis[0]
    assert h.label == "3K"
    assert h.player_steamid == 1
    assert (h.start_tick, h.end_tick, h.kills) == (1000, 1500, 3)
    assert h.won_round is True
    assert h.score == 8.0  # 3K base 6 + win bonus 2


def test_gap_longer_than_streak_window_splits_kills():
    # 10 s window = 640 ticks. Second kill is 100 ticks later, third is far after.
    found = run([kill(1000, 1, 11), kill(1100, 1, 12), kill(3000, 1, 13)])

    multis = by_kind(found, "multikill")
    assert [h.label for h in multis] == ["2K"]
    assert (multis[0].start_tick, multis[0].end_tick) == (1000, 1100)


def test_lost_round_does_not_get_win_bonus():
    found = run([kill(1000, 1, 11), kill(1100, 1, 12)], winner="CT")

    h = by_kind(found, "multikill")[0]
    assert h.won_round is False
    assert h.score == 3.0  # 2K base only


def test_tags_and_bonuses_for_special_kills():
    found = run(
        [
            kill(1000, 1, 11, headshot=True),
            kill(1100, 1, 12, weapon="knife", through_smoke=True),
        ],
        winner="T",
    )

    h = by_kind(found, "multikill")[0]
    assert set(h.tags) == {"headshot", "knife", "smoke"}
    # base 3 + headshot 0.5 + knife 2 + smoke 1 + win 2
    assert h.score == 8.5


def test_five_kills_by_one_player_is_an_ace_and_suppresses_team_ace():
    kills = [kill(1000 + i * 100, 1, victim) for i, victim in enumerate(CT_PLAYERS)]
    found = run(kills, winner="T")

    labels = [h.label for h in found]
    assert "ACE" in labels
    assert "TEAM ACE" not in labels


def test_clutch_1v2_on_round_win():
    # Start with T: 1, 2, 3 and CT: 11, 12. T2 and T3 die, leaving T1 alone vs two.
    roster = {1: "T", 2: "T", 3: "T", 11: "CT", 12: "CT"}
    kills = [
        kill(500, 11, 2),
        kill(800, 12, 3),  # T1 is now the lone survivor vs 11 and 12
        kill(900, 1, 11),
        kill(1000, 1, 12),
    ]
    found = run(kills, winner="T", roster=roster)

    clutches = by_kind(found, "clutch")
    assert len(clutches) == 1
    c = clutches[0]
    assert c.label == "1v2"
    assert c.player_steamid == 1
    assert (c.start_tick, c.end_tick, c.kills) == (900, 1000, 2)
    assert "clutch" in c.tags


def test_no_clutch_when_the_lone_player_loses_the_round():
    kills = [kill(500, 11, 2), kill(800, 12, 3), kill(900, 1, 11), kill(1000, 1, 12)]
    found = run(kills, winner="CT", roster={1: "T", 2: "T", 3: "T", 11: "CT", 12: "CT"})

    assert by_kind(found, "clutch") == []


def test_team_ace_when_one_side_kills_all_enemies_with_no_single_player_ace():
    kills = [kill(1000 + i * 100, p, 11 + i) for i, p in enumerate(T_PLAYERS)]
    found = run(kills, winner="T")

    aces = by_kind(found, "team_ace")
    assert len(aces) == 1
    assert aces[0].label == "TEAM ACE"
    assert aces[0].kills == 5
    assert aces[0].won_round is True
    assert by_kind(found, "multikill") == []


def test_no_highlights_for_round_without_kills():
    assert run([]) == []
