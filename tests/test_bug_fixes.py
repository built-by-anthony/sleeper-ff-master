"""Regression tests for four bugs found in code review.

Each test fails against the pre-fix code and passes after the fix:

  1. live loop froze the draft `status` (never detected "complete")
  2. team defenses (DST) never matched their DEF roster slot → no late need boost
  3. get_players crashed on a network error instead of using a stale cache
  4. suffix-bearing ALIASES keys were unreachable dead entries
"""

import os
import time
from pathlib import Path

from conftest import fake_players

from sleeper_assistant.cli.draft import _load_state
from sleeper_assistant.config import LeagueConfig
from sleeper_assistant.engine import Engine
from sleeper_assistant.matching import ALIASES, MatchResult, PlayerMatcher, match_all, normalize_name
from sleeper_assistant.rankings import RankedPlayer
from sleeper_assistant.rankings import load_rankings
from sleeper_assistant.roster import STARTER, SURPLUS, RosterModel
from sleeper_assistant.sleeper import SleeperClient, SleeperError
from sleeper_assistant.state import DraftMeta, DraftState, injury_tag

CSV = Path(__file__).resolve().parents[1] / "data" / "sample_redraft.csv"
POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN", "BN"]


def _matched():
    return match_all(load_rankings(CSV), PlayerMatcher(fake_players()))


# --- Bug 1: live loop must re-poll the draft so status is never frozen -------

class _FakeClient:
    """Minimal stand-in for SleeperClient covering what _load_state touches."""

    def __init__(self, draft, rosters=None, picks=None):
        self.draft = draft
        self.rosters = rosters or []
        self.picks = picks or []
        self.get_draft_calls = 0
        self.get_rosters_calls = 0

    def get_draft(self, draft_id):
        self.get_draft_calls += 1
        return self.draft

    def get_draft_picks(self, draft_id):
        return self.picks

    def get_rosters(self, league_id):
        self.get_rosters_calls += 1
        return self.rosters


def _draft_obj(status):
    return {
        "draft_id": "d1", "type": "snake", "status": status,
        "settings": {"rounds": 3, "teams": 4},
        "slot_to_roster_id": {str(s): s for s in range(1, 5)},
        "draft_order": {"me": 3},
    }


def test_load_state_reflects_live_draft_status():
    league = LeagueConfig(key="A", name="A", league_id="L1", user_id="me",
                          csv_path="x.csv", engine="redraft")
    league_obj = {"roster_positions": POSITIONS, "scoring_settings": {}}
    client = _FakeClient(draft=_draft_obj("drafting"))
    matched, players = _matched(), fake_players()

    st = _load_state(client, league, "d1", league_obj, "me", matched, players)
    assert st.meta.status == "drafting"

    # The draft ends: the very next poll must observe "complete" (pre-fix the
    # startup snapshot was reused and this stayed "drafting" forever).
    client.draft = _draft_obj("complete")
    st2 = _load_state(client, league, "d1", league_obj, "me", matched, players)
    assert st2.meta.status == "complete"
    assert client.get_draft_calls == 2  # fetched fresh each poll, not cached


def test_load_state_raises_when_draft_missing():
    league = LeagueConfig(key="A", name="A", league_id="L1", user_id="me",
                          csv_path="x.csv", engine="redraft")
    client = _FakeClient(draft=None)
    try:
        _load_state(client, league, "d1", {}, "me", _matched(), fake_players())
    except SleeperError:
        pass
    else:
        raise AssertionError("expected SleeperError when the draft can't be fetched")


# --- Dynasty roster-aware BPA: fade a position once you can't start more ------

def test_startable_slots_counts_direct_and_flex():
    r = RosterModel.from_positions(POSITIONS)  # QB,RB,RB,WR,WR,TE,FLEX(RB/WR/TE),K,DEF
    assert r.startable_slots("QB") == 1
    assert r.startable_slots("RB") == 3   # 2 direct + 1 flex
    assert r.startable_slots("WR") == 3
    assert r.startable_slots("TE") == 2
    # superflex: QB also fits the SUPER_FLEX slot, so two QBs are startable
    sf = RosterModel.from_positions(["QB", "SUPER_FLEX", "RB", "WR", "BN"])
    assert sf.startable_slots("QB") == 2


