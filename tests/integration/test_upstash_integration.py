"""Integration test against the live Upstash Redis REST endpoint.

Reads credentials from the local-only ``.env.local`` (gitignored). This
test is **not** hermetic: it requires network access to Upstash and
valid credentials. The two live-credential tests are marked ``network``
and auto-skipped when DNS is unreachable. The third test (local HTTP
server) always runs because it has no DNS dependency.

Run manually with:
    python -m pytest tests/integration/test_upstash_integration.py -v
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fpl_intelligence.cache.shared_cache import CacheEntry, UpstashRestBackend

network = pytest.mark.network


def _read_env_local() -> dict[str, str]:
    path = Path(".env.local")
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _build_backend():
    env = _read_env_local()
    url = env.get("UPSTASH_REDIS_REST_URL", "")
    token = env.get("UPSTASH_REDIS_REST_TOKEN", "")
    if not url or not token:
        pytest.skip("UPSTASH credentials missing in .env.local")
    return UpstashRestBackend(rest_url=url, rest_token=token, timeout=2.0)


def _dns_reachable() -> bool:
    """Best-effort DNS check so we skip cleanly when offline."""
    import socket

    env = _read_env_local()
    host = (env.get("UPSTASH_REDIS_REST_URL", "") or "").split("//")[-1].split(":")[0]
    if not host:
        return False
    try:
        socket.gethostbyname(host)
        return True
    except OSError:
        return False


@pytest.fixture(autouse=True)
def _skip_when_offline(request: pytest.FixtureRequest) -> None:
    # Only auto-skip tests explicitly marked ``network``. The local
    # server test runs regardless because it has no DNS dependency.
    marker = request.node.get_closest_marker("network")
    if marker is None:
        return
    if not _dns_reachable():
        pytest.skip("Upstash host not resolvable from this network")


def test_upstash_set_get_delete_round_trip() -> None:
    backend = _build_backend()
    try:
        key = "fpl:test:integration:probe"
        entry = CacheEntry(value={"hello": "world"}, expires_at=time.time() + 30)
        backend.set_entry(key, entry)
        got = backend.get_entry(key)
        assert got is not None
        assert got.value == {"hello": "world"}
        backend.delete(key)
        assert backend.get_entry(key) is None
    finally:
        backend.close()


test_upstash_set_get_delete_round_trip = network(test_upstash_set_get_delete_round_trip)


def test_upstash_handles_missing_key() -> None:
    backend = _build_backend()
    try:
        # Should be a benign miss — Upstash returns {"result": null}.
        got = backend.get_entry("fpl:test:integration:does-not-exist-key-xyz")
        assert got is None
    finally:
        backend.close()


test_upstash_handles_missing_key = network(test_upstash_handles_missing_key)


# ---------------------------------------------------------------------------
# Local mock-server test (no internet required)
# ---------------------------------------------------------------------------


def test_upstash_against_local_http_server() -> None:
    """Validate the wire format against a real HTTP server (no DNS needed)."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: dict[str, list[object]] = {"paths": [], "auth": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            seen["paths"].append(self.path)
            seen["auth"].append(self.headers.get("Authorization"))
            if self.path.startswith("/get/"):
                key = self.path[len("/get/") :]
                payload = _LOCAL_STORE.get(key)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"result": payload}).encode("utf-8"))
            elif self.path == "/set":
                payload = json.loads(body)
                key = payload[0]
                encoded = payload[1]
                _LOCAL_STORE[key] = encoded
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"result": "OK"}).encode("utf-8"))
            elif self.path.startswith("/del/"):
                key = self.path[len("/del/") :]
                _LOCAL_STORE.pop(key, None)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"result": 1}).encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

    _LOCAL_STORE.clear()
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        backend = UpstashRestBackend(
            rest_url=f"http://127.0.0.1:{port}",
            rest_token="test-token",
            timeout=5.0,
        )
        try:
            entry = CacheEntry(value={"k": "v"}, expires_at=time.time() + 60)
            backend.set_entry("probe", entry)
            got = backend.get_entry("probe")
            assert got is not None
            assert got.value == {"k": "v"}
            backend.delete("probe")
            assert backend.get_entry("probe") is None
            assert "/set" in seen["paths"]
            assert "/get/probe" in seen["paths"]
            assert "/del/probe" in seen["paths"]
            for auth in seen["auth"]:
                assert auth == "Bearer test-token"
        finally:
            backend.close()
    finally:
        server.shutdown()


_LOCAL_STORE: dict[str, str] = {}
