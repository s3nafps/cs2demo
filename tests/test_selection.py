from cs2hl.models import Highlight
from cs2hl.selection import Selection, filter_highlights


def hl(player, rnd, label="2K"):
    return Highlight(
        kind="multikill",
        label=label,
        round_num=rnd,
        player_steamid=player,
        player_name=f"p{player}",
        start_tick=0,
        end_tick=10,
        kills=2,
        won_round=True,
        score=5.0,
    )


HIGHLIGHTS = [hl(1, 1), hl(2, 1), hl(1, 2), hl(3, 3)]


def test_no_filter_keeps_everything():
    assert filter_highlights(HIGHLIGHTS, Selection()) == HIGHLIGHTS


def test_player_filter_keeps_only_that_players_pov():
    kept = filter_highlights(HIGHLIGHTS, Selection.of(players=[1]))

    assert [(h.player_steamid, h.round_num) for h in kept] == [(1, 1), (1, 2)]


def test_round_filter_keeps_only_those_rounds():
    kept = filter_highlights(HIGHLIGHTS, Selection.of(rounds=[3]))

    assert [h.player_steamid for h in kept] == [3]


def test_player_and_round_filters_combine():
    kept = filter_highlights(HIGHLIGHTS, Selection.of(players=[1, 2], rounds=[1]))

    assert [(h.player_steamid, h.round_num) for h in kept] == [(1, 1), (2, 1)]


def test_empty_player_list_selects_nobody():
    assert filter_highlights(HIGHLIGHTS, Selection.of(players=[])) == []
