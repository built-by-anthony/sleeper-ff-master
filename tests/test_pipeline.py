from pathlib import Path

from conftest import fake_players

from sleeper_assistant.engine import Engine, base_value, studs
from sleeper_assistant.matching import PlayerMatcher, match_all
from sleeper_assistant.rankings import RankedPlayer, _pos_rank, load_rankings
from sleeper_assistant.roster import FLEX, STARTER, SURPLUS, RosterModel
from sleeper_assistant.state import DraftMeta, DraftState

CSV = Path(__file__).resolve().parents[1] / "data" / "sample_redraft.csv"
POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF", "BN", "BN"]


# --- rankings --------------------------------------------------------------

def test_load_rankings():
    rk = load_rankings(CSV)
    assert rk[0].name == "Christian McCaffrey"
    assert rk[0].position == "RB"
    assert rk[0].tier == 1
    # DST normalized, positions cleaned of rank suffixes
    dst = [p for p in rk if p.position == "DST"][0]
    assert dst.name == "Dallas Cowboys"
    assert all(p.position in {"QB", "RB", "WR", "TE", "K", "DST"} for p in rk)


def test_pos_rank_tolerates_separators_between_letters_and_digits():
    # FantasyPros sheets vary in whether the POS cell's depth suffix is bare
    # ("RB14"), space-separated ("RB 14"), or hyphenated ("RB-14").
    assert _pos_rank("RB14") == 14
    assert _pos_rank("RB 14") == 14
    assert _pos_rank("RB-14") == 14
    assert _pos_rank("DST") is None


# --- matching --------------------------------------------------------------

def test_matching_covers_everyone():
    rk = load_rankings(CSV)
    matcher = PlayerMatcher(fake_players())
    results = match_all(rk, matcher)
    unmatched = [r for r in results if r.player_id is None]
    assert not unmatched, [r.ranked.name for r in unmatched]


def test_matching_handles_suffix_and_alias():
    rk = load_rankings(CSV)
    matcher = PlayerMatcher(fake_players())
    by_name = {r.ranked.name: r for r in match_all(rk, matcher)}
    assert by_name["Michael Pittman Jr."].player_id == "1011"   # suffix stripped
    assert by_name["Kenneth Walker III"].player_id == "1012"
    assert by_name["Gabe Davis"].player_id == "1014"            # alias
    assert by_name["Dallas Cowboys"].player_id == "DAL"         # team def


def test_position_override_finds_player_listed_under_different_position():
    # FantasyPros ranks Max Bredeson as an RB; Sleeper lists him at TE.
    # matching.POSITION_OVERRIDES should redirect the lookup to TE.
    rp = RankedPlayer(rank=200, tier=17, name="Max Bredeson", position="RB", team="FA")
    matcher = PlayerMatcher(fake_players())
    result = matcher.match(rp)
    assert result.player_id == "1016"
    assert result.method == "exact"


def test_team_abbr_alias_resolves_jac_to_jax_defense():
    # FantasyPros spells Jacksonville's defense "JAC"; Sleeper keys it "JAX".
    # matching._TEAM_ABBR_ALIASES should bridge that.
    rp = RankedPlayer(rank=150, tier=13, name="Jacksonville Jaguars", position="DST", team="JAC")
    matcher = PlayerMatcher(fake_players())
    result = matcher.match(rp)
    assert result.player_id == "JAX"
    assert result.method == "team-def"


def test_fullback_folds_into_rb_bucket_for_matching():
    # Sleeper lists fullbacks at position "FB"; CSVs (and everyone else) treat
    # them as RBs. matching._sleeper_pos() folds FB into the RB bucket.
    rp = RankedPlayer(rank=180, tier=15, name="Alec Ingold", position="RB", team="MIA")
    matcher = PlayerMatcher(fake_players())
    result = matcher.match(rp)
    assert result.player_id == "1017"
    assert result.method == "exact"


# --- roster model ----------------------------------------------------------

def test_roster_classification():
    r = RosterModel.from_positions(POSITIONS)
    assert r.direct_req == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}
    assert len(r.flex_slots) == 1
    # empty roster: RB is a needed starter
    assert r.classify("RB", {}) == STARTER
    # two RBs rostered → RB now goes to flex
    assert r.classify("RB", {"RB": 2}) == FLEX
    # flex consumed by a third RB → fourth RB is surplus
    assert r.classify("RB", {"RB": 3}) == SURPLUS


# --- engine ----------------------------------------------------------------

def _matched():
    rk = load_rankings(CSV)
    return match_all(rk, PlayerMatcher(fake_players()))


def test_base_value_monotonic():
    assert base_value(1) > base_value(2) > base_value(20)


def test_redraft_need_reorders_within_reach():
    r = RosterModel.from_positions(POSITIONS)
    eng = Engine("redraft", r)
    avail = _matched()
    # roster already has 2 WR and 1 QB; the top of the board is WR (Lamb #2).
    counts = {"WR": 2, "QB": 1}
    recs = eng.recommend(avail, counts, rounds_left=14, top_n=3)
    top = recs[0].match.ranked
    # a needed RB/TE should be boosted over a surplus WR near it
    assert top.position in {"RB", "TE"}


