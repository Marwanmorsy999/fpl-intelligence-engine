"""Read-only preflight for the canonical minutes validation database."""

from __future__ import annotations

from typing import cast

from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from fpl_intelligence.db.models import (
    Fixture,
    Gameweek,
    Player,
    PlayerGameweekPerformance,
    Season,
    Team,
)
from fpl_intelligence.db.session import validation_session_factory

REQUIRED_SEASONS = ("2022-23", "2023-24", "2024-25")
REQUIRED_TABLES = (
    "players",
    "teams",
    "seasons",
    "gameweeks",
    "fixtures",
    "player_gameweek_performances",
    "player_team_memberships",
)


def _season_counts(db, season_code: str) -> dict[str, int]:
    season_id = db.scalar(select(Season.id).where(Season.code == season_code))
    if season_id is None:
        return {"players": 0, "fixtures": 0, "gameweeks": 0, "performance": 0}
    gameweek_ids = select(Gameweek.id).where(Gameweek.season_id == season_id)
    player_count = select(
        func.count(func.distinct(PlayerGameweekPerformance.player_id))
    ).where(PlayerGameweekPerformance.season_id == season_id)
    return {
        "players": int(db.scalar(player_count) or 0),
        "fixtures": int(
            db.scalar(
                select(func.count()).select_from(Fixture).where(Fixture.season_id == season_id)
            )
            or 0
        ),
        "gameweeks": int(
            db.scalar(
                select(func.count()).select_from(Gameweek).where(Gameweek.season_id == season_id)
            )
            or 0
        ),
        "performance": int(
            db.scalar(
                select(func.count())
                .select_from(PlayerGameweekPerformance)
                .where(PlayerGameweekPerformance.gameweek_id.in_(gameweek_ids))
            )
            or 0
        ),
    }


def collect_preflight(db) -> dict[str, object]:
    """Collect SELECT-only checks for the historical validation scope."""
    table_names = set(inspect(db.bind).get_table_names())
    missing_tables = [table for table in REQUIRED_TABLES if table not in table_names]
    if missing_tables:
        return {"missing_tables": missing_tables}

    season_ids = list(
        db.scalars(select(Season.id).where(Season.code.in_(REQUIRED_SEASONS))).all()
    )
    historical_filter = (
        PlayerGameweekPerformance.season_id.in_(season_ids) if season_ids else text("1 = 0")
    )
    historical_total = int(
        db.scalar(
            select(func.count())
            .select_from(PlayerGameweekPerformance)
            .where(historical_filter)
        )
        or 0
    )
    temporal = {
        "performance_available_at": int(
            db.scalar(
                select(func.count())
                .select_from(PlayerGameweekPerformance)
                .where(
                    historical_filter,
                    PlayerGameweekPerformance.available_at.is_not(None),
                )
            )
            or 0
        ),
        "performance_ingested_at": int(
            db.scalar(
                select(func.count())
                .select_from(PlayerGameweekPerformance)
                .where(
                    historical_filter,
                    PlayerGameweekPerformance.ingested_at.is_not(None),
                )
            )
            or 0
        ),
        # Historical backfill intentionally leaves this NULL because genuine
        # FPL deadlines are absent from the source mirror. The validator uses
        # the conservative previous-gameweek kickoff fallback instead.
        "gameweek_deadline_time": int(
            db.scalar(
                select(func.count())
                .select_from(Gameweek)
                .where(
                    Gameweek.season_id.in_(season_ids) if season_ids else text("1 = 0"),
                    Gameweek.deadline_time.is_not(None),
                )
            )
            or 0
        ),
    }
    mappings = {
        "player": int(
            db.scalar(
                select(func.count())
                .select_from(PlayerGameweekPerformance)
                .outerjoin(Player, Player.id == PlayerGameweekPerformance.player_id)
                .where(historical_filter, Player.id.is_(None))
            )
            or 0
        ),
        "team": int(
            db.scalar(
                select(func.count())
                .select_from(PlayerGameweekPerformance)
                .outerjoin(Team, Team.id == PlayerGameweekPerformance.team_id)
                .where(historical_filter, Team.id.is_(None))
            )
            or 0
        ),
        "fixture": int(
            db.scalar(
                select(func.count(func.distinct(PlayerGameweekPerformance.gameweek_id)))
                .select_from(PlayerGameweekPerformance)
                .outerjoin(Gameweek, Gameweek.id == PlayerGameweekPerformance.gameweek_id)
                .where(historical_filter, Gameweek.id.is_(None))
            )
            or 0
        ),
    }
    duplicate_groups = db.execute(
        select(
            PlayerGameweekPerformance.player_id,
            PlayerGameweekPerformance.gameweek_id,
            func.count().label("row_count"),
        )
        .where(historical_filter)
        .group_by(
            PlayerGameweekPerformance.player_id,
            PlayerGameweekPerformance.gameweek_id,
        )
        .having(func.count() > 1)
    ).all()
    missing_critical = int(
        db.scalar(
            select(func.count())
            .select_from(PlayerGameweekPerformance)
            .where(
                historical_filter,
                (PlayerGameweekPerformance.player_id.is_(None))
                | (PlayerGameweekPerformance.gameweek_id.is_(None))
                | (PlayerGameweekPerformance.season_id.is_(None))
                | (PlayerGameweekPerformance.team_id.is_(None))
                | (PlayerGameweekPerformance.minutes.is_(None)),
            )
        )
        or 0
    )
    invalid_timestamps = int(
        db.scalar(
            select(func.count())
            .select_from(PlayerGameweekPerformance)
            .where(
                historical_filter,
                PlayerGameweekPerformance.available_at.is_not(None),
                PlayerGameweekPerformance.ingested_at.is_not(None),
                PlayerGameweekPerformance.available_at > PlayerGameweekPerformance.ingested_at,
            )
        )
        or 0
    )
    return {
        "missing_tables": [],
        "season_counts": {
            season: _season_counts(db, season) for season in REQUIRED_SEASONS
        },
        "total_performance": historical_total,
        "temporal": temporal,
        "mapping_failures": mappings,
        "duplicate_rows": sum(int(row.row_count) - 1 for row in duplicate_groups),
        "missing_critical_values": missing_critical,
        "invalid_timestamps": invalid_timestamps,
    }


