"""Turn highlights into clips and a plan file that the capture and export steps consume.

Each highlight becomes one clip. The clip keeps only the moments around the kills: a
window from ``pre_roll_s`` before to ``post_roll_s`` after each kill, with windows that
are close together joined. The windows are the clip's ``segments``; export joins them
with cuts.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .models import Clip, DemoMeta, Highlight, Plan, Round

Segment = tuple[int, int]


@dataclass(frozen=True)
class PlanConfig:
    pre_roll_s: float = 3.0  # kept before each kill
    post_roll_s: float = 2.0  # kept after each kill
    merge_gap_s: float = 3.0  # windows with a gap this short or shorter become one segment
    min_score: float = 0.0  # 0 keeps every highlight
    max_clips: int | None = None  # keep the best N by score


def build_plan(
    highlights: list[Highlight],
    rounds: list[Round],
    meta: DemoMeta,
    cfg: PlanConfig | None = None,
    player_slots: dict[int, int] | None = None,
) -> Plan:
    """One clip per highlight, in demo order. ``player_slots`` maps steamid to spectator slot."""
    cfg = cfg or PlanConfig()
    slots = player_slots or {}
    rounds_by_number = {r.number: r for r in rounds}

    kept = [h for h in highlights if h.score >= cfg.min_score]
    if cfg.max_clips is not None and len(kept) > cfg.max_clips:
        kept = sorted(kept, key=lambda h: (-h.score, h.start_tick))[: max(cfg.max_clips, 0)]

    pieces = []
    for h in kept:
        segments = highlight_segments(h, rounds_by_number.get(h.round_num), meta.tickrate, cfg)
        if segments:
            pieces.append((h, segments))
    pieces.sort(key=lambda p: (p[1][0][0], p[0].player_steamid))

    clips = [
        _clip(index, h, segments, meta.tickrate, slots.get(h.player_steamid))
        for index, (h, segments) in enumerate(pieces, start=1)
    ]
    return Plan(meta=meta, clips=clips)


def highlight_segments(
    h: Highlight, rnd: Round | None, tickrate: float, cfg: PlanConfig
) -> tuple[Segment, ...]:
    """Kill windows for one highlight, clamped to its round and merged.

    A highlight without ``kill_ticks`` gets one window around its start and end ticks.
    """
    pre = round(cfg.pre_roll_s * tickrate)
    post = round(cfg.post_roll_s * tickrate)
    floor = max(0, rnd.start_tick) if rnd else 0
    ceiling = rnd.end_tick + post if rnd else None

    spans = [(t, t) for t in h.kill_ticks] or [(h.start_tick, h.end_tick)]
    windows = []
    for first, last in spans:
        start = max(floor, first - pre)
        end = last + post if ceiling is None else min(ceiling, last + post)
        if end > start:
            windows.append((start, end))
    return merge_windows(windows, round(cfg.merge_gap_s * tickrate))


def merge_windows(windows: list[Segment], gap_ticks: int) -> tuple[Segment, ...]:
    """Join windows that overlap or are at most ``gap_ticks`` apart."""
    merged: list[Segment] = []
    for start, end in sorted(windows):
        if merged and start - merged[-1][1] <= gap_ticks:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def clip_segments(clip: Clip) -> tuple[Segment, ...]:
    """The pieces to record for a clip. A clip without segments is one piece."""
    return clip.segments or ((clip.start_tick, clip.end_tick),)


def recorded_seconds(clip: Clip, tickrate: float) -> float:
    """Length of the clip after its segments are joined: what actually gets recorded."""
    return sum(end - start for start, end in clip_segments(clip)) / tickrate


def _clip(
    index: int, h: Highlight, segments: tuple[Segment, ...], tickrate: float, pov_slot: int | None
) -> Clip:
    start, end = segments[0][0], segments[-1][1]
    return Clip(
        id=f"clip{index:03d}_r{h.round_num:02d}",
        round_num=h.round_num,
        pov_steamid=h.player_steamid,
        pov_name=h.player_name,
        start_tick=start,
        end_tick=end,
        start_s=round(start / tickrate, 3),
        end_s=round(end / tickrate, 3),
        score=round(h.score, 2),
        labels=(h.label,),
        tags=tuple(h.tags),
        segments=segments,
        pov_slot=pov_slot,
    )


def plan_to_dict(plan: Plan) -> dict:
    return {
        "demo": plan.meta.demo_name,
        "map": plan.meta.map_name,
        "tickrate": plan.meta.tickrate,
        "clips": [asdict(c) for c in plan.clips],
    }


def plan_from_dict(data: dict) -> Plan:
    meta = DemoMeta(demo_name=data["demo"], map_name=data["map"], tickrate=float(data["tickrate"]))
    return Plan(meta=meta, clips=[_clip_from_dict(c) for c in data["clips"]])


def save_plan(plan: Plan, path: Path) -> None:
    path.write_text(json.dumps(plan_to_dict(plan), indent=2) + "\n", encoding="utf-8")


def load_plan(path: Path) -> Plan:
    return plan_from_dict(json.loads(path.read_text(encoding="utf-8")))


_CLIP_FIELDS = frozenset(f.name for f in fields(Clip))


def _clip_from_dict(c: dict) -> Clip:
    """Older plan files have no segments or pov_slot; unknown keys are ignored."""
    values = {k: v for k, v in c.items() if k in _CLIP_FIELDS}
    values["labels"] = tuple(values.get("labels", ()))
    values["tags"] = tuple(values.get("tags", ()))
    values["segments"] = tuple((int(s), int(e)) for s, e in values.get("segments", ()))
    slot = values.get("pov_slot")
    values["pov_slot"] = None if slot is None else int(slot)
    return Clip(**values)
