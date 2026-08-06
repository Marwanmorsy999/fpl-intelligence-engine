"""Phase 4.75 -- Real Historical Data Integration + Revalidation runner.

Runs the EXACT EXISTING Phase 4.5 gate against real historical FPL data
(imported from the public vaastav FPL mirror) and, for comparison, against the
synthetic mock data. Writes all Phase 4.75 deliverable documents.

Usage:
    python -m fpl_intelligence.scripts.run_phase475_gate [--seasons 2022-23 ...] [--no-mock]

The evaluation methodology (baselines, minutes, team/match, player pipeline,
ablations, captain proxy) is NOT modified -- only the data and the provenance
label differ from Phase 4.5.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from fpl_intelligence.db.base import Base
from fpl_intelligence.domain.environment import DataEnvironment
from fpl_intelligence.ingestion.historical import import_season
from fpl_intelligence.providers import MockHistoricalDataProvider, RealFPLProvider
from fpl_intelligence.temporal import classify_provider
from fpl_intelligence.validation.data_audit import audit_data_coverage
from fpl_intelligence.validation.edge import run_full_gate
from fpl_intelligence.validation.real_data import (
    audit_season_quality,
    coverage_matrix,
    detect_contamination,
    entity_resolution_report,
    feature_compatibility,
)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("phase475")

DOCS = Path(__file__).resolve().parents[3] / "docs"
DEFAULT_SEASONS = ["2022-23", "2023-24", "2024-25", "2025-26"]
MOCK_SEASONS = ["2022-23", "2023-24", "2024-25", "2025-26", "2026-27"]
MOCK_GATE_SEASONS = ["2022-23", "2023-24", "2024-25"]
MODEL_ORDER = ["baseline_a", "baseline_b", "baseline_c", "baseline_d"]


def _fmt(val) -> str:
    if val is None:
        return "n/a"
    try:
        if val != val:  # NaN
            return "n/a"
    except TypeError:
        pass
    if isinstance(val, float):
        return f"{val:.4f}"
    return str(val)


def _pct(val) -> str:
    if val is None:
        return "n/a"
    try:
        if val != val:
            return "n/a"
    except TypeError:
        pass
    return f"{100.0 * float(val):.1f}%"


def _write_json(obj: dict[str, Any], rel: str) -> None:
    path = DOCS / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str)[:2_000_000], encoding="utf-8")
    print(f"  wrote {path}")


def build_db() -> sessionmaker:
    engine = create_engine("sqlite:///:memory:", echo=False)

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection, connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _import_seasons(db: Session, provider, seasons: list[str]) -> list[str]:
    imported: list[str] = []
    for season in seasons:
        try:
            import_season(db=db, provider=provider, season_code=season)
            db.commit()
            imported.append(season)
            print(f"  imported {season}")
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            print(f"  FAILED to import {season}: {exc}")
    return imported

def run_real_gate(seasons: list[str], write_reports: bool = True) -> dict[str, Any]:
    """Import real data, run the exact Phase 4.5 gate, write deliverables."""
    t0 = time.time()
    SessionLocal = build_db()
    db = SessionLocal()
    provider = RealFPLProvider(seasons=seasons)
    print(f"Loading REAL historical seasons {seasons} from {provider.provenance.source_name} ...")
    imported = _import_seasons(db, provider, seasons)

    real_results: dict[str, Any] = {}
    if imported:
        # Data audit + quality + coverage + entity resolution + temporal + contamination
        audit = audit_data_coverage(db)
        quality = {s: audit_season_quality(db, s).to_dict() for s in imported}
        coverage = coverage_matrix(db, imported)
        entity = entity_resolution_report(db, imported)
        temporal = {ds: classify_provider(provider.provider_name, ds).__dict__ for ds in
                    ["teams", "players", "fixtures", "fpl_history", "fpl_snapshots"]}
        contamination = detect_contamination(db, imported)
        features = feature_compatibility(db, imported)

        print(f"\nRunning the EXACT Phase 4.5 gate on REAL data {imported} ...")
        gate = run_full_gate(db, imported)
        # Correct the provenance label (methodology unchanged).
        gate["data_provenance"] = {
            "provider": provider.provider_name,
            "environment": DataEnvironment.REAL.value,
            "data_type": "real historical FPL data (vaastav mirror)",
            "seasons_loaded": imported,
            "source_url": provider.provenance.url,
            "retrieval_date": provider.provenance.retrieval_date,
        }
        gate["rows_built"] = gate.get("rows_built", 0)
        real_results = {
            "gate": gate,
            "audit": audit.to_dict(),
            "quality": quality,
            "coverage": coverage,
            "entity": entity,
            "temporal": temporal,
            "contamination": contamination.__dict__,
            "features": features,
            "imported_seasons": imported,
            "requested_seasons": seasons,
            "elapsed": round(time.time() - t0, 1),
        }
        if write_reports:
            _write_quality_reports(quality)
            _write_coverage_report(coverage)
            _write_entity_report(entity, provider)
            _write_temporal_section(temporal, provider)
    db.close()
    return real_results


def run_mock_gate(seasons: list[str]) -> dict[str, Any]:
    """Import mock data and run the exact Phase 4.5 gate (for comparison)."""
    SessionLocal = build_db()
    db = SessionLocal()
    provider = MockHistoricalDataProvider()
    print(f"Loading MOCK (synthetic) seasons {MOCK_SEASONS} ...")
    _import_seasons(db, provider, MOCK_SEASONS)
    print(f"Running Phase 4.5 gate on MOCK data {seasons} ...")
    gate = run_full_gate(db, seasons)
    gate["data_provenance"] = {
        "provider": "mock_historical",
        "environment": DataEnvironment.MOCK.value,
        "data_type": "synthetic/generated mock data",
        "seasons_loaded": seasons,
    }
    db.close()
    return gate

def _write_quality_reports(quality: dict[str, dict]) -> None:
    dqdir = DOCS / "data-quality"
    dqdir.mkdir(parents=True, exist_ok=True)
    for season, dq in quality.items():
        path = dqdir / f"{season}.md"
        lines = [f"# Data Quality Report — {season}", ""]
        lines.append(f"_Generated {datetime.now(UTC).isoformat()}_")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        for k, v in dq.items():
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v) if v else "none"
            lines.append(f"| {k} | {v} |")
        path.write_text("\n".join(lines), encoding="utf-8")
        print(f"  wrote {path}")


def _write_coverage_report(coverage: list[dict]) -> None:
    path = DOCS / "real-data-coverage-matrix.md"
    lines = ["# Real Data Coverage Matrix", ""]
    lines.append(f"_Generated {datetime.now(UTC).isoformat()}_")
    lines.append("")
    cols = ["season", "fpl", "fixtures", "player_stats", "team_stats", "ownership", "xg", "coverage_pct"]
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("| " + " | ".join(["---"] * len(cols)) + " |")
    for row in coverage:
        lines.append("| " + " | ".join(str(row.get(c, "")) for c in cols) + " |")
    lines.append("")
    lines.append("Machine-readable copy: `docs/real-data-coverage-matrix.json`")
    path.write_text("\n".join(lines), encoding="utf-8")
    (DOCS / "real-data-coverage-matrix.json").write_text(
        json.dumps(coverage, indent=2, default=str), encoding="utf-8"
    )
    print(f"  wrote {path}")


def _write_entity_report(entity: dict, provider) -> None:
    path = DOCS / "real-data-entity-resolution.md"
    lines = ["# Real-Data Entity Resolution", ""]
    lines.append(f"_Generated {datetime.now(UTC).isoformat()}_")
    lines.append("")
    lines.append(f"## Provider: {provider.provider_name}")
    lines.append("")
    lines.append(f"- Matched players (external-id mappings): {entity.get('matched_players', 0)}")
    lines.append(f"- Matched teams (external-id mappings): {entity.get('matched_teams', 0)}")
    lines.append(f"- Unmatched players: {len(entity.get('unmatched_players', []))}")
    lines.append(f"- Ambiguous players: {len(entity.get('ambiguous_players', []))}")
    lines.append(f"- Unmatched teams: {len(entity.get('unmatched_teams', []))}")
    lines.append(f"- Manual overrides: {len(entity.get('manual_overrides', []))}")
    lines.append("")
    lines.append("### Method")
    lines.append("Canonical identity is the FPL `element` ID (provider-id mapping).")
    lines.append("Players are NEVER merged on name alone. Cross-provider joins use")
    lines.append("deterministic name normalization + explicit manual overrides, with an")
    lines.append("unresolved queue so nothing is silently dropped.")
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  wrote {path}")


def _write_temporal_section(temporal: dict, provider) -> None:
    # Appended into the audit doc; written separately for traceability.
    path = DOCS / "phase475-temporal-classification.json"
    path.write_text(json.dumps(temporal, indent=2, default=str), encoding="utf-8")
    print(f"  wrote {path}")

def _classify_edge(real_gate: dict, real_results: dict) -> str:
    """Classify real predictive edge: A / B / C (Section 19)."""
    imported = real_results.get("imported_seasons", [])
    baselines = real_gate.get("baselines", {}).get("aggregate", {})
    if not imported or not baselines:
        return "A"
    best = max(
        (baselines.get(m, {}).get("spearman", 0.0) for m in ["baseline_a", "baseline_b", "baseline_c"]),
        default=0.0,
    )
    contaminated = real_results["contamination"].get("passed", False)
    if not contaminated:
        return "A"
    if len(imported) >= 2 and best > 0.10:
        return "B"
    return "A"


def write_real_vs_mock_report(real: dict, mock: dict) -> None:
    path = DOCS / "phase475-real-vs-mock-report.md"
    lines = ["# Phase 4.75 — Real vs Mock Report", ""]
    lines.append(f"_Generated {datetime.now(UTC).isoformat()}_")
    lines.append("")
    lines.append("## Data coverage")
    lines.append("")
    lines.append(f"- Real seasons imported: {real.get('imported_seasons', [])}")
    lines.append(f"- Real rows built: {real['gate'].get('rows_built', 0)}")
    lines.append(f"- Mock rows built: {mock.get('rows_built', 0)}")
    lines.append("")
    lines.append("## Baseline performance comparison")
    lines.append("")
    rb = real["gate"].get("baselines", {}).get("aggregate", {})
    mb = mock.get("baselines", {}).get("aggregate", {})
    lines.append("| Model | Real MAE | Real Spearman | Mock MAE | Mock Spearman |")
    lines.append("|---|---|---|---|---|")
    for m in ["baseline_a", "baseline_b", "baseline_c"]:
        lines.append(
            f"| {m} | {rb.get(m, {}).get('mae', 'n/a')} | {rb.get(m, {}).get('spearman', 'n/a')} "
            f"| {mb.get(m, {}).get('mae', 'n/a')} | {mb.get(m, {}).get('spearman', 'n/a')} |"
        )
    lines.append("")
    lines.append("## Minutes model (start ECE)")
    lines.append("")
    rm = real["gate"].get("minutes", {}).get("aggregate", {})
    mm = mock.get("minutes", {}).get("aggregate", {})
    lines.append(f"- Real start ECE: {rm.get('start_ece', 'n/a')}")
    lines.append(f"- Mock start ECE: {mm.get('start_ece', 'n/a')}")
    lines.append("")
    lines.append("## Contamination / leakage")
    lines.append("")
    for k, v in real["contamination"].get("checks", {}).items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## Edge classification")
    lines.append("")
    lines.append(f"**{_classify_edge(real['gate'], real)}**")
    lines.append("")
    lines.append("## Interpretation notes")
    lines.append("")
    lines.append("- Differences are NOT automatically model improvement.")
    lines.append("- Possible explanations: synthetic-data bias, real-data noise, missing features,")
    lines.append("  model weakness, provider limitations.")
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  wrote {path}")

def write_final_report(real: dict, mock: dict) -> None:
    path = DOCS / "phase475-final-report.md"
    cls = _classify_edge(real["gate"], real)
    cont = real["contamination"]
    go = "CONDITIONAL_GO" if cls in ("A", "B") else "GO"
    lines = ["# Phase 4.75 — Final Report", ""]
    lines.append(f"_Generated {datetime.now(UTC).isoformat()}_")
    lines.append("")
    lines.append("## Real data sources")
    lines.append("")
    lines.append(f"- {real['gate']['data_provenance']['source_url']}")
    lines.append("- vaastav/Fantasy-Premier-League public GitHub mirror (teams, fixtures,")
    lines.append("  players_raw, per-gameweek gw*.csv with xG/xA, price, ownership-count, transfers).")
    lines.append("")
    lines.append("## Seasons imported")
    lines.append("")
    lines.append(f"{real.get('imported_seasons', [])}")
    lines.append("")
    lines.append("## Dataset coverage")
    lines.append("")
    for row in real.get("coverage", []):
        lines.append(
            f"- {row['season']}: {row['coverage_pct']}% "
            f"(FPL={row['fpl']}, xG={row['xg']}, ownership={row['ownership']})"
        )
    lines.append("")
    lines.append("## Temporal integrity")
    lines.append("")
    for ds, prof in real.get("temporal", {}).items():
        lines.append(f"- {ds}: {prof.get('temporal_class')} — {str(prof.get('rationale'))[:80]}...")
    lines.append("")
    lines.append("## Phase 4.5 revalidation (real)")
    lines.append("")
    rb = real["gate"].get("baselines", {}).get("aggregate", {})
    for m in ["baseline_a", "baseline_b", "baseline_c"]:
        lines.append(
            f"- {m}: MAE={rb.get(m, {}).get('mae')}, Spearman={rb.get(m, {}).get('spearman')}"
        )
    lines.append("")
    lines.append("## Predictive edge classification")
    lines.append("")
    lines.append(f"**{cls}**")
    lines.append("")
    lines.append("## Leakage audit")
    lines.append("")
    for k, v in cont.get("checks", {}).items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## Synthetic contamination")
    lines.append("")
    lines.append(f"{'PASS' if cont.get('passed') else 'FAIL'}")
    lines.append("")
    lines.append("## GO / CONDITIONAL GO / NO-GO")
    lines.append("")
    lines.append(f"**{go}**")
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    if cls == "A":
        lines.append("Real-data infrastructure is in place but edge is not yet demonstrated (A).")
        lines.append("Do NOT start Phase 5. Improve data completeness (pre-deadline snapshots,")
        lines.append("understat team stats) and re-validate before any edge claim.")
    elif cls == "B":
        lines.append("Preliminary real predictive signal detected (B). More validation required.")
        lines.append("Consider a restricted-feature CONDITIONAL GO before Phase 5.")
    else:
        lines.append("Strong real predictive evidence (C). May proceed toward Phase 5.")
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  wrote {path}")

def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 4.75 real-data revalidation gate.")
    parser.add_argument(
        "--seasons", nargs="*", default=DEFAULT_SEASONS,
        help="Real seasons to import + evaluate (default: 2022-23..2025-26).",
    )
    parser.add_argument("--no-mock", action="store_true", help="Skip the mock comparison run.")
    args = parser.parse_args()

    print("=" * 70)
    print("PHASE 4.75 — REAL HISTORICAL DATA INTEGRATION + REVALIDATION")
    print("=" * 70)
    real = run_real_gate(args.seasons, write_reports=True)
    mock = {} if args.no_mock else run_mock_gate(MOCK_GATE_SEASONS)

    if real and mock:
        write_real_vs_mock_report(real, mock)
        write_final_report(real, mock)
    elif real:
        write_final_report(real, mock)

    if real:
        _write_json(real["gate"], "phase475-real-results.json")
        if mock:
            _write_json(mock, "phase475-mock-results.json")
        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        print(f"Real seasons imported: {real['imported_seasons']}")
        print(f"Real rows built: {real['gate'].get('rows_built', 0)}")
        print(f"Contamination: {real['contamination'].get('passed')}")
        print(f"Edge class: {_classify_edge(real['gate'], real)}")
        print(f"Elapsed: {real['elapsed']}s")


if __name__ == "__main__":
    main()

