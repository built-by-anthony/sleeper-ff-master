"""Match FantasyPros CSV names to Sleeper player IDs.

This is the highest-risk plumbing in the tool (see PLAN.md): a silent miss means
a player never shows up as available and the advice is quietly wrong. So the
strategy is layered and every failure is surfaced loudly at startup:

    1. normalize (strip suffixes / punctuation / casing)
    2. exact match on normalized name + position (+ team as a tie-breaker)
    3. small hardcoded alias map for known name divergences
    4. fuzzy fallback within the same position, above a confidence threshold

Team defenses (DST/DEF) are matched by team abbreviation, not name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from .rankings import RankedPlayer

# CSV display name -> Sleeper full_name, for cases normalization can't bridge.
# Keep this short and obvious; it's a pressure valve, not a database.
#
# Keys MUST be already-normalized (see normalize_name): the lookup is
# ALIASES.get(normalize_name(name)), so a key that still carries a suffix or
# punctuation can never be hit. Suffix-only divergences (e.g. "Kenneth Walker
# III" vs "Kenneth Walker") already collapse under normalization and need no
# alias here.
ALIASES: dict[str, str] = {
    "gabe davis": "gabriel davis",
    "hollywood brown": "marquise brown",   # nickname
    "bam knight": "zonovan knight",         # nickname
    "daylan smothers": "hollywood smothers",  # listed first name differs
    "chip trayanum": "deamonte trayanum",   # nickname
}

# Same pressure valve as ALIASES, but for players a CSV ranks under a different
# position than Sleeper lists them at (one-off tweeners, not a systemic split
# like FB/RB — those are folded in _sleeper_pos instead). Keys are normalized
# names; values are the Sleeper position to search under instead of the CSV's.
POSITION_OVERRIDES: dict[str, str] = {
    "max bredeson": "TE",  # FantasyPros ranks him as RB; Sleeper lists him TE
}

# NFL team-name (or a divergent abbreviation) -> Sleeper's abbreviation, for
# matching DST rows however FantasyPros spells them. Keys are lowercase; values
# are the abbreviation Sleeper uses. Doubles as the fix for abbreviations the
# two sources disagree on (e.g. Jacksonville: FantasyPros "JAC" vs Sleeper
# "JAX") since a lowercased abbreviation is just another key into this table.
_TEAM_ABBR = {
    "cardinals": "ARI", "arizona": "ARI",
    "falcons": "ATL", "atlanta": "ATL",
    "ravens": "BAL", "baltimore": "BAL",
    "bills": "BUF", "buffalo": "BUF",
    "panthers": "CAR", "carolina": "CAR",
    "bears": "CHI", "chicago": "CHI",
    "bengals": "CIN", "cincinnati": "CIN",
    "browns": "CLE", "cleveland": "CLE",
    "cowboys": "DAL", "dallas": "DAL",
    "broncos": "DEN", "denver": "DEN",
    "lions": "DET", "detroit": "DET",
    "packers": "GB", "green bay": "GB",
    "texans": "HOU", "houston": "HOU",
    "colts": "IND", "indianapolis": "IND",
    "jaguars": "JAX", "jacksonville": "JAX", "jac": "JAX",
    "chiefs": "KC", "kansas city": "KC",
    "raiders": "LV", "las vegas": "LV",
    "chargers": "LAC",
    "rams": "LAR",
    "dolphins": "MIA", "miami": "MIA",
    "vikings": "MIN", "minnesota": "MIN",
    "patriots": "NE", "new england": "NE",
    "saints": "NO", "new orleans": "NO",
    "giants": "NYG",
    "jets": "NYJ",
    "eagles": "PHI", "philadelphia": "PHI",
    "steelers": "PIT", "pittsburgh": "PIT",
    "49ers": "SF", "niners": "SF", "san francisco": "SF",
    "seahawks": "SEA", "seattle": "SEA",
    "buccaneers": "TB", "bucs": "TB", "tampa bay": "TB",
    "titans": "TEN", "tennessee": "TEN",
    "commanders": "WAS", "washington": "WAS",
}

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_FUZZY_THRESHOLD = 88.0


def normalize_name(name: str) -> str:
    name = name.lower().strip()
    name = name.replace("&", " and ")
    name = re.sub(r"[.'`]", "", name)      # DJ, O'... etc.
    name = re.sub(r"[^a-z0-9 ]", " ", name)  # remaining punctuation -> space
    tokens = [t for t in name.split() if t and t not in _SUFFIXES]
    return " ".join(tokens)


def _sleeper_pos(player: dict[str, Any]) -> str:
    pos = (player.get("position") or "").upper()
    if pos == "FB":
        # Fullbacks are drafted/ranked as RBs everywhere (CSVs, Sleeper's own
        # fantasy_positions); keep them in the RB bucket so name matching finds them.
        return "RB"
    if pos:
        return pos
    fps = player.get("fantasy_positions") or []
    return (fps[0].upper() if fps else "")


@dataclass
class MatchResult:
    ranked: RankedPlayer
    player_id: str | None
    method: str  # exact | alias | fuzzy | team-def | unmatched
    sleeper_name: str = ""
    score: float = 0.0


class PlayerMatcher:
    def __init__(self, sleeper_players: dict[str, Any]) -> None:
        self.players = sleeper_players
        # (normalized_name, position) -> list of player_ids
        self._by_name_pos: dict[tuple[str, str], list[str]] = {}
        # position -> list of (normalized_name, player_id) for fuzzy search
        self._by_pos: dict[str, list[tuple[str, str]]] = {}
        # team_abbr -> DEF player_id
        self._def_by_team: dict[str, str] = {}

        for pid, p in sleeper_players.items():
            if not isinstance(p, dict):
                continue
            pos = _sleeper_pos(p)
            if pos == "DEF":
                team = (p.get("team") or pid or "").upper()
                if team:
                    self._def_by_team[team] = pid
                continue
            full = p.get("full_name") or " ".join(
                x for x in (p.get("first_name"), p.get("last_name")) if x
            )
            norm = normalize_name(full or "")
            if not norm:
                continue
            self._by_name_pos.setdefault((norm, pos), []).append(pid)
            self._by_pos.setdefault(pos, []).append((norm, pid))

    def _display(self, pid: str) -> str:
        p = self.players.get(pid, {})
        return p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip()

    def match(self, rp: RankedPlayer) -> MatchResult:
        pos = "DEF" if rp.position in ("DST", "DEF") else rp.position

        # Team defenses: match by team abbreviation.
        if pos == "DEF":
            team = rp.team.upper()
            team = _TEAM_ABBR.get(team.lower(), team)
            if not team:
                # try to read the team out of the CSV "name" (e.g. "Eagles")
                for word in normalize_name(rp.name).split():
                    if word in _TEAM_ABBR:
                        team = _TEAM_ABBR[word]
                        break
            pid = self._def_by_team.get(team)
            if pid:
                return MatchResult(rp, pid, "team-def", self._display(pid))
            return MatchResult(rp, None, "unmatched")

        norm = normalize_name(rp.name)
        pos = POSITION_OVERRIDES.get(norm, pos)

        # 1. exact name+position
        pids = self._by_name_pos.get((norm, pos))
        if pids:
            pid = self._disambiguate(pids, rp)
            return MatchResult(rp, pid, "exact", self._display(pid))

        # 2. alias map
        alias = ALIASES.get(norm)
        if alias:
            anorm = normalize_name(alias)
            pids = self._by_name_pos.get((anorm, pos))
            if pids:
                pid = self._disambiguate(pids, rp)
                return MatchResult(rp, pid, "alias", self._display(pid))

        # 3. fuzzy within position
        best_pid, best_score = None, 0.0
        for cand_norm, pid in self._by_pos.get(pos, []):
            s = fuzz.token_sort_ratio(norm, cand_norm)
            if s > best_score:
                best_pid, best_score = pid, s
        if best_pid and best_score >= _FUZZY_THRESHOLD:
            return MatchResult(rp, best_pid, "fuzzy", self._display(best_pid), best_score)

        return MatchResult(rp, None, "unmatched", score=best_score)

    def _disambiguate(self, pids: list[str], rp: RankedPlayer) -> str:
        """When several Sleeper players share name+position, prefer team match."""
        if len(pids) == 1:
            return pids[0]
        if rp.team:
            for pid in pids:
                if (self.players.get(pid, {}).get("team") or "").upper() == rp.team.upper():
                    return pid
        # Otherwise prefer an active player with a team, else first.
        for pid in pids:
            if self.players.get(pid, {}).get("team"):
                return pid
        return pids[0]


def match_all(rankings: list[RankedPlayer], matcher: PlayerMatcher) -> list[MatchResult]:
    return [matcher.match(rp) for rp in rankings]
