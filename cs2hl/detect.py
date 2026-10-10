"""Find highlight moments in a parsed demo.

Pure functions over the models in ``cs2hl.models``, so they are unit-testable
without a demo file.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from . import scoring
from .models import Highlight, Kill, Rosters, Round, Side

OTHER_SIDE: dict[Side, Side] = {"T": "CT", "CT": "T"}
SIDES: tuple[Side, Side] = ("T", "CT")


@dataclass(frozen=True)
class DetectConfig:
    tickrate: float = 64.0
    # Longest pause between two kills that still counts as one multikill.
    streak_gap_s: float = 10.0


def detect_highlights(
    rounds: list[Round],
    kills: list[Kill],
    rosters: Rosters,
    cfg: DetectConfig | None = None,
) -> list[Highlight]:
    cfg = cfg or DetectConfig()

    kills_by_round: dict[int, list[Kill]] = defaultdict(list)
    for kill in sorted(kills, key=lambda k: k.tick):
        kills_by_round[kill.round_num].append(kill)

    found: list[Highlight] = []
    for rnd in rounds:
        round_kills = kills_by_round.get(rnd.number)
        if not round_kills:
            continue
        roster = rosters.get(rnd.number, {})

        multikills = _multikills(rnd, round_kills, cfg)
        found += multikills
        found += _clutches(rnd, round_kills, roster)
        # A player-level ace already covers the moment, so skip the team-level one.
        if not any(h.label == "ACE" for h in multikills):
            found += _team_aces(rnd, round_kills, roster)

    return sorted(found, key=lambda h: h.start_tick)


def _multikills(rnd: Round, round_kills: list[Kill], cfg: DetectConfig) -> list[Highlight]:
    gap_ticks = cfg.streak_gap_s * cfg.tickrate
    open_streaks: dict[int, list[Kill]] = {}
    streaks: list[list[Kill]] = []

    for kill in round_kills:
        current = open_streaks.get(kill.attacker_steamid)
        if current and kill.tick - current[-1].tick > gap_ticks:
            streaks.append(current)
            current = None
        if current is None:
            current = []
            open_streaks[kill.attacker_steamid] = current
        current.append(kill)
    streaks.extend(open_streaks.values())

    highlights = []
    for streak in streaks:
        if len(streak) < 2:
            continue
        first = streak[0]
        won = rnd.winner_side is not None and first.attacker_side == rnd.winner_side
        highlights.append(
            Highlight(
                kind="multikill",
                label="ACE" if len(streak) >= 5 else f"{len(streak)}K",
                round_num=rnd.number,
                player_steamid=first.attacker_steamid,
                player_name=first.attacker_name,
                start_tick=first.tick,
                end_tick=streak[-1].tick,
                kills=len(streak),
                won_round=won,
                score=scoring.multikill_score(streak, won),
                tags=scoring.kills_tags(streak),
            )
        )
    return highlights


def _clutches(rnd: Round, round_kills: list[Kill], roster: dict[int, Side]) -> list[Highlight]:
    """A clutch is a player left alone against N enemies who then wins the round."""
    if rnd.winner_side is None or not roster:
        return []

    alive = dict.fromkeys(roster)  # ordered set of steamids still alive
    # side -> (lone player, tick they became alone, enemies still alive then)
    lone: dict[Side, tuple[int, int, int]] = {}

    for kill in round_kills:
        alive.pop(kill.victim_steamid, None)
        for side in SIDES:
            if side in lone:
                continue
            mates = [p for p in alive if roster.get(p) == side]
            foes = [p for p in alive if roster.get(p) == OTHER_SIDE[side]]
            if len(mates) == 1 and foes:
                lone[side] = (mates[0], kill.tick, len(foes))

    highlights = []
    for side, (player, since_tick, enemies) in lone.items():
        if side != rnd.winner_side:
            continue
        clutch_kills = [k for k in round_kills if k.attacker_steamid == player and k.tick >= since_tick]
        if not clutch_kills:
            continue
        highlights.append(
            Highlight(
                kind="clutch",
                label=f"1v{enemies}",
                round_num=rnd.number,
                player_steamid=player,
                player_name=clutch_kills[0].attacker_name,
                start_tick=clutch_kills[0].tick,
                end_tick=clutch_kills[-1].tick,
                kills=len(clutch_kills),
                won_round=True,
                score=scoring.clutch_score(enemies, clutch_kills),
                tags=("clutch", *scoring.kills_tags(clutch_kills)),
            )
        )
    return highlights


def _team_aces(rnd: Round, round_kills: list[Kill], roster: dict[int, Side]) -> list[Highlight]:
    """One side kills every enemy that was alive at round start."""
    highlights = []
    for side in SIDES:
        foes = {p for p, s in roster.items() if s == OTHER_SIDE[side]}
        if not foes:
            continue
        mine = [k for k in round_kills if k.attacker_side == side and k.victim_steamid in foes]
        if {k.victim_steamid for k in mine} != foes:
            continue

        top_steamid, _ = Counter(k.attacker_steamid for k in mine).most_common(1)[0]
        top_name = next(k.attacker_name for k in mine if k.attacker_steamid == top_steamid)
        won = rnd.winner_side == side
        highlights.append(
            Highlight(
                kind="team_ace",
                label="TEAM ACE",
                round_num=rnd.number,
                player_steamid=top_steamid,
                player_name=top_name,
                start_tick=mine[0].tick,
                end_tick=mine[-1].tick,
                kills=len(mine),
                won_round=won,
                score=scoring.team_ace_score(mine, won),
                tags=("team_ace", *scoring.kills_tags(mine)),
            )
        )
    return highlights
