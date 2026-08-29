"""Import and verify the locked 2025-26 final holdout.

This is additive and idempotent. It uses the repository's real FPL provider and
canonical historical ingestion pipeline, then derives the Team Strength
team-match layer from the same real fixture + gameweek xG source.

The holdout is never used for model training, tuning, feature selection, or
calibration. This script only materializes the read-only observations required
for final evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fpl_intelligence.db.models import Fixture, Season, TeamExternalId, TeamMatchPerformance  # noqa: E402
from fpl_intelligence.db.session import validation_session_factory  # noqa: E402
from fpl_intelligence.ingestion.historical import import_season  # noqa: E402
from fpl_intelligence.providers.real_fpl import RealFPLProvider  # noqa: E402
from fpl_intelligence.providers.real_football_stats import RealFootballStatsProvider  # noqa: E402

HOLDOUT = "2025-26"
DEVELOPMENT = ("2022-23", "2023-24", "2024-25")


def _fixture_id_to_int(provider_fixture_id: str) -> int:
    return int(hashlib.md5(provider_fixture_id.encode()).hexdigest()[:8], 16) % (10**9)


def _verify_base_data(db) -> dict[str, int]:
    season = db.scalar(select(Season).where(Season.code == HOLDOUT))
    if season is None:
        raise RuntimeError(f"locked holdout season {HOLDOUT} is not present")

    fixtures = int(
        db.scalar(select(func.count()).select_from(Fixture).where(Fixture.season_id == season.id)) or 0
    )
    finished = int(
        db.scalar(
            select(func.count())
            .select_from(Fixture)
            .where(
                Fixture.season_id == season.id,
                Fixture.kickoff_time.is_not(None),
                Fixture.home_score.is_not(None),
                Fixture.away_score.is_not(None),
            )
        )
        or 0
    )
    if fixtures != 380 or finished != 380:
        raise RuntimeError(
            f"holdout fixture coverage invalid: fixtures={fixtures}, scored={finished}; expected 380/380"
        )
    return {"season_id": season.id, "fixtures": fixtures, "scored": finished}


def _ensure_team_match_layer(db, provider: RealFPLProvider, season_id: int) -> dict[str, int]:
    fpl_stats = RealFootballStatsProvider(provider)
    provider_fixtures = list(provider.get_fixtures(HOLDOUT))

    gw_end: dict[int, datetime] = {}
    team_gw_counts: dict[tuple[str, int], int] = defaultdict(int)
    for row in provider_fixtures:
        gw = row.get("gameweek")
        kickoff = row.get("kickoff_time")
        home = str(row.get("home_team_id"))
        away = str(row.get("away_team_id"))
        if gw is None or kickoff is None:
            continue
        gw_i = int(gw)
        team_gw_counts[(home, gw_i)] += 1
        team_gw_counts[(away, gw_i)] += 1
        current = gw_end.get(gw_i)
        if current is None or kickoff > current:
            gw_end[gw_i] = kickoff

    canonical_fixture_ids = {
        f.provider_fixture_id: f.id
        for f in db.scalars(select(Fixture).where(Fixture.season_id == season_id)).all()
    }
    team_external_ids = {
        (ext.provider, ext.provider_team_id): ext.team_id
        for ext in db.scalars(select(TeamExternalId)).all()
        if ext.provider == "real_fpl"
    }
    existing = {
        (row.team_id, row.fixture_id)
        for row in db.scalars(
            select(TeamMatchPerformance).where(TeamMatchPerformance.season_id == season_id)
        ).all()
    }

    inserted = 0
    xg_usable = 0
    for team in provider.get_teams(HOLDOUT):
        provider_team_id = str(team["provider_team_id"])
        canonical_team_id = team_external_ids.get(("real_fpl", provider_team_id))
        if canonical_team_id is None:
            raise RuntimeError(f"no canonical team mapping for real_fpl team {provider_team_id}")
        for stat in fpl_stats.get_team_match_stats(HOLDOUT, provider_team_id):
            provider_fixture_id = str(stat["provider_fixture_id"])
            fixture_id = canonical_fixture_ids.get(_fixture_id_to_int(provider_fixture_id))
            if fixture_id is None:
                raise RuntimeError(f"no canonical fixture mapping for provider fixture {provider_fixture_id}")
            if (canonical_team_id, fixture_id) in existing:
                continue

            provider_fixture = next(
                (row for row in provider_fixtures if str(row.get("provider_fixture_id")) == provider_fixture_id),
                None,
            )
            if provider_fixture is None:
                raise RuntimeError(f"provider fixture {provider_fixture_id} missing from fixture source")
            gw = int(provider_fixture["gameweek"])
            end_reference = gw_end.get(gw)
            if end_reference is None:
                raise RuntimeError(f"no genuine gameweek-end reference for holdout GW{gw}")

            # xG is only accepted when the team has one fixture in that GW.
            # This avoids copying a gameweek-total xG value onto multiple
            # fixtures in a double gameweek.
            usable_xg = stat.get("expected_goals") if team_gw_counts[(provider_team_id, gw)] == 1 else None
            usable_xgc = (
                stat.get("expected_goals_conceded")
                if team_gw_counts[(provider_team_id, gw)] == 1
                else None
            )
            if usable_xg is not None:
                xg_usable += 1

            db.add(
                TeamMatchPerformance(
                    team_id=canonical_team_id,
                    fixture_id=fixture_id,
                    season_id=season_id,
                    is_home=bool(stat["is_home"]),
                    goals_scored=int(stat.get("goals_scored") or 0),
                    goals_conceded=int(stat.get("goals_conceded") or 0),
                    expected_goals=float(usable_xg) if usable_xg is not None else None,
                    expected_goals_conceded=float(usable_xgc) if usable_xgc is not None else None,
                    available_at=end_reference,
                    ingested_at=end_reference,
                )
            )
            existing.add((canonical_team_id, fixture_id))
            inserted += 1

    db.flush()
    total = int(
        db.scalar(
            select(func.count())
            .select_from(TeamMatchPerformance)
            .where(TeamMatchPerformance.season_id == season_id)
        )
        or 0
    )
    usable_temporal = int(
        db.scalar(
            select(func.count())
            .select_from(TeamMatchPerformance)
            .where(
                TeamMatchPerformance.season_id == season_id,
                TeamMatchPerformance.available_at.is_not(None),
                TeamMatchPerformance.ingested_at.is_not(None),
                TeamMatchPerformance.available_at <= TeamMatchPerformance.ingested_at,
            )
        )
        or 0
    )
    if total != 760 or usable_temporal != 760:
        raise RuntimeError(
            f"holdout team-match coverage invalid: rows={total}, temporally_usable={usable_temporal}; expected 760/760"
        )
    return {"inserted": inserted, "rows": total, "temporally_usable": usable_temporal, "xg_usable": xg_usable}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    session_factory = validation_session_factory()
    provider = RealFPLProvider(seasons=[*DEVELOPMENT, HOLDOUT])
    with session_factory() as db:
        season = db.scalar(select(Season).where(Season.code == HOLDOUT))
        if season is None and not args.verify_only:
            report = import_season(db, provider, HOLDOUT, dataset="all")
            print(report.summary(), flush=True)
            season = db.scalar(select(Season).where(Season.code == HOLDOUT))
        elif season is None:
            raise RuntimeError("--verify-only requested but the locked 2025-26 season is absent")

        base = _verify_base_data(db)
        if args.verify_only:
            team = _verify_team_match_layer(db, provider, int(season.id))
            print({"holdout": HOLDOUT, **base, **team})
            return 0

        team = _ensure_team_match_layer(db, provider, int(season.id))
        db.commit()
        print({"holdout": HOLDOUT, **base, **team})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
