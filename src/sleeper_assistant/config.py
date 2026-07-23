"""Per-league saved config so nothing gets retyped on draft night.

Stores the league id, your resolved Sleeper user id, the rankings CSV path, and
which value engine to run. The draft id is intentionally *not* stored — it is
resolved live from the league id at launch (drafts can be recreated).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Config + data live next to the project by default; override with $DRAFT_HOME.
DEFAULT_HOME = Path(__file__).resolve().parents[2]


@dataclass
class LeagueConfig:
    key: str  # short selector, e.g. "A" / "B"
    name: str
    league_id: str
    username: str = ""
    user_id: str = ""
    csv_path: str = ""
    # "redraft" -> moderate roster-need engine; "dynasty" -> lean BPA.
    engine: str = "redraft"

    def validate(self) -> list[str]:
        problems = []
        if not self.league_id:
            problems.append(f"league '{self.key}': missing league_id")
        if not self.user_id and not self.username:
            problems.append(f"league '{self.key}': missing username/user_id")
        if not self.csv_path:
            problems.append(f"league '{self.key}': missing csv_path")
        if self.engine not in ("redraft", "dynasty"):
            problems.append(
                f"league '{self.key}': engine must be 'redraft' or 'dynasty', got {self.engine!r}"
            )
        return problems


@dataclass
class Config:
    leagues: dict[str, LeagueConfig] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def default_path(cls) -> Path:
        import os

        home = Path(os.environ.get("DRAFT_HOME", DEFAULT_HOME))
        return home / "configs" / "leagues.json"

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or cls.default_path()
        if not path.exists():
            return cls(leagues={}, path=path)
        raw = json.loads(path.read_text())
        leagues = {}
        for key, data in raw.get("leagues", {}).items():
            data = {**data, "key": key}
            leagues[key] = LeagueConfig(**data)
        return cls(leagues=leagues, path=path)

    def save(self, path: Path | None = None) -> Path:
        path = path or self.path or self.default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        out = {
            "leagues": {
                key: {k: v for k, v in asdict(lc).items() if k != "key"}
                for key, lc in self.leagues.items()
            }
        }
        path.write_text(json.dumps(out, indent=2))
        return path

    def resolve_csv(self, league: LeagueConfig) -> Path:
        p = Path(league.csv_path)
        if not p.is_absolute() and self.path is not None:
            p = self.path.parent.parent / league.csv_path
        return p
