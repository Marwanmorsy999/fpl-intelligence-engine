"""Integration test configuration: register custom markers."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "network: requires live network access (skipped in CI)")
