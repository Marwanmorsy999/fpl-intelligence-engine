"""Phase 11.2 — Procfile / deployment-manifest validation.

Verifies that every process type declared in the root ``Procfile`` points at a
real, importable target so a bad deploy command cannot slip through. No network
access, no live API/LLM calls.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROCFILE = REPO_ROOT / "Procfile"


def _parse_procfile() -> dict[str, str]:
    assert PROCFILE.exists(), "Procfile must exist at the repo root"
    processes: dict[str, str] = {}
    for line in PROCFILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, command = line.partition(":")
        processes[name.strip()] = command.strip()
    return processes


EXPECTED_PROCESSES = {"web", "worker", "bot", "release"}


def test_procfile_declares_required_processes() -> None:
    procs = _parse_procfile()
    missing = EXPECTED_PROCESSES - set(procs)
    assert not missing, f"Procfile is missing process type(s): {sorted(missing)}"


def test_web_command_serves_fastapi_app() -> None:
    procs = _parse_procfile()
    command = procs["web"]
    assert "fpl_intelligence.api.main:app" in command
    from fpl_intelligence.api.main import app

    assert app is not None


def test_worker_command_points_to_scheduler_script() -> None:
    procs = _parse_procfile()
    command = procs["worker"]
    assert "fpl_intelligence.scripts.run_scheduler" in command
    from fpl_intelligence.scripts import run_scheduler

    assert hasattr(run_scheduler, "main"), "run_scheduler must expose main()"


def test_bot_command_points_to_telegram_script() -> None:
    procs = _parse_procfile()
    command = procs["bot"]
    assert "fpl_intelligence.scripts.run_telegram_bot" in command
    from fpl_intelligence.scripts import run_telegram_bot

    assert hasattr(run_telegram_bot, "main"), "run_telegram_bot must expose main()"


def test_release_command_runs_alembic() -> None:
    procs = _parse_procfile()
    command = procs["release"]
    assert "alembic upgrade head" in command
    import alembic

    assert alembic.__version__ is not None
    assert (REPO_ROOT / "migrations" / "alembic.ini").exists() or (
        REPO_ROOT / "alembic.ini"
    ).exists()
