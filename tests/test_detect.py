import pytest

from cs2hl.detect import DetectConfig, detect_highlights, highlight_label
from cs2hl.models import Kill, Round

TICKRATE = 64.0
T_PLAYERS = [1, 2, 3, 4, 5]
CT_PLAYERS = [11, 12, 13, 14, 15]
ROSTER = {**{p: "T" for p in T_PLAYERS}, **{p: "CT" for p in CT_PLAYERS}}


def kill(tick, attacker, victim, *, weapon="ak47", headshot=False, noscope=False,
         through_smoke=False, wallbang=False, rnd=1, roster=ROSTER):
    return Kill(
        tick=tick,
        round_num=rnd,
        attacker_steamid=attacker,
        attacker_name=f"p{attacker}",
        attacker_side=roster[attacker],
        victim_steamid=victim,
        victim_side=roster[victim],
        weapon=weapon,
        headshot=headshot,
        noscope=noscope,
        through_smoke=through_smoke,
        wallbang=wallbang,
    )


def run(kills, winner="T", roster=ROSTER, rnd_start=0, rnd_end=20000, **cfg):
    rounds = [Round(number=1, start_tick=rnd_start, end_tick=rnd_end, winner_side=winner)]
    return detect_highlights(rounds, kills, {1: roster}, DetectConfig(tickrate=TICKRATE, **cfg))


@pytest.mark.parametrize("count, label", [(2, "2K"), (3, "3K"), (4, "4K"), (5, "ACE")])
def test_label_is_the_players_kill_count_in_the_round(count, label):
    kills = [kill(1000 + i * 100, 1, victim) for i, victim in enumerate(CT_PLAYERS[:count])]

    found = run(kills)

    assert [(h.label, h.kind, h.kills) for h in found] == [(label, "multikill", count)]


def test_more_than_five_kills_is_still_an_ace():
    roster = {**ROSTER, 16: "CT"}
    kills = [kill(1000 + i * 100, 1, v, roster=roster) for i, v in enumerate([*CT_PLAYERS, 16])]

    assert [h.label for h in run(kills, roster=roster)] == ["ACE"]


def test_kills_far_apart_stay_one_highlight():
    # A 4K spread over 40 s used to be split by the old 10 s streak gap.
    ticks = [1000, 1000 + 12 * 64, 1000 + 25 * 64, 1000 + 40 * 64]
    kills = [kill(t, 1, v) for t, v in zip(ticks, CT_PLAYERS)]

    found = run(kills)

    assert len(found) == 1
    h = found[0]
    assert h.label == "4K"
    assert h.kill_ticks == tuple(ticks)
    assert (h.start_tick, h.end_tick, h.kills) == (ticks[0], ticks[-1], 4)


def test_win_bonus_and_score_for_a_3k():
    found = run([kill(1000, 1, 11), kill(1200, 1, 12), kill(1500, 1, 13)], winner="T")

    h = found[0]
    assert h.won_round is True
    assert h.score == 8.0  # 3K base 6 + win bonus 2


def test_lost_round_does_not_get_win_bonus():
    found = run([kill(1000, 1, 11), kill(1100, 1, 12)], winner="CT")

    h = found[0]
    assert h.won_round is False
    assert h.score == 3.0  # 2K base only


def test_tags_and_bonuses_for_special_kills():
    found = run(
        [
            kill(1000, 1, 11, headshot=True),
            kill(1100, 1, 12, weapon="knife_karambit", through_smoke=True),
        ],
        winner="T",
    )

    h = found[0]
    assert set(h.tags) == {"headshot", "knife", "smoke"}
    # base 3 + headshot 0.5 + knife 2 + smoke 1 + win 2
    assert h.score == 8.5


def test_single_kill_is_not_a_highlight_by_default():
    assert run([kill(1000, 1, 11), kill(1100, 2, 12)]) == []


def test_min_kills_is_configurable():
    kills = [kill(1000, 1, 11), kill(1100, 1, 12), kill(1200, 2, 13)]

    assert [h.player_steamid for h in run(kills, min_kills=3)] == []
    assert [(h.player_steamid, h.label) for h in run(kills, min_kills=1)] == [(1, "2K"), (2, "1K")]


def test_team_wipe_with_spread_kills_is_not_a_highlight():
    # Every CT dies, one kill each by five different T players: the old "TEAM ACE".
    kills = [kill(1000 + i * 100, p, 11 + i) for i, p in enumerate(T_PLAYERS)]
    found = run(kills, winner="T")

    assert found == []


def test_each_player_with_enough_kills_gets_their_own_highlight():
    kills = [kill(1000, 1, 11), kill(1100, 2, 12), kill(1200, 1, 13), kill(1300, 2, 14)]
    found = run(kills)

    assert [(h.player_steamid, h.label, h.kill_ticks) for h in found] == [
        (1, "2K", (1000, 1200)),
        (2, "2K", (1100, 1300)),
    ]


