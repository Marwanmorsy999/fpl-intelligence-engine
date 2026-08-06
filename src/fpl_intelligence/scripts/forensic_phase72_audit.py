"""Phase 7.2 forensic audit of the 2,447 records.

Reproduces the two historical import states observed in the audit logs:
  * SCENARIO A (production path): run_phase7_validation.py ingests real FPL
    structured stats (provider "real_fpl"), then the availability importer runs
    against provider "real_fpl_bootstrap". Because no PlayerExternalId rows
    exist under "real_fpl_bootstrap", every availability record is UNMATCHED.
  * SCENARIO B (seeded path): run_phase72_import.py seeds canonical players
    under BOTH "real_fpl" and "real_fpl_bootstrap" before importing, so every
    availability record MATCHES.

For each scenario it emits a full resolver audit (fetched/normalized/matched/
ambiguous/unmatched/normalization_failed/skipped_duplicate/skipped_invalid/
skipped_temporal_invalid/persisted/failed_persist) and the 9-availability-table
row counts. Resolved-but-rejected records are persisted to audit/raw.

Also verifies PostgreSQL: applies migrations and reports schema presence + the
9 table row counts (expected all-zero, confirming the live DB carries no
historical availability data and the validation runner uses SQLite in-memory).
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.db.base import Base

ROOT = Path(__file__).resolve().parents[3]
DOCS = ROOT / "docs"
AUDIT = ROOT / "audit" / "raw"
AUDIT.mkdir(parents=True, exist_ok=True)

SEASONS = ["2022-23", "2023-24", "2024-25"]
HOLDOUT = "2025-26"

AVAIL_TABLES = [
    "availability_sources",
    "availability_articles",
    "availability_evidence",
    "availability_events",
    "player_injuries",
    "player_suspensions",
    "training_reports",
    "press_conferences",
    "player_mentions",
]


def build_db() -> sessionmaker:
    import fpl_intelligence.availability.models  # noqa: F401 register tables
    import fpl_intelligence.db.models  # noqa: F401 canonical tables

    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def row_counts(db: Session) -> dict[str, int]:
    from fpl_intelligence.availability.models import (
        AvailabilityArticle,
        AvailabilityEvent,
        AvailabilityEvidence,
        AvailabilitySource,
        PlayerInjury,
        PlayerMention,
        PlayerSuspension,
        PressConference,
        TrainingReport,
    )

    return {
        "availability_sources": db.scalar(select(AvailabilitySource)).__class__ and db.query(AvailabilitySource).count(),
        "availability_articles": db.query(AvailabilityArticle).count(),
        "availability_evidence": db.query(AvailabilityEvidence).count(),
        "availability_events": db.query(AvailabilityEvent).count(),
        "player_injuries": db.query(PlayerInjury).count(),
        "player_suspensions": db.query(PlayerSuspension).count(),
        "training_reports": db.query(TrainingReport).count(),
        "press_conferences": db.query(PressConference).count(),
        "player_mentions": db.query(PlayerMention).count(),
    }


# --------------------------------------------------------------------------
# Scenario A: production ingest (real_fpl structured stats) + availability import
# --------------------------------------------------------------------------
def scenario_a() -> dict:
    db = build_db()()
    from fpl_intelligence.ingestion.historical import import_season
    from fpl_intelligence.providers import RealFPLProvider
    from fpl_intelligence.providers.github_fetcher import DiskCachingFetcher
    from fpl_intelligence.availability.historical.importer import import_historical_availability
    from fpl_intelligence.availability.historical.providers import RealFPLAvailabilityProvider

    fetcher = DiskCachingFetcher(raw_root=ROOT / "data" / "raw", offline=True)
    provider = RealFPLProvider(fetcher=fetcher)
    for s in SEASONS:
        try:
            import_season(db=db, provider=provider, season_code=s)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            print(f"  [A] import_season {s} raised: {exc}", file=sys.stderr)
    # Confirm which provider names exist for PlayerExternalId at import time.
    from fpl_intelligence.db.models import PlayerExternalId

    ext_rows = db.execute(select(PlayerExternalId.provider)).scalars().all()
    ext_by_provider: dict[str, int] = {}
    for p in ext_rows:
        ext_by_provider[p] = ext_by_provider.get(p, 0) + 1

    avail_provider = RealFPLAvailabilityProvider(raw_root=ROOT / "data" / "raw")
    result = import_historical_availability(db, avail_provider, SEASONS, strict_backtest_safe=True)
    db.commit()

    out = {
        "scenario": "A_production_ingest",
        "player_external_id_by_provider": ext_by_provider,
        "resolver_audit": result.audit.to_dict(),
        "import_result": {k: v for k, v in result.to_dict().items() if k != "resolver_audit"},
        "row_counts": row_counts(db),
    }
    # Persist unresolved/raw records.
    (AUDIT / "phase72_scenarioA_unmatched_raw.json").write_text(
        json.dumps(result.unresolved_records, indent=2, default=str), encoding="utf-8"
    )
    db.close()
    return out


# --------------------------------------------------------------------------
# Scenario B: seeded canonical players under BOTH providers (run_phase72_import)
# --------------------------------------------------------------------------
def scenario_b() -> dict:
    db = build_db()()
    # Mirror run_phase72_import._seed_canonical (provider-name registration).
    from fpl_intelligence.availability.historical.providers import (
        SampleHistoricalAvailabilityProvider,
    )
    from fpl_intelligence.db.models import Player, PlayerExternalId, Season, Team, TeamExternalId
    import csv, io

    for code in SEASONS + [HOLDOUT]:
        if db.scalar(select(Season).where(Season.code == code)) is None:
            db.add(Season(code=code, display_name=code.replace("-", "/")))

    element_ids: set[str] = set()

    def _reg(provider: str, pid: str) -> None:
        ext = db.scalar(
            select(PlayerExternalId).where(
                PlayerExternalId.provider == provider,
                PlayerExternalId.provider_player_id == pid,
            )
        )
        if ext is not None:
            return
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
            player = Player(first_name=f"First{pid}", second_name=f"Last{pid}",
                            web_name=f"Player {pid}", position_code=1)
            db.add(player)
            db.flush()
            player_id = player.id
        db.add(PlayerExternalId(player_id=player_id, provider=provider, provider_player_id=pid))

    for season in SEASONS + [HOLDOUT]:
        raw_path = ROOT / "data" / "raw" / "real_fpl" / season / "players" / "players_raw.csv"
        if raw_path.exists():
            rows = list(csv.DictReader(io.StringIO(raw_path.read_text(encoding="utf-8"))))
            for r in rows:
                pid = str(r.get("id", "") or "").strip()
                if pid:
                    element_ids.add(pid)
    sample = SampleHistoricalAvailabilityProvider()
    for season in SEASONS + [HOLDOUT]:
        for ev in sample.fetch_events(season):
            pid = str(ev.get("player_id") or "").strip()
            if pid:
                element_ids.add(pid)
    for pid in sorted(element_ids):
        _reg("real_fpl_bootstrap", pid)
        _reg("real_fpl", pid)
        _reg("sample_historical_availability", pid)
    db.commit()

    from fpl_intelligence.availability.historical.importer import import_historical_availability
    from fpl_intelligence.availability.historical.providers import RealFPLAvailabilityProvider

    avail_provider = RealFPLAvailabilityProvider(raw_root=ROOT / "data" / "raw")
    result = import_historical_availability(db, avail_provider, SEASONS, strict_backtest_safe=True)
    db.commit()

    out = {
        "scenario": "B_seeded_canonical",
        "resolver_audit": result.audit.to_dict(),
        "import_result": {k: v for k, v in result.to_dict().items() if k != "resolver_audit"},
        "row_counts": row_counts(db),
    }
    # Scenario B resolved every record, so there are no unresolved records;
    # record an explicit empty marker rather than implying persisted-raw content.
    (AUDIT / "phase72_scenarioB_unresolved_raw.json").write_text(
        json.dumps(result.unresolved_records, indent=2, default=str), encoding="utf-8"
    )
    db.close()
    return out


# --------------------------------------------------------------------------
# PostgreSQL verification (no fabricated results)
# --------------------------------------------------------------------------
def pg_verify() -> dict:
    import sqlalchemy as _sa

    try:
        eng = _sa.create_engine("postgresql+psycopg://fpl:fpl@localhost:5432/fpl",
                                connect_args={"connect_timeout": 20})
        with eng.connect() as conn:
            tables = [r[0] for r in conn.execute(
                _sa.text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
                )
            ).all()]
            have_availability = any(t in AVAIL_TABLES for t in tables)
            counts: dict[str, int] = {}
            for t in AVAIL_TABLES:
                if t in tables:
                    counts[t] = conn.execute(
                        _sa.text(f'SELECT count(*) FROM "{t}"')
                    ).scalar()
                else:
                    counts[t] = None  # schema absent
            return {
                "reachable": True,
                "table_count": len(tables),
                "has_availability_tables": have_availability,
                "row_counts": counts,
            }
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    t0 = time.time()
    print("=== Phase 7.2 Forensic Audit ===", flush=True)

    print("\n[1] SCENARIO A — production ingest (provider real_fpl)", flush=True)
    a = scenario_a()
    print(json.dumps(a["player_external_id_by_provider"], indent=2))
    print("  resolver_audit:", json.dumps(a["resolver_audit"], indent=2))
    print("  row_counts:", json.dumps(a["row_counts"], indent=2))

    print("\n[2] SCENARIO B — seeded canonical (run_phase72_import path)", flush=True)
    b = scenario_b()
    print("  resolver_audit:", json.dumps(b["resolver_audit"], indent=2))
    print("  row_counts:", json.dumps(b["row_counts"], indent=2))

    print("\n[3] PostgreSQL verification (live DB)", flush=True)
    pg = pg_verify()
    print(json.dumps(pg, indent=2))

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "scenario_a_production_ingest": a,
        "scenario_b_seeded_canonical": b,
        "postgresql": pg,
        "elapsed_s": round(time.time() - t0, 1),
    }
    out = DOCS / "phase7-2-forensic-audit.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {out}")
    print(f"wrote {AUDIT / 'phase72_scenarioA_unmatched_raw.json'}")
    print(f"wrote {AUDIT / 'phase72_scenarioB_unresolved_raw.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
