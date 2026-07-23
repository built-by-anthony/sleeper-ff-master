"""Assemble a coherent snapshot of the draft from the raw Sleeper payloads:
whose turn it is, what you've rostered, and who's still on the board.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .matching import MatchResult

# Sleeper injury_status values → short display tags. Anything not here shows
# verbatim (uppercased); healthy/unknown states yield no tag.
_INJURY_ABBR = {
    "questionable": "Q",
    "doubtful": "D",
    "out": "OUT",
    "ir": "IR",
    "injured reserve": "IR",
    "pup": "PUP",
    "sus": "SUS",
    "suspended": "SUS",
}
_INJURY_NONE = {"", "na", "active", "healthy", "probable", "none"}


def injury_tag(player: dict[str, Any] | None) -> str:
    """Short injury flag for a Sleeper player object; '' if healthy/unknown.

    Display-only — never affects scoring. Examples: 'Q (Shoulder)',
    'OUT (Knee-ACL)', 'IR (Knee)', or 'D' when no body part is listed.
    """
    if not isinstance(player, dict):
        return ""
    status = (player.get("injury_status") or "").strip()
    if status.lower() in _INJURY_NONE:
        return ""
    abbr = _INJURY_ABBR.get(status.lower(), status.upper())
    body = re.sub(r"\s*-\s*", "-", (player.get("injury_body_part") or "").strip())
    return f"{abbr} ({body})" if body and body.lower() not in _INJURY_NONE else abbr


@dataclass
class DraftMeta:
    draft_id: str
    type: str            # snake | linear | auction
    status: str          # pre_draft | drafting | paused | complete
    rounds: int
    teams: int
    slot_to_roster_id: dict[int, int]
    draft_order: dict[str, int]        # user_id -> draft slot
    roster_positions: list[str]
    scoring_settings: dict[str, Any]

    @classmethod
    def from_api(cls, draft: dict[str, Any], league: dict[str, Any]) -> "DraftMeta":
        settings = draft.get("settings") or {}
        s2r_raw = draft.get("slot_to_roster_id") or {}
        s2r = {int(k): int(v) for k, v in s2r_raw.items()}
        order_raw = draft.get("draft_order") or {}
        order = {str(uid): int(slot) for uid, slot in order_raw.items()}
        teams = settings.get("teams") or len(s2r) or len(order) or league.get("total_rosters") or 0
        return cls(
            draft_id=str(draft.get("draft_id", "")),
            type=draft.get("type") or "snake",
            status=draft.get("status") or "pre_draft",
            rounds=int(settings.get("rounds") or 0),
            teams=int(teams),
            slot_to_roster_id=s2r,
            draft_order=order,
            roster_positions=league.get("roster_positions") or draft.get("roster_positions") or [],
            scoring_settings=league.get("scoring_settings") or {},
        )


@dataclass
class DraftState:
    meta: DraftMeta
    picks: list[dict[str, Any]]
    my_user_id: str
    my_roster_id: int | None
    matched: list[MatchResult]                 # only entries with a player_id
    players_meta: dict[str, Any]
    existing_roster_ids: set[str] = field(default_factory=set)
    # Every player already on *any* roster in the league. In a dynasty draft you
    # can also take free agents, so "available" must exclude owned players, not
    # just those picked in this draft. Empty for a fresh redraft league.
    rostered_ids: set[str] = field(default_factory=set)
    # user_id -> display/team name, for showing who's on the clock. Empty in a
    # mock (no league users), where on_clock_name falls back to the slot number.
    users: dict[str, str] = field(default_factory=dict)

    # --- identity / clock ----------------------------------------------

    @property
    def my_slot(self) -> int | None:
        if self.my_user_id and self.my_user_id in self.meta.draft_order:
            return self.meta.draft_order[self.my_user_id]
        # fall back to any pick we've made
        for p in self.picks:
            if str(p.get("picked_by")) == self.my_user_id and p.get("draft_slot"):
                return int(p["draft_slot"])
        # fall back via roster id
        if self.my_roster_id is not None:
            for slot, rid in self.meta.slot_to_roster_id.items():
                if rid == self.my_roster_id:
                    return slot
        return None

    def next_pick_no(self) -> int:
        return len(self.picks) + 1

    def current_round(self) -> int:
        teams = self.meta.teams or 1
        return (self.next_pick_no() - 1) // teams + 1

    def rounds_left(self) -> int:
        return max(0, self.meta.rounds - self.current_round() + 1)

    def _slot_on_clock(self, pick_no: int) -> int:
        teams = self.meta.teams or 1
        idx = (pick_no - 1) % teams
        rnd = (pick_no - 1) // teams + 1
        if self.meta.type == "snake" and rnd % 2 == 0:
            return teams - idx
        return idx + 1

    def on_clock_slot(self) -> int | None:
        """Draft slot whose turn it is for the next pick; None once the draft is done."""
        teams = self.meta.teams
        if teams <= 0 or self.next_pick_no() > teams * self.meta.rounds:
            return None
        return self._slot_on_clock(self.next_pick_no())

    def on_clock_name(self) -> str | None:
        """Display/team name of whoever is on the clock, or 'Team N' if unknown
        (e.g. a mock with no league users). None once the draft is done."""
        slot = self.on_clock_slot()
        if slot is None:
            return None
        slot_to_user = {s: uid for uid, s in self.meta.draft_order.items()}
        uid = slot_to_user.get(slot)
        return self.users.get(uid or "", f"Team {slot}")

    def picks_until_me(self) -> int | None:
        """0 = you're on the clock; None = you have no more picks / unknown slot."""
        slot = self.my_slot
        if slot is None:
            return None
        total = self.meta.teams * self.meta.rounds
        pick_no = self.next_pick_no()
        count = 0
        while pick_no <= total:
            if self._slot_on_clock(pick_no) == slot:
                return count
            count += 1
            pick_no += 1
        return None

    # --- rosters / board ------------------------------------------------

    def drafted_ids(self) -> set[str]:
        return {str(p["player_id"]) for p in self.picks if p.get("player_id")}

    def my_player_ids(self) -> set[str]:
        ids = set(self.existing_roster_ids)
        for p in self.picks:
            pid = p.get("player_id")
            if not pid:
                continue
            if str(p.get("picked_by")) == self.my_user_id or (
                self.my_roster_id is not None and p.get("roster_id") == self.my_roster_id
            ):
                ids.add(str(pid))
        return ids

    def _position_of(self, pid: str) -> str | None:
        p = self.players_meta.get(pid)
        if not isinstance(p, dict):
            return None
        pos = (p.get("position") or "").upper()
        if not pos:
            fps = p.get("fantasy_positions") or []
            pos = (fps[0].upper() if fps else "")
        return pos or None

    def my_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for pid in self.my_player_ids():
            pos = self._position_of(pid)
            if pos:
                counts[pos] = counts.get(pos, 0) + 1
        return counts

    def available(self) -> list[MatchResult]:
        taken = self.drafted_ids() | self.rostered_ids
        return [m for m in self.matched if m.player_id and m.player_id not in taken]

    def recent_picks(self, n: int = 8) -> list[dict[str, Any]]:
        return sorted(self.picks, key=lambda p: p.get("pick_no", 0))[-n:]

    def recent_pick_positions(self, n: int = 8) -> list[str]:
        out = []
        for p in self.recent_picks(n):
            meta = p.get("metadata") or {}
            pos = (meta.get("position") or self._position_of(str(p.get("player_id"))) or "").upper()
            if pos:
                out.append(pos)
        return out