def test_dynasty_fades_position_once_startable_slots_full():
    eng = Engine("dynasty", RosterModel.from_positions(POSITIONS))  # QB startable = 1
    # no QB yet → drafting your startable QB is full BPA value
    assert eng._multiplier("QB", {}, rounds_left=10) == (1.0, "BPA")
    # already have your 1 startable QB → another QB is surplus, faded below value
    mult, why = eng._multiplier("QB", {"QB": 1}, rounds_left=10)
    assert mult < 1.0 and "QB" in why
    # stacking a 3rd fades harder (graded by depth)
    m2, _ = eng._multiplier("QB", {"QB": 2}, rounds_left=10)
    assert m2 < mult


def test_dynasty_superflex_allows_two_qbs_before_fade():
    # Format-adaptive: a SUPER_FLEX slot means 2 startable QBs, so the fade only
    # bites on the 3rd — the same code that fades the 2nd QB in a 1-QB format.
    eng = Engine("dynasty", RosterModel.from_positions(["QB", "SUPER_FLEX", "RB", "WR", "BN"]))
    assert eng._multiplier("QB", {"QB": 1}, rounds_left=10) == (1.0, "BPA")  # 2nd QB startable
    mult, _ = eng._multiplier("QB", {"QB": 2}, rounds_left=10)               # 3rd QB surplus
    assert mult < 1.0


def test_dynasty_surplus_qb_yields_to_comparable_value():
    # The reported bug: with a QB already startable, a slightly-lower-ranked but
    # usable RB should now lead a surplus QB instead of stacking QBs.
    eng = Engine("dynasty", RosterModel.from_positions(POSITIONS))
    available = [_mr(1, 1, "QB"), _mr(10, 2, "RB")]
    recs = eng.recommend(available, {"QB": 1}, rounds_left=10, top_n=2)
    assert recs[0].match.ranked.position == "RB"


# --- Free agents: available() must exclude already-rostered players ----------

def _bare_state(picks, rostered_ids=frozenset()):
    meta = DraftMeta.from_api(_draft_obj("drafting"),
                              {"roster_positions": POSITIONS, "scoring_settings": {}})
    return DraftState(
        meta=meta, picks=picks, my_user_id="me", my_roster_id=4,
        matched=_matched(), players_meta=fake_players(),
        rostered_ids=set(rostered_ids),
    )


def test_available_excludes_rostered_players():
    # In a dynasty draft you can also take free agents, so a player already on a
    # roster must NOT be offered as available — even though it wasn't drafted here.
    picks = [{"player_id": "1001", "pick_no": 1, "picked_by": "opp", "metadata": {"position": "RB"}}]
    st = _bare_state(picks=picks, rostered_ids={"1002"})
    ids = {m.player_id for m in st.available()}
    assert "1001" not in ids   # drafted in this draft
    assert "1002" not in ids   # already owned on a roster (the FA-draft case)
    assert "1003" in ids       # genuinely available


def test_load_state_collects_all_rostered_ids():
    league = LeagueConfig(key="A", name="A", league_id="L1", user_id="me",
                          csv_path="x.csv", engine="dynasty")
    league_obj = {"roster_positions": POSITIONS, "scoring_settings": {}}
    rosters = [
        {"owner_id": "me", "roster_id": 4, "players": ["1004"]},          # mine
        {"owner_id": "opp", "roster_id": 1, "players": ["1002", "1013"]},  # another team
    ]
    client = _FakeClient(draft=_draft_obj("drafting"), rosters=rosters)
    st = _load_state(client, league, "d1", league_obj, "me", _matched(), fake_players())

    assert st.rostered_ids == {"1004", "1002", "1013"}  # union across the league
    assert st.existing_roster_ids == {"1004"}           # still tracks just mine
    ids = {m.player_id for m in st.available()}
    assert ids.isdisjoint({"1004", "1002", "1013"})     # no owned player is available
    assert "1003" in ids


# --- League selection by key OR name ----------------------------------------

