"""Thin client over Sleeper's public, no-auth REST API.

Docs: https://docs.sleeper.com/ — everything here is read-only. Sleeper's public
API cannot submit picks, which is by design (see PLAN.md): the tool advises, you tap.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

BASE = "https://api.sleeper.app/v1"

# The players/nfl endpoint returns a ~5MB blob Sleeper asks you to fetch at most
# once per day. We cache it on disk and only refetch when the cache is stale.
_PLAYERS_CACHE_TTL = 24 * 60 * 60  # seconds


class SleeperError(RuntimeError):
    """Raised when the Sleeper API returns something we can't use."""


class SleeperClient:
    def __init__(self, cache_dir: Path, timeout: float = 10.0) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "sleeper-draft-assistant/0.1"})

    def _get(self, path: str) -> Any:
        url = f"{BASE}/{path}"
        try:
            resp = self._session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SleeperError(f"request to {url} failed: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise SleeperError(f"{url} → HTTP {resp.status_code}")
        # Sleeper returns literal "null" (not an error) for missing resources.
        if not resp.content or resp.text == "null":
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise SleeperError(f"{url} → invalid JSON: {exc}") from exc

    # --- identity -------------------------------------------------------

    def get_user(self, username: str) -> dict[str, Any] | None:
        """Resolve a username (or user_id) to the user object."""
        return self._get(f"user/{username}")

    # --- league ---------------------------------------------------------

    def get_league(self, league_id: str) -> dict[str, Any] | None:
        return self._get(f"league/{league_id}")

    def get_league_drafts(self, league_id: str) -> list[dict[str, Any]]:
        return self._get(f"league/{league_id}/drafts") or []

    def get_rosters(self, league_id: str) -> list[dict[str, Any]]:
        return self._get(f"league/{league_id}/rosters") or []

    def get_league_users(self, league_id: str) -> list[dict[str, Any]]:
        return self._get(f"league/{league_id}/users") or []

    # --- draft ----------------------------------------------------------

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        return self._get(f"draft/{draft_id}")

    def get_draft_picks(self, draft_id: str) -> list[dict[str, Any]]:
        return self._get(f"draft/{draft_id}/picks") or []

    # --- players --------------------------------------------------------

    def get_players(self, force_refresh: bool = False) -> dict[str, Any]:
        """The full NFL player map (player_id -> player), cached on disk."""
        cache = self.cache_dir / "players_nfl.json"
        if not force_refresh and cache.exists():
            age = time.time() - cache.stat().st_mtime
            if age < _PLAYERS_CACHE_TTL:
                try:
                    return json.loads(cache.read_text())
                except (ValueError, OSError):
                    pass  # fall through to refetch
        try:
            data = self._get("players/nfl")
        except SleeperError:
            # A network/HTTP failure raises rather than returning None; a stale
            # cache is still far better than aborting, so fall back to it too.
            data = None
        if not isinstance(data, dict):
            # If the fetch failed but we have any cached copy, use it.
            if cache.exists():
                return json.loads(cache.read_text())
            raise SleeperError("could not load player database from Sleeper")
        try:
            cache.write_text(json.dumps(data))
        except OSError:
            pass
        return data
