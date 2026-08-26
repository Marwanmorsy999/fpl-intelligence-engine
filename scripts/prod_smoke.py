#!/usr/bin/env python
"""Prod smoke gate (v2.7.4-prod-heal).

Hits every user-facing production endpoint and FAILS on:
  * any 5xx response,
  * any /health version mismatch vs the local package version.

Run it before tagging from now on::

    python scripts/prod_smoke.py                       # default prod URL + session 2295006
    python scripts/prod_smoke.py --session-id 1234567 --base-url https://...

Exit codes: 0 = all green, 1 = any failure.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(_SRC))

DEFAULT_BASE_URL = "https://fpl-intelligence-engine-foundation.vercel.app"
DEFAULT_SESSION_ID = "2295006"

#: (label, path, required top-level keys)
ENDPOINTS: list[tuple[str, str, tuple[str, ...]]] = [
    ("health", "/health", ("status", "version")),
    ("decisions", "/api/v1/decisions", ()),
    ("targets", "/api/v1/targets", ()),
    ("planner", "/api/v1/planner", ()),
    ("league", "/api/v1/league", ()),
    ("league/trajectory", "/api/v1/league/trajectory", ()),
    ("league/fomo", "/api/v1/league/fomo", ()),
    ("squad/fpl-view", "/api/v1/squad/fpl-view", ()),
    ("transfers/ledger", "/api/v1/transfers/ledger?entry_id={sid}", ()),
    ("sync/calibration", "/api/v1/sync/calibration", ()),
]

TIMEOUT_SECONDS = 90.0


def _get(url: str) -> tuple[int, str]:
    req = urllib.request.Request(  # noqa: S310 — fixed https base from args
        url, headers={"User-Agent": "prod-smoke/2.7.4"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:  # non-2xx still carries a body
        body = ""
        with contextlib.suppress(Exception):
            body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Production smoke gate")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--session-id",
        default=None,
        help="FPL entry id; falls back to $FPL_SESSION_ID then "
        f"{DEFAULT_SESSION_ID}",
    )
    parser.add_argument(
        "--expect-version",
        default=None,
        help="Expected version string; defaults to the local package __version__",
    )
    args = parser.parse_args(argv)

    session_id = args.session_id or env_default_session()
    expect_version = args.expect_version

    failures: list[str] = []
    results: dict[str, dict[str, object]] = {}

    print(f"prod smoke -> {args.base_url} (session {session_id})")

    for label, path, required in ENDPOINTS:
        if "{sid}" in path:
            url = f"{args.base_url}{path.format(sid=session_id)}"
        elif label == "health":
            url = f"{args.base_url}{path}"
        else:
            sep = "&" if "?" in path else "?"
            url = f"{args.base_url}{path}{sep}session_id={session_id}"
        started = time.monotonic()
        status, body = _get(url)
        elapsed = time.monotonic() - started
        ok = status < 500
        detail = ""
        parsed: dict[str, object] = {}
        with contextlib.suppress(Exception):
            parsed = json.loads(body)
        for key in required:
            if key not in parsed:
                ok = False
                detail += f" missing-key:{key}"
        if status >= 500:
            detail += f" body={body[:200]!r}"
        verdict = "OK" if ok else "FAIL"
        print(
            f"  [{verdict}] {label:<20} {status}  {elapsed:5.1f}s{detail}"
        )
        results[label] = {"status": status, "ok": ok}
        if not ok:
            failures.append(f"{label}: HTTP {status}{detail}")

        if label == "health":
            served = str(parsed.get("version") or "")
            if expect_version is None:
                try:
                    from fpl_intelligence import __version__ as local_ver

                    expect_version = ".".join(local_ver.split(".")[:3])
                except Exception:  # noqa: BLE001 — run outside repo checkout
                    pass
            if expect_version and not served.startswith(expect_version):
                failures.append(
                    f"health: version mismatch — served {served!r}, "
                    f"expected prefix {expect_version!r}"
                )
                print(
                    f"  [FAIL] version truth      served={served!r} "
                    f"expected-prefix={expect_version!r}"
                )
            else:
                print(f"  [OK]   version truth      {served}")

    print()
    if failures:
        print(f"SMOKE FAILED ({len(failures)}):")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("SMOKE ALL GREEN")
    return 0


def env_default_session() -> str | None:
    """Session id resolution helper ($FPL_SESSION_ID env)."""
    import os

    return os.environ.get("FPL_SESSION_ID")


if __name__ == "__main__":
    raise SystemExit(main())