def _print_report(report: dict[str, object]) -> int:
    missing_tables = cast(list[str], report["missing_tables"])
    if missing_tables:
        print("schema: required structures missing")
        print(f"missing tables: {', '.join(str(table) for table in missing_tables)}")
        return 1

    season_counts = cast(dict[str, dict[str, int]], report["season_counts"])
    for season, counts in season_counts.items():
        print(
            f"coverage {season}: players={counts['players']} fixtures={counts['fixtures']} "
            f"gameweeks={counts['gameweeks']} historical_performance_rows={counts['performance']}"
        )
    print(f"historical performance row count: {report['total_performance']}")
    print(f"temporal data: {report['temporal']}")
    print(f"entity resolution failures: {report['mapping_failures']}")
    print(
        "data quality: "
        f"duplicate_rows={report['duplicate_rows']} "
        f"missing_critical_values={report['missing_critical_values']} "
        f"invalid_timestamps={report['invalid_timestamps']}"
    )

    temporal = cast(dict[str, int], report["temporal"])
    historical_rows = int(report["total_performance"])
    temporal_gaps = {
        key: value
        for key, value in temporal.items()
        if key != "gameweek_deadline_time" and value < historical_rows
    }
    if temporal_gaps:
        print(f"invalid historical temporal provenance: {temporal_gaps}")
        return 1
    print(
        "gameweek deadlines: source values are absent; "
        "validation will use the documented conservative gameweek-ordering cutoff fallback"
    )

    if (
        report["mapping_failures"] != {"player": 0, "team": 0, "fixture": 0}
        or report["duplicate_rows"]
        or report["missing_critical_values"]
        or report["invalid_timestamps"]
    ):
        print("invalid historical data quality: refusing validation")
        return 1

    missing_seasons = [
        season for season, counts in season_counts.items() if counts["performance"] == 0
    ]
    if missing_seasons:
        print(f"missing required seasons: {', '.join(missing_seasons)}")
        return 1
    return 0


def main() -> int:
    try:
        session_factory = validation_session_factory()
        with session_factory() as db:
            db.execute(text("SELECT 1"))
            report = collect_preflight(db)
    except (RuntimeError, SQLAlchemyError) as exc:
        print("source: unavailable")
        print(f"availability: unavailable ({str(exc).splitlines()[0]})")
        return 1

    print("source: configured canonical PostgreSQL database")
    print("availability: reachable")
    return _print_report(report)


if __name__ == "__main__":
    raise SystemExit(main())
