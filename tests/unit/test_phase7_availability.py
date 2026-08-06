"""Phase 7 unit tests: availability intelligence.

Tests the actual implemented Phase 7 domain model (evidence corroboration,
state derivation, adjustment factors, minutes-model wrapper, prediction
provider wrapper, DB providers, and the evaluate_phase7 framework) using
deterministic fixtures. No synthetic full-season data is used.
"""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from fpl_intelligence.availability import evidence as ev
from fpl_intelligence.availability import metrics as mtr
from fpl_intelligence.availability import state as st
from fpl_intelligence.availability.db_provider import (
    DBAvailabilityProvider,
    DBNewsProvider,
)
from fpl_intelligence.availability.evaluation import (
    Phase7EvaluationResult,
    evaluate_phase7,
)
from fpl_intelligence.availability.minutes_integration import (
    AvailabilityAwareMinutesModel,
)
from fpl_intelligence.availability.models import (
    AvailabilityArticle,
    AvailabilityEvent,
    AvailabilitySource,
    AvailabilityStatus,
    EvidenceType,
    SourceReliability,
    TrainingReport,
)
from fpl_intelligence.availability.prediction_wrapper import (
    AvailabilityAwarePredictionProvider,
)
from fpl_intelligence.availability.validation import (
    audit_availability_coverage,
    audit_temporal_availability,
)
from fpl_intelligence.optimization.provider import (
    DecisionPredictionProvider,
    PlayerPrediction,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(
    reliability: str,
    evidence_type: str,
    status: str,
    source_name: str = "src",
    published_at: datetime | None = None,
) -> dict:
    return {
        "reliability": reliability,
        "evidence_type": evidence_type,
        "status_mentioned": status,
        "published_at": published_at or datetime(2025, 8, 10, tzinfo=UTC),
        "source_name": source_name,
    }


# ---------------------------------------------------------------------------
# Evidence corroboration
# ---------------------------------------------------------------------------

class TestEvidenceCorroboration:
    def test_empty_returns_unknown(self):
        result = ev.corroborate([])
        assert result["status"] == AvailabilityStatus.UNKNOWN
        assert result["confidence"] == 0.0
        assert result["evidence_count"] == 0

    def test_single_source(self):
        result = ev.corroborate([
            _item(SourceReliability.VERIFIED_JOURNALIST, EvidenceType.INJURY, "doubtful"),
        ])
        assert result["status"] == AvailabilityStatus.DOUBTFUL
        assert result["evidence_count"] == 1
        assert result["confidence"] > 0.0
        assert result["confidence"] <= 1.0

    def test_multiple_corroborating_sources(self):
        result = ev.corroborate([
            _item(SourceReliability.RELIABLE_JOURNALIST, EvidenceType.INJURY, "doubtful", "a"),
            _item(SourceReliability.VERIFIED_JOURNALIST, EvidenceType.INJURY, "doubtful", "b"),
        ])
        assert result["status"] == AvailabilityStatus.DOUBTFUL
        assert result["evidence_count"] == 2
        # Corroboration must increase confidence (deterministic here).
        single = ev.corroborate([
            _item(SourceReliability.RELIABLE_JOURNALIST, EvidenceType.INJURY, "doubtful", "a"),
        ])
        assert result["confidence"] > single["confidence"]

    def test_conflicting_sources_pick_most_severe(self):
        result = ev.corroborate([
            _item(SourceReliability.VERIFIED_JOURNALIST, EvidenceType.FITNESS, "start", "a"),
            _item(SourceReliability.VERIFIED_JOURNALIST, EvidenceType.INJURY, "out", "b"),
        ])
        # OUT ranks above START in severity ordering.
        assert result["status"] == AvailabilityStatus.OUT

    def test_official_source_boost(self):
        base = ev.corroborate([
            _item(SourceReliability.RELIABLE_JOURNALIST, EvidenceType.INJURY, "doubtful", "a"),
        ])
        boosted = ev.corroborate([
            _item(SourceReliability.RELIABLE_JOURNALIST, EvidenceType.INJURY, "doubtful", "a"),
            _item(SourceReliability.OFFICIAL, EvidenceType.INJURY, "doubtful", "club"),
        ])
        assert boosted["confidence"] >= base["confidence"]

    def test_diminishing_returns(self):
        # confidence(1) < confidence(1+2) < confidence(1+2+3)
        c1 = ev.corroborate([
            _item(SourceReliability.UNVERIFIED, EvidenceType.INJURY, "doubtful", "a"),
        ])["confidence"]
        c2 = ev.corroborate([
            _item(SourceReliability.UNVERIFIED, EvidenceType.INJURY, "doubtful", "a"),
            _item(SourceReliability.UNVERIFIED, EvidenceType.INJURY, "doubtful", "b"),
        ])["confidence"]
        c3 = ev.corroborate([
            _item(SourceReliability.UNVERIFIED, EvidenceType.INJURY, "doubtful", "a"),
            _item(SourceReliability.UNVERIFIED, EvidenceType.INJURY, "doubtful", "b"),
            _item(SourceReliability.UNVERIFIED, EvidenceType.INJURY, "doubtful", "c"),
        ])["confidence"]
        # Marginal gain shrinks: c2-c1 > c3-c2 (diminishing returns).
        assert (c2 - c1) > (c3 - c2)
        assert c3 <= 1.0

    def test_low_confidence_evidence(self):
        result = ev.corroborate([
            _item(SourceReliability.UNVERIFIED, EvidenceType.TRANSFER_NEWS, "doubtful", "a"),
        ])
        assert result["confidence"] < 0.5

    def test_duplicate_evidence(self):
        dup = ev.corroborate([
            _item(SourceReliability.VERIFIED_JOURNALIST, EvidenceType.INJURY, "doubtful", "a"),
            _item(SourceReliability.VERIFIED_JOURNALIST, EvidenceType.INJURY, "doubtful", "a"),
        ])
        assert dup["evidence_count"] == 2
        assert dup["sources"] == ["a"]  # deduplicated source names


# ---------------------------------------------------------------------------
# Availability state derivation
# ---------------------------------------------------------------------------

class TestStateDerivation:
    def test_all_statuses_known(self):
        for status in AvailabilityStatus:
            prob = st.status_start_probability(status)
            factor = st.status_minutes_factor(status)
            assert 0.0 <= prob <= 1.0
            assert 0.0 <= factor <= 1.0

    def test_unknown_missing_state(self):
        # Unknown default when no mapping.
        assert st.status_start_probability("not_a_status") == 0.50
        assert st.status_minutes_factor("not_a_status") == 0.65

    def test_state_to_adjustment(self):
        adj = st.state_to_adjustment(AvailabilityStatus.OUT, 0.9)
        assert adj["start_probability"] == 0.0
        assert adj["minutes_factor"] == 0.0
        assert adj["confidence"] == 0.9

    def test_state_to_adjustment_start(self):
        adj = st.state_to_adjustment(AvailabilityStatus.START, 1.0)
        assert adj["start_probability"] == 0.95
        assert adj["confidence"] == 1.0

    def test_get_state_with_confidence_no_event(self, db_session):
        status, conf, sources = st.get_state_with_confidence(db_session, 1, 1, 1)
        assert status == AvailabilityStatus.UNKNOWN
        assert conf == 0.0
        assert sources == []

    def test_get_current_state_no_event(self, db_session):
        assert st.get_current_state(db_session, 1, 1, 1) == AvailabilityStatus.UNKNOWN


# ---------------------------------------------------------------------------
# AvailabilityAwareMinutesModel
# ---------------------------------------------------------------------------

class _FakeMinutesModel:
    model_name = "fake_minutes"
    model_version = "1.0"

    def metadata(self):
        return {"model_name": self.model_name, "model_version": self.model_version}

    def predict(self, X, context=None):
        return [{
            "expected_minutes": 60.0,
            "probability_starting": 0.8,
            "probability_30_plus": 0.85,
            "probability_60_plus": 0.8,
            "data_completeness": 0.9,
            "method": "fake",
        }]


class _FakeAvailabilityProvider:
    def __init__(self, status, confidence):
        self._status = status
        self._confidence = confidence

    def get_availability(self, player_id, game_time):
        return self._status, self._confidence, ["src"]


class TestAvailabilityAwareMinutesModel:
    def test_confidence_zero_passthrough(self):
        base = _FakeMinutesModel()
        wrapper = AvailabilityAwareMinutesModel(base, _FakeAvailabilityProvider("doubtful", 0.0))
        preds = wrapper.predict([0], {"player_id": 1, "gameweek": 1})
        assert preds[0]["expected_minutes"] == 60.0
        assert preds[0]["probability_starting"] == 0.8
        assert "no_evidence" in preds[0]["method"]

    def test_confidence_one_full_adjustment(self):
        base = _FakeMinutesModel()
        wrapper = AvailabilityAwareMinutesModel(base, _FakeAvailabilityProvider("out", 1.0))
        preds = wrapper.predict([0], {"player_id": 1, "gameweek": 1})
        # OUT: avail_start=0.0, avail_minutes=0.0
        assert preds[0]["probability_starting"] == 0.0
        assert preds[0]["expected_minutes"] == 0.0

    def test_confidence_partial_blend(self):
        base = _FakeMinutesModel()
        wrapper = AvailabilityAwareMinutesModel(base, _FakeAvailabilityProvider("doubtful", 0.5))
        preds = wrapper.predict([0], {"player_id": 1, "gameweek": 1})
        # start = 0.8*0.5 + 0.25*0.5 = 0.525
        assert preds[0]["probability_starting"] == pytest.approx(0.525, abs=1e-3)
        # minutes = 60*0.5 + (0.10*60)*0.5 = 33.0
        assert preds[0]["expected_minutes"] == pytest.approx(33.0, abs=1e-3)

    def test_missing_availability_base_usable(self):
        base = _FakeMinutesModel()
        wrapper = AvailabilityAwareMinutesModel(base, _FakeAvailabilityProvider("unknown", 0.0))
        preds = wrapper.predict([0], {})
        assert preds[0]["expected_minutes"] == 60.0

    def test_contradictory_evidence_lower_confidence(self):
        base = _FakeMinutesModel()
        wrapper = AvailabilityAwareMinutesModel(base, _FakeAvailabilityProvider("suspect", 0.2))
        preds = wrapper.predict([0], {"player_id": 1, "gameweek": 1})
        assert 0.0 <= preds[0]["probability_starting"] <= 1.0

    def test_probabilities_in_bounds(self):
        base = _FakeMinutesModel()
        wrapper = AvailabilityAwareMinutesModel(
            base, _FakeAvailabilityProvider("questionable", 0.7)
        )
        preds = wrapper.predict([0], {"player_id": 1, "gameweek": 1})
        for key in ("probability_starting", "probability_30_plus", "probability_60_plus"):
            assert 0.0 <= preds[0][key] <= 1.0


# ---------------------------------------------------------------------------
# AvailabilityAwarePredictionProvider
# ---------------------------------------------------------------------------

class _FakeDecisionProvider(DecisionPredictionProvider):
    def __init__(self):
        self._pred = PlayerPrediction(
            player_id=1, gameweek=1,
            expected_points=5.0, expected_minutes=60.0, start_probability=0.8,
            distribution=np.array([0.0, 2.0, 4.0, 6.0, 8.0, 10.0]),
            floor=0.0, ceiling=10.0,
        )

    def get_player_prediction(self, player_id, gameweek):
        return self._pred

    def get_squad_predictions(self, squad_players, gws):
        return {gw: {pid: self._pred for pid in squad_players} for gw in gws}

    def get_all_predictions(self, gameweek):
        return {1: self._pred}

    def get_fixture_count(self, player_id, gameweek):
        return 1


class TestAvailabilityAwarePredictionProvider:
    def _provider(self, status, confidence):
        base = _FakeDecisionProvider()
        avail = _FakeAvailabilityProvider(status, confidence)
        return AvailabilityAwarePredictionProvider(base, avail)

    def test_no_evidence_passthrough(self):
        p = self._provider("unknown", 0.0)
        pred = p.get_player_prediction(1, 1)
        assert pred.expected_points == 5.0
        assert pred.start_probability == 0.8
        assert pred.expected_minutes == 60.0

    def test_expected_points_adjustment_confidence_one(self):
        p = self._provider("out", 1.0)
        pred = p.get_player_prediction(1, 1)
        # OUT: start 0.0 -> points_ratio 0 -> adj_points 0
        assert pred.expected_points == 0.0
        assert pred.start_probability == 0.0

    def test_expected_minutes_adjustment(self):
        p = self._provider("doubtful", 1.0)
        pred = p.get_player_prediction(1, 1)
        # minutes = 60*(1-1) + (60*0.10)*1 = 6.0
        assert pred.expected_minutes == pytest.approx(6.0, abs=1e-3)

    def test_partial_confidence(self):
        p = self._provider("doubtful", 0.5)
        pred = p.get_player_prediction(1, 1)
        # start = 0.8*0.5 + 0.25*0.5 = 0.525
        assert pred.start_probability == pytest.approx(0.525, abs=1e-3)
        assert 0.0 <= pred.expected_points <= 5.0

    def test_distribution_adjustment(self):
        p = self._provider("out", 1.0)
        pred = p.get_player_prediction(1, 1)
        assert pred.distribution is not None
        assert np.all(pred.distribution >= 0)

    def test_floor_ceiling_consistency(self):
        p = self._provider("doubtful", 0.8)
        pred = p.get_player_prediction(1, 1)
        assert pred.floor <= pred.expected_points <= pred.ceiling

    def test_distribution_quantile_order(self):
        p = self._provider("doubtful", 0.5)
        pred = p.get_player_prediction(1, 1)
        dist = pred.distribution
        p10, p25, p50, p75, p90 = np.percentile(dist, [10, 25, 50, 75, 90])
        assert p10 <= p25 <= p50 <= p75 <= p90

    def test_floor_ceiling_bounds(self):
        p = self._provider("doubtful", 0.5)
        pred = p.get_player_prediction(1, 1)
        assert pred.floor >= 0.0
        assert pred.ceiling >= pred.expected_points


# ---------------------------------------------------------------------------
# DB providers
# ---------------------------------------------------------------------------

class TestDBProviders:
    def _seed_basic(self, db_session):
        """Create the minimal supporting rows (season/team/gameweek/player)."""
        from fpl_intelligence.db.models import Gameweek, Player, Season, Team
        season = Season(code="2025-26", display_name="2025/26")
        db_session.add(season)
        db_session.flush()
        team = Team(name="Arsenal", short_name="ARS")
        db_session.add(team)
        db_session.flush()
        gw = Gameweek(season_id=season.id, provider_event_id=1, name="GW1",
                      deadline_time=datetime(2025, 8, 1, tzinfo=UTC))
        db_session.add(gw)
        db_session.flush()
        player = Player(first_name="A", second_name="B", web_name="AB", position_code=4)
        db_session.add(player)
        db_session.flush()
        db_session.commit()
        return season.id, gw.id, player.id

    def test_db_availability_provider_no_event(self, db_session):
        self._seed_basic(db_session)
        provider = DBAvailabilityProvider(db_session)
        status, conf, srcs = provider.get_availability(1, datetime(2025, 8, 15, tzinfo=UTC))
        assert status == AvailabilityStatus.UNKNOWN
        assert conf == 0.0
        assert srcs == []

    def test_db_availability_provider_cutoff_filtering(self, db_session):
        sid, gwid, pid = self._seed_basic(db_session)
        source = AvailabilitySource(name="official", reliability="official")
        db_session.add(source)
        db_session.flush()
        db_session.add(AvailabilityEvent(
            player_id=pid, season_id=sid, gameweek_id=gwid,
            status="out", confidence=0.9, evidence_count=1,
            primary_source_id=source.id,
            valid_from=datetime(2025, 9, 1, tzinfo=UTC),
            valid_to=None, is_current=True,
        ))
        db_session.commit()
        provider = DBAvailabilityProvider(db_session)
        # Query before the event's valid_from -> no event.
        status, conf, _ = provider.get_availability(pid, datetime(2025, 8, 15, tzinfo=UTC))
        assert status == AvailabilityStatus.UNKNOWN
        # Query after -> event applies.
        status, conf, _ = provider.get_availability(pid, datetime(2025, 9, 2, tzinfo=UTC))
        assert status == AvailabilityStatus.OUT
        assert conf == 0.9

    def test_db_news_provider_fetch_evidence(self, db_session):
        source = AvailabilitySource(name="sky", reliability="reliable_journalist")
        db_session.add(source)
        db_session.flush()
        db_session.add(AvailabilityArticle(
            source_id=source.id, url="https://x/1", headline="News",
            published_at=datetime(2025, 8, 10, tzinfo=UTC),
            content="content",
        ))
        db_session.commit()
        provider = DBNewsProvider(db_session)
        articles = provider.fetch_evidence()
        assert len(articles) == 1
        assert articles[0]["source_name"] == "sky"

    def test_db_news_provider_since_filter(self, db_session):
        source = AvailabilitySource(name="sky", reliability="reliable_journalist")
        db_session.add(source)
        db_session.flush()
        db_session.add(AvailabilityArticle(
            source_id=source.id, url="https://x/1", headline="News",
            published_at=datetime(2025, 8, 10, tzinfo=UTC),
        ))
        db_session.commit()
        provider = DBNewsProvider(db_session)
        # since after publish date -> no articles
        assert provider.fetch_evidence(since=datetime(2025, 8, 11, tzinfo=UTC)) == []
        # since before publish date -> 1 article
        assert len(provider.fetch_evidence(since=datetime(2025, 8, 1, tzinfo=UTC))) == 1

    def test_training_limited(self, db_session):
        _, _, pid = self._seed_basic(db_session)
        db_session.add(TrainingReport(
            player_id=pid, session_at=datetime(2025, 8, 10, tzinfo=UTC),
            participated=True, limited=True, training_load=0.5,
        ))
        db_session.commit()
        provider = DBAvailabilityProvider(db_session)
        limited, load = provider.is_training_limited(pid, datetime(2025, 8, 11, tzinfo=UTC))
        assert limited is True
        assert load == 0.5


# ---------------------------------------------------------------------------
# Phase 7 evaluation
# ---------------------------------------------------------------------------

class TestPhase7Evaluation:
    def test_evaluate_phase7_requires_db(self):
        with pytest.raises(RuntimeError):
            evaluate_phase7(None, None, None, "2024-25")

    def test_result_dataclass_fields(self):
        result = Phase7EvaluationResult(
            season="2024-25", baseline_total_points=100.0, phase7_total_points=110.0,
            baseline_gw_average=2.6, phase7_gw_average=2.9,
            baseline_transfers=10, phase7_transfers=12, transfer_delta=2,
            captain_delta=1.5, start_prob_accuracy=0.0, minutes_mae=12.0,
            points_mae=1.2, roi_delta=10.0, improvement_pct=10.0,
        )
        d = result.to_dict()
        assert d["season"] == "2024-25"
        assert d["roi_delta"] == 10.0
        assert d["improvement_pct"] == 10.0
        assert "details" in d

    def test_evaluate_phase7_miniature_fixture(self, db_session):
        """Run evaluate_phase7 on a deterministic miniature DB fixture.

        Two providers (baseline and phase7) return slightly different
        predictions so the results are distinguishable. The DecisionBacktester
        uses the populated db_session; cutoffs are respected by the backtester.
        """
        from fpl_intelligence.db.models import (
            Gameweek,
            Player,
            PlayerGameweekPerformance,
            Season,
            Team,
        )

        season = Season(code="2024-25", display_name="2024/25")
        db_session.add(season)
        db_session.flush()
        sid = season.id
        team = Team(name="Arsenal", short_name="ARS")
        db_session.add(team)
        db_session.flush()
        team_id = team.id
        gw = Gameweek(season_id=sid, provider_event_id=1, name="GW1",
                      deadline_time=datetime(2024, 8, 1, tzinfo=UTC))
        db_session.add(gw)
        db_session.flush()
        gwid = gw.id

        players = []
        for i in range(4):
            pl = Player(first_name=f"F{i}", second_name=f"S{i}", web_name=f"P{i}", position_code=4)
            db_session.add(pl)
            db_session.flush()
            players.append(pl)
        db_session.add_all([
            PlayerGameweekPerformance(
                player_id=p.id, gameweek_id=gwid, season_id=sid, team_id=team_id,
                minutes=90, total_points=i + 1,
                ingested_at=datetime(2024, 8, 1, tzinfo=UTC),
                available_at=datetime(2024, 8, 1, tzinfo=UTC),
            ) for i, p in enumerate(players)
        ])
        db_session.commit()
        pids = [p.id for p in players]

        class MiniProvider(DecisionPredictionProvider):
            def __init__(self, captain_idx):
                # captain_idx selects which player gets the highest EV and is
                # therefore chosen as captain by the backtester.
                self._captain_idx = captain_idx

            def _pred(self, pid, gw):
                idx = pids.index(pid)
                # Give the chosen captain the highest expected points so the
                # backtester selects it as captain (different actual points).
                pts = 10.0 if idx == self._captain_idx else 1.0
                return PlayerPrediction(
                    player_id=pid, gameweek=gw, expected_points=pts,
                    expected_minutes=60.0, start_probability=0.8,
                    distribution=np.array([pts, pts, pts]),
                    floor=0.0, ceiling=pts * 2,
                )

            def get_player_prediction(self, player_id, gameweek):
                return self._pred(player_id, gameweek)

            def get_squad_predictions(self, squad_players, gws):
                return {gw: {pid: self._pred(pid, gw) for pid in squad_players} for gw in gws}

            def get_all_predictions(self, gameweek):
                return {pid: self._pred(pid, gameweek) for pid in pids}

            def get_fixture_count(self, player_id, gameweek):
                return 1

        # Baseline wrongly captains player 3 (actual points = 3); Phase 7
        # correctly captains player 4 (actual points = 4). The actual backtest
        # scores therefore differ.
        baseline = MiniProvider(captain_idx=2)
        phase7 = MiniProvider(captain_idx=3)

        result = evaluate_phase7(db_session, baseline, phase7, "2024-25")
        assert isinstance(result, Phase7EvaluationResult)
        assert result.season == "2024-25"
        # With only 1 gameweek present, both backtests run on GW1.
        assert result.baseline_total_points >= 0
        assert result.phase7_total_points >= 0
        # Results must be genuinely distinguishable (different captain -> different
        # actual points credited, not merely different predicted EV).
        assert result.phase7_total_points != result.baseline_total_points
        assert result.captain_delta != 0.0


# ---------------------------------------------------------------------------
# Real evaluation metrics
# ---------------------------------------------------------------------------

class TestAvailabilityMetrics:
    def test_empty_returns_none_not_zero(self):
        """Empty sample -> all metrics None, never 0.0 placeholders."""
        m = mtr.availability_metrics([], [], [], [])
        assert m["n"] == 0
        for key in ("start_brier", "start_log_loss", "minutes_mae", "minutes_rmse",
                    "prob60_brier", "prob60_calibration_ece"):
            assert m[key] is None

    def test_perfect_predictions_zero_brier(self):
        """Perfect start predictions -> Brier 0.0 is a genuine zero."""
        m = mtr.availability_metrics([1.0, 1.0, 0.0], [1.0, 1.0, 0.0], [90, 80, 0], [90, 80, 0])
        assert m["start_brier"] == 0.0
        assert m["minutes_mae"] == 0.0
        assert m["n"] == 3

    def test_brier_positive_for_bad_predictions(self):
        m = mtr.availability_metrics([1.0, 1.0], [0.0, 0.0], [90, 90], [0, 0])
        assert m["start_brier"] == pytest.approx(1.0, abs=1e-4)
        assert m["minutes_mae"] == pytest.approx(90.0, abs=1e-4)

    def test_log_loss_finite(self):
        m = mtr.availability_metrics([0.9, 0.1], [1.0, 0.0], [70, 10], [75, 5])
        assert m["start_log_loss"] is not None
        assert m["start_log_loss"] > 0.0

    def test_minutes_rmse(self):
        m = mtr.availability_metrics([0.5, 0.5], [1.0, 0.0], [80.0, 20.0], [90.0, 10.0])
        # diff = [-10, 10] -> rmse = sqrt(100) = 10
        assert m["minutes_rmse"] == pytest.approx(10.0, abs=1e-4)

    def test_prob60_calibration_range(self):
        m = mtr.availability_metrics([0.8, 0.2, 0.6], [1.0, 0.0, 1.0], [80, 20, 60], [90, 0, 70])
        assert 0.0 <= m["prob60_calibration_ece"] <= 1.0


class TestPredictionMetrics:
    def test_empty_returns_none(self):
        p = mtr.prediction_metrics([], [])
        assert p["n"] == 0
        assert p["points_mae"] is None
        assert p["points_rmse"] is None
        assert p["spearman"] is None

    def test_mae_rmse(self):
        p = mtr.prediction_metrics([5.0, 3.0], [4.0, 6.0])
        # diffs = [1, -3] -> MAE = 2.0, RMSE = sqrt((1+9)/2) = sqrt(5) = 2.236...
        assert p["points_mae"] == pytest.approx(2.0, abs=1e-4)
        assert p["points_rmse"] == pytest.approx(2.2361, abs=1e-3)

    def test_spearman_monotone_perfect(self):
        p = mtr.prediction_metrics([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        assert p["spearman"] == pytest.approx(1.0, abs=1e-4)

    def test_spearman_inverse(self):
        p = mtr.prediction_metrics([3.0, 2.0, 1.0], [1.0, 2.0, 3.0])
        assert p["spearman"] == pytest.approx(-1.0, abs=1e-4)

    def test_spearman_none_when_constant(self):
        p = mtr.prediction_metrics([2.0, 2.0, 2.0], [1.0, 2.0, 3.0])
        assert p["spearman"] is None


# ---------------------------------------------------------------------------
# DB availability source resolution
# ---------------------------------------------------------------------------

class TestSourceResolution:
    def _seed(self, db_session):
        from fpl_intelligence.db.models import Gameweek, Player, Season, Team
        season = Season(code="2025-26", display_name="2025/26")
        db_session.add(season)
        db_session.flush()
        team = Team(name="Arsenal", short_name="ARS")
        db_session.add(team)
        db_session.flush()
        gw = Gameweek(season_id=season.id, provider_event_id=1, name="GW1",
                      deadline_time=datetime(2025, 8, 1, tzinfo=UTC))
        db_session.add(gw)
        db_session.flush()
        player = Player(first_name="A", second_name="B", web_name="AB", position_code=4)
        db_session.add(player)
        db_session.flush()
        db_session.commit()
        return season.id, gw.id, player.id

    def test_source_present(self, db_session):
        sid, gwid, pid = self._seed(db_session)
        src = AvailabilitySource(name="team_official", reliability="official")
        db_session.add(src)
        db_session.flush()
        db_session.add(AvailabilityEvent(
            player_id=pid, season_id=sid, gameweek_id=gwid, status="out",
            confidence=0.9, evidence_count=1, primary_source_id=src.id,
            valid_from=datetime(2025, 8, 10, tzinfo=UTC), is_current=True,
        ))
        db_session.commit()
        provider = DBAvailabilityProvider(db_session)
        status, conf, sources = provider.get_availability(pid, datetime(2025, 8, 15, tzinfo=UTC))
        assert status == AvailabilityStatus.OUT
        assert sources == ["team_official"]

    def test_no_source_returns_empty(self, db_session):
        sid, gwid, pid = self._seed(db_session)
        db_session.add(AvailabilityEvent(
            player_id=pid, season_id=sid, gameweek_id=gwid, status="doubtful",
            confidence=0.5, evidence_count=1, primary_source_id=None,
            valid_from=datetime(2025, 8, 10, tzinfo=UTC), is_current=True,
        ))
        db_session.commit()
        provider = DBAvailabilityProvider(db_session)
        status, conf, sources = provider.get_availability(pid, datetime(2025, 8, 15, tzinfo=UTC))
        assert status == AvailabilityStatus.DOUBTFUL
        assert sources == []

    def test_historical_source_filtering(self, db_session):
        """A future event (valid_from after cutoff) must not be returned."""
        sid, gwid, pid = self._seed(db_session)
        src = AvailabilitySource(name="sky_sports", reliability="reliable_journalist")
        db_session.add(src)
        db_session.flush()
        db_session.add(AvailabilityEvent(
            player_id=pid, season_id=sid, gameweek_id=gwid, status="out",
            confidence=0.9, evidence_count=1, primary_source_id=src.id,
            valid_from=datetime(2025, 9, 1, tzinfo=UTC), is_current=True,
        ))
        db_session.commit()
        provider = DBAvailabilityProvider(db_session)
        # Query before valid_from -> no event, no source.
        status, conf, sources = provider.get_availability(pid, datetime(2025, 8, 15, tzinfo=UTC))
        assert status == AvailabilityStatus.UNKNOWN
        assert sources == []

    def test_multiple_sources_primary_only(self, db_session):
        """Schema exposes only the event's primary source; secondary provenance
        is not reconstructable from the persisted schema (documented limit)."""
        sid, gwid, pid = self._seed(db_session)
        primary = AvailabilitySource(name="club_official", reliability="official")
        db_session.add(primary)
        db_session.flush()
        db_session.add(AvailabilityEvent(
            player_id=pid, season_id=sid, gameweek_id=gwid, status="out",
            confidence=0.9, evidence_count=2, primary_source_id=primary.id,
            valid_from=datetime(2025, 8, 10, tzinfo=UTC), is_current=True,
        ))
        db_session.commit()
        provider = DBAvailabilityProvider(db_session)
        status, conf, sources = provider.get_availability(pid, datetime(2025, 8, 15, tzinfo=UTC))
        assert sources == ["club_official"]


# ---------------------------------------------------------------------------
# Availability validation audits
# ---------------------------------------------------------------------------

class TestValidationAudits:
    def _seed(self, db_session):
        from fpl_intelligence.db.models import Gameweek, Player, Season, Team
        season = Season(code="2024-25", display_name="2024/25")
        db_session.add(season)
        db_session.flush()
        team = Team(name="Arsenal", short_name="ARS")
        db_session.add(team)
        db_session.flush()
        gw = Gameweek(season_id=season.id, provider_event_id=1, name="GW1",
                      deadline_time=datetime(2024, 8, 1, tzinfo=UTC))
        db_session.add(gw)
        db_session.flush()
        player = Player(first_name="A", second_name="B", web_name="AB", position_code=4)
        db_session.add(player)
        db_session.flush()
        db_session.commit()
        return season.id, gw.id, player.id

    def test_coverage_empty(self, db_session):
        sid, gwid, pid = self._seed(db_session)
        report = audit_availability_coverage(db_session, ["2024-25"])
        cov = report.season_coverage.get("2024-25")
        assert cov is not None
        assert cov.availability_events == 0
        assert cov.coverage_pct == 0.0

    def test_coverage_with_event(self, db_session):
        sid, gwid, pid = self._seed(db_session)
        db_session.add(AvailabilityEvent(
            player_id=pid, season_id=sid, gameweek_id=gwid, status="out",
            confidence=0.9, evidence_count=1, primary_source_id=None,
            valid_from=datetime(2024, 8, 1, tzinfo=UTC), is_current=True,
        ))
        db_session.commit()
        report = audit_availability_coverage(db_session, ["2024-25"])
        assert report.total_events == 1

    def test_temporal_all_eligible(self, db_session):
        sid, gwid, pid = self._seed(db_session)
        db_session.add(AvailabilityEvent(
            player_id=pid, season_id=sid, gameweek_id=gwid, status="out",
            confidence=0.9, evidence_count=1, primary_source_id=None,
            valid_from=datetime(2024, 7, 1, tzinfo=UTC), is_current=True,
        ))
        db_session.commit()
        report = audit_temporal_availability(db_session)
        assert report.total_events == 1
        assert report.eligible_events == 1
        assert report.excluded_future_events == 0

    def test_temporal_future_excluded(self, db_session):
        sid, gwid, pid = self._seed(db_session)
        # Event valid_from AFTER the GW deadline -> future event, excluded.
        db_session.add(AvailabilityEvent(
            player_id=pid, season_id=sid, gameweek_id=gwid, status="out",
            confidence=0.9, evidence_count=1, primary_source_id=None,
            valid_from=datetime(2024, 9, 1, tzinfo=UTC), is_current=True,
        ))
        db_session.commit()
        report = audit_temporal_availability(db_session)
        assert report.total_events == 1
        assert report.eligible_events == 0
        assert report.excluded_future_events == 1

    def test_temporal_missing_timestamp(self, db_session):
        """Schema enforces NOT NULL on valid_from, so a missing-timestamp
        event cannot be persisted. Verify the audit reports zero missing-
        timestamp events when all persisted events carry a valid_from."""
        sid, gwid, pid = self._seed(db_session)
        db_session.add(AvailabilityEvent(
            player_id=pid, season_id=sid, gameweek_id=gwid, status="out",
            confidence=0.9, evidence_count=1, primary_source_id=None,
            valid_from=datetime(2024, 7, 1, tzinfo=UTC), is_current=True,
        ))
        db_session.commit()
        report = audit_temporal_availability(db_session)
        assert report.missing_timestamp_events == 0
        assert report.eligible_events == 1
