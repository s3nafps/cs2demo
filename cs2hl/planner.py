"""Turn highlights into clip windows and a plan file that the capture/export steps consume."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .models import Clip, DemoMeta, Highlight, Plan, Round


@dataclass(frozen=True)
class PlanConfig:
    pre_roll_s: float = 4.0
    post_roll_s: float = 2.0
    min_score: float = 5.0
    # Clips from the same player and round closer than this are merged into one.
    merge_gap_s: float = 1.0
    max_clips: int | None = None


@dataclass
class _Window:
    pov_steamid: int
    pov_name: str
    round_num: int
    start_tick: int
    end_tick: int
    highlights: list[Highlight] = field(default_factory=list)


def build_plan(
    highlights: list[Highlight],
    rounds: list[Round],
    meta: DemoMeta,
    cfg: PlanConfig | None = None,
) -> Plan:
    cfg = cfg or PlanConfig()
    tickrate = meta.tickrate
    pre_ticks = round(cfg.pre_roll_s * tickrate)
    post_ticks = round(cfg.post_roll_s * tickrate)
    merge_gap_ticks = cfg.merge_gap_s * tickrate
    round_start = {r.number: r.start_tick for r in rounds}

    windows = []
    for h in highlights:
        if h.score < cfg.min_score:
            continue
        floor = round_start.get(h.round_num, 0)
        windows.append(
            _Window(
                pov_steamid=h.player_steamid,
                pov_name=h.player_name,
                round_num=h.round_num,
                start_tick=max(floor, h.start_tick - pre_ticks),
                end_tick=h.end_tick + post_ticks,
                highlights=[h],
            )
        )

    merged = _merge(windows, merge_gap_ticks)
    if cfg.max_clips is not None and len(merged) > cfg.max_clips:
        merged = sorted(merged, key=lambda w: _score(w), reverse=True)[: cfg.max_clips]
    merged.sort(key=lambda w: w.start_tick)

    clips = [
        Clip(
            id=f"clip{index:03d}_r{w.round_num:02d}",
            round_num=w.round_num,
            pov_steamid=w.pov_steamid,
            pov_name=w.pov_name,
            start_tick=w.start_tick,
            end_tick=w.end_tick,
            start_s=round(w.start_tick / tickrate, 3),
            end_s=round(w.end_tick / tickrate, 3),
            score=round(_score(w), 2),
            labels=tuple(h.label for h in w.highlights),
            tags=tuple(sorted({t for h in w.highlights for t in h.tags})),
        )
        for index, w in enumerate(merged, start=1)
    ]
    return Plan(meta=meta, clips=clips)


def _score(window: _Window) -> float:
    return sum(h.score for h in window.highlights)


def _merge(windows: list[_Window], gap_ticks: float) -> list[_Window]:
    by_pov_round: dict[tuple[int, int], list[_Window]] = defaultdict(list)
    for w in sorted(windows, key=lambda w: w.start_tick):
        by_pov_round[(w.pov_steamid, w.round_num)].append(w)

    merged: list[_Window] = []
    for group in by_pov_round.values():
        current = None
        for w in group:
            if current and w.start_tick <= current.end_tick + gap_ticks:
                current.end_tick = max(current.end_tick, w.end_tick)
                current.highlights += w.highlights
            else:
                if current:
                    merged.append(current)
                current = replace(w, highlights=list(w.highlights))
        if current:
            merged.append(current)
    return merged


def plan_to_dict(plan: Plan) -> dict:
    return {
        "demo": plan.meta.demo_name,
        "map": plan.meta.map_name,
        "tickrate": plan.meta.tickrate,
        "clips": [asdict(c) for c in plan.clips],
    }


def save_plan(plan: Plan, path: Path) -> None:
    path.write_text(json.dumps(plan_to_dict(plan), indent=2) + "\n", encoding="utf-8")


def load_plan(path: Path) -> Plan:
    data = json.loads(path.read_text(encoding="utf-8"))
    meta = DemoMeta(demo_name=data["demo"], map_name=data["map"], tickrate=float(data["tickrate"]))
    clips = [
        Clip(
            **{
                **c,
                "labels": tuple(c["labels"]),
                "tags": tuple(c["tags"]),
            }
        )
        for c in data["clips"]
    ]
    return Plan(meta=meta, clips=clips)
