"""Highlight scores, used to rank highlights and to drop weak ones.

score = kill-count base + clutch bonus + per-kill bonuses + win bonus

    kills in the round   1K 1, 2K 3, 3K 6, 4K 10, ACE (5 or more) 15
    clutch (1vN, won)    2 + 3 per enemy: 1v1 5, 1v2 8, 1v3 11, 1v4 14, 1v5 17
    per kill             headshot 0.5, noscope 1, through smoke 1, wallbang 1, knife 2
    round won            2

A guess tuned only by eye on two real demos; change the tables, not the formula.
"""

from __future__ import annotations

from typing import Sequence

from .models import Kill

KILL_COUNT_SCORE = {1: 1.0, 2: 3.0, 3: 6.0, 4: 10.0, 5: 15.0}  # 5 or more kills = ace
CLUTCH_BASE = 2.0
CLUTCH_PER_ENEMY = 3.0
WIN_BONUS = 2.0

KILL_BONUS = {"headshot": 0.5, "noscope": 1.0, "smoke": 1.0, "wallbang": 1.0, "knife": 2.0}


def kill_tags(kill: Kill) -> list[str]:
    tags = []
    if kill.headshot:
        tags.append("headshot")
    if kill.noscope:
        tags.append("noscope")
    if kill.through_smoke:
        tags.append("smoke")
    if kill.wallbang:
        tags.append("wallbang")
    weapon = kill.weapon.lower()
    # Real demos name knives "knife", "knife_karambit", ...; "bayonet" is assumed, not seen yet.
    if "knife" in weapon or "bayonet" in weapon:
        tags.append("knife")
    return tags


def kills_tags(kills: Sequence[Kill]) -> tuple[str, ...]:
    return tuple(sorted({tag for kill in kills for tag in kill_tags(kill)}))


def kill_count_score(kills: int) -> float:
    return KILL_COUNT_SCORE[min(kills, 5)] if kills > 0 else 0.0


def clutch_bonus(enemies: int) -> float:
    return CLUTCH_BASE + CLUTCH_PER_ENEMY * enemies if enemies > 0 else 0.0


def kill_bonus(kills: Sequence[Kill]) -> float:
    return sum(KILL_BONUS[tag] for kill in kills for tag in kill_tags(kill))


def highlight_score(kills: Sequence[Kill], won_round: bool, clutch_enemies: int = 0) -> float:
    """Score one player's round: ``kills`` are their kills, ``clutch_enemies`` is N of a won 1vN."""
    return (
        kill_count_score(len(kills))
        + clutch_bonus(clutch_enemies)
        + kill_bonus(kills)
        + (WIN_BONUS if won_round else 0.0)
    )
