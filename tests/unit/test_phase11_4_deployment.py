"""Phase 11.4 / hotfix v1.2.1 — deployment configuration tests."""

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
FUNCTION_ENTRY = "api/index.py"
FUNCTION_DESTINATION = "/api/index.py"
REQUIRED_REWRITE_SOURCES = [
    "/api/v1/telegram/webhook",
    "/api/v1/admin/(.*)",
    "/(.*)",
]
REQUIRED_INCLUDE_GLOBS = ["src/**", "migrations/**", "config/**", "alembic.ini"]
REQUIRED_CRON_PATHS = ["/api/v1/admin/daily"]
SECRET_PATTERNS = [
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-.]{8,}"),
    re.compile(r"(?i)(secret|token|password|api[_-]?key)\"?\s*[:=]\s*\"?[A-Za-z0-9_\-]{12,}"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"gh[pous]_[A-Za-z0-9]{20,}"),
    re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:@/\s]+:[^@/\s]+@"),
    re.compile(r"\b\d{8,}:[A-Za-z0-9_\-]{30,}\b"),
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
    on = wf.get("on", wf.get(True, {}))
    return on if isinstance(on, dict) else {}


def test_vercel_json_is_valid_json() -> None:
    _load_vercel()


def test_vercel_json_declares_platform_version() -> None:
    assert _load_vercel().get("version") == 2


def test_vercel_json_references_the_config_schema() -> None:
    schema = _load_vercel().get("$schema", "")
    assert "vercel.json" in schema


def test_vercel_json_build_command_installs_the_project() -> None:
    build = _load_vercel().get("buildCommand", "").strip()
    assert build == "bash vercel_build.sh"


def test_vercel_build_is_hermetic_and_migrations_are_separate() -> None:
    """A transient production DB outage must never prevent application builds."""
    vercel_build = (REPO_ROOT / "vercel_build.sh").read_text(encoding="utf-8")
    assert "pip install ." in vercel_build
    assert "production migrations are managed separately" in vercel_build
    assert "python -m fpl_intelligence.prod_migrate" not in vercel_build


def test_vercel_json_does_not_mix_functions_with_legacy_builds() -> None:
    cfg = _load_vercel()
    assert "builds" not in cfg
    assert cfg.get("functions")


def test_function_entrypoint_file_exists() -> None:
    entry = REPO_ROOT / FUNCTION_ENTRY
    assert entry.is_file()
    assert "app" in entry.read_text(encoding="utf-8")


def test_function_does_not_pin_a_broken_runtime_version() -> None:
    fn = _function_config()
    assert "runtime" not in fn or "@vercel/python" in str(fn.get("runtime"))


def test_function_pins_region_and_max_duration() -> None:
    fn = _function_config()
    assert fn.get("regions")
    assert isinstance(fn.get("maxDuration"), int)
    assert 1 <= fn["maxDuration"] <= 300


def test_function_declares_include_files() -> None:
    include = _function_config().get("includeFiles", "")
    assert include


@pytest.mark.parametrize("glob", REQUIRED_INCLUDE_GLOBS)
def test_function_includes_required_runtime_files(glob: str) -> None:
    include = _function_config().get("includeFiles", "")
    assert glob in include


def test_function_ships_the_src_layout_package() -> None:
    include = _function_config().get("includeFiles", "")
    assert "src/**" in include
    assert (REPO_ROOT / "src" / "fpl_intelligence" / "__init__.py").is_file()


def test_function_excludes_data_and_tests() -> None:
    exclude = _function_config().get("excludeFiles", "")
    assert "data" in exclude and "tests" in exclude


def test_function_exclusions_do_not_strip_runtime_sources() -> None:
    exclude = _function_config().get("excludeFiles", "")
    for protected in ("src/**", "migrations/**", "config/**", "alembic.ini"):
        assert protected not in exclude


