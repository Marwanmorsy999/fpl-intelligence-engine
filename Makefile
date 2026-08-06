.PHONY: install test lint typecheck run up down migrate fpl-bootstrap fpl-fixtures fpl-all

install:
	python -m pip install -e '.[dev]'

test:
	pytest

lint:
	ruff check .

typecheck:
	mypy src

run:
	uvicorn fpl_intelligence.api.main:app --reload

up:
	docker compose up -d --build

down:
	docker compose down

migrate:
	alembic upgrade head

fpl-bootstrap:
	python -m fpl_intelligence.cli fpl-bootstrap

fpl-fixtures:
	python -m fpl_intelligence.cli fpl-fixtures

fpl-all:
	python -m fpl_intelligence.cli fpl-all
