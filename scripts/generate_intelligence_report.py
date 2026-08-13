"""scripts/generate_intelligence_report.py — Phase 9.4 Quantitative Bridge CLI.

Generates an :class:`~fpl_intelligence.live_intelligence.report.IntelligenceReport`
from **real quantitative predictions** and the **live evidence database** —
no manual ``PredictionContext`` inputs required.

Flow
-----
* :class:`PredictionContextBuilder` reads the quantitative engine through
  :class:`DecisionPredictionProvider` (Phase 4/5/6 read-only).
* :class:`EvidenceQueryService` reads pre-deadline availability / tactical /
  unresolved evidence from the database, filtered by the gameweek cutoff.
* :class:`AnalystReportGenerator` orchestrates both and delegates to the
  :class:`AIAnalyst` to produce the report.
* The report is printed as Markdown.

``--dry-run`` uses :class:`MockLLMProvider` + :class:`StaticPredictionProvider`
and never issues DB writes, so it is fully offline and safe.

Usage::

    python scripts/generate_intelligence_report.py --player-id 1 --gameweek 3
    python scripts/generate_intelligence_report.py --player-id 1 --gameweek 3 --dry-run
    python scripts/generate_intelligence_report.py \\
        --player-id 7 --gameweek 4 --cutoff 2025-08-17T18:30:00+00:00 \\
        --task captaincy_debate --db ./fpl.db
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(_SRC))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from fpl_intelligence.db.base import Base  # noqa: E402
from fpl_intelligence.db.models import Gameweek, PlayerGameweekPerformance  # noqa: E402
from fpl_intelligence.live_intelligence.analyst import AnalystTask  # noqa: E402
from fpl_intelligence.live_intelligence.bridge import (  # noqa: E402
    AnalystReportGenerator,
    EvidenceQueryService,
    PredictionContextBuilder,
    StaticPredictionProvider,
)
from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider  # noqa: E402
from fpl_intelligence.optimization.provider import (  # noqa: E402
    DecisionPredictionProvider,
    PlayerPrediction,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PROVIDER = 2


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, assuming UTC for naive inputs."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 9.4 — generate an IntelligenceReport from real predictions "
            "and the evidence database."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--player-id", type=int, required=True, help="Canonical player id.")
    parser.add_argument("--gameweek", type=int, required=True, help="FPL gameweek number.")
    parser.add_argument(
        "--cutoff",
        default=None,
        help="ISO-8601 cutoff time (defaults to now). Only evidence available "
        "before this instant is included.",
    )
    parser.add_argument(
        "--task",
        choices=[t.value for t in AnalystTask],
        default=AnalystTask.TRANSFER_RECOMMENDATION.value,
        help="Analyst task to run.",
    )
    parser.add_argument("--subject-label", default=None, help="Human-readable player label.")
    parser.add_argument("--notes", default=None, help="Free-form notes for the analyst context.")
    parser.add_argument(
        "--provider",
        choices=["mock", "real"],
        default="mock",
        help="LLM provider. 'mock' (default) makes no network calls.",
    )
    parser.add_argument(
        "--allow-mock-evidence",
        action="store_true",
        help="Allow mock-environment evidence into the report (test/dry-run only).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use MockLLMProvider + StaticPredictionProvider and skip DB writes.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite DB path. Defaults to an in-memory database.",
    )
    return parser.parse_args(argv)


def _build_session(db_path: Path | None):
    from sqlalchemy import create_engine, event

    if db_path is None:
        engine = create_engine("sqlite:///:memory:", echo=False)
    else:
        engine = create_engine(f"sqlite:///{db_path}", echo=False)

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _build_llm_provider(args: argparse.Namespace) -> Any:
    if args.dry_run or args.provider == "mock":
        return MockLLMProvider()

    from fpl_intelligence.live_intelligence.llm_providers import ProviderFactory
    from fpl_intelligence.live_intelligence.llm_settings import (
        LLMSettingsError,
        load_llm_settings,
    )

    try:
        settings = load_llm_settings()
        return ProviderFactory(settings).create(None, http_client=None)
    except LLMSettingsError as exc:
        print(f"CONFIGURATION ERROR: {exc}")
        raise SystemExit(EXIT_USAGE) from exc


class _RecentFormPredictionProvider(DecisionPredictionProvider):
    """DB-backed baseline provider; EV = rolling mean of prior-GW FPL points.

    Local read of the database only — no live API calls, consistent with the
    Phase 9.4 constraint that no scraper/fetcher is built yet. This is the
    ``--dry-run`` counterpart's opposite: it uses real stored player
    performances instead of fixed numbers.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_player_prediction(self, player_id: int, gameweek: int) -> PlayerPrediction:
        rows = list(
            self.db.execute(
                select(PlayerGameweekPerformance).where(
                    PlayerGameweekPerformance.player_id == player_id,
                    PlayerGameweekPerformance.gameweek_id.is_not(None),
                )
            ).scalars().all()
        )
        prior: list[float] = []
        for perf in rows:
            gw_event = 0
            if perf.gameweek_id is not None:
                gameweek_row = self.db.get(Gameweek, perf.gameweek_id)
                gw_event = gameweek_row.provider_event_id if gameweek_row else 0
            if gw_event < gameweek:
                prior.append(float(perf.total_points or 0))
        ev = float(np.mean(prior)) if prior else 2.0
        return PlayerPrediction(
            player_id=player_id,
            gameweek=gameweek,
            expected_points=round(ev, 4),
            expected_minutes=60.0,
            start_probability=0.7,
            distribution=np.array([ev]),
            floor=0.0,
            ceiling=round(ev * 2.0, 4),
            confidence=0.6,
        )

    def get_squad_predictions(
        self, squad_players: list[int], gameweeks: list[int]
    ) -> dict[int, dict[int, PlayerPrediction]]:
        return {
            gw: {pid: self.get_player_prediction(pid, gw) for pid in squad_players}
            for gw in gameweeks
        }

    def get_all_predictions(self, gameweek: int) -> dict[int, PlayerPrediction]:
        return {}

    def get_fixture_count(self, player_id: int, gameweek: int) -> int:
        return 1


