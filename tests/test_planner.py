from cs2hl.models import DemoMeta, Highlight, Round
from cs2hl.planner import PlanConfig, build_plan, load_plan, save_plan

META = DemoMeta(demo_name="match.dem", map_name="de_mirage", tickrate=64.0)
ROUNDS = [Round(number=1, start_tick=0, end_tick=10000, winner_side="T")]


def hl(start, end, *, score=8.0, pov=1, name="alpha", label="3K", rnd=1, tags=()):
    return Highlight(
        kind="multikill",
        label=label,
        round_num=rnd,
        player_steamid=pov,
        player_name=name,
        start_tick=start,
        end_tick=end,
        kills=3,
        won_round=True,
        score=score,
        tags=tags,
    )


def test_clip_window_has_pre_and_post_roll_in_seconds():
    plan = build_plan([hl(1000, 1100)], ROUNDS, META, PlanConfig(pre_roll_s=4, post_roll_s=2))

    clip = plan.clips[0]
    assert clip.start_tick == 1000 - 4 * 64
    assert clip.end_tick == 1100 + 2 * 64
    assert clip.start_s == round((1000 - 256) / 64, 3)
    assert clip.end_s == round((1100 + 128) / 64, 3)


def test_clip_start_is_clamped_to_round_start():
    rounds = [Round(number=1, start_tick=500, end_tick=10000, winner_side="T")]
    plan = build_plan([hl(600, 700)], rounds, META, PlanConfig(pre_roll_s=4))

    assert plan.clips[0].start_tick == 500


def test_overlapping_moments_from_same_player_and_round_merge():
    plan = build_plan(
        [hl(1000, 1100, label="2K"), hl(1200, 1300, label="3K", score=6.0)],
        ROUNDS,
        META,
        PlanConfig(pre_roll_s=4, post_roll_s=2),
    )

    assert len(plan.clips) == 1
    clip = plan.clips[0]
    assert clip.labels == ("2K", "3K")
    assert clip.score == 14.0
    assert clip.end_tick == 1300 + 128


def test_different_players_in_the_same_round_are_not_merged():
    plan = build_plan(
        [hl(1000, 1100, pov=1, name="alpha"), hl(1050, 1150, pov=2, name="bravo")],
        ROUNDS,
        META,
    )

    assert len(plan.clips) == 2
    assert {c.pov_name for c in plan.clips} == {"alpha", "bravo"}


def test_low_scoring_highlights_are_dropped():
    plan = build_plan([hl(1000, 1100, score=4.0)], ROUNDS, META, PlanConfig(min_score=5.0))

    assert plan.clips == []


def test_max_clips_keeps_best_then_plays_in_demo_order():
    highlights = [
        hl(1000, 1100, score=6.0, pov=1, name="a"),
        hl(3000, 3100, score=20.0, pov=2, name="b"),
        hl(5000, 5100, score=9.0, pov=3, name="c"),
    ]
    plan = build_plan(highlights, ROUNDS, META, PlanConfig(max_clips=2))

    assert [c.pov_name for c in plan.clips] == ["b", "c"]
    assert plan.clips[0].start_tick < plan.clips[1].start_tick


def test_plan_round_trips_through_json(tmp_path):
    plan = build_plan([hl(1000, 1100, label="3K", tags=("headshot",))], ROUNDS, META)
    path = tmp_path / "plan.json"

    save_plan(plan, path)
    loaded = load_plan(path)

    assert loaded.meta == META
    assert loaded.clips == plan.clips
    assert loaded.clips[0].labels == ("3K",)
    assert loaded.clips[0].tags == ("headshot",)
