"""Regression test for the dashboard's inline <script>.

Live QA found the dashboard dead because the inline script in
``dashboard.html`` re-declared ``async function loadHealth()`` (malformed,
no closing brace) and never invoked the init functions. The browser aborts
the whole script on the duplicate-declaration SyntaxError, so every section
stays on "Loading...".

This test guards against that class of breakage:

* ``function loadHealth`` must appear exactly once in the inline script;
* the init calls (``loadHealth(); loadUnresolved(); loadSquadDecisions();``)
  must be present so the page actually boots;
* when a ``node`` binary is available, the extracted script must pass
  ``node --check`` (clean parse). Environments without ``node`` skip only
  that sub-check so the suite still passes.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

STATIC_HTML = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "fpl_intelligence"
    / "web"
    / "static"
    / "dashboard.html"
)

SCRIPT_OPEN = "<script>"
SCRIPT_CLOSE = "</script>"


def _extract_inline_script(html: str) -> str:
    """Return the content of the first <script>...</script> block."""
    start = html.index(SCRIPT_OPEN) + len(SCRIPT_OPEN)
    end = html.index(SCRIPT_CLOSE, start)
    return html[start:end]


def test_dashboard_html_exists() -> None:
    assert STATIC_HTML.exists(), f"dashboard.html missing at {STATIC_HTML}"


def test_load_health_declared_exactly_once() -> None:
    html = STATIC_HTML.read_text(encoding="utf-8")
    script = _extract_inline_script(html)
    assert script.count("function loadHealth") == 1, (
        "loadHealth must be declared exactly once; found "
        f"{script.count('function loadHealth')} occurrences"
    )


def test_init_calls_present() -> None:
    html = STATIC_HTML.read_text(encoding="utf-8")
    script = _extract_inline_script(html)
    for call in ("loadHealth();", "loadUnresolved();", "loadSquadDecisions();"):
        assert call in script, f"missing init call: {call}"


def test_inline_script_parses_with_node() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node binary not available; skipping node --check sub-check")

    html = STATIC_HTML.read_text(encoding="utf-8")
    script = _extract_inline_script(html)

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        js_path = fh.name

    try:
        result = subprocess.run(
            [node, "--check", js_path],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"node --check failed:\n{result.stdout}\n{result.stderr}"
        )
    finally:
        Path(js_path).unlink(missing_ok=True)
