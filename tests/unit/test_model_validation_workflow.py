from pathlib import Path

WORKFLOW = Path(".github/workflows/model-validation.yml")


def test_model_validation_workflow_is_manual_and_uses_database_url_secret() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "push:" not in workflow
    assert "DATABASE_URL: ${{ secrets.DATABASE_URL }}" in workflow
    assert "psycopg2" not in workflow.lower()
    assert "python scripts/preflight_minutes_validation.py" in workflow
    assert (
        "python scripts/evaluate_minutes_walkforward.py --report "
        "docs/STAGE_2A_MINUTES_VALIDATION.md"
    ) in workflow
    assert "python scripts/evaluate_team_strength.py" in workflow


def test_model_validation_workflow_does_not_upload_or_print_credentials() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8").lower()

    assert "echo ${{ secrets.database_url }}" not in workflow
    assert "print(os.environ[\"database_url\"])" not in workflow
    for environment_file in (".env", ".env.local", ".env.production.local"):
        assert f"path: {environment_file}" not in workflow
    assert "upload-artifact" in workflow