def _two_league_cfg():
    from sleeper_assistant.config import Config
    return Config(leagues={
        "A": LeagueConfig(key="A", name="NHFL", league_id="1", user_id="u", csv_path="a.csv", engine="dynasty"),
        "B": LeagueConfig(key="B", name="Dynasty Doods", league_id="2", user_id="u", csv_path="b.csv", engine="dynasty"),
    })


def test_pick_league_by_key_or_name():
    from sleeper_assistant.cli.common import _pick_league
    cfg = _two_league_cfg()
    assert _pick_league(cfg, "A").name == "NHFL"            # short key
    assert _pick_league(cfg, "NHFL").key == "A"             # exact name
    assert _pick_league(cfg, "nhfl").key == "A"             # name, case-insensitive
    assert _pick_league(cfg, "Dynasty Doods").key == "B"    # name with spaces


def test_pick_league_unknown_raises():
    import typer

    from sleeper_assistant.cli.common import _pick_league
    try:
        _pick_league(_two_league_cfg(), "does-not-exist")
    except typer.Exit:
        pass
    else:
        raise AssertionError("expected typer.Exit for an unknown league")


# --- Current drafter on the clock -------------------------------------------

def _draft_with_order():
    # 2-team snake: slot 1 = u1, slot 2 = me
    return {
        "draft_id": "d1", "type": "snake", "status": "drafting",
        "settings": {"rounds": 2, "teams": 2},
        "slot_to_roster_id": {"1": 1, "2": 2},
        "draft_order": {"u1": 1, "me": 2},
    }


def test_on_clock_name_resolves_and_falls_back():
    meta = DraftMeta.from_api(_draft_with_order(),
                              {"roster_positions": POSITIONS, "scoring_settings": {}})
    st = DraftState(meta=meta, picks=[], my_user_id="me", my_roster_id=2,
                    matched=[], players_meta={}, users={"u1": "Rival", "me": "Antony"})
    assert st.on_clock_slot() == 1              # pick 1 → slot 1
    assert st.on_clock_name() == "Rival"        # slot 1 → u1 → display name

    # unknown users → fall back to the slot number
    st2 = DraftState(meta=meta, picks=[], my_user_id="me", my_roster_id=2,
                     matched=[], players_meta={}, users={})
    assert st2.on_clock_name() == "Team 1"


def test_users_map_prefers_team_name():
    from sleeper_assistant.cli.draft import _users_map

    class C:
        def get_league_users(self, league_id):
            return [
                {"user_id": "u1", "display_name": "rival_guy", "metadata": {"team_name": "Da Bears"}},
                {"user_id": "u2", "display_name": "solo"},
                {"user_id": None},  # skipped
            ]

    assert _users_map(C(), "L1") == {"u1": "Da Bears", "u2": "solo"}


def test_load_state_threads_users():
    client = _FakeClient(draft=_draft_obj("drafting"))
    league = LeagueConfig(key="A", name="A", league_id="L1", user_id="me",
                          csv_path="x.csv", engine="dynasty")
    st = _load_state(client, league, "d1",
                     {"roster_positions": POSITIONS, "scoring_settings": {}},
                     "me", _matched(), fake_players(), users={"me": "Antony"})
    assert st.users == {"me": "Antony"}


def test_render_shows_current_drafter():
    import io

    from rich.console import Console

    from sleeper_assistant.ui import render

    meta = DraftMeta.from_api(_draft_with_order(),
                              {"roster_positions": POSITIONS, "scoring_settings": {}})
    st = DraftState(meta=meta, picks=[], my_user_id="me", my_roster_id=2,
                    matched=_matched(), players_meta=fake_players(),
                    users={"u1": "Rival", "me": "Antony"})
    con = Console(width=200, file=io.StringIO())
    con.print(render(st, RosterModel.from_positions(POSITIONS), [], [], "NHFL", "dynasty"))
    assert "Rival" in con.file.getvalue()   # slot 1 (u1) is on the clock


# --- Injury tag: surface Sleeper injury status on suggestions (display only) --

