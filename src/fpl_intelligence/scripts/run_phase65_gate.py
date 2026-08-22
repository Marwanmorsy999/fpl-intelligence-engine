"""Phase 6.5 -- Decision Optimization Validation Gate (real-data runner).

Rebuilds a real historical database from the public vaastav FPL mirror (cached
under ``data/raw/real_fpl``) and runs the real :class:`DecisionBacktester` over
the development seasons and the locked 2025-26 holdout. Every metric is computed
from actual historical observations - never hardcoded constants.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import Gameweek, PlayerGameweekPerformance, Season
from fpl_intelligence.ingestion.historical import import_season
from fpl_intelligence.optimization.backtesting import DecisionBacktester
from fpl_intelligence.optimization.domain import DecisionObjective, SquadState
from fpl_intelligence.optimization.provider import (
    DecisionPredictionProvider,
    PlayerPrediction,
)
from fpl_intelligence.providers import RealFPLProvider

DOCS = Path(__file__).resolve().parents[3] / "docs"
DEFAULT_SEASONS = ["2022-23", "2023-24", "2024-25"]
HOLDOUT = "2025-26"


class FormProvider(DecisionPredictionProvider):
    """Recent-form baseline provider; EV = rolling average of prior-GW points."""

    def __init__(self, db: Session, season_id: int):
        self.db = db
        self.season_id = season_id
        self.num: dict[int, int] = {}

    def _n(self, gid):
        if gid is None:
            return 0
        if gid not in self.num:
            gw = self.db.get(Gameweek, gid)
            self.num[gid] = gw.provider_event_id if gw else 0
        return self.num[gid]

    def _ev(self, pid, gw):
        perfs = list(
            self.db.execute(
                select(PlayerGameweekPerformance).where(
                    PlayerGameweekPerformance.player_id == pid,
                    PlayerGameweekPerformance.season_id == self.season_id,
                    PlayerGameweekPerformance.gameweek_id.is_not(None),
                )
            )
            .scalars()
            .all()
        )
        prior = [p.total_points or 0 for p in perfs if self._n(p.gameweek_id) < gw]
        return float(np.mean(prior)) if prior else 2.0

    def get_player_prediction(self, player_id, gameweek):
        ev = self._ev(player_id, gameweek)
        return PlayerPrediction(
            player_id=player_id,
            gameweek=gameweek,
            expected_points=round(ev, 4),
            expected_minutes=60.0,
            start_probability=0.7,
            distribution=np.array([ev]),
            floor=0.0,
            ceiling=ev * 2.0,
        )

    def get_squad_predictions(self, sp, gws):
        return {gw: {pid: self.get_player_prediction(pid, gw) for pid in sp} for gw in gws}

    def get_all_predictions(self, gameweek):
        pids = list(
            self.db.execute(
                select(PlayerGameweekPerformance.player_id)
                .where(PlayerGameweekPerformance.season_id == self.season_id)
                .distinct()
            )
            .scalars()
            .all()
        )
        return {pid: self.get_player_prediction(pid, gameweek) for pid in pids}

    def get_fixture_count(self, pid, gw):
        return 1


def build_db():
    eng = create_engine("sqlite:///:memory:", echo=False)

    @event.listens_for(eng, "connect")
    def _p(dbapi, rec):
        cur = dbapi.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, autoflush=False, autocommit=False, expire_on_commit=False)


def run_season(db: Session, code: str) -> dict:
    season = db.scalar(select(Season).where(Season.code == code))
    if season is None:
        return {"season": code, "status": "BLOCKED", "reason": "season not imported"}
    prov = FormProvider(db, season.id)
    preds = prov.get_all_predictions(1)
    ordered = sorted(preds.items(), key=lambda kv: kv[1].expected_points, reverse=True)
    players = [pid for pid, _ in ordered[:15]]
    squad = SquadState(
        manager_id=1,
        season=code,
        gameweek=1,
        squad_players=players,
        starting_xi=players[:11],
        bench_order=players[11:15],
        captain=players[0],
        vice_captain=players[1],
        bank=0.0,
        team_value=100.0,
        free_transfers=1,
        rolled_transfers=0,
        transfer_hits=0,
    )
    try:
        res = DecisionBacktester(prov, db).backtest_strategy(
            "optimizer", 1, 38, squad, objective=DecisionObjective.MAXIMIZE_GW_POINTS
        )
        out: dict[str, object] = dict(res)
        out["season"] = code
        out["status"] = "COMPLETED"
        return out
    except Exception as exc:  # noqa: BLE001
        return {"season": code, "status": "BLOCKED", "reason": str(exc)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 6.5 real-data decision gate.")
    ap.add_argument("--seasons", nargs="*", default=DEFAULT_SEASONS)
    ap.add_argument("--include-holdout", action="store_true")
    ap.add_argument("--offline", action="store_true", help="Replay from cached raw files.")
    args = ap.parse_args()

    t0 = time.time()
    from fpl_intelligence.providers.github_fetcher import DiskCachingFetcher

    fetcher = DiskCachingFetcher(
        raw_root=Path(__file__).resolve().parents[3] / "data" / "raw", offline=args.offline
    )
    db = build_db()()
    provider = RealFPLProvider(fetcher=fetcher)
    seasons = list(args.seasons) + ([HOLDOUT] if args.include_holdout else [])

    imported = []
    for s in seasons:
        try:
            import_season(db=db, provider=provider, season_code=s)
            db.commit()
            imported.append(s)
            print(f"  imported real season {s}")
        except Exception as exc:  # noqa: BLE001
            print(f"  import failed {s}: {exc}")

    seasons_results: dict[str, dict] = {
        s: (
            run_season(db, s)
            if s in imported
            else {"season": s, "status": "BLOCKED", "reason": "import failed"}
        )
        for s in seasons
    }
    meta: dict[str, object] = {
        "imported_seasons": imported,
        "elapsed_s": round(time.time() - t0, 1),
        "note": "Real DecisionBacktester, recent-form provider. No fabricated constants.",
    }
    results: dict[str, object] = {
        "seasons": seasons_results,
        "meta": meta,
    }
    out = DOCS / "phase6-5-real-results.json"
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {out}")
    for s, r in seasons_results.items():
        print(f"  {s}: {r.get('status')} {r.get('total_points', '')}")
    print(f"\nElapsed: {meta['elapsed_s']}s")
    db.close()


if __name__ == "__main__":
    main()
