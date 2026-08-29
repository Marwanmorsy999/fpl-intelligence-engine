import typer

from fpl_intelligence.data_providers.registry import fpl_ingestion_adapter
from fpl_intelligence.db.session import SessionLocal
from fpl_intelligence.ingestion.fpl import ingest_bootstrap, ingest_fixtures

app = typer.Typer(add_completion=False)


def _provider():
    return fpl_ingestion_adapter()


def _run(job: str) -> None:
    db = SessionLocal()
    try:
        provider = _provider()
        if job == "bootstrap":
            count = ingest_bootstrap(db, provider, "2026-27")
        elif job == "fixtures":
            count = ingest_fixtures(db, provider, "2026-27")
        else:
            count = ingest_bootstrap(db, provider, "2026-27")
            count += ingest_fixtures(db, provider, "2026-27")
        typer.echo(f"{job}: processed {count} records")
    finally:
        db.close()


@app.command("fpl-bootstrap")
def fpl_bootstrap() -> None:
    """Ingest current FPL bootstrap/static data."""
    _run("bootstrap")


@app.command("fpl-fixtures")
def fpl_fixtures() -> None:
    """Ingest current FPL fixtures."""
    _run("fixtures")


@app.command("fpl-all")
def fpl_all() -> None:
    """Ingest current FPL bootstrap and fixtures."""
    _run("all")


if __name__ == "__main__":
    app()