def test_injury_tag_formats():
    assert injury_tag({"injury_status": "Questionable", "injury_body_part": "Shoulder"}) == "Q (Shoulder)"
    assert injury_tag({"injury_status": "Out", "injury_body_part": "Knee - ACL"}) == "OUT (Knee-ACL)"
    assert injury_tag({"injury_status": "IR", "injury_body_part": "Knee"}) == "IR (Knee)"
    assert injury_tag({"injury_status": "Doubtful"}) == "D"          # no body part
    # healthy / unknown / missing → empty
    assert injury_tag({"injury_status": "NA", "injury_body_part": "None"}) == ""
    assert injury_tag({"injury_status": ""}) == ""
    assert injury_tag({}) == ""
    assert injury_tag(None) == ""


def test_render_shows_injury_tag():
    import io

    from rich.console import Console

    from sleeper_assistant.ui import render

    players = fake_players()
    players["1001"]["injury_status"] = "Out"          # McCaffrey = rank 1 → top rec
    players["1001"]["injury_body_part"] = "Knee - ACL"
    meta = DraftMeta.from_api(_draft_obj("drafting"),
                              {"roster_positions": POSITIONS, "scoring_settings": {}})
    st = DraftState(meta=meta, picks=[], my_user_id="me", my_roster_id=4,
                    matched=_matched(), players_meta=players)
    roster = RosterModel.from_positions(POSITIONS)
    eng = Engine("dynasty", roster)
    recs = eng.recommend(st.available(), st.my_counts(), st.rounds_left(), top_n=3)

    con = Console(width=200, file=io.StringIO())
    con.print(render(st, roster, recs, [], "NHFL", "dynasty"))
    out = con.file.getvalue()
    assert "OUT" in out and "Knee-ACL" in out


# --- Mock command: drive the live loop off a raw draft id, no league rosters --

def test_roster_positions_from_draft_expands_slots():
    from sleeper_assistant.cli.draft import _roster_positions_from_draft
    draft = {"settings": {"slots_qb": 1, "slots_super_flex": 1, "slots_rb": 2,
                          "slots_flex": 1, "slots_bn": 5, "rounds": 5, "teams": 10}}
    pos = _roster_positions_from_draft(draft)
    assert pos.count("QB") == 1
    assert pos.count("RB") == 2
    assert "SUPER_FLEX" in pos and "FLEX" in pos
    assert pos.count("BN") == 5
    # non-slot settings (rounds/teams) are not positions
    assert "ROUNDS" not in pos and "TEAMS" not in pos


def test_load_mock_state_ignores_league_rosters():
    from sleeper_assistant.cli.draft import _load_mock_state
    draft = _draft_obj("drafting")  # draft_order maps user "me" -> slot 3
    picks = [{"player_id": "1001", "pick_no": 1, "picked_by": "opp", "metadata": {"position": "RB"}}]
    # get_rosters would return a roster owning 1002 — a mock must NOT consult it.
    client = _FakeClient(draft=draft, rosters=[{"owner_id": "me", "players": ["1002"]}], picks=picks)
    league_obj = {"roster_positions": POSITIONS, "scoring_settings": {}}

    st = _load_mock_state(client, "d1", league_obj, "me", _matched(), fake_players())

    assert client.get_rosters_calls == 0     # league rosters never fetched for a mock
    assert st.rostered_ids == set()          # nothing seeded from ownership
    ids = {m.player_id for m in st.available()}
    assert "1001" not in ids                 # drafted this mock → excluded
    assert "1002" in ids                     # on a "roster" but NOT filtered in a mock
    assert st.my_slot == 3                   # identity still comes from draft_order


# --- Bug 2: DST candidates must classify against the DEF roster slot ---------

def test_dst_classifies_against_def_slot():
    r = RosterModel.from_positions(POSITIONS)  # has one "DEF" slot
    # empty roster: a defense fills a needed starter (was NEUTRAL pre-fix)
    assert r.classify("DST", {}) == STARTER
    # DEF slot already filled → another defense is surplus
    assert r.classify("DST", {"DEF": 1}) == SURPLUS


