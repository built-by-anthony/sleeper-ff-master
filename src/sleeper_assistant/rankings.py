"""Load a FantasyPros cheat-sheet CSV into a ranked, tiered player list.

FantasyPros exports vary in column names and casing across their redraft /
superflex / dynasty-rookie sheets, so we detect columns by fuzzy header matching
rather than assuming a fixed schema. The POS column often carries a positional
rank suffix (e.g. "RB1", "WR12"); we strip that down to the base position.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

# Base fantasy positions we care about. DST covers team defenses (Sleeper: DEF).
_KNOWN_POS = {"QB", "RB", "WR", "TE", "K", "DST", "DEF"}


@dataclass
class RankedPlayer:
    rank: int
    tier: int
    name: str
    position: str
    team: str
    bye: int | None = None
    pos_rank: int | None = None  # depth at position, e.g. 1 for "RB1" in the CSV's POS cell


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", h.lower())


def _find_col(headers: list[str], *candidates: str) -> int | None:
    normed = [_norm_header(h) for h in headers]
    # exact-ish match first
    for cand in candidates:
        c = _norm_header(cand)
        for i, h in enumerate(normed):
            if h == c:
                return i
    # substring fallback
    for cand in candidates:
        c = _norm_header(cand)
        for i, h in enumerate(normed):
            if c and c in h:
                return i
    return None


def _clean_pos(raw: str) -> str:
    raw = raw.strip().upper()
    m = re.match(r"([A-Z]+)", raw)
    base = m.group(1) if m else raw
    if base == "DEF":
        base = "DST"
    return base


def _pos_rank(raw: str) -> int | None:
    """Pull the depth number off a POS cell like "RB14" -> 14, or None if bare."""
    m = re.match(r"[A-Z]+[\s-]*(\d+)", raw.strip().upper())
    return int(m.group(1)) if m else None


def load_rankings(path: Path) -> list[RankedPlayer]:
    if not path.exists():
        raise FileNotFoundError(f"rankings CSV not found: {path}")

    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        rows = [r for r in reader if any(cell.strip() for cell in r)]

    if not rows:
        raise ValueError(f"rankings CSV is empty: {path}")

    header = rows[0]
    i_rank = _find_col(header, "rank", "rk", "overall", "ovr")
    i_tier = _find_col(header, "tier", "tiers")
    i_name = _find_col(header, "player name", "player", "name")
    i_pos = _find_col(header, "pos", "position")
    i_team = _find_col(header, "team", "tm")
    i_bye = _find_col(header, "bye week", "bye")

    if i_name is None or i_pos is None:
        raise ValueError(
            f"could not find PLAYER and POS columns in {path.name}; headers were: {header}"
        )

    players: list[RankedPlayer] = []
    fallback_rank = 0
    for row in rows[1:]:
        if len(row) <= max(i for i in (i_name, i_pos) if i is not None):
            continue
        name = row[i_name].strip()
        if not name:
            continue
        raw_pos = row[i_pos] if i_pos < len(row) else ""
        pos = _clean_pos(raw_pos)
        if pos not in _KNOWN_POS:
            continue
        pos_rank = _pos_rank(raw_pos)

        fallback_rank += 1
        rank = _to_int(row[i_rank]) if i_rank is not None and i_rank < len(row) else None
        if rank is None:
            rank = fallback_rank
        tier = _to_int(row[i_tier]) if i_tier is not None and i_tier < len(row) else None
        team = row[i_team].strip().upper() if i_team is not None and i_team < len(row) else ""
        bye = _to_int(row[i_bye]) if i_bye is not None and i_bye < len(row) else None

        players.append(
            RankedPlayer(
                rank=rank,
                tier=tier if tier is not None else 0,
                name=name,
                position=pos,
                team=team,
                bye=bye,
                pos_rank=pos_rank,
            )
        )

    if not players:
        raise ValueError(f"no usable player rows parsed from {path.name}")

    # If the sheet had no tier column, synthesize coarse tiers of 12 by rank so
    # the tier-break alerts still have something sane to work with.
    if all(p.tier == 0 for p in players):
        for p in players:
            p.tier = (p.rank - 1) // 12 + 1

    players.sort(key=lambda p: p.rank)
    return players


def _to_int(s: str) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    m = re.search(r"-?\d+", s)
    return int(m.group()) if m else None
