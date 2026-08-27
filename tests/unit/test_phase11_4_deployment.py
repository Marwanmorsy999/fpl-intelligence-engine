"""Phase 11.4 / hotfix v1.2.1 — deployment configuration tests.

Validates the artifacts that power the free, no-credit-card deployment:

* ``vercel.json`` — the ``api/index.py`` serverless function, its runtime
  packaging rules (``includeFiles`` / ``excludeFiles``), cron paths, and the
  rewrites that route the admin scheduler and Telegram webhook to the function.
* ``.vercelignore`` — must not strip the ``src`` layout from the upload.
* ``.github/workflows/scheduler.yml`` — the free hourly scheduler / keep-warm.

These tests only parse the artifacts: no network calls, and no changes to the
quantitative logic in Phases 1-8.

Packaging contract (this is what broke production with
``ModuleNotFoundError: No module named 'fpl_intelligence'``): the app is a src
layout, so the function bundle must ship ``src/**`` and ``api/index.py`` must
put it on ``sys.path``. Both halves are asserted here; the runtime half is
exercised end-to-end in ``test_vercel_runtime_import.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

VERCEL_JSON = REPO_ROOT / "vercel.json"
VERCEL_IGNORE = REPO_ROOT / ".vercelignore"
SCHEDULER_YML = REPO_ROOT / ".github" / "workflows" / "scheduler.yml"

#: The serverless function entrypoint (a src-layout aware shim).
FUNCTION_ENTRY = "api/index.py"
#: How rewrites address that function.
FUNCTION_DESTINATION = "/api/index.py"

REQUIRED_REWRITE_SOURCES = [
    "/api/v1/telegram/webhook",
    "/api/v1/admin/(.*)",
    "/(.*)",
]

#: Runtime files that are *not* part of the installed wheel and therefore have
#: to be shipped explicitly with the function.
REQUIRED_INCLUDE_GLOBS = [
    "src/**",
    "migrations/**",
    "config/**",
    "alembic.ini",
]

# Phase 20.4 — cron consolidation: vercel.json carries EXACTLY ONE cron and
# it targets the consolidated daily job (materialize + syncs + briefs + grade).
REQUIRED_CRON_PATHS = [
    "/api/v1/admin/daily",
]

#: Credential shapes that must never be committed in deployment artifacts.
SECRET_PATTERNS = [
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-.]{8,}"),
    re.compile(r"(?i)(secret|token|password|api[_-]?key)\"?\s*[:=]\s*\"?[A-Za-z0-9_\-]{12,}"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"gh[pous]_[A-Za-z0-9]{20,}"),
    re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:@/\s]+:[^@/\s]+@"),
    re.compile(r"\b\d{8,}:[A-Za-z0-9_\-]{30,}\b"),  # Telegram bot token
]


def _load_vercel() -> dict:
    assert VERCEL_JSON.exists(), "vercel.json is missing from repo root"
    return json.loads(VERCEL_JSON.read_text(encoding="utf-8"))


def _function_config() -> dict:
    cfg = _load_vercel()
    functions = cfg.get("functions", {})
    assert FUNCTION_ENTRY in functions, (
        f"vercel.json must configure the {FUNCTION_ENTRY} serverless function; "
        f"found keys: {sorted(functions)}"
    )
    return functions[FUNCTION_ENTRY]


def _load_scheduler() -> dict:
    assert SCHEDULER_YML.exists(), "scheduler.yml is missing"
    return yaml.safe_load(SCHEDULER_YML.read_text(encoding="utf-8"))


def _scheduler_on(wf: dict) -> dict:
    # NOTE: PyYAML (YAML 1.1) parses the bare key `on:` as the boolean True.
    # GitHub's own parser handles it correctly; we normalise here so the
    # artifact validates the same regardless of parser quirk.
    on = wf.get("on", wf.get(True, {}))
    return on if isinstance(on, dict) else {}


# --------------------------------------------------------------------------- #
# vercel.json — structure
# --------------------------------------------------------------------------- #
def test_vercel_json_is_valid_json() -> None:
    _load_vercel()


def test_vercel_json_declares_platform_version() -> None:
    assert _load_vercel().get("version") == 2


def test_vercel_json_references_the_config_schema() -> None:
    schema = _load_vercel().get("$schema", "")
    assert "vercel.json" in schema, "keep the $schema hint for config validation"


def test_vercel_json_build_command_installs_the_project() -> None:
    """The build must install the package (or its pinned requirements)."""
    build = _load_vercel().get("buildCommand", "").strip()
    assert build.startswith("pip install"), f"unexpected buildCommand: {build!r}"
    install_part = build.split("&&")[0].strip()
    assert install_part.endswith(".") or "requirements.txt" in install_part, (
        f"buildCommand must install this project: {build!r}"
    )


def test_vercel_json_build_command_applies_migrations() -> None:
    """v2.7.4-prod-heal: the deploy step migrates the prod DB explicitly.

    The 0021 gap (missing ``local_squad_state``) 500'd /league and
    /league/trajectory; the schema must move with the code, never behind it.
    """
    build = _load_vercel().get("buildCommand", "")
    assert "python -m fpl_intelligence.prod_migrate" in build, (
        f"buildCommand must run the migration step: {build!r}"
    )


def test_vercel_json_does_not_mix_functions_with_legacy_builds() -> None:
    """``functions`` and the legacy ``builds`` key are mutually exclusive."""
    cfg = _load_vercel()
    assert "builds" not in cfg
    assert cfg.get("functions"), "the serverless function must be configured"


# --------------------------------------------------------------------------- #
# vercel.json — the api/index.py serverless function
# --------------------------------------------------------------------------- #
def test_function_entrypoint_file_exists() -> None:
    entry = REPO_ROOT / FUNCTION_ENTRY
    assert entry.is_file(), f"{FUNCTION_ENTRY} is missing"
    assert "app" in entry.read_text(encoding="utf-8")


def test_function_does_not_pin_a_broken_runtime_version() -> None:
    """v2.7.4-prod-heal: the explicit @vercel/python@x.y.z pin is omitted.

    The pinned runtime broke deploys twice (pin-version-mismatch, then a
    peer-dependency conflict in Vercel's builder image). Vercel auto-detects
    Python entrypoints; the pin adds no value and only couples us to
    platform-internal builder versions.
    """
    fn = _function_config()
    assert "runtime" not in fn or "@vercel/python" in str(fn.get("runtime")), (
        f"unexpected runtime override: {fn.get('runtime')!r}"
    )


def test_function_pins_region_and_max_duration() -> None:
    fn = _function_config()
    assert fn.get("regions"), "pin the function region to keep DB latency low"
    assert isinstance(fn.get("maxDuration"), int)
    assert 1 <= fn["maxDuration"] <= 300


# --------------------------------------------------------------------------- #
# vercel.json — runtime packaging (the ModuleNotFoundError root cause)
# --------------------------------------------------------------------------- #
def test_function_declares_include_files() -> None:
    include = _function_config().get("includeFiles", "")
    assert include, (
        "the function must declare includeFiles; without it the src layout can "
        "be dropped from the bundle and the runtime raises "
        "ModuleNotFoundError: No module named 'fpl_intelligence'"
    )


@pytest.mark.parametrize("glob", REQUIRED_INCLUDE_GLOBS)
def test_function_includes_required_runtime_files(glob: str) -> None:
    include = _function_config().get("includeFiles", "")
    assert glob in include, f"includeFiles must ship {glob}"


def test_function_ships_the_src_layout_package() -> None:
    """``src/**`` must be bundled: it is the only copy of the package source."""
    include = _function_config().get("includeFiles", "")
    assert "src/**" in include
    assert (REPO_ROOT / "src" / "fpl_intelligence" / "__init__.py").is_file()


def test_function_excludes_data_and_tests() -> None:
    exclude = _function_config().get("excludeFiles", "")
    assert "data" in exclude and "tests" in exclude


def test_function_exclusions_do_not_strip_runtime_sources() -> None:
    """Bundle trimming must never remove the package or its runtime configs."""
    exclude = _function_config().get("excludeFiles", "")
    for protected in ("src/**", "migrations/**", "config/**", "alembic.ini"):
        assert protected not in exclude, f"excludeFiles must not drop {protected}"


def test_vercelignore_keeps_the_package_sources() -> None:
    assert VERCEL_IGNORE.exists(), ".vercelignore is missing"
    ignored = {
        line.strip().rstrip("/")
        for line in VERCEL_IGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for protected in ("src", "api", "migrations", "config", "alembic.ini"):
        assert protected not in ignored, f".vercelignore must not exclude {protected}"


# --------------------------------------------------------------------------- #
# vercel.json — routing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("source", REQUIRED_REWRITE_SOURCES)
def test_vercel_json_rewrites_route_to_the_function(source: str) -> None:
    rewrites = _load_vercel().get("rewrites", [])
    match = next((r for r in rewrites if r.get("source") == source), None)
    assert match is not None, f"missing rewrite for {source}"
    assert match.get("destination") == FUNCTION_DESTINATION


def test_vercel_json_handles_telegram_webhook() -> None:
    sources = [r.get("source") for r in _load_vercel().get("rewrites", [])]
    assert "/api/v1/telegram/webhook" in sources


def test_vercel_json_handles_admin_run_scheduler() -> None:
    sources = [r.get("source") for r in _load_vercel().get("rewrites", [])]
    assert any(s.startswith("/api/v1/admin/") for s in sources)


def test_catch_all_rewrite_is_declared_last() -> None:
    """The ``/(.*)`` catch-all must not shadow the explicit rewrites."""
    sources = [r.get("source") for r in _load_vercel().get("rewrites", [])]
    assert sources[-1] == "/(.*)"


# --------------------------------------------------------------------------- #
# vercel.json — cron jobs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", REQUIRED_CRON_PATHS)
def test_vercel_cron_paths(path: str) -> None:
    crons = _load_vercel().get("crons", [])
    match = next((c for c in crons if c.get("path") == path), None)
    assert match is not None, f"missing cron for {path}"
    assert len(str(match.get("schedule", "")).split()) == 5, "invalid cron expression"


def test_vercel_cron_paths_are_routed_to_the_function() -> None:
    """Every cron path must be matched by a rewrite, or the cron 404s."""
    rewrites = _load_vercel().get("rewrites", [])
    patterns = [re.compile(f"^{r['source']}$") for r in rewrites if r.get("source")]
    for path in REQUIRED_CRON_PATHS:
        assert any(p.match(path) for p in patterns), f"cron {path} is not routed"


# --------------------------------------------------------------------------- #
# No secrets in deployment artifacts
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("artifact", [VERCEL_JSON, Path("api/index.py")])
def test_deployment_artifacts_contain_no_secrets(artifact: Path) -> None:
    path = artifact if artifact.is_absolute() else REPO_ROOT / artifact
    text = path.read_text(encoding="utf-8")
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(text), (
            f"{path.name} appears to contain a hardcoded credential matching {pattern.pattern!r}"
        )


def test_admin_auth_is_environment_driven() -> None:
    """The cron/admin secret must come from the environment, never the config."""
    admin_route = REPO_ROOT / "src" / "fpl_intelligence" / "api" / "routes" / "admin.py"
    source = admin_route.read_text(encoding="utf-8")
    # Audit 2026-08: reads via os.environ with constant-time comparison.
    assert 'os.environ.get("CRON_SECRET"' in source
    assert "hmac.compare_digest" in source
    assert "CRON_SECRET" not in VERCEL_JSON.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# .github/workflows/scheduler.yml — Phase 20.4 contract
#
# vercel.json owns THE single schedule (10 6 * * * UTC → /admin/daily). The
# workflow is a manual-dispatch trigger for the same consolidated job; no
# scheduled crons and no legacy endpoints remain.
# --------------------------------------------------------------------------- #
def test_scheduler_yaml_is_valid_yaml() -> None:
    _load_scheduler()


def test_scheduler_has_no_schedule_triggers_left() -> None:
    wf = _load_scheduler()
    assert not _scheduler_on(wf).get("schedule"), (
        "scheduler.yml must carry no schedule — vercel.json's single cron owns timing"
    )


def test_scheduler_has_workflow_dispatch() -> None:
    wf = _load_scheduler()
    assert "workflow_dispatch" in _scheduler_on(wf)


def test_scheduler_defines_run_daily_job() -> None:
    wf = _load_scheduler()
    assert "run-daily" in wf.get("jobs", {})


def test_run_daily_posts_to_daily_endpoint() -> None:
    wf = _load_scheduler()
    run = wf["jobs"]["run-daily"]["steps"][0]["run"]
    lowered = run.lower()
    assert "post" in lowered
    assert "/api/v1/admin/daily" in lowered


def test_run_daily_sends_bearer_auth_header() -> None:
    wf = _load_scheduler()
    step = wf["jobs"]["run-daily"]["steps"][0]
    run = step["run"]
    lowered = run.lower()
    assert "authorization: bearer" in lowered
    assert "$cron_secret" in lowered
    env = step.get("env", {})
    assert "secrets.cron_secret" in str(env.get("CRON_SECRET", "")).lower()


def test_run_daily_uses_deploy_url_secret() -> None:
    wf = _load_scheduler()
    env = wf["jobs"]["run-daily"]["steps"][0].get("env", {})
    assert "VERCEL_DEPLOY_URL" in env
    assert "secrets.vercel_deploy_url" in str(env["VERCEL_DEPLOY_URL"]).lower()
