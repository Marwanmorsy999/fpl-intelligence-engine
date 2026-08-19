#!/usr/bin/env python
"""scripts/deploy.py — Phase 9.8 Production Deployment CLI.

Validates the production configuration, checks the Dockerfile for production
readiness, wires the monitoring/logging layer, and optionally builds the
production Docker image.

Usage::

    python scripts/deploy.py                       # offline readiness check (default)
    python scripts/deploy.py --check-only            # same, explicit
    python scripts/deploy.py --build                  # readiness check + docker build
    python scripts/deploy.py --config path/production.yaml
    python scripts/deploy.py --build --image-tag v0.9.8

The default ``--check-only`` run is fully offline: it loads the production
config (file + environment), validates it, validates the Dockerfile and reports
the readiness of the monitoring stack. ``--build`` additionally runs the Docker
build through the (mockable) deployment builder.

Secrets (``SLACK_WEBHOOK_URL``, ``SMTP_USERNAME``, ``SMTP_PASSWORD``,
``CRITICAL_ERROR_WEBHOOK_URL``) are read from environment variables only and are
always masked in the report output.

Exit codes: ``0`` ready/build ok, ``1`` usage/configuration error, ``2``
deployment error.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(_SRC))

from fpl_intelligence.deployment.config import DEFAULT_CONFIG_PATH  # noqa: E402
from fpl_intelligence.deployment.runner import (  # noqa: E402
    DeploymentReport,
    DeploymentRunner,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_DEPLOY = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 9.8 — production configuration check, Dockerfile "
            "readiness validation and (optionally) Docker image build."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Production config file (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Build the production Docker image after the readiness checks.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Run the offline readiness checks only (default behaviour).",
    )
    return parser.parse_args(argv)


def _print_readiness(report: DeploymentReport) -> None:
    readiness = report.readiness
    print("=" * 78)
    print("Phase 9.8 deployment readiness")
    print("=" * 78)
    for check in readiness.checks:
        status = "PASS" if check.ok else "FAIL"
        print(f"  [{status}] {check.name}: {check.detail}")
    print("-" * 78)
    print(f"  overall      : {'READY' if readiness.ok else 'NOT READY'}")
    print(f"  config       : {report.config}")
    print(f"  dockerfile   : {'production-ready' if report.dockerfile_ok else 'issues found'}")
    print("=" * 78)


def _print_build(report: DeploymentReport) -> None:
    if report.build is None:
        print("  no build result produced.")
        return
    build = report.build
    status = "SUCCESS" if build.success else "FAILED"
    print(f"  build {status}: {build.image_ref}")
    if build.error:
        print(f"  build error  : {build.error}")
    for line in build.output[-5:]:
        print(f"    {line}")


def _do_check(args: argparse.Namespace) -> int:
    runner = DeploymentRunner(config_path=args.config)
    report = runner.run(build=False)
    _print_readiness(report)
    return EXIT_OK if report.readiness.ok else EXIT_DEPLOY


def _do_build(args: argparse.Namespace) -> int:
    runner = DeploymentRunner(config_path=args.config)
    report = runner.run(build=True)
    _print_readiness(report)
    _print_build(report)
    if report.build is None or not report.build.success or not report.readiness.ok:
        return EXIT_DEPLOY
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.build and args.check_only:
        print("USAGE ERROR: --build and --check-only are mutually exclusive.")
        return EXIT_USAGE
    try:
        if args.build:
            return _do_build(args)
        return _do_check(args)
    except Exception as exc:  # noqa: BLE001 - report deployment errors cleanly
        print(f"DEPLOYMENT ERROR: {exc}")
        return EXIT_DEPLOY


if __name__ == "__main__":
    raise SystemExit(main())