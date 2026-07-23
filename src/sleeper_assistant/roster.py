"""Turn a Sleeper `roster_positions` list into a starting-slot model and answer
the one question the redraft engine keeps asking: *does this position still need a
starter, can it go in a flex, or is it already surplus?*
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Which base positions each flex-type slot will accept.
FLEX_ELIGIBILITY: dict[str, frozenset[str]] = {
    "FLEX": frozenset({"RB", "WR", "TE"}),
    "WRRB_FLEX": frozenset({"RB", "WR"}),
    "WRRB": frozenset({"RB", "WR"}),
    "REC_FLEX": frozenset({"WR", "TE"}),
    "SUPER_FLEX": frozenset({"QB", "RB", "WR", "TE"}),
    "SUPERFLEX": frozenset({"QB", "RB", "WR", "TE"}),
}

DIRECT_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}
_NON_STARTER = {"BN", "IR", "TAXI"}

# classification of a candidate position given the current roster
STARTER = "starter"   # fills an unfilled direct starting slot
FLEX = "flex"         # direct full, but an eligible flex slot is open
SURPLUS = "surplus"   # all eligible starting slots already full
NEUTRAL = "neutral"   # position isn't a starting slot at all (shouldn't happen)


@dataclass
class RosterModel:
    direct_req: dict[str, int] = field(default_factory=dict)
    flex_slots: list[frozenset[str]] = field(default_factory=list)

    @classmethod
    def from_positions(cls, positions: list[str]) -> "RosterModel":
        direct: dict[str, int] = {}
        flex: list[frozenset[str]] = []
        for raw in positions:
            tok = raw.upper()
            if tok in _NON_STARTER:
                continue
            if tok in DIRECT_POSITIONS:
                direct[tok] = direct.get(tok, 0) + 1
            elif tok in FLEX_ELIGIBILITY:
                flex.append(FLEX_ELIGIBILITY[tok])
            # anything else (IDP slots, etc.) is ignored for v1
        return cls(direct_req=direct, flex_slots=flex)

    def starting_size(self) -> int:
        return sum(self.direct_req.values()) + len(self.flex_slots)

    def startable_slots(self, position: str) -> int:
        """Most starters of `position` you could field: its direct slots plus every
        flex slot it's eligible for. In superflex QB gains the SUPER_FLEX slot, so
        this returns 2 — which is what makes the dynasty depth-fade format-aware.
        """
        pos = "DEF" if position.upper() == "DST" else position.upper()
        return self.direct_req.get(pos, 0) + sum(1 for e in self.flex_slots if pos in e)

    def open_flex(self, counts: dict[str, int]) -> list[frozenset[str]]:
        """Flex slots left open after assigning position overflow greedily."""
        overflow = {
            pos: max(0, counts.get(pos, 0) - self.direct_req.get(pos, 0))
            for pos in set(counts) | set(self.direct_req)
        }
        open_slots: list[frozenset[str]] = []
        # Fill flex slots most-restrictive first so a scarce overflow player lands
        # in the narrowest slot it qualifies for.
        for eligible in sorted(self.flex_slots, key=len):
            filled = False
            for pos in sorted(eligible, key=lambda p: overflow.get(p, 0), reverse=True):
                if overflow.get(pos, 0) > 0:
                    overflow[pos] -= 1
                    filled = True
                    break
            if not filled:
                open_slots.append(eligible)
        return open_slots

    def classify(self, position: str, counts: dict[str, int]) -> str:
        # Rankings use "DST" for team defenses; Sleeper roster slots + roster
        # counts use "DEF". Canonicalize so a DST candidate matches its DEF slot
        # (otherwise defenses never classify as a needed starter — see the
        # last-rounds K/DEF need boost in the engine).
        pos = "DEF" if position.upper() == "DST" else position.upper()
        if counts.get(pos, 0) < self.direct_req.get(pos, 0):
            return STARTER
        for eligible in self.open_flex(counts):
            if pos in eligible:
                return FLEX
        if self.direct_req.get(pos, 0) > 0 or any(pos in e for e in self.flex_slots):
            return SURPLUS
        return NEUTRAL

    def missing_starters(self, counts: dict[str, int]) -> dict[str, int]:
        """How many direct starters are still unfilled, per position."""
        out = {}
        for pos, req in self.direct_req.items():
            gap = req - counts.get(pos, 0)
            if gap > 0:
                out[pos] = gap
        return out
