from __future__ import annotations

import json
import lzma
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from fpl_intelligence.availability.historical.chronological import (
    evaluate_materialize_report,
)
from fpl_intelligence.availability.historical.materialize_pit import (
    DeadlineCutoff,
    local_snapshot_path,
    materialize_cutoffs,
)
from fpl_intelligence.availability.historical.signal_lift import evaluate_signal_lift


def _write_snapshot(root: Path, captured_at: datetime, elements: list[dict]) -> Path:
    path = local_snapshot_path(root, captured_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    with lzma.open(path, "wt", encoding="utf-8") as fh:
        json.dump({"elements": elements}, fh)
    return path


def test_chronological_all_eligible_when_snapshot_before_deadline(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    captured = datetime(2025, 8, 15, 6, 0, tzinfo=UTC)
    _write_snapshot(
        root,
        captured,
        [
            {
                "id": 10,
                "team": 1,
                "status": "i",
                "chance_of_playing_this_round": 0,
                "news": "Hamstring",
            }
        ],
    )
    cutoff = DeadlineCutoff(
        season_code="2025-26",
        gameweek=1,
        cutoff=datetime(2025, 8, 15, 16, 0, tzinfo=UTC),
    )
    with patch(
        "fpl_intelligence.availability.historical.materialize_pit.latest_remote_before",
        return_value=(
            captured,
            "https://example.test/0600.json.xz",
        ),
    ):
        report = materialize_cutoffs(root, [cutoff])

    chrono = evaluate_materialize_report(report)
    assert chrono.total_events == 1
    assert chrono.eligible_before_cutoff == 1
    assert chrono.ineligible == 0
    assert chrono.eligibility_rate == 1.0


def test_chronological_flags_ineligible_when_snapshot_after_deadline(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    # Snapshot AFTER the cutoff — should never happen via latest_before, but
    # if forced into the report it must be counted ineligible.
    captured = datetime(2025, 8, 15, 18, 0, tzinfo=UTC)
    _write_snapshot(
        root,
        captured,
        [
            {
                "id": 10,
                "team": 1,
                "status": "d",
                "chance_of_playing_this_round": 50,
                "news": "Knock",
            }
        ],
    )
    cutoff = DeadlineCutoff(
        season_code="2025-26",
        gameweek=1,
        cutoff=datetime(2025, 8, 15, 16, 0, tzinfo=UTC),
    )
    with patch(
        "fpl_intelligence.availability.historical.materialize_pit.latest_remote_before",
        return_value=(captured, "https://example.test/1800.json.xz"),
    ):
        report = materialize_cutoffs(root, [cutoff])

    chrono = evaluate_materialize_report(report)
    assert chrono.total_events == 1
    assert chrono.ineligible == 1
    assert chrono.eligible_before_cutoff == 0


def test_signal_lift_offline_without_db(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    captured = datetime(2025, 8, 15, 6, 0, tzinfo=UTC)
    _write_snapshot(
        root,
        captured,
        [
            {
                "id": 1,
                "team": 1,
                "status": "i",
                "chance_of_playing_this_round": 0,
                "news": "Injured",
            },
            {
                "id": 2,
                "team": 1,
                "status": "d",
                "chance_of_playing_this_round": 25,
                "news": "Doubt",
            },
        ],
    )
    cutoff = DeadlineCutoff(
        season_code="2025-26",
        gameweek=1,
        cutoff=datetime(2025, 8, 15, 16, 0, tzinfo=UTC),
    )
    with patch(
        "fpl_intelligence.availability.historical.materialize_pit.latest_remote_before",
        return_value=(captured, "https://example.test/0600.json.xz"),
    ):
        report = materialize_cutoffs(root, [cutoff])

    lift = evaluate_signal_lift(report, db=None)
    assert lift.db_linked is False
    assert lift.matched_rows == 2
    assert "out" in lift.by_status or "doubtful" in lift.by_status