def test_same_player_in_two_rounds_gets_two_highlights():
    rounds = [
        Round(number=1, start_tick=0, end_tick=5000, winner_side="T"),
        Round(number=2, start_tick=6000, end_tick=12000, winner_side="CT"),
    ]
    kills = [kill(1000, 1, 11), kill(1100, 1, 12), kill(7000, 1, 11, rnd=2), kill(7100, 1, 12, rnd=2)]
    found = detect_highlights(rounds, kills, {1: ROSTER, 2: ROSTER}, DetectConfig(tickrate=TICKRATE))

    assert [(h.round_num, h.won_round) for h in found] == [(1, True), (2, False)]


def test_kills_after_round_end_and_team_kills_do_not_count():
    kills = [
        kill(1000, 1, 11),
        kill(1100, 1, 2),  # team kill
        kill(2100, 1, 12),  # after the round ended at 2000
    ]
    assert run(kills, rnd_end=2000) == []


def test_clutch_1v2_with_2k():
    # Start with T: 1, 2, 3 and CT: 11, 12. T2 and T3 die, leaving T1 alone vs two.
    roster = {1: "T", 2: "T", 3: "T", 11: "CT", 12: "CT"}
    kills = [
        kill(500, 11, 2),
        kill(800, 12, 3),  # T1 is now the lone survivor vs 11 and 12
        kill(900, 1, 11),
        kill(1000, 1, 12),
    ]
    found = run(kills, winner="T", roster=roster)

    assert len(found) == 1
    c = found[0]
    assert c.kind == "clutch"
    assert c.label == "2K + 1v2"
    assert c.player_steamid == 1
    assert (c.start_tick, c.end_tick, c.kills, c.kill_ticks) == (900, 1000, 2, (900, 1000))
    assert c.tags[0] == "clutch"
    assert c.score == 13.0  # 2K 3 + clutch 2 + 3 * 2 + win 2


def test_kills_before_the_clutch_count_toward_the_label():
    roster = {1: "T", 2: "T", 11: "CT", 12: "CT"}
    kills = [
        kill(400, 1, 11),
        kill(600, 12, 2),  # T1 is now alone vs 12
        kill(900, 1, 12),
    ]
    found = run(kills, winner="T", roster=roster)

    assert [(h.label, h.kill_ticks) for h in found] == [("2K + 1v1", (400, 900))]


def test_single_kill_clutch_is_a_highlight_unless_clutches_are_off():
    roster = {1: "T", 2: "T", 11: "CT"}
    kills = [kill(500, 11, 2), kill(900, 1, 11)]

    found = run(kills, winner="T", roster=roster)
    assert [(h.label, h.kind, h.kills) for h in found] == [("1v1", "clutch", 1)]
    assert run(kills, winner="T", roster=roster, include_clutches=False) == []


def test_no_clutch_when_the_lone_player_loses_the_round():
    roster = {1: "T", 2: "T", 3: "T", 11: "CT", 12: "CT"}
    kills = [kill(500, 11, 2), kill(800, 12, 3), kill(900, 1, 11), kill(1000, 1, 12)]
    found = run(kills, winner="CT", roster=roster)

    assert [(h.label, h.kind) for h in found] == [("2K", "multikill")]


def test_side_alone_from_round_start_counts_every_enemy():
    roster = {1: "T", 11: "CT", 12: "CT"}
    kills = [kill(900, 1, 11, roster=roster), kill(1000, 1, 12, roster=roster)]

    assert [h.label for h in run(kills, winner="T", roster=roster, rnd_start=100)] == ["2K + 1v2"]


def test_ace_clutch_label():
    roster = {1: "T", 2: "T", **{p: "CT" for p in CT_PLAYERS}}
    kills = [kill(500, 11, 2), *[kill(1000 + i * 100, 1, v) for i, v in enumerate(CT_PLAYERS)]]

    assert [h.label for h in run(kills, roster=roster)] == ["ACE + 1v5"]


def test_highlight_label_parts():
    assert highlight_label(2) == "2K"
    assert highlight_label(1) == "1K"
    assert highlight_label(1, 1) == "1v1"
    assert highlight_label(3, 2) == "3K + 1v2"
    assert highlight_label(7) == "ACE"


def test_no_highlights_for_round_without_kills():
    assert run([]) == []


def run_with_deaths(kills, deaths, winner="T", roster=ROSTER, rounds=None):
    rounds = rounds or [Round(number=1, start_tick=0, end_tick=20000, winner_side=winner)]
    rosters = {r.number: roster for r in rounds}
    return detect_highlights(rounds, kills, rosters, DetectConfig(tickrate=TICKRATE), deaths=deaths)


