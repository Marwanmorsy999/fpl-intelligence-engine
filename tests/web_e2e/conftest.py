"""Shared browser-E2E plumbing for tests/web_e2e.

The pages load four EXTERNAL CDN resources (Google Fonts, fonts.gstatic,
Tailwind CDN, and the Premier League photo/badge CDN). In a normal
development environment these simply load; in a network-restricted runner
they fail and the repo's console-clean guarantee trips on
"Failed to load resource". This autouse fixture stubs exactly those four
hosts with tiny 200 payloads — the tests' route mocks (registered later,
therefore taking precedence) keep full control of every API response, so
no test assertion is affected.

It also marks the Phase 3.4 first-visit tour as done BEFORE the first
script of any page runs. Every test exercises post-onboarding behavior
(entry flows, session restore, ribbon states, …); a fresh Playwright
context would otherwise auto-start the tour, whose full-viewport
`#fplTourBackdrop` overlay (z-index 950) intercepts every click and its
`#inputSection` highlight collides with entry-screen assertions.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import time
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, Route


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session", autouse=True)
def _live_server() -> Iterator[None]:
    """Start uvicorn once for the entire E2E session, then tear it down."""
    # If a server is already running (e.g. dev machine), just use it.
    if _port_open("localhost", 8000):
        yield
        return

    env = {**os.environ, "DATABASE_URL": "sqlite:///./test_e2e.db"}
    proc = subprocess.Popen(
        [
            "python", "-m", "uvicorn",
            "fpl_intelligence.api.main:app",
            "--host", "127.0.0.1",
            "--port", "8000",
            "--log-level", "warning",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait up to 15 s for the server to accept connections.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _port_open("localhost", 8000):
            break
        time.sleep(0.25)
    else:
        proc.kill()
        raise RuntimeError("uvicorn did not start within 15 s")

    yield

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    # Clean up the test DB file if it was created here.
    with contextlib.suppress(FileNotFoundError):
        os.unlink("test_e2e.db")

# 1x1 transparent PNG — stands in for player photos / team badges.
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x9a\x91\x0b\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture(autouse=True)
def _stub_external_cdns(page: Page) -> Iterator[None]:
    # Suppress the first-visit tour in every fresh test context (see module
    # docstring). Runs before any page script, on every navigation.
    page.add_init_script(
        "localStorage.setItem('fpl_tour_done_v1', '1');"
    )

    def _css(route: Route) -> None:
        route.fulfill(status=200, content_type="text/css", body=b"/* e2e stub */")

    def _js(route: Route) -> None:
        route.fulfill(status=200, content_type="application/javascript", body=b"/* e2e stub */")

    def _font(route: Route) -> None:
        route.fulfill(status=200, content_type="font/woff2", body=b"")

    def _png(route: Route) -> None:
        route.fulfill(status=200, content_type="image/png", body=_PNG_1X1)

    page.route("https://fonts.googleapis.com/**", _css)
    page.route("https://fonts.gstatic.com/**", _font)
    page.route("https://cdn.tailwindcss.com/**", _js)
    page.route("https://resources.premierleague.com/**", _png)
    yield
