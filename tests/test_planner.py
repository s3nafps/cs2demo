import json

from cs2hl.detect import DetectConfig, detect_highlights
from cs2hl.models import DemoMeta, Highlight, Kill, Round
from cs2hl.planner import (
    PlanConfig,
    build_plan,
    clip_segments,
    load_plan,
    merge_windows,
    recorded_seconds,
    save_plan,
)

TR = 64
META = DemoMeta(demo_name="match.dem", map_name="de_mirage", tickrate=64.0)
ROUNDS = [
    Round(number=1, start_tick=0, end_tick=10000, winner_side="T"),
    Round(number=2, start_tick=12000, end_tick=20000, winner_side="CT"),
    Round(number=3, start_tick=22000, end_tick=30000, winner_side="T"),
]
CFG = PlanConfig(pre_roll_s=3, post_roll_s=2, merge_gap_s=3)


def hl(kill_ticks, *, score=8.0, pov=1, name="alpha", label="2K", rnd=1, tags=()):
    kill_ticks = tuple(kill_ticks)
    return Highlight(
        kind="multikill",
        label=label,
        round_num=rnd,
        player_steamid=pov,
        player_name=name,
        start_tick=kill_ticks[0],
        end_tick=kill_ticks[-1],
        kills=len(kill_ticks),
        won_round=True,
        score=score,
        tags=tags,
        kill_ticks=kill_ticks,
    )


def test_each_kill_gets_a_window_with_pre_and_post_roll():
    plan = build_plan([hl([1000])], ROUNDS, META, CFG)

    clip = plan.clips[0]
    assert clip.segments == ((1000 - 3 * TR, 1000 + 2 * TR),)
    assert (clip.start_tick, clip.end_tick) == clip.segments[0]
    assert clip.start_s == round((1000 - 3 * TR) / 64, 3)
    assert clip.end_s == round((1000 + 2 * TR) / 64, 3)


def test_kills_far_apart_become_separate_segments_of_one_clip():
    ticks = [1000, 1000 + 12 * TR, 1000 + 25 * TR, 1000 + 40 * TR]
    plan = build_plan([hl(ticks, label="4K")], ROUNDS, META, CFG)

    assert len(plan.clips) == 1
    clip = plan.clips[0]
    assert clip.segments == tuple((t - 3 * TR, t + 2 * TR) for t in ticks)
    assert clip.start_tick == ticks[0] - 3 * TR
    assert clip.end_tick == ticks[-1] + 2 * TR
    assert clip.labels == ("4K",)
    assert recorded_seconds(clip, 64.0) == 4 * 5.0


def test_windows_with_a_short_gap_merge():
    # Windows are 5 s long, so kills 8 s apart leave a 3 s gap: merged. 8.5 s apart: not merged.
    merged = build_plan([hl([1000, 1000 + 8 * TR])], ROUNDS, META, CFG).clips[0]
    split = build_plan([hl([1000, 1000 + 8 * TR + 32])], ROUNDS, META, CFG).clips[0]

    assert merged.segments == ((1000 - 3 * TR, 1000 + 10 * TR),)
    assert len(split.segments) == 2


def test_overlapping_windows_merge():
    assert merge_windows([(0, 100), (50, 200), (400, 500)], gap_ticks=0) == ((0, 200), (400, 500))
    assert merge_windows([(400, 500), (0, 100)], gap_ticks=300) == ((0, 500),)
    assert merge_windows([], gap_ticks=10) == ()


def test_windows_are_clamped_to_the_round():
    rounds = [Round(number=1, start_tick=900, end_tick=2000, winner_side="T")]
    # The second tick is after the round end, which detect never produces; it is clamped anyway.
    plan = build_plan([hl([1000, 2300])], rounds, META, CFG)

    assert plan.clips[0].segments == ((900, 1000 + 2 * TR), (2300 - 3 * TR, 2000 + 2 * TR))


def test_highlight_without_kill_ticks_gets_one_window():
    h = Highlight(
        kind="multikill", label="2K", round_num=1, player_steamid=1, player_name="a",
        start_tick=1000, end_tick=1500, kills=2, won_round=True, score=5.0,
    )
    clip = build_plan([h], ROUNDS, META, CFG).clips[0]

    assert clip.segments == ((1000 - 3 * TR, 1500 + 2 * TR),)


def test_pov_slot_comes_from_player_slots():
    highlights = [hl([1000], pov=1, name="a"), hl([3000], pov=2, name="b")]

    plan = build_plan(highlights, ROUNDS, META, CFG, player_slots={1: 4})
    assert [c.pov_slot for c in plan.clips] == [4, None]
    assert [c.pov_slot for c in build_plan(highlights, ROUNDS, META, CFG).clips] == [None, None]


