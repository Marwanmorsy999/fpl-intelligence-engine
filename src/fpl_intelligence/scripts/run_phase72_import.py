"""Phase 7.2 historical availability import runner.

Imports historical availability events from a provider into the canonical
Phase 7 availability tables, with honest STRICT_BACKTEST_SAFE vs
HISTORICAL_EVENT_ONLY / UNKNOWN temporal classification.

Default behaviour imports the deterministic SAMPLE provider (labelled
MOCK / ENGINEERING VERIFICATION ONLY) so the pipeline can be verified without
any real data dependency. Use ``--provider real_fpl_bootstrap`` to import the
REAL FPL bootstrap availability source (news / status / chance_of_playing from
``players_raw.csv``), which carries publication timestamps.

Usage:
    python -m fpl_intelligence.scripts.run_phase72_import [--provider sample|real_fpl_bootstrap]
    [--seasons 2022-23 2023-24 2024-25] [--strict]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.db.base import Base

DOCS = Path(__file__).resolve().parents[3] / "docs"
DEFAULT_SEASONS = ["2022-23", "2023-24", "2024-25"]
HOLDOUT = "2025-26"


def build_db() -> sessionmaker:
    # Import availability models so Base.metadata knows about the Phase 7
    # tables before create_all runs. Without this, create_all skips the
    # availability tables and the importer fails with "no such table".
    import fpl_intelligence.availability.models  # noqa: F401  (register tables)
    import fpl_intelligence.db.models  # noqa: F401  (canonical tables)

    engine = create_engine("sqlite:///:memory:", echo=False)

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _seed_canonical(db: Session) -> None:
    """Seed minimal canonical entities needed for availability import.

    The import resolves players via PlayerExternalId / TeamExternalId. The real
    FPL bootstrap provider (provider_name ``real_fpl_bootstrap``) uses the FPL
    'element' IDs from ``players_raw.csv`` as its player IDs and the FPL team
    IDs as its team IDs. To resolve those, we register canonical players AND
    teams under the ``real_fpl_bootstrap`` provider name (and alias the same
    canonical rows under ``real_fpl`` for cross-provider consistency).

    The sample provider's deterministic IDs are a subset of the same element
    IDs, so they are covered by the same registration when present.
    """
    from fpl_intelligence.availability.historical.providers import (
        SampleHistoricalAvailabilityProvider,
    )
    from fpl_intelligence.db.models import (
        Player,
        PlayerExternalId,
        Season,
        Team,
        TeamExternalId,
    )

    # Seasons.
    for code in DEFAULT_SEASONS + [HOLDOUT]:
        if db.scalar(select(Season).where(Season.code == code)) is None:
            db.add(Season(code=code, display_name=code.replace("-", "/")))

    # Player element IDs from the real players_raw.csv (all seasons), plus the
    # sample provider's subset. Both map to the same canonical Player rows.
    element_ids: set[str] = set()

    def _register_player(provider: str, pid: str) -> None:
        ext = db.scalar(
            select(PlayerExternalId).where(
                PlayerExternalId.provider == provider,
                PlayerExternalId.provider_player_id == pid,
            )
        )
        if ext is not None:
            return
        # Reuse an existing canonical player for this element ID if one exists.
        existing = db.scalar(
            select(PlayerExternalId).where(
                PlayerExternalId.provider == "real_fpl_bootstrap",
                PlayerExternalId.provider_player_id == pid,
            )
        ) or db.scalar(
            select(PlayerExternalId).where(
                PlayerExternalId.provider == "sample_historical_availability",
                PlayerExternalId.provider_player_id == pid,
            )
        )
        if existing is not None:
            player_id = existing.player_id
        else:
            player = Player(
                first_name=f"First{pid}", second_name=f"Last{pid}",
                web_name=f"Player {pid}", position_code=1,
            )
            db.add(player)
            db.flush()
            player_id = player.id
        db.add(
            PlayerExternalId(
                player_id=player_id,
                provider=provider,
                provider_player_id=pid,
            )
        )

    for season in DEFAULT_SEASONS + [HOLDOUT]:
        raw_path = (
            Path(__file__).resolve().parents[3]
            / "data" / "raw" / "real_fpl" / season / "players" / "players_raw.csv"
        )
        if raw_path.exists():
            with raw_path.open(encoding="utf-8") as fh:
                rows = [r for r in csv.DictReader(io.StringIO(fh.read()))]
            for r in rows:
                pid = str(r.get("id", "") or "").strip()
                if pid:
                    element_ids.add(pid)

    # Sample provider's deterministic subset (should already be in element_ids,
    # but keep this for robustness if raw files are absent).
    sample = SampleHistoricalAvailabilityProvider()
    for season in DEFAULT_SEASONS + [HOLDOUT]:
        for ev in sample.fetch_events(season):
            pid = str(ev.get("player_id") or "").strip()
            if pid:
                element_ids.add(pid)

    for pid in sorted(element_ids):
        _register_player("real_fpl_bootstrap", pid)
        _register_player("real_fpl", pid)
        _register_player("sample_historical_availability", pid)

    # Teams: register the real FPL team IDs under the bootstrap provider so
    # team context can be resolved (primary path is still provider player ID).
    # Team IDs are reused across seasons, so dedupe with a seen set and flush
    # after each season so the DB sees pending inserts before the next query.
    seen_teams: set[tuple[str, str]] = set()
    for season in DEFAULT_SEASONS + [HOLDOUT]:
        teams_path = (
            Path(__file__).resolve().parents[3]
            / "data" / "raw" / "real_fpl" / season / "teams" / "teams.csv"
        )
        if not teams_path.exists():
            continue
        with teams_path.open(encoding="utf-8") as fh:
            team_rows = [r for r in csv.DictReader(io.StringIO(fh.read()))]
        for r in team_rows:
            tid = str(r.get("id", "") or "").strip()
            if not tid:
                continue
            for provider in ("real_fpl_bootstrap", "real_fpl"):
                key = (provider, tid)
                if key in seen_teams:
                    continue
                seen_teams.add(key)
                ext = db.scalar(
                    select(TeamExternalId).where(
                        TeamExternalId.provider == provider,
                        TeamExternalId.provider_team_id == tid,
                    )
                )
                if ext is not None:
                    continue
                team = db.scalar(
                    select(Team).where(Team.short_name == r.get("short_name", ""))
                )
                if team is None:
                    team = Team(
                        name=r.get("name", f"Team {tid}"),
                        short_name=r.get("short_name") or None,
                    )
                    db.add(team)
                    db.flush()
                db.add(
                    TeamExternalId(
                        team_id=team.id,
                        provider=provider,
                        provider_team_id=tid,
                    )
                )
        db.flush()
    db.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 7.2 historical availability import.")
    parser.add_argument(
        "--provider", choices=["sample", "real_fpl_bootstrap"], default="sample",
        help="Provider to import (default: sample, MOCK / ENGINEERING VERIFICATION ONLY).",
    )
    parser.add_argument("--seasons", nargs="*", default=DEFAULT_SEASONS,
                        help="Development seasons to import.")
    parser.add_argument("--include-holdout", action="store_true",
                        help="Also import the locked 2025-26 holdout (isolated).")
    parser.add_argument("--strict", action="store_true", default=True,
                        help="Strict backtest-safe temporal mode (default on).")
    parser.add_argument("--no-strict", action="store_true",
                        help="Disable strict mode (no event is marked strict).")
    args = parser.parse_args()

    t0 = time.time()
    db = build_db()()
    _seed_canonical(db)
    db.commit()

    from fpl_intelligence.availability.historical.importer import import_historical_availability
    from fpl_intelligence.availability.historical.providers import (
        RealFPLAvailabilityProvider,
        SampleHistoricalAvailabilityProvider,
    )
    from fpl_intelligence.availability.historical.quality import validate_historical_availability
    from fpl_intelligence.availability.historical.coverage import audit_historical_coverage

    seasons = list(args.seasons) + ([HOLDOUT] if args.include_holdout else [])
    strict = not args.no_strict

    if args.provider == "real_fpl_bootstrap":
        raw_root = Path(__file__).resolve().parents[3] / "data" / "raw"
        provider = RealFPLAvailabilityProvider(raw_root=raw_root)
        print(f"[import] REAL FPL bootstrap provider (raw_root={raw_root})")
    else:
        provider = SampleHistoricalAvailabilityProvider(seasons=seasons)
        print("[import] SAMPLE provider (MOCK / ENGINEERING VERIFICATION ONLY)")

    result = import_historical_availability(
        db, provider, seasons, strict_backtest_safe=strict,
    )
    db.commit()

    quality = validate_historical_availability(db, provider, seasons)
    coverage = audit_historical_coverage(db, seasons)

    print("\n=== Phase 7.2 Import Result ===")
    print(json.dumps(result.to_dict(), indent=2, default=str))
    print("\n=== Data Quality ===")
    print(json.dumps(quality.to_dict(), indent=2, default=str))
    print("\n=== Coverage ===")
    print(json.dumps(coverage.to_dict(), indent=2, default=str))

    out = DOCS / "phase7-2-import-result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "provider": args.provider,
                "seasons": seasons,
                "strict_backtest_safe": strict,
                "import": result.to_dict(),
                "quality": quality.to_dict(),
                "coverage": coverage.to_dict(),
                "elapsed_s": round(time.time() - t0, 1),
            },
            indent=2, default=str,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {out}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
