"""The `sleeper` root command: shared config commands + the assistant sub-apps.

    sleeper setup / list         per-league config, shared by every assistant
    sleeper draft  ...           live draft assistant
    sleeper waiver ...           waiver-wire assistant (stub)
    sleeper start  ...           start/sit assistant   (stub)
"""

from __future__ import annotations

import typer
from rich.prompt import Prompt
from rich.table import Table

from ..config import Config, LeagueConfig
from . import draft, start, waiver
from .common import _guarded, console

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Sleeper fantasy-football assistant — draft, waiver, and start/sit tools.",
)

app.add_typer(draft.app, name="draft")
app.add_typer(waiver.app, name="waiver")
app.add_typer(start.app, name="start")


# --------------------------------------------------------------------------- #
# shared per-league config
# --------------------------------------------------------------------------- #

def cmd_setup(cfg: Config) -> int:
    console.print("[bold]Add / update a league[/]  (Ctrl-C to cancel)\n")
    key = Prompt.ask("league key (short, e.g. A or B)")
    existing = cfg.leagues.get(key)
    name = Prompt.ask("display name", default=existing.name if existing else f"League {key}")
    league_id = Prompt.ask("Sleeper league id", default=existing.league_id if existing else "")
    username = Prompt.ask("your Sleeper username", default=existing.username if existing else "")
    engine = Prompt.ask("engine", choices=["redraft", "dynasty"], default=existing.engine if existing else "redraft")
    csv_path = Prompt.ask(
        "rankings CSV path (relative to project root ok)",
        default=existing.csv_path if existing else f"data/{key}.csv",
    )
    cfg.leagues[key] = LeagueConfig(
        key=key, name=name, league_id=league_id, username=username,
        user_id=existing.user_id if existing else "", csv_path=csv_path, engine=engine,
    )
    path = cfg.save()
    console.print(f"\n[green]saved[/] → {path}")
    console.print("Next: drop the CSV at the path above, then run [bold]sleeper draft check[/] to verify names.")
    return 0


def cmd_list(cfg: Config) -> int:
    if not cfg.leagues:
        console.print("[yellow]No leagues configured. Run [bold]sleeper setup[/].[/]")
        return 0
    tbl = Table(title="Configured leagues")
    for col in ("key", "name", "engine", "league_id", "username", "csv"):
        tbl.add_column(col)
    for lc in cfg.leagues.values():
        tbl.add_row(lc.key, lc.name, lc.engine, lc.league_id, lc.username or lc.user_id, lc.csv_path)
    console.print(tbl)
    return 0


@app.command("setup", help="Add or update a league's saved config (shared by all assistants).")
def _setup() -> None:
    raise typer.Exit(_guarded(lambda: cmd_setup(Config.load())))


@app.command("list", help="Show configured leagues.")
def _list() -> None:
    raise typer.Exit(_guarded(lambda: cmd_list(Config.load())))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