def _build_prediction_provider(
    args: argparse.Namespace, db: Session
) -> DecisionPredictionProvider:
    if args.dry_run:
        return StaticPredictionProvider()
    return _RecentFormPredictionProvider(db)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.cutoff is not None:
        try:
            cutoff = _parse_iso(args.cutoff)
        except ValueError as exc:
            print(f"USAGE ERROR: could not parse --cutoff: {exc}")
            return EXIT_USAGE
    else:
        cutoff = datetime.now(UTC)

    SessionLocal = _build_session(args.db)
    db = SessionLocal()
    try:
        llm = _build_llm_provider(args)
        prediction = _build_prediction_provider(args, db)
        builder = PredictionContextBuilder(prediction_provider=prediction)
        evidence_service = EvidenceQueryService(db, allow_mock=args.allow_mock_evidence)
        generator = AnalystReportGenerator(
            builder,
            evidence_service,
            llm,
            task=AnalystTask(args.task),
            strict_leakage=True,
            allow_mock_evidence=args.allow_mock_evidence,
        )
        report = generator.generate(
            args.player_id,
            args.gameweek,
            cutoff_time=cutoff,
            subject_label=args.subject_label,
            notes=args.notes or "",
        )
    except Exception as exc:  # noqa: BLE001 - report provider/usage errors cleanly
        print(f"PROVIDER ERROR: {exc}")
        return EXIT_PROVIDER
    finally:
        db.close()

    print(report.render_markdown())
    print("=" * 78)
    print("PHASE 9.4 — INTELLIGENCE REPORT (Markdown above)")
    print(f"  player_id      : {args.player_id}")
    print(f"  gameweek       : {args.gameweek}")
    print(f"  cutoff         : {cutoff.isoformat()}")
    print(f"  task           : {report.task}")
    print(f"  is_mock        : {report.is_mock}")
    if args.dry_run:
        print(
            "  dry_run        : True (MockLLMProvider + StaticPredictionProvider; "
            "no DB writes)"
        )
    print("=" * 78)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())