def test_vercelignore_keeps_the_package_sources() -> None:
    assert VERCEL_IGNORE.exists()
    ignored = {
        line.strip().rstrip("/")
        for line in VERCEL_IGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    for protected in ("src", "api", "migrations", "config", "alembic.ini"):
        assert protected not in ignored


@pytest.mark.parametrize("source", REQUIRED_REWRITE_SOURCES)
def test_vercel_json_rewrites_route_to_the_function(source: str) -> None:
    rewrites = _load_vercel().get("rewrites", [])
    match = next((r for r in rewrites if r.get("source") == source), None)
    assert match is not None, f"missing rewrite for {source}"
    destination = str(match.get("destination", ""))
    # Dynamic routes deliberately carry the public path in __route so the ASGI
    # shim can restore it; static destinations remain the same function entry.
    assert destination == FUNCTION_DESTINATION or destination.startswith(FUNCTION_DESTINATION + "?__route=")
    if source == "/api/v1/telegram/webhook":
        assert destination == "/api/index.py?__route=/api/v1/telegram/webhook"
    elif source == "/api/v1/admin/(.*)":
        assert destination == "/api/index.py?__route=/api/v1/admin/$1"
    elif source == "/(.*)":
        assert destination == "/api/index.py?__route=/$1"


def test_vercel_json_handles_telegram_webhook() -> None:
    sources = [r.get("source") for r in _load_vercel().get("rewrites", [])]
    assert "/api/v1/telegram/webhook" in sources


def test_vercel_json_handles_admin_run_scheduler() -> None:
    sources = [r.get("source") for r in _load_vercel().get("rewrites", [])]
    assert any(s.startswith("/api/v1/admin/") for s in sources)


def test_catch_all_rewrite_is_declared_last() -> None:
    sources = [r.get("source") for r in _load_vercel().get("rewrites", [])]
    assert sources[-1] == "/(.*)"


@pytest.mark.parametrize("path", REQUIRED_CRON_PATHS)
def test_vercel_cron_paths(path: str) -> None:
    crons = _load_vercel().get("crons", [])
    match = next((c for c in crons if c.get("path") == path), None)
    assert match is not None
    assert len(str(match.get("schedule", "")).split()) == 5


def test_vercel_cron_paths_are_routed_to_the_function() -> None:
    rewrites = _load_vercel().get("rewrites", [])
    patterns = [re.compile(f"^{r['source']}$") for r in rewrites if r.get("source")]
    for path in REQUIRED_CRON_PATHS:
        assert any(p.match(path) for p in patterns)


@pytest.mark.parametrize("artifact", [VERCEL_JSON, Path("api/index.py")])
def test_deployment_artifacts_contain_no_secrets(artifact: Path) -> None:
    path = artifact if artifact.is_absolute() else REPO_ROOT / artifact
    text = path.read_text(encoding="utf-8")
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(text), f"{path.name} appears to contain a hardcoded credential"


def test_admin_auth_is_environment_driven() -> None:
    admin_route = REPO_ROOT / "src" / "fpl_intelligence" / "api" / "routes" / "admin.py"
    source = admin_route.read_text(encoding="utf-8")
    assert 'os.environ.get("CRON_SECRET"' in source
    assert "hmac.compare_digest" in source
    assert "CRON_SECRET" not in VERCEL_JSON.read_text(encoding="utf-8")


def test_scheduler_yaml_is_valid_yaml() -> None:
    _load_scheduler()


def test_scheduler_has_no_schedule_triggers_left() -> None:
    wf = _load_scheduler()
    assert not _scheduler_on(wf).get("schedule")


def test_scheduler_has_workflow_dispatch() -> None:
    wf = _load_scheduler()
    assert "workflow_dispatch" in _scheduler_on(wf)


def test_scheduler_defines_run_daily_job() -> None:
    wf = _load_scheduler()
    assert "run-daily" in wf.get("jobs", {})


def _all_step_runs(wf: dict) -> str:
    return "\n".join(
        str(step.get("run", ""))
        for step in wf["jobs"]["run-daily"].get("steps", [])
    ).lower()


def test_run_daily_posts_to_daily_endpoint() -> None:
    lowered = _all_step_runs(_load_scheduler())
    assert "post" in lowered
    assert "/api/v1/admin/daily" in lowered


def test_run_daily_sends_bearer_auth_header() -> None:
    wf = _load_scheduler()
    lowered = _all_step_runs(wf)
    assert "authorization: bearer" in lowered
    assert "$cron_secret" in lowered
    env_values = " ".join(
        str(v)
        for step in wf["jobs"]["run-daily"].get("steps", [])
        for v in step.get("env", {}).values()
    ).lower()
    assert "secrets.cron_secret" in env_values


def test_run_daily_uses_deploy_url_secret() -> None:
    wf = _load_scheduler()
    env_values = " ".join(
        str(v)
        for step in wf["jobs"]["run-daily"].get("steps", [])
        for v in step.get("env", {}).values()
    ).lower()
    assert "vercel_deploy_url" in env_values
    assert "secrets.vercel_deploy_url" in env_values
