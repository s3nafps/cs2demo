"""Heuristic highlight scores.

These numbers are a first guess to get the pipeline running. Tune them on real
demos once parsing is verified, rather than trusting them up front.
"""

from __future__ import annotations

from .models import Kill

MULTIKILL_BASE = {2: 3.0, 3: 6.0, 4: 10.0, 5: 15.0}  # 5 kills by one player = ace
TEAM_ACE_BASE = 15.0
CLUTCH_BASE = 5.0
CLUTCH_PER_ENEMY = 2.0
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
    if "knife" in kill.weapon.lower():
        tags.append("knife")
    return tags


def kills_tags(kills: list[Kill]) -> tuple[str, ...]:
    return tuple(sorted({tag for kill in kills for tag in kill_tags(kill)}))


def _bonus(kills: list[Kill]) -> float:
    return sum(KILL_BONUS[tag] for kill in kills for tag in kill_tags(kill))


def multikill_score(kills: list[Kill], won_round: bool) -> float:
    base = MULTIKILL_BASE[min(len(kills), 5)]
    return base + _bonus(kills) + (WIN_BONUS if won_round else 0.0)


def clutch_score(enemies: int, kills: list[Kill]) -> float:
    return CLUTCH_BASE + CLUTCH_PER_ENEMY * enemies + _bonus(kills) + WIN_BONUS


def team_ace_score(kills: list[Kill], won_round: bool) -> float:
    return TEAM_ACE_BASE + _bonus(kills) + (WIN_BONUS if won_round else 0.0)
