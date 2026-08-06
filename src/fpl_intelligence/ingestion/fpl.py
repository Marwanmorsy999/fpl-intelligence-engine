from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.collectors.official_fpl import OfficialFPLDataProvider
from fpl_intelligence.db.models import (
    Fixture,
    Gameweek,
    IngestionRun,
    Player,
    RawRecord,
    Season,
    Team,
)


def _hash_payload(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(serialized).hexdigest()


def _get_or_create_season(db: Session, code: str) -> Season:
    season = db.scalar(select(Season).where(Season.code == code))
    if season:
        return season
    season = Season(code=code, display_name=code.replace("-", "/"))
    db.add(season)
    db.flush()
    return season


def ingest_bootstrap(db: Session, provider: OfficialFPLDataProvider, season_code: str) -> int:
    started = datetime.now(UTC)
    run = IngestionRun(source="official_fpl", job_name="bootstrap", status="RUNNING", started_at=started)
    db.add(run)
    db.flush()

    try:
        payload = provider.get_bootstrap_static()
        retrieved_at = datetime.now(UTC)
        db.add(
            RawRecord(
                source="official_fpl",
                endpoint="/api/bootstrap-static/",
                retrieved_at=retrieved_at,
                payload_hash=_hash_payload(payload),
                payload=dict(payload),
            )
        )

        _get_or_create_season(db, season_code)
        teams = payload.get("teams", [])
        elements = payload.get("elements", [])

        team_map: dict[int, Team] = {}
        for item in teams:
            if not isinstance(item, dict):
                continue
            provider_id = int(item["id"])
            team = db.scalar(
                select(Team).where(
                    Team.provider == "official_fpl", Team.provider_team_id == provider_id
                )
            )
            if team is None:
                team = Team(
                    provider="official_fpl",
                    provider_team_id=provider_id,
                    name=str(item.get("name", "Unknown")),
                    short_name=str(item.get("short_name", "")) or None,
                )
                db.add(team)
                db.flush()
            else:
                team.name = str(item.get("name", team.name))
                team.short_name = str(item.get("short_name", team.short_name or "")) or None
            team_map[provider_id] = team

        for item in elements:
            if not isinstance(item, dict):
                continue
            provider_id = int(item["id"])
            player = db.scalar(
                select(Player).where(
                    Player.provider == "official_fpl", Player.provider_player_id == provider_id
                )
            )
            team = team_map.get(int(item["team"]))
            values = {
                "first_name": str(item.get("first_name", "")),
                "second_name": str(item.get("second_name", "")),
                "web_name": str(item.get("web_name", "")),
                "position_code": int(item["element_type"]) if item.get("element_type") else None,
                "current_team_id": team.id if team else None,
            }
            if player is None:
                db.add(
                    Player(
                        provider="official_fpl",
                        provider_player_id=provider_id,
                        **values,
                    )
                )
            else:
                for key, value in values.items():
                    setattr(player, key, value)

        run.status = "SUCCESS"
        run.records_processed = len(teams) + len(elements)
        run.finished_at = datetime.now(UTC)
        db.commit()
        return run.records_processed
    except Exception as exc:
        db.rollback()
        run.status = "FAILED"
        run.error_summary = str(exc)
        run.finished_at = datetime.now(UTC)
        db.add(run)
        db.commit()
        raise


def ingest_fixtures(db: Session, provider: OfficialFPLDataProvider, season_code: str) -> int:
    started = datetime.now(UTC)
    run = IngestionRun(source="official_fpl", job_name="fixtures", status="RUNNING", started_at=started)
    db.add(run)
    db.flush()

    try:
        payload = provider.get_fixtures()
        retrieved_at = datetime.now(UTC)
        db.add(
            RawRecord(
                source="official_fpl",
                endpoint="/api/fixtures/",
                retrieved_at=retrieved_at,
                payload_hash=_hash_payload(payload),
                payload={"fixtures": payload},
            )
        )

        season = _get_or_create_season(db, season_code)
        team_rows = db.scalars(select(Team)).all()
        provider_team_map = {
            team.provider_team_id: team.id
            for team in team_rows
            if team.provider == "official_fpl"
        }
        processed = 0
        for item in payload:
            provider_id = int(item["id"])
            fixture = db.scalar(
                select(Fixture).where(
                    Fixture.season_id == season.id,
                    Fixture.provider_fixture_id == provider_id,
                )
            )
            kickoff_time = None
            if item.get("kickoff_time"):
                kickoff_time = datetime.fromisoformat(str(item["kickoff_time"]).replace("Z", "+00:00"))

            home_provider_id = int(item["team_h"])
            away_provider_id = int(item["team_a"])
            if home_provider_id not in provider_team_map or away_provider_id not in provider_team_map:
                raise ValueError(
                    f"Fixture {provider_id} references unknown team provider IDs "
                    f"{home_provider_id} and/or {away_provider_id}"
                )

            if fixture is None:
                fixture = Fixture(
                    season_id=season.id,
                    provider_fixture_id=provider_id,
                    kickoff_time=kickoff_time,
                    home_team_id=provider_team_map[home_provider_id],
                    away_team_id=provider_team_map[away_provider_id],
                    home_score=item.get("team_h_score"),
                    away_score=item.get("team_a_score"),
                )
                db.add(fixture)
            else:
                fixture.kickoff_time = kickoff_time
                fixture.home_team_id = provider_team_map[home_provider_id]
                fixture.away_team_id = provider_team_map[away_provider_id]
                fixture.home_score = item.get("team_h_score")
                fixture.away_score = item.get("team_a_score")

            event = item.get("event")
            if event is not None:
                gameweek = db.scalar(
                    select(Gameweek).where(
                        Gameweek.season_id == season.id,
                        Gameweek.provider_event_id == int(event),
                    )
                )
                if gameweek is None:
                    gameweek = Gameweek(
                        season_id=season.id,
                        provider_event_id=int(event),
                        name=f"Gameweek {event}",
                    )
                    db.add(gameweek)
                    db.flush()
                fixture.gameweek_id = gameweek.id
            processed += 1

        run.status = "SUCCESS"
        run.records_processed = processed
        run.finished_at = datetime.now(UTC)
        db.commit()
        return processed
    except Exception as exc:
        db.rollback()
        run.status = "FAILED"
        run.error_summary = str(exc)
        run.finished_at = datetime.now(UTC)
        db.add(run)
        db.commit()
        raise
