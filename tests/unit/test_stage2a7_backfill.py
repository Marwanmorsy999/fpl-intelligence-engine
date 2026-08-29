"""Stage 2A.7 tests: historical backfill provenance, idempotence, temporal safety.

All tests run against in-memory SQLite -- no production credentials required.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.backtesting.cutoff import get_all_gameweek_cutoffs
from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import (
    Fixture,
    Gameweek,
    PlayerGameweekPerformance,
    Season,
)
from fpl_intelligence.ingestion.historical import import_season
from fpl_intelligence.prediction.training import TrainingDataBuilder
from fpl_intelligence.providers.mock_historical import MockHistoricalDataProvider

SEASON = "2022-23"


@pytest.fixture
def db_session() -> Session:
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()


def _pgp_count(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(PlayerGameweekPerformance)) or 0)


# ---------------------------------------------------------------------------
# Provider provenance (Steps 4-6)
# ---------------------------------------------------------------------------


class TestImportProvenance:
    def test_performance_rows_stamped_with_gameweek_end_reference(
        self, db_session: Session
    ) -> None:
        provider = MockHistoricalDataProvider("mock_prov")
        import_season(db_session, provider, SEASON, dataset="all")

        assert _pgp_count(db_session) > 0
        rows = list(db_session.execute(select(PlayerGameweekPerformance)).scalars())
        for row in rows:
            assert row.available_at is not None, "outcome rows must carry a provenance stamp"
            assert row.ingested_at is not None
            # Invariant: available_at <= ingested_at (equal by convention here).
            assert row.available_at <= row.ingested_at

        # The stamp must equal the latest genuine kickoff of that gameweek's
        # fixtures (gameweek-end reference), not ingestion wall-clock time.
        gw = db_session.scalar(select(Gameweek).where(Gameweek.provider_event_id == 2))
        assert gw is not None
        expected = db_session.scalar(
            select(func.max(Fixture.kickoff_time)).where(Fixture.gameweek_id == gw.id)
        )
        stamped = db_session.scalar(
            select(PlayerGameweekPerformance.available_at).where(
                PlayerGameweekPerformance.gameweek_id == gw.id
            )
        )
        assert expected is not None
        assert stamped is not None
        assert stamped.replace(tzinfo=UTC) == expected.replace(tzinfo=UTC)

    def test_import_is_idempotent(self, db_session: Session) -> None:
        provider = MockHistoricalDataProvider("mock_prov")
        import_season(db_session, provider, SEASON, dataset="all")
        first = _pgp_count(db_session)

        report = import_season(db_session, provider, SEASON, dataset="all")
        second = _pgp_count(db_session)

        assert first > 0
        assert second == first, "re-import must not duplicate canonical rows"
        # The idempotence shortcut returns the stored run without re-processing.
        assert report.records_received == 0

    def test_unknown_season_raises(self, db_session: Session) -> None:
        provider = MockHistoricalDataProvider("mock_prov")
        with pytest.raises(ValueError, match="not found"):
            import_season(db_session, provider, "1999-00", dataset="all")


# ---------------------------------------------------------------------------
# Reconciliation (Steps 4-5)
# ---------------------------------------------------------------------------


class _StubProvider:
    """Minimal provider for reconciliation and temporal edge cases."""

    provider_name = "stub_prov"
    schema_version = "v1"

    def __init__(self) -> None:
        self.history_extra: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []

    def get_seasons(self) -> Sequence[Mapping[str, object]]:
        return [
            {
                "season_name": SEASON,
                "start_date": datetime(2022, 8, 1, tzinfo=UTC),
                "end_date": datetime(2023, 5, 31, tzinfo=UTC),
                "competition": "Premier League",
            }
        ]

    def get_teams(self, season: str) -> Sequence[Mapping[str, object]]:
        return [{"provider_team_id": "1", "name": "Arsenal", "short_name": "ARS"}]

    def get_players(self, season: str) -> Sequence[Mapping[str, object]]:
        return [
            {
                "provider_player_id": "10",
                "first_name": "Test",
                "second_name": "Player",
                "web_name": "T. Player",
                "position_code": 3,
                "team_id": "1",
            }
        ]

    def get_fixtures(self, season: str) -> Sequence[Mapping[str, object]]:
        return [
            {
                "provider_fixture_id": f"f{gw}",
                "gameweek": gw,
                "kickoff_time": (
                    datetime(2022, 8, 5, tzinfo=UTC) + timedelta(days=7 * (gw - 1))
                ).isoformat(),
                "home_team_id": "1",
                "away_team_id": "1",
                "home_score": 1,
                "away_score": 0,
                "status": "finished",
                "postponed": False,
            }
            for gw in (1, 2, 3)
        ]

    def get_fpl_history(self, season: str) -> Sequence[Mapping[str, object]]:
        rows: list[dict[str, Any]] = [self._row(10, gw, minutes=45) for gw in (1, 2, 3)]
        # Double gameweek: a second fixture row for the same player/GW.
        rows.append(self._row(10, 2, minutes=30))
        # Unknown player must be rejected by reconciliation, never attached.
        rows.extend(self.history_extra)
        return rows

    @staticmethod
    def _row(pid: int, gw: int, minutes: int) -> dict[str, Any]:
        return {
            "provider_player_id": str(pid),
            "season_name": SEASON,
            "gameweek": gw,
            "total_points": 2,
            "minutes": minutes,
            "value": 100,
            "selected": 1000,
            "transfers_in": 10,
            "transfers_out": 5,
            "price": 9.0,
            "ep_this": 3.0,
            "ep_next": 4.0,
        }

    def get_fpl_snapshots(
        self, season: str, gameweek: int | None = None
    ) -> Sequence[Mapping[str, object]]:
        return self.snapshots

    def get_player_match_stats(self, season: str, player_id: str) -> Sequence[Mapping[str, object]]:
        return []

    def get_team_match_stats(self, season: str, team_id: str) -> Sequence[Mapping[str, object]]:
        return []


class TestReconciliation:
    def test_double_gameweek_rows_are_summed(self, db_session: Session) -> None:
        import_season(db_session, _StubProvider(), SEASON, dataset="all")

        gw2 = db_session.scalar(select(Gameweek).where(Gameweek.provider_event_id == 2))
        assert gw2 is not None
        row = db_session.scalar(
            select(PlayerGameweekPerformance).where(PlayerGameweekPerformance.gameweek_id == gw2.id)
        )
        assert row is not None
        assert row.minutes == 75, "additive fields must sum across double-GW fixture rows"

    def test_unknown_player_rejected_not_silently_attached(self, db_session: Session) -> None:
        provider = _StubProvider()
        provider.history_extra.append(_StubProvider._row(9999, 1, minutes=90))
        report = import_season(db_session, provider, SEASON, dataset="all")

        assert report.records_rejected >= 1
        assert any("unmatched_player" in w.category for w in report.warnings)
        gw1_ids = select(Gameweek.id).where(Gameweek.provider_event_id == 1)
        orphan = db_session.scalar(
            select(PlayerGameweekPerformance).where(
                PlayerGameweekPerformance.minutes == 90,
                PlayerGameweekPerformance.gameweek_id.in_(gw1_ids),
            )
        )
        assert orphan is None, "unresolvable identity must be quarantined, not persisted"

    def test_snapshot_without_event_time_is_not_fabricated(self, db_session: Session) -> None:
        provider = _StubProvider()
        provider.snapshots.append(
            {
                "provider_player_id": "10",
                "gameweek": 1,
                "event_time": None,  # no genuine timestamp available
                "price": 9.5,
            }
        )
        import_season(db_session, provider, SEASON, dataset="all")

        from fpl_intelligence.db.models import FPLSnapshot

        assert int(db_session.scalar(select(func.count()).select_from(FPLSnapshot)) or 0) == 0, (
            "rows without genuine event_time must be dropped, not stamped now()"
        )


# ---------------------------------------------------------------------------
# Temporal safety (Steps 6-8, 14)
# ---------------------------------------------------------------------------


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class TestCutoffFallback:
    def test_cutoffs_derive_from_previous_gw_kickoffs_when_deadlines_missing(
        self, db_session: Session
    ) -> None:
        provider = MockHistoricalDataProvider("mock_prov")
        import_season(db_session, provider, SEASON, dataset="all")

        cutoffs = get_all_gameweek_cutoffs(db_session, SEASON)
        by_gw = {c.gameweek: c for c in cutoffs}

        # GW1 has no previous gameweek in the database -> skipped (fail-closed).
        assert 1 not in by_gw
        # GW2's cutoff = latest genuine kickoff of GW1 fixtures.
        gw1_max = db_session.scalar(
            select(func.max(Fixture.kickoff_time)).where(
                Fixture.gameweek_id.in_(select(Gameweek.id).where(Gameweek.provider_event_id == 1))
            )
        )
        assert gw1_max is not None
        assert by_gw[2].cutoff_time == _utc(gw1_max) - timedelta(hours=1)
        # Cutoffs must be chronologically ordered.
        ordered = [c.cutoff_time for c in cutoffs]
        assert ordered == sorted(ordered)

    def test_deadline_based_cutoffs_unchanged(self, db_session: Session) -> None:
        """When genuine deadlines exist, behavior is identical to before."""
        season = Season(code="2025-26", display_name="2025/26")
        db_session.add(season)
        db_session.flush()
        deadlines = (
            (1, datetime(2025, 8, 12, 18, tzinfo=UTC)),
            (2, datetime(2025, 8, 19, 18, tzinfo=UTC)),
        )
        for gw_num, deadline in deadlines:
            db_session.add(
                Gameweek(
                    season_id=season.id,
                    provider_event_id=gw_num,
                    name=f"GW{gw_num}",
                    deadline_time=deadline,
                )
            )
        db_session.commit()

        cutoffs = get_all_gameweek_cutoffs(db_session, "2025-26")
        assert [c.gameweek for c in cutoffs] == [1, 2]
        assert cutoffs[0].cutoff_time == datetime(2025, 8, 12, 17, tzinfo=UTC)
        assert cutoffs[0].deadline_time == datetime(2025, 8, 12, 18, tzinfo=UTC)

    def test_next_gameweek_fallback_without_deadlines(self, db_session: Session) -> None:
        provider = MockHistoricalDataProvider("mock_prov")
        import_season(db_session, provider, SEASON, dataset="all")

        builder = TrainingDataBuilder(db_session)
        gw1_last = db_session.scalar(
            select(func.max(Fixture.kickoff_time)).where(
                Fixture.gameweek_id.in_(select(Gameweek.id).where(Gameweek.provider_event_id == 1))
            )
        )
        assert gw1_last is not None
        target = builder._get_next_gameweek(_utc(gw1_last))
        assert target is not None
        assert target.provider_event_id == 2


class TestNoLeakage:
    def test_features_exclude_target_gw_outcomes_and_ep_fields(
        self, db_session: Session
    ) -> None:
        provider = MockHistoricalDataProvider("mock_prov")
        import_season(db_session, provider, SEASON, dataset="all")

        cutoff = _utc(
            db_session.scalar(
                select(func.max(Fixture.kickoff_time)).where(
                    Fixture.gameweek_id.in_(
                        select(Gameweek.id).where(Gameweek.provider_event_id == 1)
                    )
                )
            )
        )  # exactly the GW1 gameweek-end stamp
        builder = TrainingDataBuilder(db_session)
        dataset = builder.build_player_dataset("minutes", cutoff, "2.0.0")

        # Target gameweek must be GW2 (first gameweek after the cutoff).
        assert dataset.metadata["target_gameweek"] == 2
        assert dataset.features and dataset.targets

        # Look-ahead-sensitive expected-point fields never appear as features.
        for features in dataset.features.values():
            assert not any(key.startswith("ep_") for key in features), (
                "ep_this/ep_next are look-ahead-unsafe and must never be features"
            )

        # Feature values must derive only from strictly pre-cutoff rows: the
        # GW2 outcome (minutes) must not influence any feature of the GW2 fold.
        for pid in dataset.entity_ids():
            prev_minutes = dataset.features[pid]["minutes_prev_match"]
            gw1_minutes = db_session.execute(
                select(PlayerGameweekPerformance.minutes)
                .join(Gameweek, Gameweek.id == PlayerGameweekPerformance.gameweek_id)
                .where(
                    PlayerGameweekPerformance.player_id == pid,
                    Gameweek.provider_event_id == 1,
                )
            ).scalar()
            assert gw1_minutes is not None
            assert prev_minutes == float(gw1_minutes), (
                "minutes_prev_match must reflect GW1 outcomes only, not the target GW"
            )
