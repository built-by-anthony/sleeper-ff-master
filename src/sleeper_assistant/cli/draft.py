"""The `sleeper draft` sub-app: the live draft assistant.

    sleeper draft [--league A]      launch the live assistant (default action)
    sleeper draft check [A]         startup name-match dry run
    sleeper draft mock <id>         drive the loop off a raw (mock) draft id
"""

from __future__ import annotations

import time
from typing import Optional

import typer
from rich.live import Live

from ..config import Config, LeagueConfig
from ..engine import Engine, studs
from ..roster import RosterModel
from ..sleeper import SleeperClient, SleeperError
from ..state import DraftMeta, DraftState
from ..ui import render
from .common import (
    _build_matched,
    _client,
    _ensure_user_id,
    _guarded,
    _pick_league,
    console,
)

POLL_SECONDS = 2.5


def _resolve_draft(client: SleeperClient, league_id: str) -> dict:
    drafts = client.get_league_drafts(league_id)
    if not drafts:
        console.print(
            f"[red]No draft found for league {league_id}.[/] "
            "The draft object may not exist until it's created/scheduled in Sleeper."
        )
        raise typer.Exit(1)

    def rank(d: dict) -> tuple:
        status_pri = {"drafting": 3, "paused": 3, "pre_draft": 2, "complete": 1}
        return (status_pri.get(d.get("status"), 0), d.get("start_time") or 0, d.get("created") or 0)

    return max(drafts, key=rank)


def cmd_check(cfg: Config, key: str | None) -> int:
    league = _pick_league(cfg, key)
    client = _client()
    matched, _ = _build_matched(cfg, league, client, verbose=True)
    return 0 if matched else 1


# --------------------------------------------------------------------------- #
# live loop
# --------------------------------------------------------------------------- #

def _users_map(client, league_id: str) -> dict[str, str]:
    """user_id -> display name (preferring a custom team name) for the league,
    so the screen can show who's on the clock."""
    out: dict[str, str] = {}
    for u in client.get_league_users(league_id):
        uid = str(u.get("user_id") or "")
        if not uid:
            continue
        meta = u.get("metadata") or {}
        out[uid] = meta.get("team_name") or u.get("display_name") or f"user {uid}"
    return out


def _load_state(client, league, draft_id, league_obj, my_user_id, matched, players, users=None) -> DraftState:
    # Re-fetch the draft object every poll: its `status` flips to "complete" when
    # the draft ends, and that's the loop's only termination signal. Reusing the
    # startup snapshot would freeze the status and poll forever.
    draft = client.get_draft(draft_id)
    if not draft:
        raise SleeperError(f"draft {draft_id} not found on this poll")
    meta = DraftMeta.from_api(draft, league_obj)
    picks = client.get_draft_picks(meta.draft_id)

    # my roster id + existing (dynasty) roster, plus every player owned across the
    # league so free-agent-eligible drafts don't offer already-rostered players.
    my_roster_id = None
    existing: set[str] = set()
    rostered: set[str] = set()
    for r in client.get_rosters(league.league_id):
        players_on = {str(p) for p in (r.get("players") or [])}
        rostered |= players_on
        if str(r.get("owner_id")) == my_user_id:
            my_roster_id = r.get("roster_id")
            existing = players_on

    return DraftState(
        meta=meta,
        picks=picks,
        my_user_id=my_user_id,
        my_roster_id=my_roster_id,
        matched=matched,
        players_meta=players,
        existing_roster_ids=existing,
        rostered_ids=rostered,
        users=users or {},
    )


def _roster_positions_from_draft(draft: dict) -> list[str]:
    """Rebuild a roster_positions list from a draft's `settings.slots_*` counts.

    A standalone mock draft isn't attached to a league, so there's no
    league.roster_positions to read; the slot counts on the draft are the only
    source of the format. Each `slots_<token>: n` becomes n copies of TOKEN
    (uppercased), matching the vocabulary RosterModel understands (QB, RB, WR,
    TE, K, DEF, FLEX, SUPER_FLEX, …).
    """
    settings = draft.get("settings") or {}
    positions: list[str] = []
    for key, count in settings.items():
        if not key.startswith("slots_"):
            continue
        try:
            n = int(count)
        except (TypeError, ValueError):
            continue
        positions.extend([key[len("slots_"):].upper()] * n)
    return positions


def _load_mock_state(client, draft_id, league_obj, my_user_id, matched, players) -> DraftState:
    """State for a mock draft: like _load_state but with no league rosters — a
    standalone mock has none, so nothing is seeded as owned and the whole board is
    available minus what's been drafted in the mock itself."""
    draft = client.get_draft(draft_id)
    if not draft:
        raise SleeperError(f"draft {draft_id} not found")
    meta = DraftMeta.from_api(draft, league_obj)
    picks = client.get_draft_picks(meta.draft_id)
    return DraftState(
        meta=meta,
        picks=picks,
        my_user_id=my_user_id,
        my_roster_id=None,
        matched=matched,
        players_meta=players,
    )


