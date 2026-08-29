from __future__ import annotations

from fpl_intelligence.availability.historical.signal_lift import SignalLiftReport


def test_signal_report_requires_real_comparative_lift_or_hard_out_signal() -> None:
    report = SignalLiftReport(
        matched_rows=20,
        control_rows=20,
        restricted_rows=10,
        available_rows=20,
        restricted_mean_minutes=5.0,
        available_mean_minutes=70.0,
        hard_out_mean_minutes=0.0,
        by_status={"out": {"n": 10}},
    )
    assert report.to_dict()["signal_direction_ok"] is True


def test_signal_report_does_not_treat_missing_control_as_measured_lift() -> None:
    report = SignalLiftReport(
        matched_rows=20,
        control_rows=0,
        restricted_rows=20,
        hard_out_mean_minutes=0.0,
        by_status={"out": {"n": 20}},
    )
    assert report.to_dict()["signal_direction_ok"] is True
    assert report.available_mean_minutes is None
