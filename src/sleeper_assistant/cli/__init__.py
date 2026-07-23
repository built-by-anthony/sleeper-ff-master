"""The `sleeper` command-line surface.

Sub-apps, one per assistant, are wired together in `root.py`:

    sleeper setup / list        shared per-league config
    sleeper draft  ...          live draft assistant  (cli/draft.py)
    sleeper waiver ...          waiver-wire assistant (cli/waiver.py — stub)
    sleeper start  ...          start/sit assistant   (cli/start.py — stub)
"""