def test_kdef_guardrail_early_vs_late():
    r = RosterModel.from_positions(POSITIONS)
    eng = Engine("redraft", r)
    # empty K slot; early in the draft K must NOT get a need boost
    early = eng._multiplier("K", {}, rounds_left=14)
    assert early[0] == 1.0
    # in the last two rounds it may be boosted to fill the slot
    late = eng._multiplier("K", {}, rounds_left=2)
    assert late[0] > 1.0


def test_dynasty_is_bpa_when_roster_has_room():
    r = RosterModel.from_positions(POSITIONS)
    eng = Engine("dynasty", r)
    avail = _matched()
    # With an empty roster nothing is surplus, so dynasty is still pure rank order.
    recs = eng.recommend(avail, {}, rounds_left=10, top_n=3)
    assert [x.match.ranked.rank for x in recs] == [1, 2, 3]


# --- stud indicator --------------------------------------------------------

def test_studs_are_tier1_or_2_best_rank_first():
    avail = _matched()
    s = studs(avail)
    ranks = [m.ranked.rank for m in s]
    # sorted best-rank-first, and every stud is tier 1 or 2
    assert ranks == sorted(ranks)
    assert all(m.ranked.tier <= 2 for m in s)
    assert any(m.ranked.tier == 2 for m in s)  # confirms the tier-2 broadening took


def test_stud_surfaces_even_when_scored_out_of_top3():
    # A stud the engine buries (need boost pushes needed non-studs above him) still
    # appears in studs() — display-layer surfacing, engine math untouched.
    r = RosterModel.from_positions(POSITIONS)
    eng = Engine("redraft", r)
    avail = _matched()
    counts = {"RB": 2, "WR": 2}  # RB/WR now surplus, so studs get discounted
    recs = eng.recommend(avail, counts, rounds_left=14, top_n=3)
    rec_ids = {x.match.player_id for x in recs}
    stud_ids = {m.player_id for m in studs(avail)}
    # at least one stud is out of the top-3 recs yet still surfaced
    assert stud_ids - rec_ids


def test_studs_table_lists_all_ranked_with_depth():
    from rich.console import Console

    from sleeper_assistant.ui import _studs_table
    avail = _matched()
    s = studs(avail)
    assert _studs_table([], {}) is None
    group = _studs_table(s, {})
    console = Console(width=100)
    with console.capture() as cap:
        console.print(group)
    out = cap.get()
    # best-rank-first, all four present, with position depth (e.g. "RB1")
    assert "Christian McCaffrey" in out
    assert "RB1" in out
    assert "WR1" in out


# --- draft state / clock ---------------------------------------------------

def _state(picks, my_slot=3, teams=4, rounds=3, dtype="snake"):
    draft = {
        "draft_id": "d1", "type": dtype, "status": "drafting",
        "settings": {"rounds": rounds, "teams": teams},
        "slot_to_roster_id": {str(s): s for s in range(1, teams + 1)},
        "draft_order": {"me": my_slot},
    }
    league = {"roster_positions": POSITIONS, "scoring_settings": {}}
    meta = DraftMeta.from_api(draft, league)
    return DraftState(
        meta=meta, picks=picks, my_user_id="me", my_roster_id=my_slot,
        matched=_matched(), players_meta=fake_players(),
    )


def test_snake_clock_and_picks_until_me():
    st = _state(picks=[])
    assert st.my_slot == 3
    assert st.next_pick_no() == 1
    # slot 3 is the 3rd pick of round 1 → up in 2 picks
    assert st.picks_until_me() == 2
    # snake: round 2 reverses, slot 3 = pick 4+ (4 teams → picks 5,6 = slots 4,3)
    picks = [{"player_id": "1001", "pick_no": i, "metadata": {}} for i in range(1, 6)]
    st = _state(picks=picks)  # next pick_no = 6, round 2
    assert st.current_round() == 2
    assert st._slot_on_clock(6) == 3   # my slot on the clock now
    assert st.picks_until_me() == 0


def test_available_excludes_drafted():
    picks = [{"player_id": "1001", "pick_no": 1, "picked_by": "opp", "metadata": {"position": "RB"}}]
    st = _state(picks=picks)
    avail_ids = {m.player_id for m in st.available()}
    assert "1001" not in avail_ids
    assert "1002" in avail_ids


def test_my_counts_from_picks():
    picks = [
        {"player_id": "1001", "pick_no": 1, "picked_by": "me", "roster_id": 3, "metadata": {"position": "RB"}},
        {"player_id": "1002", "pick_no": 2, "picked_by": "opp", "roster_id": 1, "metadata": {"position": "WR"}},
    ]
    st = _state(picks=picks)
    assert st.my_counts() == {"RB": 1}
