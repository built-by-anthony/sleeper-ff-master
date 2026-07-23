"""The single auto-refreshing draft screen. Glance-and-know under a 60-90s clock:
three suggestions, your starting slots as checkboxes, alerts only when firing,
and a small recent-picks ticker so you can see the tool is keeping up.
"""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .bannerfont import banner
from .engine import Alert, Recommendation
from .matching import MatchResult
from .roster import RosterModel
from .state import DraftState, injury_tag

_POS_ORDER = ["QB", "RB", "WR", "TE", "K", "DEF"]


def _injury_style(tag: str) -> str:
    return "bold red" if tag.startswith(("OUT", "IR", "D", "PUP", "SUS")) else "yellow"


def slot_display(roster: RosterModel, counts: dict[str, int]) -> list[tuple[str, bool]]:
    slots: list[tuple[str, bool]] = []
    for pos in _POS_ORDER:
        req = roster.direct_req.get(pos, 0)
        for i in range(req):
            label = f"{pos}{i + 1}" if req > 1 else pos
            slots.append((label, counts.get(pos, 0) > i))
    open_flex = roster.open_flex(counts)
    filled_flex = len(roster.flex_slots) - len(open_flex)
    for i in range(len(roster.flex_slots)):
        slots.append(("FLEX", i < filled_flex))
    return slots


def _header(state: DraftState, league_name: str) -> Text:
    t = Text()
    t.append(f" {league_name} ", style="bold white on dark_blue")
    t.append(f"  Round {state.current_round()} · Pick {state.next_pick_no()}")
    drafter = state.on_clock_name()
    if drafter:
        t.append(f" · {drafter} on the clock", style="bold cyan")
    t.append("   ")
    until = state.picks_until_me()
    if until == 0:
        t.append(" ⏱ YOU'RE ON THE CLOCK ", style="bold black on green")
    elif until is None:
        t.append(" (no more picks) ", style="dim")
    elif until <= 2:
        t.append(f" ⏱ YOU'RE UP IN {until} PICK{'S' if until != 1 else ''} ", style="bold black on yellow")
    else:
        t.append(f" ⏱ up in {until} picks ", style="dim")
    return t


def _roster_line(state: DraftState, roster: RosterModel) -> Text:
    counts = state.my_counts()
    t = Text("YOUR ROSTER  ", style="bold")
    for label, filled in slot_display(roster, counts):
        mark = "✓" if filled else "–"
        style = "green" if filled else "red"
        t.append(f" {label} {mark} ", style=style)
    return t


def _need_line(state: DraftState, roster: RosterModel, engine_mode: str) -> Text:
    counts = state.my_counts()
    missing = roster.missing_starters(counts)
    t = Text("STILL NEED   ", style="bold")
    if engine_mode == "dynasty":
        t.append("(informational — suggestions stay BPA)  ", style="dim italic")
    if missing:
        t.append(", ".join(f"{pos}×{n}" if n > 1 else pos for pos, n in missing.items()))
    else:
        t.append("starters full", style="dim")
    open_flex = roster.open_flex(counts)
    if open_flex:
        t.append(f"   ({len(open_flex)} FLEX open)", style="dim")
    return t


def _recs_table(recs: list[Recommendation], injuries: dict[str, str]) -> Table:
    tbl = Table.grid(padding=(0, 1))
    tbl.add_column(justify="right")
    tbl.add_column()
    tbl.add_column()
    tbl.add_column()
    tbl.add_column()
    for i, r in enumerate(recs, 1):
        rp = r.match.ranked
        star = "◀ best value + need" if i == 1 else ""
        detail = Text(f"Tier {rp.tier} · {r.reason}", style="dim")
        tag = injuries.get(r.match.player_id or "", "")
        if tag:
            # Out/Doubtful/IR are draft-relevant even in dynasty → louder.
            detail.append(f"  ⚕ {tag}", style=_injury_style(tag))
        tbl.add_row(
            Text(f"{i}.", style="bold"),
            Text(rp.name, style="bold white"),
            Text(f"{rp.position}", style="cyan"),
            detail,
            Text(star, style="green"),
        )
    if not recs:
        tbl.add_row("", Text("no available players matched — check startup warnings", style="red"), "", "", "")
    return tbl


def _studs_table(studs: list[MatchResult], injuries: dict[str, str]) -> Group | None:
    """All available tier-1 players, best-rank first, as a table: rank, name,
    depth (e.g. "RB1"), and injury status. Hidden when the board has no tier-1
    player. Not a per-row badge and not a pinned rec — the top-3 stay pure
    engine output (PLAN.md §10)."""
    if not studs:
        return None
    heading = Text(" ⭐ STUD AVAILABLE", style="bold yellow")
    tbl = Table.grid(padding=(0, 1))
    tbl.add_column(justify="right")
    tbl.add_column()
    tbl.add_column()
    tbl.add_column()
    for i, m in enumerate(studs, 1):
        rp = m.ranked
        depth = f"{rp.position}{rp.pos_rank}" if rp.pos_rank else rp.position
        tag = injuries.get(m.player_id or "", "")
        style = _injury_style(tag)
        tbl.add_row(
            Text(f"{i}.", style="bold"),
            Text(rp.name, style="bold white"),
            Text(depth, style="cyan"),
            Text(f"⚕ {tag}" if tag else "", style=style),
        )
    return Group(heading, tbl)


def _alerts_group(alerts: list[Alert]) -> Group | None:
    if not alerts:
        return None
    lines = []
    for a in alerts:
        style = "yellow" if a.kind == "tier-break" else "magenta"
        lines.append(Text(f" ⚠ {a.kind.upper()}: {a.text}", style=style))
    return Group(*lines)


def _ticker(state: DraftState) -> Text:
    t = Text("recent: ", style="dim")
    for p in reversed(state.recent_picks(6)):
        meta = p.get("metadata") or {}
        name = f"{(meta.get('first_name') or '')[:1]}.{meta.get('last_name') or '?'}".strip(".")
        pos = meta.get("position") or "?"
        t.append(f"{p.get('pick_no', '?')} {name}({pos})  ", style="dim")
    return t


def render(
    state: DraftState,
    roster: RosterModel,
    recs: list[Recommendation],
    alerts: list[Alert],
    league_name: str,
    engine_mode: str,
    studs: list[MatchResult] | None = None,
    is_mock: bool = False,
) -> Panel:
    parts: list = []
    if is_mock:
        parts.append(Text(banner("MOCK\nDRAFT"), style="bold yellow"))
    else:
        parts.append(Text(banner(league_name), style="bold blue"))
    parts.append(Text(""))
    studs = studs or []
    injuries = {
        pid: injury_tag(state.players_meta.get(pid))
        for pid in {r.match.player_id for r in recs} | {m.player_id for m in studs}
    }
    parts += [
        _header(state, league_name),
        Text(""),
        _roster_line(state, roster),
        _need_line(state, roster, engine_mode),
        Text(""),
        Text(" ▶ DRAFT NOW", style="bold green"),
        _recs_table(recs, injuries),
    ]
    stud_table = _studs_table(studs, injuries)
    if stud_table is not None:
        parts.append(Text(""))
        parts.append(stud_table)
    ag = _alerts_group(alerts)
    if ag is not None:
        parts.append(Text(""))
        parts.append(ag)
    parts.append(Text(""))
    parts.append(_ticker(state))
    return Panel(
        Group(*parts),
        border_style="yellow" if is_mock else "blue",
        title="Sleeper Draft Assistant — MOCK" if is_mock else "Sleeper Draft Assistant",
        title_align="left",
    )
