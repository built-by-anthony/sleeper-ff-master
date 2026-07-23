import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def fake_players() -> dict:
    """A tiny Sleeper-style player map matching data/sample_redraft.csv."""
    p = {}

    def add(pid, first, last, pos, team):
        p[pid] = {
            "player_id": pid,
            "first_name": first,
            "last_name": last,
            "full_name": f"{first} {last}",
            "position": pos,
            "fantasy_positions": [pos],
            "team": team,
        }

    add("1001", "Christian", "McCaffrey", "RB", "SF")
    add("1002", "CeeDee", "Lamb", "WR", "DAL")
    add("1003", "Tyreek", "Hill", "WR", "MIA")
    add("1004", "Bijan", "Robinson", "RB", "ATL")
    add("1005", "Josh", "Allen", "QB", "BUF")
    add("1006", "Breece", "Hall", "RB", "NYJ")
    add("1007", "Ja'Marr", "Chase", "WR", "CIN")
    add("1008", "Sam", "LaPorta", "TE", "DET")
    add("1009", "Jahmyr", "Gibbs", "RB", "DET")
    add("1010", "Patrick", "Mahomes", "QB", "KC")
    add("1011", "Michael", "Pittman", "WR", "IND")   # CSV has "Jr." — alias/normalize
    add("1012", "Kenneth", "Walker", "RB", "SEA")     # CSV has "III"
    add("1013", "Travis", "Kelce", "TE", "KC")
    add("1014", "Gabriel", "Davis", "WR", "JAX")      # CSV "Gabe Davis" — alias
    add("1015", "Justin", "Tucker", "K", "BAL")
    # Sleeper lists him at TE though FantasyPros ranks him RB — exercises
    # matching.POSITION_OVERRIDES.
    add("1016", "Max", "Bredeson", "TE", "FA")
    # Sleeper lists fullbacks at "FB" — exercises the FB->RB fold in
    # matching._sleeper_pos().
    add("1017", "Alec", "Ingold", "FB", "MIA")
    # team defense
    p["DAL"] = {
        "player_id": "DAL", "first_name": "Dallas", "last_name": "Cowboys",
        "full_name": "Dallas Cowboys", "position": "DEF",
        "fantasy_positions": ["DEF"], "team": "DAL",
    }
    # Sleeper keys Jacksonville under "JAX" while FantasyPros CSVs often use
    # "JAC" — exercises matching._TEAM_ABBR_ALIASES.
    p["JAX"] = {
        "player_id": "JAX", "first_name": "Jacksonville", "last_name": "Jaguars",
        "full_name": "Jacksonville Jaguars", "position": "DEF",
        "fantasy_positions": ["DEF"], "team": "JAX",
    }
    return p
