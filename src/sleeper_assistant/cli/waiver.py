"""The `sleeper waiver` sub-app: waiver-wire add/drop assistant.

Stub for now — wired to shared config/identity so `--league` resolves, but the
evaluation itself isn't built yet. Unlike draft, this is a one-shot report (a
single pull when you run it), not a live poll loop.
"""

from __future__ import annotations

from typing import Optional

import typer

from ..config import Config
from .common import _guarded, _pick_league, console

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode="rich",
    help="Waiver-wire assistant (coming soon).",
)


def cmd_waiver(cfg: Config, key: str | None) -> int:
    league = _pick_league(cfg, key)
    console.print(
        f"[yellow]waiver assistant for {league.name} — coming soon.[/] "
        "(one-shot add/drop report; not yet implemented)"
    )
    return 0


@app.callback(invoke_without_command=True)
def _entry(
    ctx: typer.Context,
    league: Optional[str] = typer.Option(
        None, "--league", "-l", help="League key or name to evaluate waivers for."
    ),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    cfg = Config.load()
    raise typer.Exit(_guarded(lambda: cmd_waiver(cfg, league)))
