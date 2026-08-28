from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.data_providers.registry import (
    FplProviderAdapter,
    ProviderRegistry,
    fpl_ingestion_adapter,
)
from fpl_intelligence.db.models import (
    Fixture,
    Gameweek,
    IngestionRun,
    Player,
    PlayerExternalId,
    PlayerGameweekPerformance,
    PlayerTeamMembership,
    RawRecord,
    Season,
    Team,
    TeamExternalId,
)

PROVIDER = "official_fpl"


def _hash_payload(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(serialized).hexdigest()


def _save_raw_record(db: Session, endpoint: str, payload: Any, season_code: str) -> None:
    """Insert a RawRecord, skipping it when an identical payload already exists."""
    payload_hash = _hash_payload(payload)
    existing = db.scalar(
        select(RawRecord).where(
            RawRecord.source == PROVIDER,
            RawRecord.endpoint == endpoint,
            RawRecord.payload_hash == payload_hash,
        )
    )
    if existing is not None:
        return
    db.add(
        RawRecord(
            source=PROVIDER,
            provider=PROVIDER,
            endpoint=endpoint,
            retrieved_at=datetime.now(UTC),
            payload_hash=payload_hash,
            payload=dict(payload),
            season_code=season_code,
        )
    )
    db.flush()


def _get_or_create_season(db: Session, code: str) -> Season:
    season = db.scalar(select(Season).where(Season.code == code))
    if season:
        return season
    season = Season(code=code, display_name=code.replace("-", "/"))
    db.add(season)
    db.flush()
    return season


def _get_or_create_team(
    db: Session, provider_team_id: str, name: str, short_name: str | None
) -> Team:
    """Resolve a team by its official_fpl id, creating the mapping if needed."""
    ext = db.scalar(
        select(TeamExternalId).where(
            TeamExternalId.provider == PROVIDER,
            TeamExternalId.provider_team_id == provider_team_id,
        )
    )
    if ext is not None:
        return ext.team

    team = db.scalar(select(Team).where(Team.name == name))
    if team is None:
        team = Team(name=name, short_name=short_name or name[:3].upper())
        db.add(team)
        db.flush()

    db.add(
        TeamExternalId(
            team_id=team.id,
            provider=PROVIDER,
            provider_team_id=provider_team_id,
        )
    )
    db.flush()
    return team


def _get_or_create_player(
    db: Session,
    provider_player_id: str,
    first_name: str,
    second_name: str,
    web_name: str,
    position_code: int | None,
    fpl_code: int | None = None,
) -> Player:
    """Resolve a player by their official_fpl id, creating the mapping if needed."""
    ext = db.scalar(
        select(PlayerExternalId).where(
            PlayerExternalId.provider == PROVIDER,
            PlayerExternalId.provider_player_id == provider_player_id,
        )
    )
    # The provider id IS the official FPL element id — mirror it onto the
    # dedicated column so squad imports can join directly (v1.5.1 alignment fix).
    element_id = int(provider_player_id) if str(provider_player_id).isdigit() else None
    if ext is not None:
        player = ext.player
        player.first_name = first_name
        player.second_name = second_name
        player.web_name = web_name
        player.position_code = position_code
        if fpl_code is not None:
            player.fpl_code = fpl_code
        if element_id is not None:
            player.fpl_element_id = element_id
        return player

    player = Player(
        first_name=first_name,
        second_name=second_name,
        web_name=web_name,
        position_code=position_code,
        fpl_code=fpl_code,
        fpl_element_id=element_id,
    )
    db.add(player)
    db.flush()
    db.add(
        PlayerExternalId(
            player_id=player.id,
            provider=PROVIDER,
            provider_player_id=provider_player_id,
        )
    )
    db.flush()
    return player


def _adapter(provider: Any = None) -> FplProviderAdapter:
    if isinstance(provider, FplProviderAdapter):
        return provider
    if isinstance(provider, ProviderRegistry):
        return fpl_ingestion_adapter(registry=provider)
    return fpl_ingestion_adapter(provider)


def ingest_bootstrap(db: Session, provider: Any, season_code: str) -> int:
    started = datetime.now(UTC)
    run = IngestionRun(source=PROVIDER, job_name="bootstrap", status="RUNNING", started_at=started)
    db.add(run)
    db.flush()

    try:
        payload = _adapter(provider).get_bootstrap_static()
        _save_raw_record(db, "/api/bootstrap-static/", payload, season_code)

        season = _get_or_create_season(db, season_code)
        teams = payload.get("teams", [])
        elements = payload.get("elements", [])

        #: Map official_fpl team id -> internal team id so players can be linked.
        team_ext_map: dict[str, int] = {}
        for item in teams:
            if not isinstance(item, dict):
                continue
            provider_id = str(int(item["id"]))
            team = _get_or_create_team(
                db,
                provider_id,
                str(item.get("name", "Unknown")),
                str(item.get("short_name", "")) or None,
            )
            team_ext_map[provider_id] = team.id

        # Ensure at least one gameweek exists so a current-price snapshot
        # (PlayerGameweekPerformance.price) can be stored for Browse Players.
        reference_gw = None
        for ev in payload.get("events", []) or []:
            if not isinstance(ev, dict):
                continue
            provider_event_id = int(ev["id"])
            gw = db.scalar(
                select(Gameweek).where(
                    Gameweek.season_id == season.id,
                    Gameweek.provider_event_id == provider_event_id,
                )
            )
            if gw is None:
                gw = Gameweek(
                    season_id=season.id,
                    provider_event_id=provider_event_id,
                    name=str(ev.get("name", f"Gameweek {provider_event_id}")),
                )
                db.add(gw)
                db.flush()
            if reference_gw is None or gw.provider_event_id < reference_gw.provider_event_id:
                reference_gw = gw

        for item in elements:
            if not isinstance(item, dict):
                continue
            provider_id = str(int(item["id"]))
            player = _get_or_create_player(
                db,
                provider_id,
                str(item.get("first_name", "")),
                str(item.get("second_name", "")),
                str(item.get("web_name", "")),
                int(item["element_type"]) if item.get("element_type") else None,
                fpl_code=item.get("code"),
            )

            # Link the player to their current team and seed a current-price
            # snapshot so GET /api/v1/players can return team + price without a
            # separate historical ingest. Idempotent: skip if already present.
            team_provider_id = str(int(item["team"])) if item.get("team") is not None else None
            team_id = team_ext_map.get(team_provider_id) if team_provider_id else None
            if team_id is not None:
                existing_membership = db.scalar(
                    select(PlayerTeamMembership).where(
                        PlayerTeamMembership.player_id == player.id,
                        PlayerTeamMembership.team_id == team_id,
                        PlayerTeamMembership.season_id == season.id,
                    )
                )
                if existing_membership is None:
                    db.add(
                        PlayerTeamMembership(
                            player_id=player.id,
                            team_id=team_id,
                            season_id=season.id,
                            valid_from=season.start_date,
                        )
                    )

                now_cost = item.get("now_cost")
                price = (float(now_cost) / 10.0) if now_cost is not None else None
                if reference_gw is not None:
                    existing_pgp = db.scalar(
                        select(PlayerGameweekPerformance).where(
                            PlayerGameweekPerformance.player_id == player.id,
                            PlayerGameweekPerformance.gameweek_id == reference_gw.id,
                        )
                    )
                    if existing_pgp is None:
                        db.add(
                            PlayerGameweekPerformance(
                                player_id=player.id,
                                gameweek_id=reference_gw.id,
                                season_id=season.id,
                                team_id=team_id,
                                price=price,
                            )
                        )

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


def ingest_fixtures(db: Session, provider: Any, season_code: str) -> int:
    started = datetime.now(UTC)
    run = IngestionRun(source=PROVIDER, job_name="fixtures", status="RUNNING", started_at=started)
    db.add(run)
    db.flush()

    try:
        payload = _adapter(provider).get_fixtures()
        _save_raw_record(db, "/api/fixtures/", {"fixtures": payload}, season_code)

        season = _get_or_create_season(db, season_code)
        team_rows = db.scalars(select(TeamExternalId)).all()
        provider_team_map = {
            te.provider_team_id: te.team_id for te in team_rows if te.provider == PROVIDER
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
                kickoff_time = datetime.fromisoformat(
                    str(item["kickoff_time"]).replace("Z", "+00:00")
                )

            home_provider_id = str(int(item["team_h"]))
            away_provider_id = str(int(item["team_a"]))
            if (
                home_provider_id not in provider_team_map
                or away_provider_id not in provider_team_map
            ):
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