def deaths_of(kills, *others):
    """DemoData.deaths for these kills plus the (tick, victim) deaths without a kill, in round 1."""
    found = [(k.tick, k.round_num, k.victim_steamid) for k in kills] + [(t, 1, v) for t, v in others]
    return tuple(sorted(found))


def test_world_damage_death_makes_a_clutch():
    roster = {1: "T", 2: "T", 3: "T", 11: "CT", 12: "CT"}
    kills = [kill(500, 11, 2), kill(900, 1, 11), kill(1000, 1, 12)]
    deaths = deaths_of(kills, (700, 3))  # T3 falls to his death: T1 is alone vs two

    assert [(h.label, h.kind) for h in run_with_deaths(kills, deaths, roster=roster)] == [("2K + 1v2", "clutch")]
    assert [h.label for h in run(kills, roster=roster)] == ["2K"]  # kills alone keep T3 alive


def test_single_kill_clutch_after_a_world_death_is_kept():
    roster = {1: "T", 2: "T", 3: "T", 11: "CT"}
    kills = [kill(500, 11, 2), kill(900, 1, 11)]
    deaths = deaths_of(kills, (700, 3))

    assert [(h.label, h.kills) for h in run_with_deaths(kills, deaths, roster=roster)] == [("1v1", 1)]
    assert run(kills, roster=roster) == []


def test_team_kill_death_makes_a_clutch_but_is_not_one_of_the_kills():
    roster = {1: "T", 2: "T", 3: "T", 11: "CT", 12: "CT"}
    team_kill = kill(400, 1, 2)  # in kills as a fake might pass it; parse leaves it out
    kills = [team_kill, kill(600, 11, 3), kill(900, 1, 11), kill(1000, 1, 12)]

    found = run_with_deaths(kills, deaths_of(kills), roster=roster)

    assert [(h.label, h.kills, h.kill_ticks) for h in found] == [("2K + 1v2", 2, (900, 1000))]


def test_team_kill_left_out_of_kills_still_removes_the_victim():
    roster = {1: "T", 2: "T", 3: "T", 11: "CT", 12: "CT"}
    kills = [kill(600, 11, 3), kill(900, 1, 11), kill(1000, 1, 12)]
    deaths = deaths_of(kills, (400, 2))  # T1 team-killed T2, as parse records it: a death, no kill

    assert [h.label for h in run_with_deaths(kills, deaths, roster=roster)] == ["2K + 1v2"]


def test_suicide_makes_a_clutch():
    roster = {1: "T", 2: "T", 11: "CT", 12: "CT"}
    kills = [kill(900, 1, 11), kill(1000, 1, 12)]
    deaths = deaths_of(kills, (500, 2))  # T2 dies to his own grenade

    assert [h.label for h in run_with_deaths(kills, deaths, roster=roster)] == ["2K + 1v2"]


def test_a_suicide_in_kills_does_not_count_as_the_clutch_kill():
    roster = {1: "T", 2: "T", 11: "CT", 12: "CT"}
    kills = [kill(400, 1, 11), kill(600, 12, 2), kill(900, 1, 1)]  # T1 alone, then only kills himself

    assert run(kills, winner="T", roster=roster) == []


def test_without_deaths_kills_decide_who_is_alive():
    roster = {1: "T", 2: "T", 3: "T", 11: "CT", 12: "CT"}
    kills = [kill(500, 11, 2), kill(800, 12, 3), kill(900, 1, 11), kill(1000, 1, 12)]

    without = run_with_deaths(kills, (), roster=roster)

    assert without == run_with_deaths(kills, deaths_of(kills), roster=roster) == run(kills, roster=roster)
    assert [h.label for h in without] == ["2K + 1v2"]


def test_deaths_only_count_in_their_own_round():
    roster = {1: "T", 2: "T", 3: "T", 11: "CT", 12: "CT"}
    rounds = [
        Round(number=1, start_tick=0, end_tick=5000, winner_side="T"),
        Round(number=2, start_tick=6000, end_tick=12000, winner_side="T"),
    ]
    kills = [kill(500, 11, 2), kill(900, 1, 11), kill(1000, 1, 12)]
    deaths = (
        *deaths_of(kills),
        (7000, 2, 3),  # T3 dies in round 2, not round 1
        (5500, 1, 3),  # after round 1 ended
    )

    assert [h.label for h in run_with_deaths(kills, deaths, roster=roster, rounds=rounds)] == ["2K"]


def test_players_who_die_at_the_same_tick_leave_together():
    roster = {1: "T", 2: "T", 11: "CT", 12: "CT"}
    kills = [kill(500, 12, 2), kill(500, 2, 12), kill(900, 1, 11)]  # a trade on one tick

    assert [h.label for h in run(kills, winner="T", roster=roster)] == ["1v1"]
