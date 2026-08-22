"""Hotfix v1.2.1 — Vercel runtime import simulation for the src layout.

The production 500s were caused by ``ModuleNotFoundError: No module named
'fpl_intelligence'``: Vercel's Python runtime imports ``api/index.py`` with the
*project root* on ``sys.path``, while the package actually lives in
``src/fpl_intelligence``. These tests reconstruct a minimal, Vercel-like
function bundle in a temp directory and prove the entrypoint imports cleanly
from every layout Vercel may use.

The bundle deliberately contains only what ``vercel.json`` ships (``api/`` plus
``src/``) — no ``pyproject.toml``, no ``.env``, no installed distribution — so a
successful import can only come from the ``sys.path`` bootstrap in
``api/index.py``. Every probe runs in a subprocess with a scrubbed environment
and with the socket layer disabled, which proves the import needs neither
secrets nor network access.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_PY = REPO_ROOT / "api" / "index.py"
PACKAGE_DIR = REPO_ROOT / "src" / "fpl_intelligence"

#: Environment variables that must never be required to import the app.
SECRET_ENV_NAMES = (
    "DATABASE_URL",
    "CRON_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "FOOTBALL_DATA_ORG_KEY",
    "API_FOOTBALL_KEY",
)

#: Keys the interpreter itself needs on Windows/POSIX to start at all.
_OS_ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
)

#: Routes that must exist on the imported app for the deployment to be useful.
REQUIRED_ROUTES = (
    "/",
    "/dashboard",
    "/api/v1/health",
    "/api/v1/intelligence/unresolved",
    "/api/v1/admin/run-scheduler",
    "/api/v1/admin/ingest-fpl",
    "/api/v1/telegram/webhook",
)

# --------------------------------------------------------------------------- #
# Probe script executed inside the simulated runtime
# --------------------------------------------------------------------------- #
_PROBE = r"""
import json
import socket
import sys


def _blocked(*args, **kwargs):
    raise AssertionError("network access attempted while importing api/index.py")


# Any DNS lookup or TCP connect during import is a hard failure.
socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked

mode, index_path = sys.argv[1], sys.argv[2]

if mode == "package":
    # How Vercel imports it: project root on sys.path, `api` as a namespace pkg.
    from api.index import app

    import api.index as entrypoint
elif mode == "file":
    # How a runtime that loads the entrypoint by absolute file path does it.
    import importlib.util

    spec = importlib.util.spec_from_file_location("vercel_entrypoint", index_path)
    entrypoint = importlib.util.module_from_spec(spec)
    sys.modules["vercel_entrypoint"] = entrypoint
    spec.loader.exec_module(entrypoint)
    app = entrypoint.app
else:  # pragma: no cover - guarded by the test parametrisation
    raise SystemExit("unknown probe mode: " + mode)

import fpl_intelligence
from fastapi import FastAPI

print(
    "RESULT_JSON:"
    + json.dumps(
        {
            "is_fastapi": isinstance(app, FastAPI),
            "app_type": type(app).__module__ + "." + type(app).__qualname__,
            "package_file": fpl_intelligence.__file__,
            "entrypoint_file": entrypoint.__file__,
            "sys_path_additions": list(entrypoint.SYS_PATH_ADDITIONS),
            "routes": sorted({getattr(r, "path", "") for r in app.routes}),
            "cwd": __import__("os").getcwd(),
            "env_keys": sorted(__import__("os").environ),
        }
    )
)
"""

_NAIVE_PROBE = r"""
import json
import sys

try:
    import fpl_intelligence
except ModuleNotFoundError:
    payload = {"imported": False, "package_file": None}
else:
    payload = {"imported": True, "package_file": fpl_intelligence.__file__}

print("RESULT_JSON:" + json.dumps(payload))
"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a minimal Vercel-like function bundle (``api/`` + ``src/``)."""
    root = tmp_path_factory.mktemp("vercel_task")
    (root / "api").mkdir()
    shutil.copy2(INDEX_PY, root / "api" / "index.py")
    shutil.copytree(
        PACKAGE_DIR,
        root / "src" / "fpl_intelligence",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    return root


def _scrubbed_env() -> dict[str, str]:
    """A minimal environment: interpreter essentials only, zero secrets."""
    env = {name: os.environ[name] for name in _OS_ENV_ALLOWLIST if name in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_probe(script: str, *args: str, cwd: Path) -> dict:
    """Run ``script`` in a fresh interpreter and return its RESULT_JSON payload."""
    # Fixed argv, never a shell: the probe script is a module-level constant.
    proc = subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=str(cwd),
        env=_scrubbed_env(),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"probe failed (exit {proc.returncode})\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT_JSON:")), None)
    assert line is not None, f"probe produced no RESULT_JSON\nstdout:\n{proc.stdout}"
    return json.loads(line[len("RESULT_JSON:") :])


