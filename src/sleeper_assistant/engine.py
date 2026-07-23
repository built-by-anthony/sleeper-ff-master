"""The value engine: score available players and produce recommendations.

Two modes (see PLAN.md §6):

  redraft  — score = base_value(rank) × need_multiplier(roster). "Moderate" dial:
             a needed player can climb about one tier, never leapfrog a clearly
             better tier. K/DEF get no need boost until the last ~2 rounds.
  dynasty  — lean best-player-available: need multiplier is OFF, pure rank order.

`base_value` decays with rank so the boost/discount range maps to ~one tier:
a 1.15× boost lets a needed player pass an equally-tiered player about 12 ranks
lower (one FantasyPros tier), and no further.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .matching import MatchResult
from .roster import FLEX, STARTER, SURPLUS, RosterModel

# Chosen so that 1.15 * DECAY**12 ≈ 1.0 → a full boost moves a player ~12 ranks
# (one tier) and no more. See module docstring.
_DECAY = 0.9884
_BASE = 1000.0

BOOST_STARTER = 1.15   # fills an unfilled starting slot
BOOST_FLEX = 1.06      # slots into an open flex
DISCOUNT_SURPLUS = 0.85  # position's starting slots already full

# Dynasty stays value-first (BPA), but once you can't start another of a position
# its extras fade so the board stops stacking one spot (e.g. QBs in superflex).
# The fade is graded by how deep past your startable slots you already are — one
# step ≈ one tier (0.85), compounding, floored so value still wins for studs.
_DYNASTY_DEPTH_FADE = 0.85
_DYNASTY_FADE_FLOOR = 0.5

# K/DEF get no need boost until this many rounds remain.
_KDEF_LATE_WINDOW = 2
_KDEF = {"K", "DEF", "DST"}


def base_value(rank: int) -> float:
    return _BASE * (_DECAY ** (rank - 1))


def studs(available: list[MatchResult]) -> list[MatchResult]:
    """Available tier-1 players, best-rank-first — the stud indicator (PLAN.md §10).

    A stud is an *absolute, tier-based* property of the player (`tier == 1` on the
    loaded CSV), not a function of the board or your pick number: a tier-1 player is
    a stud in round 1 and still a stud if he slides. Sheets without a tier column
    synthesize tiers in buckets of 12, so this degrades to "rank <= 12".

    Display-layer only: the engine scoring is deliberately untouched. The stud line
    gets its teeth purely by surfacing — because the need boost (redraft) or depth
    fade (dynasty) can score a stud below a need pick and out of the top-3, this
    guarantees he can't hide. He is never rescored.
    """
    return sorted(
        (m for m in available if m.ranked.tier == 1),
        key=lambda m: m.ranked.rank,
    )


@dataclass
class Recommendation:
    match: MatchResult
    score: float
    reason: str            # human-readable "why"
    fills: str             # roster.STARTER / FLEX / SURPLUS / NEUTRAL
    base: float


@dataclass
class Alert:
    kind: str   # "tier-break" | "run"
    text: str


class Engine:
    def __init__(self, mode: str, roster: RosterModel) -> None:
        if mode not in ("redraft", "dynasty"):
            raise ValueError(f"unknown engine mode: {mode!r}")
        self.mode = mode
        self.roster = roster

    def _multiplier(
        self, position: str, counts: dict[str, int], rounds_left: int
    ) -> tuple[float, str]:
        if self.mode == "dynasty":
            pos = "DEF" if position.upper() in ("DST", "DEF") else position.upper()
            # How deep this pick would put you past what you can start at `pos`.
            extra = counts.get(pos, 0) + 1 - self.roster.startable_slots(position)
            if extra <= 0:
                return 1.0, "BPA"
            mult = max(_DYNASTY_FADE_FLOOR, _DYNASTY_DEPTH_FADE ** extra)
            return mult, f"deep at {pos}"

        pos = position.upper()
        if pos in _KDEF and rounds_left > _KDEF_LATE_WINDOW:
            # Hard guardrail: no early K/DEF need boost.
            return 1.0, ""

        cls = self.roster.classify(pos, counts)
        if cls == STARTER:
            return BOOST_STARTER, f"fills {pos}"
        if cls == FLEX:
            return BOOST_FLEX, "FLEX"
        if cls == SURPLUS:
            return DISCOUNT_SURPLUS, f"{pos} full"
        return 1.0, ""

    def recommend(
        self,
        available: list[MatchResult],
        counts: dict[str, int],
        rounds_left: int,
        top_n: int = 3,
    ) -> list[Recommendation]:
        recs: list[Recommendation] = []
        for m in available:
            base = base_value(m.ranked.rank)
            mult, why = self._multiplier(m.ranked.position, counts, rounds_left)
            score = base * mult
            cls = (
                "neutral"
                if self.mode == "dynasty"
                else self.roster.classify(m.ranked.position, counts)
            )
            reason = why or f"Tier {m.ranked.tier}"
            recs.append(Recommendation(m, score, reason, cls, base))
        recs.sort(key=lambda r: (-r.score, r.match.ranked.rank))
        return recs[:top_n]

    # --- alerts ---------------------------------------------------------

    def alerts(
        self,
        available: list[MatchResult],
        recent_positions: list[str],
        recs: list["Recommendation"] | None = None,
    ) -> list[Alert]:
        out: list[Alert] = []
        out.extend(self._tier_break_alerts(available, self._scarcity_positions(recs)))
        out.extend(self._run_alerts(recent_positions))
        return out

    @staticmethod
    def _scarcity_positions(recs: list["Recommendation"] | None) -> set[str] | None:
        """Positions worth a tier-break warning: the recommended positions *except*
        the top pick's own — you're taking your #1 regardless, so its scarcity isn't
        actionable; the useful signal is the opportunity cost at the runners-up.
        None (no recs given) means "don't filter", the old fire-for-all behavior.
        """
        if recs is None:
            return None
        if not recs:
            return set()
        top_pos = recs[0].match.ranked.position
        return {r.match.ranked.position for r in recs} - {top_pos}

    def _tier_break_alerts(
        self, available: list[MatchResult], rec_positions: set[str] | None = None
    ) -> list[Alert]:
        """Warn when a position is about to run out of its current tier.

        Restricting to `rec_positions` (the positions you're actually being
        recommended this pick) is what keeps this to *genuine* scarcity: on a
        picked-over board — e.g. a dynasty rookie/FA draft where most veterans are
        owned — nearly every position shows "1 left" in its top remaining tier, so
        only a cliff at a position you're choosing among is worth flagging.
        With no filter (None) every position is eligible, the old behavior.
        """
        alerts: list[Alert] = []
        # group available by position, find the best (lowest) tier still present
        by_pos: dict[str, list[MatchResult]] = {}
        for m in available:
            by_pos.setdefault(m.ranked.position, []).append(m)
        for pos, players in by_pos.items():
            if pos in _KDEF:
                continue
            if rec_positions is not None and pos not in rec_positions:
                continue
            players.sort(key=lambda m: m.ranked.rank)
            top_tier = players[0].ranked.tier
            remaining = sum(1 for m in players if m.ranked.tier == top_tier)
            if remaining == 1:
                alerts.append(
                    Alert("tier-break", f"only 1 {pos} left in Tier {top_tier} — scarce after this")
                )
        return alerts

    def _run_alerts(self, recent_positions: list[str], window: int = 6, threshold: int = 4) -> list[Alert]:
        recent = recent_positions[-window:]
        out: list[Alert] = []
        for pos in ("RB", "WR", "QB", "TE"):
            n = recent.count(pos)
            if n >= threshold:
                out.append(Alert("run", f"{n} {pos}s gone in last {len(recent)} picks"))
        return out
