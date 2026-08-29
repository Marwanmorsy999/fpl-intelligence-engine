"""Audit imported fplcache PIT evidence against the validation database."""
from __future__ import annotations

import argparse
import json

from fpl_intelligence.availability.historical.pit_audit import audit_pit_events
from fpl_intelligence.db.session import validation_session_factory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", action="append", dest="seasons", default=[])
    args = parser.parse_args()

    Session = validation_session_factory()
    with Session() as db:
        report = audit_pit_events(db, args.seasons or None)
    payload = report.to_dict()
    print(json.dumps(payload, indent=2, default=str))

    if report.event_count == 0:
        return 2
    if report.strict_safe != report.event_count:
        return 3
    if report.timestamp_complete != report.event_count:
        return 4
    if report.gameweek_linked != report.event_count:
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
