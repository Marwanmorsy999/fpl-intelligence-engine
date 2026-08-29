from __future__ import annotations

from fpl_intelligence.availability.historical.pit_audit import PITAuditReport


def test_empty_audit_fails_signal_closed() -> None:
    report = PITAuditReport()
    assert report.chronology_rate == 0.0
    assert report.hard_out_signal_ok is False
    assert report.comparative_signal_ok is False


def test_hard_out_signal_requires_nontrivial_sample() -> None:
    report = PITAuditReport(hard_out_rows=9, hard_out_mean_minutes=0.0)
    assert report.hard_out_signal_ok is False


def test_hard_out_signal_accepts_near_zero_minutes() -> None:
    report = PITAuditReport(
        event_count=12,
        timestamp_complete=12,
        gameweek_linked=12,
        hard_out_rows=10,
        hard_out_mean_minutes=0.0,
    )
    assert report.chronology_rate == 1.0
    assert report.hard_out_signal_ok is True


def test_comparative_signal_requires_material_control_sample_and_positive_delta() -> None:
    report = PITAuditReport(
        restricted_rows=20,
        control_rows=50,
        restricted_mean_minutes=5.0,
        control_mean_minutes=35.0,
        minutes_delta=30.0,
    )
    assert report.comparative_signal_ok is True


def test_comparative_signal_fails_without_positive_delta() -> None:
    report = PITAuditReport(
        restricted_rows=20,
        control_rows=50,
        restricted_mean_minutes=40.0,
        control_mean_minutes=35.0,
        minutes_delta=-5.0,
    )
    assert report.comparative_signal_ok is False
