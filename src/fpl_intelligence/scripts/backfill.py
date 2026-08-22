"""Historical data backfill CLI.

Usage:
    python -m fpl_intelligence.scripts.backfill --season 2024-25
    python -m fpl_intelligence.scripts.backfill --season 2024-25 --provider mock_provider
    python -m fpl_intelligence.scripts.backfill --season 2024-25 --dataset fixtures
    python -m fpl_intelligence.scripts.backfill --season 2024-25 --dry-run
    python -m fpl_intelligence.scripts.backfill --season 2024-25 --force
    python -m fpl_intelligence.scripts.backfill --season 2024-25 --resume
"""

from __future__ import annotations

import argparse
import logging
import sys

from fpl_intelligence.db.session import SessionLocal
from fpl_intelligence.ingestion.historical import import_season
from fpl_intelligence.providers import (
    MockHistoricalDataProvider,
    RealFootballStatsProvider,
    RealFPLProvider,
)
from fpl_intelligence.validation.historical import (
    validate_fixture_integrity,
    validate_gameweek_integrity,
    validate_no_duplicate_records,
    validate_player_stats_integrity,
    validate_season_integrity,
)

logger = logging.getLogger(__name__)


def _get_provider(provider_name: str):  # type: ignore[no-untyped-def]
    """Get a provider by name.

    Supports the mock provider (synthetic) and the real historical providers
    backed by the public vaastav FPL mirror.
    """
    if provider_name in ("real_fpl", "real_football"):
        fetcher = _get_fetcher()
        if provider_name == "real_football":
            return RealFootballStatsProvider(fpl=RealFPLProvider(fetcher=fetcher))
        return RealFPLProvider(fetcher=fetcher)
    providers = {
        "mock_provider": MockHistoricalDataProvider(
            provider_name="mock_provider", schema_version="v1"
        ),
        "mock_provider_v2": MockHistoricalDataProvider(
            provider_name="mock_provider_v2", schema_version="v2"
        ),
    }
    if provider_name not in providers:
        available = ", ".join(list(providers.keys()) + ["real_fpl", "real_football"])
        raise ValueError(f"Unknown provider '{provider_name}'. Available: {available}")
    return providers[provider_name]


def _get_fetcher():
    from pathlib import Path

    from fpl_intelligence.providers.github_fetcher import DiskCachingFetcher

    raw_root = Path(__file__).resolve().parents[3] / "data" / "raw"
    return DiskCachingFetcher(raw_root=raw_root)


def _validate_import(db_session, season_code: str) -> None:
    """Run validation checks after import."""
    from sqlalchemy import select

    from fpl_intelligence.db.models import Season

    season = db_session.scalar(select(Season).where(Season.code == season_code))
    if season is None:
        logger.warning("Season %s not found in database, skipping validation", season_code)
        return

    season_id = season.id
    logger.info("Running validation checks for season %s (id=%d)...", season_code, season_id)

    checks = [
        ("Season integrity", validate_season_integrity(db_session, season_id)),
        ("Gameweek integrity", validate_gameweek_integrity(db_session, season_id)),
        ("Fixture integrity", validate_fixture_integrity(db_session, season_id)),
        ("Player stats integrity", validate_player_stats_integrity(db_session, season_id)),
        ("No duplicate records", validate_no_duplicate_records(db_session)),
    ]

    all_passed = True
    for name, result in checks:
        if result.passed:
            logger.info("  ✓ %s: passed", name)
        else:
            all_passed = False
            logger.warning("  ✗ %s: FAILED", name)
            for err in result.errors:
                logger.warning("      - %s", err.message)

    if all_passed:
        logger.info("All validation checks passed for season %s.", season_code)
    else:
        logger.warning("Some validation checks failed for season %s.", season_code)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill historical FPL/football data for a season.",
    )
    parser.add_argument(
        "--season",
        required=True,
        help="Season code, e.g. 2024-25",
    )
    parser.add_argument(
        "--provider",
        default="mock_provider",
        help="Data provider name (default: mock_provider)",
    )
    parser.add_argument(
        "--dataset",
        default="all",
        choices=["all", "teams", "players", "fixtures", "stats", "fpl"],
        help="Dataset to import (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without persisting any data",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-import even if previously completed",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume a previously interrupted import",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run validation checks after import",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    logger.info(
        "Starting backfill: season=%s provider=%s dataset=%s dry_run=%s force=%s resume=%s",
        args.season,
        args.provider,
        args.dataset,
        args.dry_run,
        args.force,
        args.resume,
    )

    provider = _get_provider(args.provider)
    db = SessionLocal()
    try:
        report = import_season(
            db=db,
            provider=provider,
            season_code=args.season,
            dataset=args.dataset,
            dry_run=args.dry_run,
            force=args.force,
        )

        print("\n" + report.summary())
        print()

        if report.has_critical_errors():
            logger.error("Import completed with critical errors.")
            sys.exit(1)

        if args.validate and not args.dry_run:
            _validate_import(db, args.season)

        logger.info("Backfill completed successfully.")
    except Exception as exc:
        logger.error("Backfill failed: %s", exc)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
