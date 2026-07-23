# Sleeper Draft Assistant

A CLI that watches a live Sleeper fantasy-football draft, auto-ingests every pick
from Sleeper's public API, and shows a single auto-refreshing screen telling you
who to draft next. It's an **advisor, not an autopilot** — you still tap the pick
in the Sleeper app; the tool detects it on the next poll and recomputes.

See [PLAN.md](PLAN.md) for the full design rationale.

## Install

Managed with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

## One-time setup (per league)

```bash
uv run sleeper setup
```

You'll be asked for a short key (e.g. `A`), the Sleeper **league id**, your Sleeper
**username**, the **engine** (`redraft` = moderate roster-need, `dynasty` = lean
best-player-available), and a path to the rankings CSV. Config is saved to
`configs/leagues.json`; your username is resolved to a user id on first launch.

Then drop a **FantasyPros cheat-sheet CSV** (with tiers, format-matched to the
league) at the path you gave, and verify the names match:

```bash
uv run sleeper draft check A   # run a day or two out, especially for rookie drafts
```

Any unmatched player prints as a loud warning — fix the CSV name/team or add an
entry to `ALIASES` in `src/sleeper_assistant/matching.py` **before** draft night.

## Draft night

```bash
uv run sleeper draft --league A   # or just `uv run sleeper draft` to pick interactively
```

The tool resolves the current draft id from the league id, reads scoring / roster
slots / draft type live from the API, then polls every ~2.5s and redraws:

- three suggestions (not ten),
- your starting slots as ✓ / – checkboxes so you see *why*,
- tier-break and positional-run alerts, only when firing,
- a recent-picks ticker to confirm the tool is keeping up.

## Commands

| command | what it does |
|---|---|
| `sleeper setup` | add / update a league config (shared by all assistants) |
| `sleeper list` | show configured leagues |
| `sleeper draft` | launch; pick a league, run the live assistant |
| `sleeper draft --league A` | launch straight into league A |
| `sleeper draft check [A]` | name-match dry run (no draft needed) |
| `sleeper draft mock <id>` | test the live loop against a mock-draft id |
| `sleeper waiver --league A` | waiver-wire assistant (coming soon) |
| `sleeper start --league A` | start/sit assistant (coming soon) |

## The value engine

- **redraft:** `score = base_value(rank) × need_multiplier(roster)`. The multiplier
  is deliberately moderate — a needed player climbs at most ~one tier, ties break
  within a tier, and a position is discounted once its starting slots are full.
  FLEX stays open to the strongest position. **K/DEF get no need boost until the
  last ~2 rounds.**
- **dynasty:** lean best-player-available — the need multiplier is off, so the
  superflex rookie rankings drive the order and you draft the best long-term asset.

## Tests

```bash
uv run pytest
```

Covers CSV parsing, name matching (suffixes / aliases / team defenses), the
roster-need model, the engine (need reordering, the K/DEF guardrail, dynasty BPA),
and the snake-draft clock — all offline against a fixture, no live draft needed.
