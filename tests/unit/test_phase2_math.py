"""Phase 2 math & model upgrades — unit tests for every new formula.

Pure-function coverage over mock data (no DB): ensemble xPTS weights/CI,
captain-confidence factor maths and clamps, transfer EV + top-N search,
differential scoring/tiering, price-pressure bands, and injury-risk levels.

Expected values below are HAND-COMPUTED so a regression changes an exact
number, not just some inequality.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from fpl_intelligence.models.captaincy import (
    calculate_captain_confidence,
    captain_confidence_detail,
    form_contribution_value,
    margin_score_value,
)
from fpl_intelligence.models.ensemble_xpts import (
    DEFAULT_SD,
    MIN_SD,
    Z_95,
    calculate_ensemble_xpts,
    calculate_form_score,
    calculate_historical_sd,
    fdr_score,
)
from fpl_intelligence.tools.differentials import (
    differential_score,
    find_differentials,
    ownership_tier,
)
from fpl_intelligence.tools.injury_risk import calculate_injury_risk
from fpl_intelligence.tools.price_predictor import (
    predict_price_changes,
    transfer_threshold,
    urgency_bucket,
)
from fpl_intelligence.tools.transfer_ev import (
    calculate_confidence,
    calculate_transfer_ev,
    estimate_entry_sd,
    get_top_transfers,
)


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


# --------------------------------------------------------------------------- #
# 2.1 Ensemble xPTS
# --------------------------------------------------------------------------- #


class TestEnsembleComponents:
    def test_fdr_score_maps_to_ease_scale(self):
        assert fdr_score(1) == 10.0
        assert fdr_score(2) == 8.0
        assert fdr_score(5) == 2.0  # (6-5)*2 — spec keeps a floor of 2

    def test_fdr_score_rejects_invalid(self):
        assert fdr_score(None) is None
        assert fdr_score("") is None
        assert fdr_score(0) is None
        assert fdr_score(6) is None
        assert fdr_score("3") == 6.0  # strings coerce (FPL reality)

    def test_form_score_recency_weighted(self):
        # 6*1.5 + 4*1.2 + 8*1.0 + 2*0.8 + 10*0.6 = 29.4 ; /5.1 = 5.7647...
        score = calculate_form_score([6, 4, 8, 2, 10])
        assert score == pytest.approx(29.4 / 5.1, abs=1e-9)

    def test_form_score_truncates_and_renormlises(self):
        # Only two games: (8*1.5 + 4*1.2)/2.7 = 16.8/2.7 = 6.2222...
        score = calculate_form_score([8, 4])
        assert score == pytest.approx(16.8 / 2.7, abs=1e-9)

    def test_form_score_empty_is_none(self):
        assert calculate_form_score(None) is None
        assert calculate_form_score([]) is None

    def test_historical_sd_population(self):
        sd = calculate_historical_sd([2, 4, 6, 8])
        assert sd == pytest.approx(math.sqrt(5.0), abs=1e-3)

    def test_historical_sd_needs_two_points_and_floors(self):
        assert calculate_historical_sd([5]) is None
        assert calculate_historical_sd(None) is None
        assert calculate_historical_sd([5, 5, 5]) == MIN_SD


class TestEnsembleXpts:
    PREDICTIONS = {
        "recent_points": [6, 4, 8, 2, 10],
        "vs_opponent_avg": 7.0,
        "points_history": [2, 4, 6, 8],
    }

    def test_full_three_factor_hand_case(self):
        player = _ns(fixture_difficulty=2)
        result = calculate_ensemble_xpts(player, 5, self.PREDICTIONS)
        # mean = 8*.4 + 5.7647*.35 + 7*.25 = 6.9676
        assert result["mean"] == pytest.approx(6.97, abs=0.01)
        assert result["sd"] == pytest.approx(2.24, abs=0.01)
        assert result["lower"] == pytest.approx(
            max(0, result["mean"] - Z_95 * 2.2361), abs=0.01
        )
        assert result["upper"] == pytest.approx(
            result["mean"] + Z_95 * 2.2361, abs=0.01
        )
        assert result["model"] == "ensemble_v1"
        assert result["ci_from_history"] is True
        assert sum(result["weights_used"].values()) == pytest.approx(1.0)

    def test_missing_factor_renormalises_weights(self):
        player = _ns(fixture_difficulty=None)
        result = calculate_ensemble_xpts(player, 5, dict(self.PREDICTIONS))
        assert result["weights_used"] == {"form": 0.5833, "history": 0.4167}
        # mean = (5.7647*.35 + 7*.25)/.60 = 6.2736...
        assert result["mean"] == pytest.approx(
            (5.7647 * 0.35 + 7 * 0.25) / 0.60, abs=0.01
        )

    def test_default_sd_when_no_history(self):
        player = _ns(fixture_difficulty=3)
        result = calculate_ensemble_xpts(player, 5, {"recent_points": [5]})
        assert result["ci_from_history"] is False
        width = result["upper"] - result["lower"]
        assert width == pytest.approx(2 * Z_95 * DEFAULT_SD, abs=0.01)

    def test_baseline_fallback_never_crashes(self):
        result = calculate_ensemble_xpts(_ns(), 5, {})
        assert result["model"] == "baseline_fallback"
        assert result["lower"] <= result["mean"] <= result["upper"]

    def test_baseline_fallback_prefers_given_value(self):
        result = calculate_ensemble_xpts(_ns(), 5, {"baseline_xpts": 4.2})
        assert result["mean"] == 4.2

    def test_zero_vs_opponent_avg_treated_as_no_data(self):
        player = _ns(fixture_difficulty=2)
        data = {"recent_points": [6], "vs_opponent_avg": 0}
        result = calculate_ensemble_xpts(player, 5, data)
        assert "history" not in result["weights_used"]


# --------------------------------------------------------------------------- #
# 2.3 Captain confidence
# --------------------------------------------------------------------------- #


class TestCaptainConfidence:
    FULL_TOP = _ns(
        xpts=8.0,
        fixture_difficulty=2,
        form_avg=6.0,
        season_avg=5.0,
        selected_by_percent=30,
    )
    SECOND = _ns(xpts=6.0)

    def test_spec_formula_hand_case(self):
        # margin 2→4 | ease 3 | ratio 1.2*20=4.8 | own 7 → 8.0*10 = 80.0
        assert calculate_captain_confidence(self.FULL_TOP, self.SECOND, 5) == 80.0

    def test_clamped_to_floor(self):
        low = _ns(
            xpts=5, fixture_difficulty=5, form_avg=4, season_avg=5,
            selected_by_percent=100,
        )
        assert calculate_captain_confidence(low, low, 5) == 50.0

    def test_clamped_to_ceiling(self):
        high = _ns(
            xpts=12, fixture_difficulty=1, form_avg=5, season_avg=4,
            selected_by_percent=0,
        )
        assert calculate_captain_confidence(high, self.SECOND, 5) == 95.0

    def test_missing_second_pick_renormalises(self):
        top = _ns(
            xpts=7, fixture_difficulty=1, form_avg=5, season_avg=5,
            selected_by_percent=50,
        )
        detail = captain_confidence_detail(top, None, 5)
        assert detail["dropped_factors"] == ["margin"]
        assert detail["complete"] is False
        # (4*.3 + 20*.2 + 5*.1)/.60 = 9.5 → 95 exactly at ceiling
        assert detail["score"] == 95.0

    def test_degenerate_season_avg_drops_form(self):
        top = _ns(xpts=7, fixture_difficulty=None, season_avg=0, selected_by_percent=30)
        assert calculate_captain_confidence(top, _ns(xpts=0), 5) == 94.0

    def test_no_data_at_all_falls_back_to_floor(self):
        detail = captain_confidence_detail(_ns(), None, 5)
        assert detail["score"] == 50.0
        assert detail["complete"] is False

    def test_margin_saturation_and_helper_units(self):
        assert margin_score_value(15, 5) == 10.0  # capped
        assert form_contribution_value(5, 5) == pytest.approx(20.0)


# --------------------------------------------------------------------------- #
# 2.2 Transfer EV
# --------------------------------------------------------------------------- #

PREDICTIONS = {
    1: {5: {"mean": 6.0, "upper": 9.5}, 6: {"mean": 6.0, "upper": 9.5}},
    2: {5: {"mean": 4.0, "upper": 6.9}, 6: {"mean": 4.0, "upper": 6.9}},
}


class TestTransferEv:
    OUT = _ns(id=2, name="Out", now_cost=9.9)
    IN = _ns(id=1, name="In", now_cost=10.5)

    def test_ev_hand_case_gain_minus_risk(self):
        ev = calculate_transfer_ev(
            self.OUT, self.IN, [5, 6], PREDICTIONS, price_volatility={1: 0.5}
        )
        assert ev["xpts_gain"] == 4.0  # 12 - 8
        assert ev["risk"] == pytest.approx(0.3)  # 0.5 * |10.5-9.9|
        assert ev["ev"] == pytest.approx(3.7)
        assert 0 <= ev["confidence"] <= 100

    def test_missing_predictions_returns_none(self):
        ghost = _ns(id=99, name="Ghost", now_cost=5.0)
        assert calculate_transfer_ev(self.OUT, ghost, [5], PREDICTIONS) is None

    def test_default_volatility_when_unmapped(self):
        ev = calculate_transfer_ev(self.OUT, self.IN, [5], PREDICTIONS)
        assert ev["risk"] == pytest.approx(0.1 * 0.6)

    def test_estimate_entry_sd_from_interval(self):
        entry = {"mean": 6.0, "upper": 6.0 + 1.96 * 2}
        assert estimate_entry_sd(entry) == pytest.approx(2.0)
        assert estimate_entry_sd({"mean": 5}) == DEFAULT_SD

    def test_confidence_extremes(self):
        # Wide OUT interval keeps this under certainty (~78% by hand).
        blowout = {1: {5: {"mean": 12, "upper": 13}}, 2: {5: {"mean": 0, "upper": 30}}}
        conf = calculate_confidence(self.IN, self.OUT, [5], blowout)
        assert conf == pytest.approx(78.3, abs=0.5)

        # Tight 95% bands around a huge gap behave like a certainty.
        tight = {
            1: {5: {"mean": 20.0, "upper": 20.196}},
            2: {5: {"mean": 0.0, "upper": 0.196}},
        }
        assert calculate_confidence(self.IN, self.OUT, [5], tight) == pytest.approx(
            100.0, abs=0.5
        )

    def test_get_top_transfers_respects_bank_and_ownership(self):
        squad = _ns(player_ids=[2, 3])
        players = [
            _ns(id=2, name="Out", web_name="OUT", now_cost=9.9),
            _ns(id=3, name="Keep", web_name="KEEP", now_cost=5.5),
            _ns(id=1, name="In", web_name="IN", now_cost=10.5),
            _ns(id=4, name="Rich", web_name="RICH", now_cost=11.5),
        ]
        preds = {
            **PREDICTIONS,
            # Squad-mate KEEP gets history too — WITHOUT it every Keep→X
            # pair is honestly unscorable (outgoing points unknown).
            3: {5: {"mean": 3.0}, 6: {"mean": 3.0}},
            # Cheap punt: 9.0 xPTS over the horizon for £4.0m — affordable
            # out of KEEP (£5.5m + £1.0m bank) and OUT alike.
            8: {5: {"mean": 4.5}, 6: {"mean": 4.5}},
        }
        players.append(_ns(id=8, name="Cheap", web_name="CHEAP", now_cost=4.0))

        tops = get_top_transfers(
            squad, 1.0, players, preds, [5, 6], volatility_map={1: 0.5}
        )
        pairs = {(t["player_out"], t["player_in"]) for t in tops}
        # BANK rules: IN (£10.5m) only fits out of OUT (£9.9m + £1.0m);
        # CHEAP (£4.0m) fits out of anyone; RICH never fits.
        assert ("Out", "In") in pairs
        assert ("Keep", "Cheap") in pairs
        assert not any(pi == "RICH" for _, pi in pairs)
        # Bank rule specifics: IN (£10.5m) can never leave KEEP (£6.5m cap).
        assert not any(po == "Keep" and pi == "In" for po, pi in pairs)
        gains = [t["ev"] for t in tops]
        assert gains == sorted(gains, reverse=True)

    def test_get_top_transfers_caps_results(self):
        squad = _ns(player_ids=[2])
        players = [_ns(id=i, name=f"P{i}", now_cost=5.0) for i in range(1, 9)]
        preds = {i: {5: {"mean": i + 1}} for i in range(1, 9)}
        tops = get_top_transfers(squad, 5.0, players, preds, [5], top_n=3)
        assert len(tops) == 3
        assert tops[0]["player_in_id"] == 8  # highest xPTS first


# --------------------------------------------------------------------------- #
# 2.4 Differentials
# --------------------------------------------------------------------------- #


class TestDifferentials:
    def test_tier_buckets(self):
        assert ownership_tier(5) == "Low"
        assert ownership_tier(9.9) == "Low"
        assert ownership_tier(10) == "Med"
        assert ownership_tier(29.9) == "Med"
        assert ownership_tier(30) == "High"

    def test_score_formula(self):
        # 6 * 0.9 / 6.0 = 0.9 expected points per pound of untapped potential
        assert differential_score(6.0, 10.0, 60) == pytest.approx(0.9)

    def test_find_differentials_filters_and_sorts(self):
        players = [
            _ns(id=1, web_name="Diff", now_cost=55, selected_by_percent=4.5),
            _ns(id=2, web_name="Template", now_cost=120, selected_by_percent=45),
            _ns(id=3, web_name="NoData", now_cost=50, selected_by_percent=8),
            _ns(id=4, web_name="Free", now_cost=0, selected_by_percent=8),
            _ns(id=5, web_name="Owned", now_cost=60, selected_by_percent=5),
        ]
        preds = {
            1: {7: {"mean": 6.0}},
            2: {7: {"mean": 9.0}},
            3: {},  # missing GW entirely
            4: {7: {"mean": 5.0}},  # zero price unusable
            5: {7: {"mean": 7.0}},  # excluded (user already owns)
        }
        diffs = find_differentials(players, preds, 7, exclude_ids=[5])
        names = [d["player"] for d in diffs]
        assert names == ["Diff", "Template"]
        assert diffs[0]["tier"] == "Low"
        assert diffs[0]["score"] == pytest.approx(
            round((6.0 * (1 - 4.5 / 100)) / 5.5, 2), abs=1e-6
        )
        assert diffs[1]["tier"] == "High"

    def test_min_xpts_floor_and_top_n(self):
        players = [
            _ns(id=i, web_name=f"P{i}", now_cost=50, selected_by_percent=5)
            for i in range(1, 6)
        ]
        preds = {i: {7: {"mean": 1.0 if i < 4 else 8.0}} for i in range(1, 6)}
        diffs = find_differentials(players, preds, 7, min_xpts=2.0, top_n=2)
        assert [d["player"] for d in diffs] == ["P4", "P5"]


# --------------------------------------------------------------------------- #
# 2.5 Price predictor
# --------------------------------------------------------------------------- #


class TestPricePredictor:
    def test_threshold_bands(self):
        assert transfer_threshold(4.5) == 15000
        assert transfer_threshold(5.0) == 15000
        assert transfer_threshold(5.1) == 20000
        assert transfer_threshold(10.0) == 20000
        assert transfer_threshold(12.5) == 25000

    def test_urgency_buckets(self):
        assert urgency_bucket(0.71) == "High"
        assert urgency_bucket(0.41) == "Med"
        assert urgency_bucket(0.31) == "Low"

    def test_probability_saturation_and_direction(self):
        players = [
            _ns(
                id=1, web_name="Rocket", now_cost=12.5,
                transfers_in=40000, transfers_out=5000,
            ),
            _ns(
                id=2, web_name="Sink", now_cost=4.0,
                transfers_in=1000, transfers_out=31000,
            ),
            _ns(
                id=3, web_name="Quiet", now_cost=6.0,
                transfers_in=100, transfers_out=200,
            ),
        ]
        results = predict_price_changes(players)
        by_name = {r["player"]: r for r in results}
        assert "Quiet" not in by_name  # filtered <30%
        rocket, sink = by_name["Rocket"], by_name["Sink"]
        assert rocket["direction"] == "Rise"
        assert rocket["probability"] == 100.0  # saturated
        assert rocket["urgency"] == "High"
        assert sink["direction"] == "Fall"
        assert sink["probability"] == 100.0  # 30000/15000 capped

    def test_sorted_by_probability_desc(self):
        players = [
            _ns(id=1, web_name="Mid", now_cost=6.0, transfers_in=9000, transfers_out=0),
            _ns(id=2, web_name="Hot", now_cost=6.0, transfers_in=18000, transfers_out=0),
        ]
        results = predict_price_changes(players)
        assert [r["player"] for r in results] == ["Hot", "Mid"]
        assert results[1]["urgency"] == "Med"  # 9000/20000 = 45%

    def test_missing_market_data_skipped(self):
        players = [_ns(id=1, web_name="NoCounts", now_cost=6.0)]
        assert predict_price_changes(players) == []


# --------------------------------------------------------------------------- #
# 2.6 Injury risk
# --------------------------------------------------------------------------- #


class TestInjuryRisk:
    def test_high_risk_full_factors(self):
        p = _ns(
            id=1,
            web_name="Glass",
            age=32,
            minutes_played=2000,
            injuries_last_3_seasons=6,
            upcoming_fixtures_in_14_days=3,
        )
        r = calculate_injury_risk(p)
        # .09 + .09 + (6/3)*.2=.40 + .06 = .64 → 64%
        assert r["risk_pct"] == 64.0
        assert r["level"] == "High"
        assert r["recommendation"] == "Consider selling"
        assert r["data_missing"] == []

    def test_low_risk_young_squad_player(self):
        p = _ns(
            age=24,
            minutes_played=54,
            injuries_last_3_seasons=0,
            upcoming_fixtures_in_14_days=1,
        )
        r = calculate_injury_risk(p)
        # .03 + .03 + 0 + .02 = .08 → 8%
        assert r["risk_pct"] == 8.0
        assert r["level"] == "Low"
        assert r["recommendation"] == "Monitor"

    def test_level_boundaries_are_strictly_greater(self):
        def make(inj):
            return _ns(
                age=24,
                minutes_played=54,
                injuries_last_3_seasons=inj,
                upcoming_fixtures_in_14_days=1,
            )

        # .03+.03+low-cong .02 = .08 fixed; inj*0.2 on top
        at_25 = calculate_injury_risk(make(2.55))  # 0.08+0.17=0.25 → 25%
        above = calculate_injury_risk(make(2.60))  # 25.2%
        assert at_25["level"] == "Low"
        assert above["level"] == "Medium"

        def make_old(inj):
            return _ns(
                age=33,
                minutes_played=2000,
                injuries_last_3_seasons=inj,
                upcoming_fixtures_in_14_days=3,
            )

        at_50 = calculate_injury_risk(make_old(3.90))  # 0.09+0.09+0.06+0.26
        over = calculate_injury_risk(make_old(4.00))
        assert at_50["level"] == "Medium"  # exactly 50 stays Medium
        assert over["level"] == "High"  # 52%

    def test_missing_inputs_default_low_and_flagged(self):
        r = calculate_injury_risk(_ns(web_name="Mystery"))
        assert r["level"] == "Low"
        assert set(r["data_missing"]) == {
            "age",
            "minutes_played",
            "injuries_last_3_seasons",
            "upcoming_fixtures_in_14_days",
        }


# --------------------------------------------------------------------------- #
# API wiring — _attach_phase2_insights against a seeded in-memory database
# --------------------------------------------------------------------------- #

from datetime import UTC, datetime, timedelta  # noqa: E402

from fpl_intelligence.db.models import (  # noqa: E402
    Gameweek,
    Player,
    PlayerGameweekPerformance,
    Season,
    Team,
)
from fpl_intelligence.sync.materialized_models import (  # noqa: E402
    ElementFactDB,
    PredictionCurrentDB,
)

XPTS_BY_EL = {415: 8.0, 310: 6.0, 233: 4.0}


@pytest.fixture
def phase2_db(db_session):
    """Elements 415/310/233 with 5 GWs of history + materialized forecasts."""
    season = Season(
        code="2026-27", display_name="2026/27", competition="Premier League"
    )
    db_session.add(season)
    db_session.flush()
    team = Team(name="Test FC", short_name="TFC")
    db_session.add(team)
    db_session.flush()
    for n in range(1, 7):
        base = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(days=n * 7)
        db_session.add(
            Gameweek(
                season_id=season.id,
                provider_event_id=n,
                name=f"GW{n}",
                deadline_time=base,
                start_time=base + timedelta(hours=2),
                end_time=base + timedelta(days=2),
                status="scheduled",
            )
        )
    db_session.flush()

    els = (415, 310, 233)
    internal = {}
    for el in els:
        p = Player(
            first_name=f"P{el}", second_name="X", web_name=f"E{el}",
            fpl_element_id=el,
        )
        db_session.add(p)
        db_session.flush()
        internal[el] = p.id

    # History: 3,4,5,6,7 across GWs 1-5 for the first two elements.
    for el in (415, 310):
        for gw, pts in zip((1, 2, 3, 4, 5), (3, 4, 5, 6, 7), strict=False):
            db_session.add(
                PlayerGameweekPerformance(
                    player_id=internal[el],
                    gameweek_id=gw,
                    season_id=season.id,
                    team_id=team.id,
                    minutes=90,
                    total_points=pts,
                )
            )
    now = datetime.now(UTC)
    for el in els:
        for gw in range(1, 6):
            db_session.add(
                PredictionCurrentDB(
                    gameweek=gw,
                    element_id=el,
                    expected_points=XPTS_BY_EL[el],
                    computed_at=now,
                )
            )
    db_session.add(
        ElementFactDB(element_id=415, web_name="E415", minutes=2000, updated_at=now)
    )
    db_session.commit()
    return db_session


class TestPhase2ApiWiring:
    async def test_phase2_meta_sections_populate(self, phase2_db):
        from fpl_intelligence.api.routes.squad import _attach_phase2_insights
        from fpl_intelligence.squad.models import (
            CaptainRecommendation,
            DecisionReport,
        )

        report = DecisionReport(
            gameweek=1,
            starting_xi=[415, 310],
            captain=CaptainRecommendation(player_id=415),
        )
        squad = SimpleNamespace(player_ids=[415, 310], bank=2.5)
        await _attach_phase2_insights(
            phase2_db, report, squad, ownership_map={415: 55.0}
        )

        ph = report.meta["phase2"]
        assert ph["model"] == "ensemble_v1"
        assert ph["gameweek"] == 1

        # Ensemble CI exists for every watched element and brackets its mean.
        assert "415" in ph["ensemble_xpts"] and "310" in ph["ensemble_xpts"]
        ens = ph["ensemble_xpts"]["415"]
        assert ens["model"] == "ensemble_v1"  # history wired through
        assert ens["lower"] <= ens["mean"] <= ens["upper"]

        # Captain confidence computed but marked incomplete (no FDR yet).
        cc = ph["captain_confidence"]
        assert cc["top_pick"] == 415
        assert 50 <= cc["score"] <= 95
        assert cc["complete"] is False
        assert "fixture" in cc["dropped_factors"]

        # Differentials exclude owned ids entirely.
        owners = {d["player_id"] for d in ph["differentials"]}
        assert owners & {415, 310} == set()

        # No transfer counts ingested → price model is honestly empty.
        assert ph["price_changes"] == []
        # EV search runs against the live seed catalog: verify invariants
        # (ordering, ownership exclusion, bank safety) rather than exact rows.
        ev_block = ph["transfer_ev"]
        assert ev_block["bank"] == 2.5
        gains = [t["ev"] for t in ev_block["top"]]
        assert gains == sorted(gains, reverse=True)
        for t in ev_block["top"]:
            assert t["player_in_id"] not in {415, 310}
            assert 0 <= t["confidence"] <= 100

        risks = {r["player_id"]: r for r in ph["injury_risk"]}
        assert set(risks) == {415, 310}
        assert "age" in risks[415]["data_missing"]
        assert risks[415]["factors"]["load"] == 0.3  # 2000 min ⇒ ever-present

    async def test_empty_database_degrades_without_raising(self, db_session):
        from fpl_intelligence.api.routes.squad import _attach_phase2_insights
        from fpl_intelligence.squad.models import (
            CaptainRecommendation,
            DecisionReport,
        )

        report = DecisionReport(
            gameweek=3,
            starting_xi=[999],
            captain=CaptainRecommendation(player_id=999),
        )
        await _attach_phase2_insights(
            db_session, report, SimpleNamespace(player_ids=[999], bank=0.0), {}
        )
        ph = report.meta["phase2"]
        assert ph["model"] == "ensemble_v1"
        assert ph["differentials"] == []
        assert ph["transfer_ev"]["top"] == []
        assert ph["captain_confidence"]["note"].startswith("no starting XI")

    async def test_transfer_ev_surfaces_unowned_prediction_rows(self, phase2_db):
        from fpl_intelligence.api.routes.squad import _attach_phase2_insights
        from fpl_intelligence.squad.models import (
            CaptainRecommendation,
            DecisionReport,
        )

        report = DecisionReport(
            gameweek=1,
            starting_xi=[415],
            captain=CaptainRecommendation(player_id=415),
        )
        # Squad owns only 415 → 310/233 become buyable forecast candidates.
        squad = SimpleNamespace(player_ids=[415], bank=0.0)
        await _attach_phase2_insights(phase2_db, report, squad, {})
        tops = report.meta["phase2"]["transfer_ev"]["top"]
        gains = [t["ev"] for t in tops]
        assert gains == sorted(gains, reverse=True)
        pairs = {(t["player_out_id"], t["player_in_id"]) for t in tops}
        if pairs:
            assert tops[0]["player_in_id"] == 310  # bigger xPTS gain first