def _assert_healthy_app(result: dict, bundle: Path) -> None:
    assert result["is_fastapi"] is True, f"app is {result['app_type']}, not FastAPI"
    # The package must be resolved from the bundle, not from an outside install.
    package_file = Path(result["package_file"]).resolve()
    assert package_file.is_relative_to(bundle), (
        f"fpl_intelligence resolved outside the bundle: {package_file}"
    )
    assert str(bundle / "src") in result["sys_path_additions"]
    for route in REQUIRED_ROUTES:
        assert route in result["routes"], f"missing route {route}"


# --------------------------------------------------------------------------- #
# api/index.py must exist and be self-bootstrapping
# --------------------------------------------------------------------------- #
def test_api_index_entrypoint_exists() -> None:
    assert INDEX_PY.is_file(), "api/index.py (the Vercel entrypoint) is missing"


def test_api_index_bootstraps_src_before_importing_package() -> None:
    """The sys.path bootstrap must run *before* the package import."""
    source = INDEX_PY.read_text(encoding="utf-8")
    bootstrap_at = source.index("SYS_PATH_ADDITIONS")
    import_at = source.index("from fpl_intelligence.api.main import app")
    assert bootstrap_at < import_at
    assert '"src"' in source or "'src'" in source
    assert "/var/task" in source, "the Vercel task root must be probed explicitly"
    assert "Path.cwd()" in source, "the working directory must be probed explicitly"


def test_repo_root_import_yields_fastapi_app() -> None:
    """``from api.index import app`` works in-process from the repo root."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from api.index import app  # local import: this is the behaviour under test

    assert isinstance(app, FastAPI)
    assert app.title == "FPL Intelligence Engine"


# --------------------------------------------------------------------------- #
# Vercel-like runtime simulations
# --------------------------------------------------------------------------- #
def test_import_from_task_root_like_vercel(bundle: Path) -> None:
    """``/var/task`` shape: cwd is the bundle root, ``api`` is a namespace pkg."""
    result = _run_probe(_PROBE, "package", str(bundle / "api" / "index.py"), cwd=bundle)
    _assert_healthy_app(result, bundle)
    assert Path(result["cwd"]).resolve() == bundle.resolve()


def test_import_from_api_directory(bundle: Path) -> None:
    """Executed from inside ``api/``: the parent directory must still resolve."""
    result = _run_probe(_PROBE, "file", str(bundle / "api" / "index.py"), cwd=bundle / "api")
    _assert_healthy_app(result, bundle)


def test_import_with_unrelated_working_directory(bundle: Path, tmp_path: Path) -> None:
    """Neither cwd nor sys.path[0] point at the bundle; probing must recover."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = _run_probe(_PROBE, "file", str(bundle / "api" / "index.py"), cwd=elsewhere)
    _assert_healthy_app(result, bundle)
    assert Path(result["cwd"]).resolve() == elsewhere.resolve()


def test_import_requires_no_secrets(bundle: Path) -> None:
    """No secret env var and no dotenv file is needed to import the app."""
    assert not (bundle / ".env").exists()
    result = _run_probe(_PROBE, "package", str(bundle / "api" / "index.py"), cwd=bundle)
    leaked = [name for name in SECRET_ENV_NAMES if name in result["env_keys"]]
    assert not leaked, f"probe environment leaked secrets: {leaked}"
    assert result["is_fastapi"] is True


def test_bundle_alone_does_not_expose_the_src_layout(bundle: Path) -> None:
    """Negative control: without the bootstrap the src layout is unimportable.

    This is exactly the production failure — the bundle root on ``sys.path`` is
    not enough for ``import fpl_intelligence`` — so the bootstrap in
    ``api/index.py`` is what makes the deployment work.
    """
    result = _run_probe(_NAIVE_PROBE, cwd=bundle)
    if result["imported"]:
        # An outside installation may exist (e.g. a dev editable install), but
        # it must never be the bundle's copy.
        resolved = Path(result["package_file"]).resolve()
        assert not resolved.is_relative_to(bundle)
    else:
        assert result["package_file"] is None