def _live_loop(engine, roster_model, load_state, screen_name, engine_mode, is_mock=False) -> None:
    """Shared poll/redraw loop. `load_state` is a zero-arg callable returning a
    fresh DraftState each poll (real draft or mock)."""
    console.print(f"[green]ready[/] — polling every {POLL_SECONDS:g}s. Ctrl-C to quit.\n")
    with Live(console=console, refresh_per_second=4, screen=False) as live:
        while True:
            try:
                state = load_state()
                counts = state.my_counts()
                available = state.available()
                recs = engine.recommend(available, counts, state.rounds_left())
                alerts = engine.alerts(available, state.recent_pick_positions(), recs)
                live.update(render(state, roster_model, recs, alerts, screen_name, engine_mode, studs(available), is_mock))
                if state.meta.status == "complete":
                    break
            except SleeperError as exc:
                console.print(f"[yellow]poll hiccup: {exc} — retrying[/]")
            except KeyboardInterrupt:
                break
            try:
                time.sleep(POLL_SECONDS)
            except KeyboardInterrupt:
                break
    console.print("\n[dim]draft complete / stopped.[/]")


def cmd_run(cfg: Config, key: str | None) -> int:
    league = _pick_league(cfg, key)
    client = _client()

    problems = league.validate()
    if problems:
        console.print("[red]Config problems:[/]")
        for p in problems:
            console.print(f"  - {p}")
        return 1

    my_user_id = _ensure_user_id(cfg, league, client)

    console.print(f"[dim]resolving draft for {league.name}…[/]")
    draft = _resolve_draft(client, league.league_id)
    league_obj = client.get_league(league.league_id) or {}
    meta = DraftMeta.from_api(draft, league_obj)

    # report detected format
    n_qb = sum(1 for p in meta.roster_positions if p.upper() in ("QB", "SUPER_FLEX", "SUPERFLEX"))
    console.print(
        f"[dim]draft {meta.draft_id} · type={meta.type} · {meta.teams} teams · "
        f"{meta.rounds} rounds · engine={league.engine} · QB-ish slots={n_qb} · status={meta.status}[/]"
    )

    matched, players = _build_matched(cfg, league, client, verbose=False)
    if not matched:
        console.print("[red]No players matched — aborting.[/]")
        return 1

    roster_model = RosterModel.from_positions(meta.roster_positions)
    engine = Engine(mode=league.engine, roster=roster_model)

    users = _users_map(client, league.league_id)  # static for the draft — fetch once

    _live_loop(
        engine,
        roster_model,
        lambda: _load_state(client, league, meta.draft_id, league_obj, my_user_id, matched, players, users),
        league.name,
        league.engine,
    )
    return 0


def cmd_mock(cfg: Config, draft_id: str, key: str | None) -> int:
    """Drive the live loop off a raw Sleeper draft id (e.g. a mock-lobby draft).

    Borrows a configured league only for identity (username→user_id), the rankings
    CSV, and the engine mode. There are no league rosters, so the board is *not*
    filtered by ownership — every ranked player is available minus mock picks.
    """
    league = _pick_league(cfg, key)
    client = _client()

    my_user_id = _ensure_user_id(cfg, league, client)

    draft = client.get_draft(draft_id)
    if not draft:
        console.print(f"[red]No draft found with id {draft_id}.[/] Check the mock draft id.")
        return 1
    league_obj = {"roster_positions": _roster_positions_from_draft(draft)}
    meta = DraftMeta.from_api(draft, league_obj)

    console.print(
        f"[yellow]MOCK[/] draft {meta.draft_id} · type={meta.type} · {meta.teams} teams · "
        f"{meta.rounds} rounds · engine={league.engine} · status={meta.status}"
    )
    console.print(
        "[dim]no league rosters in a mock — the board is not filtered by ownership, "
        "so top-ranked veterans will show as available.[/]"
    )

    matched, players = _build_matched(cfg, league, client, verbose=False)
    if not matched:
        console.print("[red]No players matched — aborting.[/]")
        return 1

    roster_model = RosterModel.from_positions(meta.roster_positions)
    engine = Engine(mode=league.engine, roster=roster_model)

    _live_loop(
        engine,
        roster_model,
        lambda: _load_mock_state(client, meta.draft_id, league_obj, my_user_id, matched, players),
        f"{league.name} (MOCK)",
        league.engine,
        is_mock=True,
    )
    return 0


# --------------------------------------------------------------------------- #
# Typer sub-app  (mounted at `sleeper draft` in root.py)
# --------------------------------------------------------------------------- #

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode="rich",
    help="Live draft assistant — auto-ingests picks, tells you who to draft next.",
)


@app.callback(invoke_without_command=True)
def _entry(
    ctx: typer.Context,
    league: Optional[str] = typer.Option(
        None, "--league", "-l", help="League key or name to launch straight into (skips the prompt)."
    ),
) -> None:
    """Launch the live assistant when no draft subcommand is given."""
    if ctx.invoked_subcommand is not None:
        return
    cfg = Config.load()
    raise typer.Exit(_guarded(lambda: cmd_run(cfg, league)))


@app.command("check", help="Name-match dry run — run a day or two out, no live draft needed.")
def _check(
    league: Optional[str] = typer.Argument(None, help="League key; omit to pick interactively."),
) -> None:
    cfg = Config.load()
    raise typer.Exit(_guarded(lambda: cmd_check(cfg, league)))


@app.command("mock", help="Test the live loop against a Sleeper mock-draft id (board is unfiltered).")
def _mock(
    draft_id: str = typer.Argument(..., help="Sleeper draft id of the mock draft to watch."),
    league: Optional[str] = typer.Option(
        None, "--league", "-l", help="League key or name to borrow CSV/engine/identity from."
    ),
) -> None:
    cfg = Config.load()
    raise typer.Exit(_guarded(lambda: cmd_mock(cfg, draft_id, league)))
