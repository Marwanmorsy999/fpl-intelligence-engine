"""Regression test for the dashboard's inline <script>.

Live QA found the dashboard dead because the inline script in
``dashboard.html`` re-declared an async function (malformed, no closing brace)
and never invoked the init functions. The browser aborts the whole script on a
SyntaxError, so every section stays on "Loading...".

Phase 13.0 replaced the old multi-panel script with a single one-click flow
driven by ``analyze()`` (calls ``/api/v1/squad/from-fpl`` then
``/api/v1/decisions``). These guards ensure the new script:

* declares ``function analyze`` exactly once (no duplicate/malformed decl);
* wires the Analyze button + Enter key so the page actually boots;
* calls the one-click import and decisions endpoints;
* parses cleanly under ``node --check`` when a node binary is available.
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
    start = html.index(SCRIPT_OPEN) + len(SCRIPT_OPEN)
    end = html.index(SCRIPT_CLOSE, start)
    return html[start:end]


def test_dashboard_html_exists() -> None:
    assert STATIC_HTML.exists(), f"dashboard.html missing at {STATIC_HTML}"


def test_analyze_declared_exactly_once() -> None:
    html = STATIC_HTML.read_text(encoding="utf-8")
    script = _extract_inline_script(html)
    assert script.count("function analyze") == 1, (
        "analyze must be declared exactly once; found "
        f"{script.count('function analyze')} occurrences"
    )


def test_boot_wiring_present() -> None:
    html = STATIC_HTML.read_text(encoding="utf-8")
    script = _extract_inline_script(html)
    assert 'getElementById("analyzeBtn").addEventListener("click", analyze)' in script, (
        "Analyze button must be wired to the analyze() handler"
    )
    assert 'getElementById("teamId").addEventListener("keydown"' in script, (
        "Enter key must trigger analyze()"
    )


def test_calls_one_click_endpoints() -> None:
    """The one-click flow must hit the import + decisions endpoints."""
    html = STATIC_HTML.read_text(encoding="utf-8")
    script = _extract_inline_script(html)
    assert "/api/v1/squad/from-fpl" in script, "must call POST /api/v1/squad/from-fpl"
    assert "/api/v1/decisions" in script, "must call GET /api/v1/decisions"


def test_play_names_declared_before_manual_save() -> None:
    """Hotfix v1.3.4 — PLAYER_NAMES caused a ReferenceError in saveManual().

    It must be declared exactly once (as ``let``) and populated from the player
    list, so ``saveManual``'s writes and ``render()``'s name lookups never hit an
    undeclared global.
    """
    html = STATIC_HTML.read_text(encoding="utf-8")
    script = _extract_inline_script(html)
    assert script.count("let PLAYER_NAMES") == 1, (
        "PLAYER_NAMES must be declared exactly once; found "
        f"{script.count('let PLAYER_NAMES')} occurrences"
    )
    assert "Object.fromEntries(ALL_PLAYERS.map(p => [p.id, p.web_name]))" in script, (
        "PLAYER_NAMES must be populated from the loaded /api/v1/players list"
    )
    # saveManual must still build the 15-player squad body and POST it.
    assert "player_ids: ids" in script
    assert "captain_id: cap" in script
    assert "vice_captain_id: vice" in script
    assert "POST" in script and "/api/v1/squad" in script


def test_retry_sync_button_wired() -> None:
    """Phase 13.5 — the 503 message shows a 🔄 Try Again button that retries."""
    html = STATIC_HTML.read_text(encoding="utf-8")
    script = _extract_inline_script(html)
    assert "retrySyncBtn" in html
    assert "🔄 Try Again" in html
    assert "/api/v1/squad/retry-sync" in script
    assert 'getElementById("retrySyncBtn").addEventListener("click", retrySync)' in script
    assert 'getElementById("retrySyncBtn").style.display = "inline-block"' in script


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


def test_chain_banner_renders_per_source() -> None:
    """The chain banner must disclose the resolved source_label + data_quality.

    Phase 15.0 honest UI: the banner text changes to reflect whichever chain
    level actually served the numbers. We exercise ``renderChainBanner`` under
    node with a minimal ``document`` stub and assert the banner carries the
    proxy's human-readable label and quality tier — and that market/weather
    note lines appear ONLY when the data is present.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node binary not available; skipping banner render sub-check")

    html = STATIC_HTML.read_text(encoding="utf-8")
    script = _extract_inline_script(html)

    # Minimal DOM stub: the inline script defines its own ``function $`` that
    # calls ``document.getElementById``, and registers ``window.__photoFail`` /
    # ``window.__badgeFail`` at load. We provide ``document`` + ``window`` stubs
    # so the script loads cleanly, then exercise renderChainBanner and read back
    # the banner element's innerHTML from our per-id state map.
    harness = (
        "var $state = {}; "
        "globalThis.window = globalThis.window || {}; "
        "globalThis.document = { getElementById: function(id){ "
        "  if(!$state[id]){ $state[id] = { innerHTML:'', textContent:'' , "
        "    addEventListener: function(){}, style:{}, value:'' }; } "
        "  return $state[id]; } }; "
        + script
        + "; "
        # Proxy-level report (the offline default).
        + "renderChainBanner({ meta: { chain: { source: 'pre-season-proxy-v2',"
        "  source_label: 'Pre-season proxy v2 (price + fixtures + xG + market)',"
        "  data_quality: 'heuristic-proxy-enriched', covered_players: 599,"
        "  notes: { understat_players_matched: 320, weather_severe_fixtures: 2 },"
        "  market_check: { enabled: true, fixtures_matched: 10 } } } });"
        + " var proxyBanner = $state['chainBanner'].innerHTML; "
        # Backtest-level report.
        + "renderChainBanner({ meta: { chain: { source: 'model-backtest',"
        "  source_label: 'Backtest model',"
        "  data_quality: 'historical-backtest', covered_players: 480,"
        "  notes: {},"
        "  market_check: { enabled: false, reason: 'no key' } } } });"
        + " var backtestBanner = $state['chainBanner'].innerHTML; "
        # No chain provenance (static stub path) -> banner must be empty.
        + "renderChainBanner({ meta: {} }); "
        + " var emptyBanner = $state['chainBanner'].innerHTML; "
        + " console.log('PROXY:' + proxyBanner);"
        + " console.log('BACKTEST:' + backtestBanner);"
        + " console.log('EMPTY:' + emptyBanner);"
    )

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(harness)
        js_path = fh.name

    try:
        result = subprocess.run(
            [node, js_path],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"node eval failed:\n{result.stdout}\n{result.stderr}"
        out = result.stdout
        proxy_banner = _extract_console(out, "PROXY:")
        backtest_banner = _extract_console(out, "BACKTEST:")
        empty_banner = _extract_console(out, "EMPTY:")

        # Proxy banner discloses its label, quality, coverage, and the conditional
        # market/weather note lines (present because the data exists).
        assert "Pre-season proxy v2" in proxy_banner
        assert "heuristic-proxy-enriched" in proxy_banner
        assert "599" in proxy_banner
        assert "Market check: agrees" in proxy_banner
        assert "Severe-weather fixtures: 2" in proxy_banner

        # Backtest banner shows a DIFFERENT label per source.
        assert "Backtest model" in backtest_banner
        assert "historical-backtest" in backtest_banner
        # Disabled market check (enabled:false) -> "off" line, NO "agrees" line.
        assert "Market check: off" in backtest_banner
        assert "agrees" not in backtest_banner

        # No chain object -> banner is empty (never fabricate provenance).
        assert empty_banner == ""
    finally:
        Path(js_path).unlink(missing_ok=True)


def _extract_console(output: str, prefix: str) -> str:
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return ""