def test_ids_are_numbered_in_demo_order_with_the_round():
    highlights = [hl([23000], rnd=3), hl([13000], rnd=2), hl([1000], rnd=1)]
    plan = build_plan(highlights, ROUNDS, META, CFG)

    assert [c.id for c in plan.clips] == ["clip001_r01", "clip002_r02", "clip003_r03"]


def test_different_players_in_the_same_round_get_separate_clips():
    plan = build_plan(
        [hl([1000, 1100], pov=2, name="bravo"), hl([1000, 1100], pov=1, name="alpha")],
        ROUNDS,
        META,
    )

    assert [c.pov_name for c in plan.clips] == ["alpha", "bravo"]


def test_default_min_score_keeps_every_highlight():
    plan = build_plan([hl([1000], score=0.0), hl([3000], score=1.0)], ROUNDS, META)

    assert len(plan.clips) == 2


def test_low_scoring_highlights_are_dropped_when_min_score_is_raised():
    plan = build_plan([hl([1000], score=4.0), hl([3000], score=6.0)], ROUNDS, META, PlanConfig(min_score=5.0))

    assert [c.score for c in plan.clips] == [6.0]


def test_max_clips_keeps_best_then_plays_in_demo_order():
    highlights = [
        hl([1000], score=6.0, pov=1, name="a"),
        hl([13000], score=20.0, pov=2, name="b", rnd=2),
        hl([23000], score=9.0, pov=3, name="c", rnd=3),
        hl([25000], score=9.0, pov=4, name="d", rnd=3),
    ]
    plan = build_plan(highlights, ROUNDS, META, PlanConfig(max_clips=2))

    # b is best; c and d tie and the earlier one wins.
    assert [c.pov_name for c in plan.clips] == ["b", "c"]
    assert [c.id for c in plan.clips] == ["clip001_r02", "clip002_r03"]


def test_plan_round_trips_through_json(tmp_path):
    plan = build_plan(
        [hl([1000, 1000 + 20 * TR], label="2K + 1v1", tags=("clutch", "headshot"))],
        ROUNDS,
        META,
        CFG,
        player_slots={1: 7},
    )
    path = tmp_path / "plan.json"

    save_plan(plan, path)
    loaded = load_plan(path)

    assert loaded.meta == META
    assert loaded.clips == plan.clips
    clip = loaded.clips[0]
    assert clip.labels == ("2K + 1v1",)
    assert clip.tags == ("clutch", "headshot")
    assert len(clip.segments) == 2 and all(isinstance(s, tuple) for s in clip.segments)
    assert clip.pov_slot == 7


def test_old_plan_files_without_segments_still_load(tmp_path):
    clip = {
        "id": "clip001_r01", "round_num": 1, "pov_steamid": 1, "pov_name": "a",
        "start_tick": 100, "end_tick": 400, "start_s": 1.5625, "end_s": 6.25,
        "score": 5.0, "labels": ["2K"], "tags": [], "unknown_key": 1,
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"demo": "m.dem", "map": "de_x", "tickrate": 64, "clips": [clip]}))

    loaded = load_plan(path).clips[0]

    assert loaded.segments == () and loaded.pov_slot is None
    assert clip_segments(loaded) == ((100, 400),)


def kill(tick, attacker, victim, sides=("T", "CT")):
    return Kill(
        tick=tick, round_num=1, attacker_steamid=attacker, attacker_name=f"p{attacker}",
        attacker_side=sides[0], victim_steamid=victim, victim_side=sides[1],
    )


def test_spread_4k_from_detect_becomes_one_clip_with_several_segments():
    roster = {1: "T", 2: "T", 11: "CT", 12: "CT", 13: "CT", 14: "CT", 15: "CT"}
    ticks = [1000, 1000 + 12 * TR, 1000 + 15 * TR, 1000 + 40 * TR]
    kills = [kill(t, 1, v) for t, v in zip(ticks, [11, 12, 13, 14])]
    highlights = detect_highlights(ROUNDS[:1], kills, {1: roster}, DetectConfig(tickrate=64.0))

    plan = build_plan(highlights, ROUNDS, META, CFG, player_slots={1: 2})

    assert [c.labels for c in plan.clips] == [("4K",)]
    clip = plan.clips[0]
    # The kills at 12 s and 15 s merge; the others stand alone.
    assert clip.segments == (
        (ticks[0] - 3 * TR, ticks[0] + 2 * TR),
        (ticks[1] - 3 * TR, ticks[2] + 2 * TR),
        (ticks[3] - 3 * TR, ticks[3] + 2 * TR),
    )
    assert clip.pov_slot == 2
