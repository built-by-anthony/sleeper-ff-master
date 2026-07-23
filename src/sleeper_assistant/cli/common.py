"""Plumbing shared by every assistant sub-app (draft, waiver, start).

Nothing here is draft-specific: client construction, league selection, identity
resolution, the CSV name-match, and the Ctrl-C guard. Each sub-app imports what
it needs so the assistants can't drift apart on config/identity handling.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Prompt

from ..config import DEFAULT_HOME, Config, LeagueConfig
from ..matching import PlayerMatcher, match_all
from ..rankings import load_rankings
from ..sleeper import SleeperClient

console = Console()


def _cache_dir() -> Path:
    return Path(os.environ.get("DRAFT_HOME", DEFAULT_HOME)) / ".cache"


def _client() -> SleeperClient:
    return SleeperClient(cache_dir=_cache_dir())


def _guarded(fn) -> int:
    """Run a command, turning Ctrl-C into a clean exit code."""
    try:
        return fn()
    except KeyboardInterrupt:
        console.print("\n[dim]cancelled.[/]")
        return 130


# --------------------------------------------------------------------------- #
# league selection / identity
# --------------------------------------------------------------------------- #

def _pick_league(cfg: Config, key: str | None) -> LeagueConfig:
    if not cfg.leagues:
        console.print("[red]No leagues configured.[/] Run:  [bold]sleeper setup[/]")
        raise typer.Exit(1)
    if key:
        # Accept the short key OR the league's display name (case-insensitive),
        # so `--league NHFL` works as well as `--league A`.
        if key in cfg.leagues:
            return cfg.leagues[key]
        matches = [
            lc for lc in cfg.leagues.values()
            if key.lower() in (lc.key.lower(), lc.name.lower())
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            console.print(
                f"[red]Ambiguous league '{key}'[/] — matches "
                f"{', '.join(lc.key for lc in matches)}. Use the short key."
            )
            raise typer.Exit(1)
        configured = ", ".join(f"{k} ({lc.name})" for k, lc in cfg.leagues.items())
        console.print(f"[red]Unknown league '{key}'.[/] Configured: {configured}")
        raise typer.Exit(1)
    if len(cfg.leagues) == 1:
        return next(iter(cfg.leagues.values()))
    console.print("[bold]Which league?[/]")
    for k, lc in cfg.leagues.items():
        console.print(f"  [cyan]{k}[/] — {lc.name} ({lc.engine})")
    choice = Prompt.ask("league", choices=list(cfg.leagues.keys()))
    return cfg.leagues[choice]


def _ensure_user_id(cfg: Config, league: LeagueConfig, client: SleeperClient) -> str:
    if league.user_id:
        return league.user_id
    if not league.username:
        console.print(f"[red]League '{league.key}' has no username or user_id.[/]")
        raise typer.Exit(1)
    user = client.get_user(league.username)
    if not user or not user.get("user_id"):
        console.print(f"[red]Could not resolve Sleeper username '{league.username}'.[/]")
        raise typer.Exit(1)
    league.user_id = str(user["user_id"])
    cfg.save()
    console.print(f"[dim]resolved {league.username} → user_id {league.user_id} (saved)[/]")
    return league.user_id


# --------------------------------------------------------------------------- #
# CSV name-match (shared: draft uses it now, waiver/start will too)
# --------------------------------------------------------------------------- #

def _build_matched(cfg: Config, league: LeagueConfig, client: SleeperClient, verbose: bool):
    csv_path = cfg.resolve_csv(league)
    rankings = load_rankings(csv_path)
    players = client.get_players()
    matcher = PlayerMatcher(players)
    results = match_all(rankings, matcher)

    unmatched = [r for r in results if r.player_id is None]
    fuzzy = [r for r in results if r.method == "fuzzy"]
    matched = [r for r in results if r.player_id is not None]

    if verbose or unmatched or fuzzy:
        console.rule(f"[bold]Name match report — {league.name}[/]")
        console.print(
            f"CSV: [cyan]{csv_path}[/]  ·  {len(rankings)} ranked  ·  "
            f"[green]{len(matched)} matched[/]  ·  [red]{len(unmatched)} unmatched[/]"
        )
    if fuzzy:
        console.print(f"\n[yellow]⚠ {len(fuzzy)} fuzzy matches — eyeball these:[/]")
        for r in fuzzy:
            console.print(
                f"    [yellow]{r.ranked.name} ({r.ranked.position})[/] → "
                f"{r.sleeper_name}  [dim](score {r.score:.0f})[/]"
            )
    if unmatched:
        console.print(f"\n[bold red]⚠ {len(unmatched)} UNMATCHED — fix before the draft:[/]")
        for r in unmatched:
            console.print(f"    [red]#{r.ranked.rank:<3} {r.ranked.name} ({r.ranked.position}) {r.ranked.team}[/]")
        console.print(
            "\n[dim]Fix by correcting the CSV name/team, or add an alias in "
            "matching.py ALIASES. Team defenses match by team abbreviation.[/]"
        )
    elif verbose:
        console.print("[green]✓ every ranked player matched a Sleeper id.[/]")
    return matched, players
