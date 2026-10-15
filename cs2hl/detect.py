"""Find highlight moments in a parsed demo.

One highlight per player per round: all of that player's kills in the round, kept when
there are at least ``min_kills`` of them or the player won a clutch. Pure functions over
the models in ``cs2hl.models``, so they are unit-testable without a demo file.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from . import scoring
from .models import Highlight, Kill, Rosters, Round, Side

OTHER_SIDE: dict[Side, Side] = {"T": "CT", "CT": "T"}
SIDES: tuple[Side, Side] = ("T", "CT")

# (tick, round_num, victim_steamid), as in DemoData.deaths
Death = tuple[int, int, int]


@dataclass(frozen=True)
class DetectConfig:
    tickrate: float = 64.0
    min_kills: int = 2  # kills by one player in one round that make a highlight
    include_clutches: bool = True  # a won clutch makes a highlight even below min_kills


@dataclass(frozen=True)
class Clutch:
    player_steamid: int
    since_tick: int  # tick the player became the last one alive on their side
    enemies: int  # enemies alive at that tick: the N in 1vN


def detect_highlights(
    rounds: list[Round],
    kills: list[Kill],
    rosters: Rosters,
    cfg: DetectConfig | None = None,
    deaths: Iterable[Death] = (),
) -> list[Highlight]:
    """``deaths`` (``DemoData.deaths``) tells clutches who is alive; without it only kill victims die."""
    cfg = cfg or DetectConfig()

    kills_by_round: dict[int, list[Kill]] = defaultdict(list)
    for kill in sorted(kills, key=lambda k: k.tick):
        kills_by_round[kill.round_num].append(kill)
    deaths_by_round: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for tick, round_num, victim in deaths:
        deaths_by_round[round_num].append((tick, victim))

    found: list[Highlight] = []
    for rnd in rounds:
        round_kills = in_round_kills(rnd, kills_by_round.get(rnd.number, []))
        if not round_kills:
            continue
        roster = rosters.get(rnd.number, {})
        clutch = None
        if cfg.include_clutches:
            clutch = find_clutch(rnd, round_kills, roster, deaths_by_round.get(rnd.number, ()))

        by_player: dict[int, list[Kill]] = defaultdict(list)
        for kill in round_kills:
            if is_enemy_kill(kill, roster):
                by_player[kill.attacker_steamid].append(kill)

        for player, player_kills in by_player.items():
            player_clutch = clutch if clutch and clutch.player_steamid == player else None
            if len(player_kills) >= cfg.min_kills or player_clutch:
                found.append(_highlight(rnd, player, player_kills, roster, player_clutch))

    return sorted(found, key=lambda h: (h.start_tick, h.player_steamid))


def in_round_kills(rnd: Round, kills: list[Kill]) -> list[Kill]:
    """Kills between round start and round end; kills after the round is decided are dropped."""
    return [k for k in kills if rnd.start_tick <= k.tick <= rnd.end_tick]


def is_enemy_kill(kill: Kill, roster: dict[int, Side]) -> bool:
    """False for suicides and team kills, which do not count toward a player's highlight."""
    if kill.attacker_steamid == kill.victim_steamid:
        return False
    attacker = roster.get(kill.attacker_steamid, kill.attacker_side)
    victim = roster.get(kill.victim_steamid, kill.victim_side)
    return attacker is None or victim is None or attacker != victim


def highlight_label(kills: int, clutch_enemies: int = 0) -> str:
    """'3K', 'ACE', '1v2', or both joined: '3K + 1v2'. A single kill only shows as '1K' without a clutch."""
    parts = []
    if kills >= 2 or not clutch_enemies:
        parts.append("ACE" if kills >= 5 else f"{kills}K")
    if clutch_enemies:
        parts.append(f"1v{clutch_enemies}")
    return " + ".join(parts)


def deaths_by_tick(
    rnd: Round, round_kills: list[Kill], round_deaths: Iterable[tuple[int, int]] = ()
) -> list[tuple[int, set[int]]]:
    """Who dies at each tick of the round, from ``(tick, victim)`` deaths plus every kill's victim.

    Deaths cover world damage, suicides and team kills; the kill victims keep this working
    when a caller has no deaths.
    """
    dead: dict[int, set[int]] = defaultdict(set)
    for tick, victim in [*round_deaths, *((k.tick, k.victim_steamid) for k in round_kills)]:
        if rnd.start_tick <= tick <= rnd.end_tick:
            dead[tick].add(victim)
    return sorted(dead.items())


def find_clutch(
    rnd: Round,
    round_kills: list[Kill],
    roster: dict[int, Side],
    round_deaths: Iterable[tuple[int, int]] = (),
) -> Clutch | None:
    """A player left alone against N enemies whose side wins the round, with a kill after that.

    ``round_deaths`` are this round's ``(tick, victim)`` deaths of any cause. Players who die
    at the same tick leave together. Only the winning side can clutch, so a round has at most one.
    """
    if rnd.winner_side is None or not roster:
        return None

    alive = dict.fromkeys(roster)  # ordered set of steamids still alive
    lone: dict[Side, Clutch] = {}

    def note_lone_players(tick: int) -> None:
        for side in SIDES:
            if side in lone:
                continue
            mates = [p for p in alive if roster.get(p) == side]
            foes = [p for p in alive if roster.get(p) == OTHER_SIDE[side]]
            if len(mates) == 1 and foes:
                lone[side] = Clutch(player_steamid=mates[0], since_tick=tick, enemies=len(foes))

    # A side can already be down to one player when the round starts (disconnects).
    note_lone_players(rnd.start_tick)
    for tick, victims in deaths_by_tick(rnd, round_kills, round_deaths):
        for victim in victims:
            alive.pop(victim, None)
        note_lone_players(tick)

    clutch = lone.get(rnd.winner_side)
    if clutch is None:
        return None
    player, since = clutch.player_steamid, clutch.since_tick
    if not any(k.attacker_steamid == player and k.tick >= since and is_enemy_kill(k, roster) for k in round_kills):
        return None
    return clutch


def _highlight(
    rnd: Round,
    player: int,
    kills: list[Kill],
    roster: dict[int, Side],
    clutch: Clutch | None,
) -> Highlight:
    side = roster.get(player, kills[0].attacker_side)
    won = rnd.winner_side is not None and side == rnd.winner_side
    enemies = clutch.enemies if clutch else 0
    return Highlight(
        kind="clutch" if clutch else "multikill",
        label=highlight_label(len(kills), enemies),
        round_num=rnd.number,
        player_steamid=player,
        player_name=kills[0].attacker_name,
        start_tick=kills[0].tick,
        end_tick=kills[-1].tick,
        kills=len(kills),
        won_round=won,
        score=scoring.highlight_score(kills, won, enemies),
        tags=(("clutch",) if clutch else ()) + scoring.kills_tags(kills),
        kill_ticks=tuple(k.tick for k in kills),
    )
