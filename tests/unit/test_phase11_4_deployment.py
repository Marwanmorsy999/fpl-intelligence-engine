"""Phase 11.4 — No-credit-card deployment configuration tests.

Validates the Vercel (``vercel.json``) and GitHub Actions
(``.github/workflows/scheduler.yml``) artifacts that power the 100% free,
no-credit-card deployment. These tests only parse the artifacts; they make no
network calls and never touch the quantitative logic in Phases 1-8.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

VERCEL_JSON = REPO_ROOT / "vercel.json"
SCHEDULER_YML = REPO_ROOT / ".github" / "workflows" / "scheduler.yml"

APP_ENTRY = "src/fpl_intelligence/api/main.py"
REQUIRED_REWRITE_SOURCES = [
    "/api/v1/telegram/webhook",
    "/api/v1/admin/(.*)",
    "/(.*)",
]


def _load_vercel() -> dict:
    assert VERCEL_JSON.exists(), "vercel.json is missing from repo root"
    return json.loads(VERCEL_JSON.read_text(encoding="utf-8"))


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
# vercel.json
# --------------------------------------------------------------------------- #
def test_vercel_json_is_valid_json() -> None:
    _load_vercel()


def test_vercel_json_has_build_command() -> None:
    cfg = _load_vercel()
    assert cfg.get("buildCommand", "").strip() == "pip install -r requirements.txt"


def test_vercel_json_uses_python_build() -> None:
    cfg = _load_vercel()
    builds = cfg.get("builds", [])
    assert any(
        build.get("src") == APP_ENTRY and "@vercel/python" in build.get("use", "")
        for build in builds
    ), "vercel.json must build src/fpl_intelligence/api/main.py with @vercel/python"


@pytest.mark.parametrize("source", REQUIRED_REWRITE_SOURCES)
def test_vercel_json_rewrites_route_to_fastapi_app(source: str) -> None:
    cfg = _load_vercel()
    rewrites = cfg.get("rewrites", [])
    match = next((r for r in rewrites if r.get("source") == source), None)
    assert match is not None, f"missing rewrite for {source}"
    assert APP_ENTRY in match.get("destination", "")


def test_vercel_json_handles_telegram_webhook() -> None:
    cfg = _load_vercel()
    sources = [r.get("source") for r in cfg.get("rewrites", [])]
    assert "/api/v1/telegram/webhook" in sources


def test_vercel_json_handles_admin_run_scheduler() -> None:
    cfg = _load_vercel()
    sources = [r.get("source") for r in cfg.get("rewrites", [])]
    assert any(s.startswith("/api/v1/admin/") for s in sources)


def test_vercel_json_excludes_data_and_tests() -> None:
    cfg = _load_vercel()
    fn_cfg = cfg.get("functions", {}).get(APP_ENTRY, {})
    exclude = fn_cfg.get("excludeFiles", "")
    assert "data" in exclude and "tests" in exclude


def test_vercel_json_excludes_both_directories_separately() -> None:
    cfg = _load_vercel()
    fn_cfg = cfg.get("functions", {}).get(APP_ENTRY, {})
    exclude = fn_cfg.get("excludeFiles", "")
    # The glob "{data,tests}/**" must reference each directory.
    assert "{data,tests}" in exclude or ("data" in exclude and "tests" in exclude)


# --------------------------------------------------------------------------- #
# .github/workflows/scheduler.yml
# --------------------------------------------------------------------------- #
def test_scheduler_yaml_is_valid_yaml() -> None:
    _load_scheduler()


def test_scheduler_has_hourly_cron() -> None:
    wf = _load_scheduler()
    crons = [s.get("cron") for s in _scheduler_on(wf).get("schedule", [])]
    assert "0 * * * *" in crons, "hourly scheduler cron (0 * * * *) missing"


def test_scheduler_has_ten_minute_keep_warm_cron() -> None:
    wf = _load_scheduler()
    crons = [s.get("cron") for s in _scheduler_on(wf).get("schedule", [])]
    assert "*/10 * * * *" in crons, "10-minute keep-warm cron (*/10 * * * *) missing"


def test_scheduler_has_workflow_dispatch() -> None:
    wf = _load_scheduler()
    assert "workflow_dispatch" in _scheduler_on(wf)


def test_scheduler_defines_run_scheduler_job() -> None:
    wf = _load_scheduler()
    assert "run-scheduler" in wf.get("jobs", {})


def test_scheduler_defines_keep_warm_job() -> None:
    wf = _load_scheduler()
    assert "keep-warm" in wf.get("jobs", {})


def test_run_scheduler_posts_to_admin_endpoint() -> None:
    wf = _load_scheduler()
    run = wf["jobs"]["run-scheduler"]["steps"][0]["run"]
    lowered = run.lower()
    assert "post" in lowered
    assert "/api/v1/admin/run-scheduler" in lowered


def test_run_scheduler_sends_x_admin_secret_header() -> None:
    wf = _load_scheduler()
    step = wf["jobs"]["run-scheduler"]["steps"][0]
    run = step["run"]
    lowered = run.lower()
    assert "x-admin-secret" in lowered
    assert "$admin_secret_key" in lowered
    env = step.get("env", {})
    assert "secrets.admin_secret_key" in str(env.get("ADMIN_SECRET_KEY", "")).lower()


def test_run_scheduler_uses_deploy_url_secret() -> None:
    wf = _load_scheduler()
    env = wf["jobs"]["run-scheduler"]["steps"][0].get("env", {})
    assert "VERCEL_DEPLOY_URL" in env
    assert "secrets.vercel_deploy_url" in str(env["VERCEL_DEPLOY_URL"]).lower()


def test_keep_warm_pings_health_endpoint() -> None:
    wf = _load_scheduler()
    run = wf["jobs"]["keep-warm"]["steps"][0]["run"]
    lowered = run.lower()
    assert "health" in lowered
    assert "/api/v1/health" in lowered


def test_keep_warm_uses_deploy_url_secret() -> None:
    wf = _load_scheduler()
    env = wf["jobs"]["keep-warm"]["steps"][0].get("env", {})
    assert "VERCEL_DEPLOY_URL" in env
    assert "secrets.vercel_deploy_url" in str(env["VERCEL_DEPLOY_URL"]).lower()


def test_run_scheduler_gated_to_hourly() -> None:
    wf = _load_scheduler()
    if_expr = str(wf["jobs"]["run-scheduler"].get("if", ""))
    assert "0 * * * *" in if_expr


def test_keep_warm_gated_to_ten_minutes() -> None:
    wf = _load_scheduler()
    if_expr = str(wf["jobs"]["keep-warm"].get("if", ""))
    assert "*/10 * * * *" in if_expr
