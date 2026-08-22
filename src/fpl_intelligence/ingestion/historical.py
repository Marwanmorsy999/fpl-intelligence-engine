"""Historical data ingestion pipeline.

Orchestrates the full ingestion flow: fetch from provider -> normalize ->
reconcile -> persist to database. Supports idempotent, resumable imports.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.db.models import (
    Fixture,
    FPLSnapshot,
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
from fpl_intelligence.domain.canonical import (
    normalize_fixture,
    normalize_fpl_history,
    normalize_fpl_snapshot,
    normalize_player,
    normalize_season,
    normalize_team,
)
from fpl_intelligence.domain.historical_provider import HistoricalFootballDataProvider
from fpl_intelligence.ingestion.reconciliation import (
    ReconciliationReport,
    reconcile_fixtures,
    validate_fpl_history,
)

logger = logging.getLogger(__name__)


def _hash_payload(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(serialized).hexdigest()


def _make_json_safe(payload: Any) -> dict[str, Any]:
    """Convert a payload to a JSON-safe dict by serializing non-serializable types."""
    if isinstance(payload, dict):
        return {k: _make_json_safe(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_make_json_safe(item) for item in payload]
    if isinstance(payload, datetime):
        return payload.isoformat()
    if isinstance(payload, (int, float, str, bool, type(None))):
        return payload
    return str(payload)


def _save_raw_record(
    db: Session,
    source: str,
    provider: str,
    endpoint: str,
    payload: Any,
    season_code: str | None = None,
) -> None:
    retrieved_at = datetime.now(UTC)
    payload_hash = _hash_payload(payload)
    existing = db.scalar(
        select(RawRecord).where(
            RawRecord.source == source,
            RawRecord.endpoint == endpoint,
            RawRecord.payload_hash == payload_hash,
        )
    )
    if existing:
        return
    safe_payload = _make_json_safe(payload)
    db.add(
        RawRecord(
            source=source,
            provider=provider,
            endpoint=endpoint,
            retrieved_at=retrieved_at,
            payload_hash=payload_hash,
            payload=safe_payload if isinstance(safe_payload, dict) else {"data": safe_payload},
            season_code=season_code,
        )
    )
    db.flush()


def _get_or_create_season(
    db: Session, code: str, start_date: Any = None, end_date: Any = None
) -> Season:
    season = db.scalar(select(Season).where(Season.code == code))
    if season:
        return season
    season = Season(
        code=code,
        display_name=code.replace("-", "/"),
        start_date=start_date,
        end_date=end_date,
        competition="Premier League",
    )
    db.add(season)
    db.flush()
    return season


def _get_or_create_team(
    db: Session, provider_name: str, provider_team_id: str, name: str, short_name: str | None
) -> Team:
    """Get or create a team by external ID, creating the mapping if needed."""
    ext_id = db.scalar(
        select(TeamExternalId).where(
            TeamExternalId.provider == provider_name,
            TeamExternalId.provider_team_id == provider_team_id,
        )
    )
    if ext_id:
        return ext_id.team

    # Check if team exists by name
    team = db.scalar(select(Team).where(Team.name == name))
    if team is None:
        team = Team(name=name, short_name=short_name or name[:3].upper())
        db.add(team)
        db.flush()

    # Create external ID mapping
    db.add(
        TeamExternalId(
            team_id=team.id,
            provider=provider_name,
            provider_team_id=provider_team_id,
        )
    )
    db.flush()
    return team


def _get_or_create_player(
    db: Session,
    provider_name: str,
    provider_player_id: str,
    first_name: str,
    second_name: str,
    web_name: str,
    position_code: int | None,
) -> Player:
    """Get or create a player by external ID, creating the mapping if needed."""
    ext_id = db.scalar(
        select(PlayerExternalId).where(
            PlayerExternalId.provider == provider_name,
            PlayerExternalId.provider_player_id == provider_player_id,
        )
    )
    if ext_id:
        return ext_id.player

    player = Player(
        first_name=first_name,
        second_name=second_name,
        web_name=web_name,
        position_code=position_code,
    )
    db.add(player)
    db.flush()

    db.add(
        PlayerExternalId(
            player_id=player.id,
            provider=provider_name,
            provider_player_id=provider_player_id,
        )
    )
    db.flush()
    return player


def _get_or_create_gameweek(
    db: Session,
    season_id: int,
    provider_event_id: int,
    name: str,
    deadline_time: Any = None,
    start_time: Any = None,
    end_time: Any = None,
) -> Gameweek:
    gw = db.scalar(
        select(Gameweek).where(
            Gameweek.season_id == season_id,
            Gameweek.provider_event_id == provider_event_id,
        )
    )
    if gw:
        return gw
    gw = Gameweek(
        season_id=season_id,
        provider_event_id=provider_event_id,
        name=name,
        deadline_time=deadline_time,
        start_time=start_time,
        end_time=end_time,
        status="scheduled",
    )
    db.add(gw)
    db.flush()
    return gw


def _fixture_id_to_int(provider_fixture_id: str) -> int:
    """Convert a provider fixture ID string to a deterministic integer.

    Uses hashlib for a deterministic hash (unlike Python's built-in hash()
    which is randomized by PYTHONHASHSEED).
    """
    return int(hashlib.md5(provider_fixture_id.encode()).hexdigest()[:8], 16) % (10**9)


def _get_or_create_fixture(
    db: Session,
    season_id: int,
    provider_fixture_id: str,
    home_team_id: int,
    away_team_id: int,
    gameweek_id: int | None,
    kickoff_time: Any,
    home_score: Any,
    away_score: Any,
    status: str,
    postponed: bool,
) -> Fixture:
    numeric_id = _fixture_id_to_int(provider_fixture_id)
    fixture = db.scalar(
        select(Fixture).where(
            Fixture.season_id == season_id,
            Fixture.provider_fixture_id == numeric_id,
        )
    )
    if fixture:
        return fixture

    fixture = Fixture(
        season_id=season_id,
        provider_fixture_id=numeric_id,
        gameweek_id=gameweek_id,
        kickoff_time=kickoff_time,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        home_score=home_score,
        away_score=away_score,
        status=status,
        postponed=postponed,
    )
    db.add(fixture)
    db.flush()
    return fixture


def import_season(
    db: Session,
    provider: HistoricalFootballDataProvider,
    season_code: str,
    dataset: str = "all",
    dry_run: bool = False,
    force: bool = False,
) -> ReconciliationReport:
    """Import a historical season from a provider.

    Args:
        db: Database session.
        provider: Historical data provider.
        season_code: Season code e.g. "2024-25".
        dataset: Dataset to import ("all", "teams", "players", "fixtures", "stats", "fpl").
        dry_run: If True, don't persist anything.
        force: If True, re-import even if previously imported.

    Returns:
        ReconciliationReport with details of what was imported.
    """
    provider_name = provider.provider_name
    started = datetime.now(UTC)
    report = ReconciliationReport(season=season_code, provider=provider_name, dataset=dataset)

    # Check for existing completed run
    if not force:
        existing = db.scalar(
            select(IngestionRun).where(
                IngestionRun.source == provider_name,
                IngestionRun.job_name == f"historical_{dataset}",
                IngestionRun.season_code == season_code,
                IngestionRun.status == "SUCCESS",
            )
        )
        if existing:
            logger.info(
                "Import already completed for %s/%s (%s)", provider_name, dataset, season_code
            )
            report.records_accepted = existing.records_processed
            return report

    run = IngestionRun(
        source=provider_name,
        job_name=f"historical_{dataset}",
        season_code=season_code,
        status="RUNNING",
        started_at=started,
    )
    db.add(run)
    db.flush()

    try:
        # 1. Seasons
        seasons_data = provider.get_seasons()
        season_info = None
        for s in seasons_data:
            if str(s.get("season_name", "")) == season_code:
                season_info = s
                break
        if season_info is None:
            raise ValueError(f"Season {season_code} not found in provider data")

        norm_season = normalize_season(season_info, provider_name)
        season = _get_or_create_season(
            db,
            norm_season["code"],
            norm_season["start_date"],
            norm_season["end_date"],
        )
        logger.info("Season %s: id=%d", season_code, season.id)

        if not dry_run:
            _save_raw_record(
                db,
                provider_name,
                provider_name,
                f"/seasons/{season_code}",
                dict(season_info),
                season_code,
            )

        # 2. Teams
        teams_data = provider.get_teams(season_code)
        norm_teams = [
            normalize_team(t, provider_name, getattr(provider, "schema_version", "v1"))
            for t in teams_data
        ]

        team_map: dict[str, Team] = {}
        for nt in norm_teams:
            team = _get_or_create_team(
                db, provider_name, nt["provider_team_id"], nt["name"], nt["short_name"]
            )
            team_map[nt["provider_team_id"]] = team

        known_team_ids = set(team_map.keys())
        logger.info("Teams: %d known", len(known_team_ids))

        if not dry_run:
            _save_raw_record(
                db,
                provider_name,
                provider_name,
                f"/teams/{season_code}",
                list(teams_data),
                season_code,
            )

        # 3. Players
        players_data = provider.get_players(season_code)
        norm_players = [
            normalize_player(p, provider_name, getattr(provider, "schema_version", "v1"))
            for p in players_data
        ]

        player_map: dict[str, Player] = {}
        for np_data in norm_players:
            player = _get_or_create_player(
                db,
                provider_name,
                np_data["provider_player_id"],
                np_data["first_name"],
                np_data["second_name"],
                np_data["web_name"],
                np_data["position_code"],
            )
            player_map[np_data["provider_player_id"]] = player

            # Create player-team membership
            team_id_str = np_data.get("team_id")
            if team_id_str and team_id_str in team_map:
                existing_membership = db.scalar(
                    select(PlayerTeamMembership).where(
                        PlayerTeamMembership.player_id == player.id,
                        PlayerTeamMembership.team_id == team_map[team_id_str].id,
                        PlayerTeamMembership.season_id == season.id,
                    )
                )
                if not existing_membership:
                    db.add(
                        PlayerTeamMembership(
                            player_id=player.id,
                            team_id=team_map[team_id_str].id,
                            season_id=season.id,
                            valid_from=season.start_date,
                        )
                    )

        known_player_ids = set(player_map.keys())
        logger.info("Players: %d known", len(known_player_ids))

        if not dry_run:
            _save_raw_record(
                db,
                provider_name,
                provider_name,
                f"/players/{season_code}",
                list(players_data),
                season_code,
            )

        # 4. Fixtures
        fixtures_data = provider.get_fixtures(season_code)
        norm_fixtures = [
            normalize_fixture(f, provider_name, getattr(provider, "schema_version", "v1"))
            for f in fixtures_data
        ]

        known_fixture_ids: set[str] = set()
        for nf in norm_fixtures:
            known_fixture_ids.add(nf["provider_fixture_id"])

        accepted_fixtures = reconcile_fixtures(
            norm_fixtures,
            known_fixture_ids,
            known_team_ids,
            report,
        )

        for af in accepted_fixtures:
            gameweek_id = None
            gw_num = af.get("gameweek")
            if gw_num is not None:
                gw = _get_or_create_gameweek(
                    db,
                    season.id,
                    int(gw_num),
                    f"Gameweek {gw_num}",
                )
                gameweek_id = gw.id

            _get_or_create_fixture(
                db,
                season.id,
                af["provider_fixture_id"],
                team_map[af["home_team_id"]].id,
                team_map[af["away_team_id"]].id,
                gameweek_id,
                af["kickoff_time"],
                af["home_score"],
                af["away_score"],
                af["status"],
                af["postponed"],
            )

        if not dry_run:
            _save_raw_record(
                db,
                provider_name,
                provider_name,
                f"/fixtures/{season_code}",
                list(fixtures_data),
                season_code,
            )

        # 5. Player match statistics
        if dataset in ("all", "stats"):
            fpl_history = provider.get_fpl_history(season_code)
            norm_history = [normalize_fpl_history(h, provider_name) for h in fpl_history]
            accepted_history = validate_fpl_history(norm_history, known_player_ids, report)

            # Real FPL data may contain multiple fixture rows per (player,
            # gameweek) (double/blank gameweeks). The canonical schema keys on
            # (player_id, gameweek_id), so we aggregate per (player, gameweek):
            # additive stats are summed; price/value/selected take the final
            # fixture's value. This is the correct FPL gameweek-total semantics
            # and is a correctness bug-fix (not a methodology change).
            aggregated: dict[tuple[str, int], dict[str, Any]] = {}
            order: list[tuple[str, int]] = []
            _sum_fields = [
                "total_points",
                "minutes",
                "goals_scored",
                "assists",
                "clean_sheets",
                "goals_conceded",
                "own_goals",
                "penalties_saved",
                "penalties_missed",
                "yellow_cards",
                "red_cards",
                "saves",
                "bonus",
                "bps",
            ]
            _sum_float = [
                "influence",
                "creativity",
                "threat",
                "ict_index",
                "expected_goals",
                "expected_assists",
                "expected_goal_involvements",
                "expected_goals_conceded",
            ]
            _last_fields = [
                "value",
                "selected",
                "transfers_in",
                "transfers_out",
                "price",
                "form",
                "points_per_game",
            ]
            for nh in accepted_history:
                pid = nh.get("provider_player_id")
                gw_num = nh.get("gameweek")
                if not pid or gw_num is None:
                    continue
                key = (pid, int(gw_num))
                if key not in aggregated:
                    aggregated[key] = dict(nh)
                    order.append(key)
                else:
                    merged = aggregated[key]
                    for f in _sum_fields:
                        merged[f] = (merged.get(f) or 0) + (nh.get(f) or 0)
                    for f in _sum_float:
                        merged[f] = (merged.get(f) or 0.0) + (nh.get(f) or 0.0)
                    for f in _last_fields:
                        if nh.get(f) is not None:
                            merged[f] = nh.get(f)

            for key in order:
                nh = aggregated[key]
                player = player_map.get(key[0])
                gw_num = key[1]
                if player is None:
                    continue

                gw = db.scalar(
                    select(Gameweek).where(
                        Gameweek.season_id == season.id,
                        Gameweek.provider_event_id == gw_num,
                    )
                )
                if gw is None:
                    continue

                existing_pgp = db.scalar(
                    select(PlayerGameweekPerformance).where(
                        PlayerGameweekPerformance.player_id == player.id,
                        PlayerGameweekPerformance.gameweek_id == gw.id,
                    )
                )
                if existing_pgp:
                    continue

                membership = db.scalar(
                    select(PlayerTeamMembership).where(
                        PlayerTeamMembership.player_id == player.id,
                        PlayerTeamMembership.season_id == season.id,
                    )
                )
                team_id_val = membership.team_id if membership else None

                if team_id_val is None:
                    continue

                db.add(
                    PlayerGameweekPerformance(
                        player_id=player.id,
                        gameweek_id=gw.id,
                        season_id=season.id,
                        team_id=team_id_val,
                        minutes=nh.get("minutes"),
                        goals_scored=nh.get("goals_scored"),
                        assists=nh.get("assists"),
                        clean_sheets=nh.get("clean_sheets"),
                        goals_conceded=nh.get("goals_conceded"),
                        own_goals=nh.get("own_goals"),
                        penalties_saved=nh.get("penalties_saved"),
                        penalties_missed=nh.get("penalties_missed"),
                        yellow_cards=nh.get("yellow_cards"),
                        red_cards=nh.get("red_cards"),
                        saves=nh.get("saves"),
                        bonus=nh.get("bonus"),
                        bps=nh.get("bps"),
                        influence=nh.get("influence"),
                        creativity=nh.get("creativity"),
                        threat=nh.get("threat"),
                        ict_index=nh.get("ict_index"),
                        expected_goals=nh.get("expected_goals"),
                        expected_assists=nh.get("expected_assists"),
                        expected_goal_involvements=nh.get("expected_goal_involvements"),
                        expected_goals_conceded=nh.get("expected_goals_conceded"),
                        total_points=nh.get("total_points"),
                        value=nh.get("value"),
                        selected=nh.get("selected"),
                        transfers_in=nh.get("transfers_in"),
                        transfers_out=nh.get("transfers_out"),
                        price=nh.get("price"),
                        selected_by_percent=nh.get("selected_by_percent"),
                        form=nh.get("form"),
                        points_per_game=nh.get("points_per_game"),
                        ep_this=nh.get("ep_this"),
                        ep_next=nh.get("ep_next"),
                    )
                )

            if not dry_run:
                _save_raw_record(
                    db,
                    provider_name,
                    provider_name,
                    f"/fpl_history/{season_code}",
                    list(fpl_history),
                    season_code,
                )

        # 6. Player performance snapshots (price/ownership/transfers)
        if dataset in ("all", "fpl"):
            try:
                snapshots_data = provider.get_fpl_snapshots(season_code)
                norm_snapshots = [normalize_fpl_snapshot(s, provider_name) for s in snapshots_data]

                # Real FPL data may contain multiple fixture rows per (player,
                # gameweek) whose unique key (player, gameweek, event_time) can
                # collide (double/blank gameweeks sharing a kickoff window).
                # Aggregate per (player, gameweek, event_time): additive snapshot
                # counts are summed; scalar values take the final row. This keeps
                # the FPLSnapshot unique constraint satisfied and is idempotent.
                snap_agg: dict[tuple[str, int, Any], dict[str, Any]] = {}
                snap_order: list[tuple[str, int, Any]] = []
                _snap_sum = [
                    "transfers_in_event",
                    "transfers_out_event",
                    "transfers_in_season",
                    "transfers_out_season",
                ]
                for ns in norm_snapshots:
                    pid = ns.get("provider_player_id")
                    gw_num = ns.get("gameweek")
                    if not pid or gw_num is None:
                        continue
                    key = (pid, int(gw_num), ns.get("event_time"))
                    if key not in snap_agg:
                        snap_agg[key] = dict(ns)
                        snap_order.append(key)
                    else:
                        merged = snap_agg[key]
                        for f in _snap_sum:
                            if ns.get(f) is not None:
                                merged[f] = (merged.get(f) or 0) + (ns.get(f) or 0)
                        for f in [
                            "price",
                            "selected_by_percent",
                            "form",
                            "points_per_game",
                            "ep_this",
                            "ep_next",
                            "total_points",
                            "event_time",
                        ]:
                            if ns.get(f) is not None:
                                merged[f] = ns.get(f)

                for key in snap_order:
                    ns = snap_agg[key]
                    player = player_map.get(key[0])
                    gw_num = key[1]
                    if player is None:
                        continue

                    gw = db.scalar(
                        select(Gameweek).where(
                            Gameweek.season_id == season.id,
                            Gameweek.provider_event_id == int(gw_num),
                        )
                    )
                    if gw is None:
                        continue

                    event_time = ns.get("event_time")

                    existing_snap = db.scalar(
                        select(FPLSnapshot).where(
                            FPLSnapshot.player_id == player.id,
                            FPLSnapshot.gameweek_id == gw.id,
                            FPLSnapshot.event_time == event_time,
                        )
                    )
                    if existing_snap:
                        continue

                    db.add(
                        FPLSnapshot(
                            player_id=player.id,
                            gameweek_id=gw.id,
                            season_id=season.id,
                            event_time=event_time,
                            price=ns.get("price"),
                            selected_by_percent=ns.get("selected_by_percent"),
                            transfers_in_event=ns.get("transfers_in_event"),
                            transfers_out_event=ns.get("transfers_out_event"),
                            transfers_in_season=ns.get("transfers_in_season"),
                            transfers_out_season=ns.get("transfers_out_season"),
                            total_points=ns.get("total_points"),
                            form=ns.get("form"),
                            points_per_game=ns.get("points_per_game"),
                            ep_this=ns.get("ep_this"),
                            ep_next=ns.get("ep_next"),
                        )
                    )

                if not dry_run:
                    _save_raw_record(
                        db,
                        provider_name,
                        provider_name,
                        f"/fpl_snapshots/{season_code}",
                        list(snapshots_data),
                        season_code,
                    )
            except NotImplementedError:
                logger.info("FPL snapshots not supported by provider %s", provider_name)

        # NOTE: Team-level advanced match stats (shots/possession/big chances)
        # are NOT present in the public FPL mirror source. Team goals/xG are
        # derivable via RealFootballStatsProvider but are not consumed by the
        # Phase 4.5 evaluation pipeline; they are documented as "unavailable"
        # (the honest representation required by Phase 4.75).
        # RealFootballStatsProvider remains the extension point for future
        # Understat/FBref-style team-level statistics.

        if not dry_run:
            db.flush()
            run.status = "SUCCESS"
            run.records_processed = report.records_accepted
            run.finished_at = datetime.now(UTC)
            db.commit()
        else:
            db.rollback()
            run.status = "DRY_RUN"
            run.finished_at = datetime.now(UTC)
            db.add(run)
            db.commit()

        logger.info("Import complete: %s", report.summary())

    except Exception as exc:
        if not dry_run:
            db.rollback()
        run.status = "FAILED"
        run.error_summary = str(exc)
        run.finished_at = datetime.now(UTC)
        if not dry_run:
            db.add(run)
            db.commit()
        logger.error("Import failed: %s", exc)
        raise

    return report
