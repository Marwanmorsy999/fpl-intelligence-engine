from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fpl_intelligence.availability.historical.chronological import evaluate_materialize_report
from fpl_intelligence.availability.historical.materialize_pit import DeadlineCutoff, MaterializedSnapshot, MaterializeReport


def _event(captured_at: datetime) -> dict[str, object]:
    return {
        "player_id": "1",
        "status": "out",
        "timestamps": {"published_at": captured_at.isoformat(), "available_at": captured_at.isoformat()},
    }


def _report(captured_at: datetime, cutoff: datetime) -> MaterializeReport:
    dc = DeadlineCutoff("2024-25", 1, cutoff)
    snapshot = MaterializedSnapshot(dc, captured_at, Path("snapshot.json.xz"), "https://example.invalid/snapshot", 800, 1, [_event(captured_at)])
    return MaterializeReport(snapshots=[snapshot], event_count=1)


def test_chronology_accepts_snapshot_at_or_before_cutoff() -> None:
    cutoff = datetime(2024, 8, 16, 16, tzinfo=UTC)
    report = evaluate_materialize_report(_report(cutoff - timedelta(hours=6), cutoff))
    assert report.total_events == 1
    assert report.eligible_before_cutoff == 1
    assert report.eligibility_rate == 1.0


def test_chronology_rejects_post_deadline_information() -> None:
    cutoff = datetime(2024, 8, 16, 16, tzinfo=UTC)
    report = evaluate_materialize_report(_report(cutoff + timedelta(minutes=1), cutoff))
    assert report.ineligible == 1
    assert report.eligible_before_cutoff == 0
    assert report.eligibility_rate == 0.0


def test_empty_report_fails_closed() -> None:
    report = evaluate_materialize_report(MaterializeReport())
    assert report.eligibility_rate == 0.0
