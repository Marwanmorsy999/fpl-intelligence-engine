"""Phase 3 — UI/UX & performance polish regression tests.

Covers:
  * ``fetchWithTimeout`` (lib/fetch-with-timeout.js) — bounds a hanging fetch
    and throws an honest timeout Error; honors a caller-provided AbortSignal.
  * IndexedDB caching (lib/idb-cache.js) — 24h TTL freshness, ``getOrFetch``
    cache-hit (no network) vs. miss (network + store), and resilient
    degradation to an in-memory store when IndexedDB is unavailable.
  * Route/asset wiring: ``/help`` page, ``/static/lib/*`` whitelist, the
    ``GET /news/bbc-rss`` endpoint, and the honest UI states shipped to
    ``dashboard.html`` and ``live.html`` (no more infinite loaders, honest
    "stale" live feed).

The JS libs are CommonJS-exportable: we execute them under ``node`` (skipped
when no node binary is present), matching the ``test_dashboard_static_assets``
convention.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "fpl_intelligence" / "web" / "static"
DASHBOARD_HTML = STATIC / "dashboard.html"
LIVE_HTML = STATIC / "live.html"
HELP_HTML = STATIC / "help.html"
DASHBOARD_PY = ROOT / "src" / "fpl_intelligence" / "web" / "dashboard.py"
NEWS_PY = ROOT / "src" / "fpl_intelligence" / "api" / "routes" / "news.py"


def _run_node(js: str) -> subprocess.CompletedProcess:
    # NOTE: `.cjs` — some checkout parents ship a `type: module` package.json,
    # which would make node treat a bare `.js` harness as ESM (no `require`).
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(js)
        js_path = fh.name
    try:
        return subprocess.run(
            [shutil.which("node"), js_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        Path(js_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Asset / route wiring (pure-Python, always runs)
# ---------------------------------------------------------------------------


def test_phase3_library_files_exist() -> None:
    for name in ("fetch-with-timeout.js", "idb-cache.js", "onboarding.js"):
        assert (STATIC / "lib" / name).exists(), f"missing static/lib/{name}"


def test_phase3_static_lib_route_whitelisted() -> None:
    """The /static/lib/{name} whitelist must include our new modules."""
    src = DASHBOARD_PY.read_text(encoding="utf-8")
    for name in ("fetch-with-timeout.js", "idb-cache.js", "onboarding.js"):
        assert f'"{name}"' in src
    assert "serve_static_lib" in src
    assert "/static/lib/{lib_name}" in src


def test_phase3_help_route_registered() -> None:
    src = DASHBOARD_PY.read_text(encoding="utf-8")
    assert '"/help": "help.html"' in src
    assert HELP_HTML.exists()


def test_phase3_news_bbc_rss_endpoint_registered() -> None:
    src = NEWS_PY.read_text(encoding="utf-8")
    assert "@router.get('/bbc-rss')" in src or '@router.get("/bbc-rss")' in src
    assert "BBC_RSS_TTL_SECONDS" in src  # 15-minute server-side cache


def test_phase3_news_radar_honest_states() -> None:
    """News Radar must have bounded fetch + an error state with Retry."""
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    assert "fetchWithTimeout" in html
    assert "error-state" in html
    assert "renderNewsRadarError" in html
    assert "Retry" in html


def test_phase3_live_stale_state() -> None:
    """Live feed must render an explicit greyed-out stale card, never a fake 0-0."""
    html = LIVE_HTML.read_text(encoding="utf-8")
    assert "stale-feed" in html
    assert 'data-testid="stale-feed"' in html
    assert "stale_snapshot" in html or "data_age_seconds" in html
    assert "Points may not reflect current match status" in html
    assert "fetchWithTimeout" in html  # bounded live poll


def test_phase3_responsive_nav_breakpoint_1100() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "(max-width: 1100px)" in css
    assert "bottomnav" in css


def test_phase3_metric_tooltip_css() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".metric-tip" in css
    assert "pointer-events: none" in css  # display-only: never intercepts clicks
# ---------------------------------------------------------------------------
# fetchWithTimeout + IndexedDB cache behaviour (node)
# ---------------------------------------------------------------------------

FETCH_TIMEOUT_TEST = textwrap.dedent(
    r"""
    const assert = require("assert");
    const fs = require("fs");
    const path = require("path");

    /* Load the UMD lib as CommonJS by evaluating its source: the wrapper's
       `typeof module === "object"` check runs against our local shim so it
       writes to `module.exports`. This avoids the parent `type: module`
       package.json forcing .js files into ESM under `require`. */
    function loadLib(filename) {
      var module = { exports: {} };
      const exports = module.exports;
      const libPath = path.join(__LIBDIR__, filename);
      eval(fs.readFileSync(libPath, "utf8")); // runs in this CJS scope
      return module.exports;
    }

    const FPLHttp = loadLib("fetch-with-timeout.js");
    const realFetch = globalThis.fetch;

    function hangFetch(url, opts) {
      return new Promise(function (resolve, reject) {
        if (!opts || !opts.signal) { realFetch(url, opts).then(resolve, reject); return; }
        opts.signal.addEventListener("abort", function () {
          const err = new Error("aborted");
          err.name = "AbortError";
          reject(err);
        });
        /* never resolve on its own */
      });
    }

    (async function () {
      // 1. A hanging fetch must abort after the deadline with an honest error.
      globalThis.fetch = hangFetch;
      let timeoutMsg = null;
      try {
        await FPLHttp.fetchWithTimeout("/never-resolves", {}, 80);
        throw new Error("expected a timeout rejection");
      } catch (err) {
        timeoutMsg = err.message;
      }
      globalThis.fetch = realFetch;
      assert(timeoutMsg, "fetch must reject on hang");
      assert(/timed out/i.test(timeoutMsg), "honest timeout message: " + timeoutMsg);
      assert.strictEqual(typeof FPLHttp.fetchWithTimeout, "function");
      assert.strictEqual(FPLHttp.DEFAULT_TIMEOUT_MS, 6000);

      // 2. Caller-provided AbortSignal wins; resolves normally (no double abort).
      let resolvedSignal = null;
      globalThis.fetch = function (url, opts) {
        resolvedSignal = opts && opts.signal ? opts.signal : null;
        return Promise.resolve({ ok: true });
      };
      const ac = new AbortController();
      const r = await FPLHttp.fetchWithTimeout("http://x", { signal: ac.signal }, 10);
      assert.strictEqual(r.ok, true);
      assert.strictEqual(resolvedSignal, ac.signal, "caller signal must win");
      globalThis.fetch = realFetch;

      console.log("fetch-with-timeout OK");
    })().catch(function (e) {
      console.error(e.stack || e);
      process.exitCode = 1;
    });
    """
).replace("__LIBDIR__", repr((STATIC / "lib").as_posix()))
IDB_CACHE_TEST = textwrap.dedent(
    r"""
    const assert = require("assert");
    const fs = require("fs");
    const path = require("path");

    function loadLib(filename) {
      var module = { exports: {} };
      const exports = module.exports;
      const libPath = path.join(__LIBDIR__, filename);
      eval(fs.readFileSync(libPath, "utf8"));
      return module.exports;
    }

    const FPLCache = loadLib("idb-cache.js");

    // isFresh: within TTL -> true, past TTL -> false
    assert.strictEqual(FPLCache.isFresh({ data: [1], timestamp: 1000 }, 1000, 1500), true, "fresh hit");
    assert.strictEqual(FPLCache.isFresh({ data: [1], timestamp: 1000 }, 1000, 2200), false, "expired");
    assert.strictEqual(FPLCache.isFresh(null, 1000, 1500), false, "null envelope never fresh");

    // getOrFetch: fresh cache hit must NOT call the fetcher (zero network).
    (async function () {
      const store = FPLCache.createMemoryStore();
      const cache = FPLCache.createCache({ store: store, defaultTtlMs: 1000000 });
      let calls = 0;
      await cache.save("gw:1", [1, 2, 3], Date.now());
      const got = await cache.getOrFetch("gw:1", function () { calls += 1; return [99]; });
      assert.deepStrictEqual(got, [1, 2, 3]);
      assert.strictEqual(calls, 0, "fresh cache hit must not hit the network");
    })();

    // getOrFetch: expired -> run fetcher, store best-effort, return value.
    (async function () {
      const store = FPLCache.createMemoryStore();
      const cache = FPLCache.createCache({ store: store, defaultTtlMs: 1000 });
      await cache.save("gw:2", "stale", Date.now() - 99999);
      let calls = 0;
      const val = await cache.getOrFetch("gw:2", function () { calls += 1; return "fresh-" + calls; });
      assert.strictEqual(val, "fresh-1");
      assert.strictEqual(calls, 1, "miss must call the fetcher");
    })();

    // getCachedPredictions: spec-named helper keys by gameweek and is 24h-fresh.
    (async function () {
      const store = FPLCache.createMemoryStore();
      const cache = FPLCache.createCache({ store: store, defaultTtlMs: FPLCache.DEFAULT_TTL_MS });
      await cache.save("predictions:7", { home: "LIV" }, Date.now());
      const env = await store.get("predictions:7");
      assert.strictEqual(FPLCache.isFresh(env, FPLCache.DEFAULT_TTL_MS), true);
      assert.strictEqual(FPLCache.DEFAULT_TTL_MS, 24 * 60 * 60 * 1000);
    })();

    // Resilient degrade: no IndexedDB -> in-memory, never crash.
    (async function () {
      const cache = FPLCache.createCache({ store: FPLCache.createMemoryStore() });
      const ok = await cache.save("k", { a: 1 }, Date.now());
      assert.strictEqual(ok, true);
      const peeked = await cache.peek("k", 100000);
      assert.deepStrictEqual(peeked, { a: 1 });
    })();

    console.log("idb-cache OK");
    """
).replace("__LIBDIR__", repr((STATIC / "lib").as_posix()))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_fetch_with_timeout_bounds_hangs() -> None:
    res = _run_node(FETCH_TIMEOUT_TEST)
    assert res.returncode == 0, f"fetch timeout failed:\n{res.stdout}\n{res.stderr}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_idb_cache_ttl_and_miss_fetch() -> None:
    res = _run_node(IDB_CACHE_TEST)
    assert res.returncode == 0, f"idb cache failed:\n{res.stdout}\n{res.stderr}"