def test_def_gets_late_need_boost_but_not_early():
    eng = Engine("redraft", RosterModel.from_positions(POSITIONS))
    # early: guardrail keeps K/DEF flat
    assert eng._multiplier("DST", {}, rounds_left=14)[0] == 1.0
    # last two rounds with an open DEF slot: the boost must actually apply
    late_mult, why = eng._multiplier("DST", {}, rounds_left=2)
    assert late_mult > 1.0
    assert why  # a non-empty "why fills DEF" reason
    # if the DEF slot is already filled, late DST is discounted, not boosted
    assert eng._multiplier("DST", {"DEF": 1}, rounds_left=2)[0] < 1.0


# --- Tier-break alerts must fire only on genuine (near-top-of-board) scarcity -

def _mr(rank, tier, pos):
    return MatchResult(
        RankedPlayer(rank=rank, tier=tier, name=f"P{rank}", position=pos, team=""),
        player_id=str(rank), method="exact",
    )


def test_tier_break_gate_limits_to_given_positions():
    # The primitive: only positions in the gate set are eligible to fire.
    eng = Engine("dynasty", RosterModel.from_positions(POSITIONS))
    available = [
        _mr(1, 2, "RB"), _mr(2, 3, "RB"),
        _mr(3, 6, "QB"), _mr(4, 7, "QB"),
    ]
    texts = " ".join(a.text for a in eng._tier_break_alerts(available, rec_positions={"RB"}))
    assert "RB" in texts and "Tier 2" in texts
    assert "QB" not in texts


def test_tier_break_skips_top_pick_position():
    # You're taking your #1 anyway, so its position isn't scarcity — the useful
    # warning is the opportunity cost at the *other* recommended positions.
    eng = Engine("dynasty", RosterModel.from_positions(POSITIONS))
    available = [
        _mr(1, 4, "RB"),   # best available → your #1 pick, lone in its tier
        _mr(2, 6, "RB"),
        _mr(3, 5, "WR"),   # a WR you'd pass on, lone in tier 5 → the real cliff
        _mr(4, 6, "WR"),
    ]
    recs = eng.recommend(available, {}, rounds_left=10, top_n=3)  # top pick is the RB
    texts = " ".join(a.text for a in eng.alerts(available, recent_positions=[], recs=recs))
    assert "WR" in texts        # opportunity cost at a non-top position → fires
    assert "RB" not in texts    # your #1 pick's own position → suppressed


def test_tier_break_no_filter_when_recs_omitted():
    # Backwards-compatible default: with no recs, a genuine lone-in-tier fires.
    eng = Engine("dynasty", RosterModel.from_positions(POSITIONS))
    available = [_mr(1, 2, "RB"), _mr(2, 3, "RB")]
    texts = " ".join(a.text for a in eng.alerts(available, recent_positions=[]))
    assert "RB" in texts


# --- Bug 3: get_players falls back to a stale cache on a network error -------

def test_get_players_uses_stale_cache_on_network_error(tmp_path):
    client = SleeperClient(cache_dir=tmp_path)
    cache = tmp_path / "players_nfl.json"
    cache.write_text('{"1": {"full_name": "Cached Player"}}')
    stale = time.time() - 48 * 60 * 60  # older than the 24h TTL
    os.utime(cache, (stale, stale))

    def boom(path):  # simulate a network/HTTP failure (raises, returns nothing)
        raise SleeperError("network down")

    client._get = boom
    data = client.get_players()
    assert data == {"1": {"full_name": "Cached Player"}}


def test_get_players_raises_when_no_cache(tmp_path):
    client = SleeperClient(cache_dir=tmp_path)

    def boom(path):
        raise SleeperError("network down")

    client._get = boom
    try:
        client.get_players()
    except SleeperError:
        pass
    else:
        raise AssertionError("expected SleeperError with no cache to fall back to")


# --- Bug 4: every ALIASES key must be reachable (already normalized) ---------

def test_alias_keys_are_reachable():
    # The lookup is ALIASES.get(normalize_name(name)); a key that isn't already
    # in normalized form can never be hit.
    for key in ALIASES:
        assert normalize_name(key) == key, f"unreachable alias key: {key!r}"


def test_gabe_davis_alias_still_resolves():
    by_name = {r.ranked.name: r for r in _matched()}
    davis = by_name["Gabe Davis"]
    assert davis.player_id == "1014"
    assert davis.method == "alias"
