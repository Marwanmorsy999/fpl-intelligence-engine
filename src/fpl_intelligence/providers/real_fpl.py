"""Real historical FPL data provider.

Implements the existing ``HistoricalFootballDataProvider`` protocol using real,
publicly-available historical FPL data from the ``vaastav/Fantasy-Premier-League``
mirror (a widely used, open, GitHub-hosted dataset of scraped FPL history).

All provider-specific source handling is isolated here. Raw payloads are cached
on disk under ``data/raw/real_fpl/<season>/...`` for reproducibility.

Temporal-integrity note (documented, not hidden):
The mirror's per-Gameweek ``gw*.csv`` files are post-hoc, cleaned exports. They
represent Gameweek-END state, NOT pre-deadline state. Therefore:
* performance fields (points/minutes/xG/xA) are legitimate *outcomes* and, once
  finalised for a past Gameweek, were genuinely available as features for later
  Gameweeks (strict-safe via Gameweek ordering);
* price / ownership / transfers are Gameweek-END snapshots and MUST NOT be
  treated as pre-deadline ``available_at`` values (``HISTORICAL_OUTCOME_ONLY``
  for snapshot-timing purposes);
* the mirror does not expose per-Gameweek ``selected_by_percent``, so ownership
  is only available as the absolute ``selected`` count.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from fpl_intelligence.domain.environment import (
    DataEnvironment,
    DatasetMarker,
    DatasetClass,
    SourceProvenance,
)
from fpl_intelligence.providers.github_fetcher import DiskCachingFetcher

_SEASON_STARTS = {
    "2022-23": {"start": datetime(2022, 8, 1, tzinfo=UTC), "end": datetime(2023, 5, 31, tzinfo=UTC)},
    "2023-24": {"start": datetime(2023, 8, 1, tzinfo=UTC), "end": datetime(2024, 5, 31, tzinfo=UTC)},
    "2024-25": {"start": datetime(2024, 8, 1, tzinfo=UTC), "end": datetime(2025, 5, 31, tzinfo=UTC)},
    "2025-26": {"start": datetime(2025, 8, 1, tzinfo=UTC), "end": datetime(2026, 5, 31, tzinfo=UTC)},
}

MIRROR_BASE = (
    "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/" "master/data/{season}"
)

# Public / licensing note for the mirror.
LICENSE_NOTES = (
    "vaastav/Fantasy-Premier-League is a public GitHub repository of FPL historical "
    "data. Verify upstream licensing before any commercial use."
)


class RealFPLProvider:
    """Real historical FPL provider backed by the vaastav mirror."""

    schema_version = "v1"

    def __init__(
        self,
        fetcher: DiskCachingFetcher | None = None,
        seasons: Sequence[str] | None = None,
        max_gameweeks: int = 38,
    ) -> None:
        self._fetcher = fetcher or DiskCachingFetcher()
        self._seasons = list(seasons or list(_SEASON_STARTS.keys()))
        self._max_gameweeks = max_gameweeks
        self._cache: dict[str, list[Mapping[str, object]]] = {}
        self.environment = DataEnvironment.REAL

    @property
    def provider_name(self) -> str:
        return "real_fpl"

    @property
    def provenance(self) -> SourceProvenance:
        return SourceProvenance(
            provider=self.provider_name,
            environment=DataEnvironment.REAL,
            source_name="vaastav/Fantasy-Premier-League (FPL historical mirror)",
            url="https://github.com/vaastav/Fantasy-Premier-League",
            access_method="public HTTP (raw.githubusercontent.com)",
            retrieval_date=datetime.now(UTC).date().isoformat(),
            license_notes=LICENSE_NOTES,
            fields_provided=[
                "teams", "players", "fixtures", "gameweek_performance",
                "xG/xA", "price", "ownership(selected)", "transfers",
            ],
            seasons_covered=list(self._seasons),
            known_limitations=[
                "Gameweek-end (not pre-deadline) price/ownership snapshots",
                "no per-Gameweek selected_by_percent (only absolute 'selected')",
                "post-hoc cleaned exports; exact availability timestamps not recorded",
            ],
        )

    def dataset_marker(self, dataset: str) -> DatasetMarker:
        """Temporal marker for a specific real dataset."""
        if dataset == "fpl_history":
            temporal = DatasetClass.STRICT_BACKTEST_SAFE
            timing = "gameweek outcome; available after each gameweek (strict via ordering)"
        elif dataset == "fpl_snapshots":
            temporal = DatasetClass.HISTORICAL_OUTCOME_ONLY
            timing = "gameweek_end (post-deadline); NOT pre-deadline"
        else:
            temporal = DatasetClass.STRICT_BACKTEST_SAFE
            timing = None
        return DatasetMarker(
            provider=self.provider_name,
            environment=self.environment,
            dataset=dataset,
            temporal_class=temporal,
            snapshot_timing=timing,
        )
    # ------------------------------------------------------------------ utils
    def _mirror_url(self, season: str, path: str) -> str:
        return f"{MIRROR_BASE.format(season=season)}/{path}"

    def _load_csv(self, season: str, dataset: str, filename: str, url: str) -> list[dict[str, Any]]:
        import csv
        import io

        if (season, dataset) in self._cache:
            return list(self._cache[(season, dataset)])
        path = self._fetcher.ensure_cached(self.provider_name, season, dataset, filename, url)
        rows = [r for r in csv.DictReader(io.StringIO(path.read_text(encoding="utf-8")))]
        self._cache[(season, dataset)] = rows
        return list(rows)

    # ----------------------------------------------------------- protocol
    def get_seasons(self) -> Sequence[Mapping[str, object]]:
        out: list[dict[str, object]] = []
        for season in self._seasons:
            info = _SEASON_STARTS.get(season)
            out.append(
                {
                    "season_name": season,
                    "start_date": info["start"] if info else None,
                    "end_date": info["end"] if info else None,
                    "competition": "Premier League",
                }
            )
        return out

    def get_teams(self, season: str) -> Sequence[Mapping[str, object]]:
        url = self._mirror_url(season, "teams.csv")
        rows = self._load_csv(season, "teams", "teams.csv", url)
        return [
            {
                "provider_team_id": str(r.get("id", "")),
                "name": r.get("name", "Unknown"),
                "short_name": r.get("short_name"),
            }
            for r in rows
        ]

    def get_players(self, season: str) -> Sequence[Mapping[str, object]]:
        url = self._mirror_url(season, "players_raw.csv")
        rows = self._load_csv(season, "players", "players_raw.csv", url)
        out: list[dict[str, object]] = []
        for r in rows:
            pid = str(r.get("id", ""))
            if not pid:
                continue
            pos = r.get("element_type")
            try:
                pos_code = int(pos) if pos not in (None, "") else None
            except (TypeError, ValueError):
                pos_code = None
            out.append(
                {
                    "provider_player_id": pid,
                    "first_name": r.get("first_name", ""),
                    "second_name": r.get("second_name", ""),
                    "web_name": r.get("web_name", "")
                    or f"{r.get('first_name', '')} {r.get('second_name', '')}",
                    "position_code": pos_code,
                    "team_id": str(r.get("team", "")) if r.get("team") not in (None, "") else None,
                }
            )
        return out

    def get_fixtures(self, season: str) -> Sequence[Mapping[str, object]]:
        url = self._mirror_url(season, "fixtures.csv")
        rows = self._load_csv(season, "fixtures", "fixtures.csv", url)
        out: list[dict[str, object]] = []
        for r in rows:
            home = str(r.get("team_h", ""))
            away = str(r.get("team_a", ""))
            if not home or not away:
                continue
            finished = str(r.get("finished", "")).lower() == "true"
            out.append(
                {
                    "provider_fixture_id": str(r.get("id", "")),
                    "gameweek": _int_or_none(r.get("event")),
                    "kickoff_time": _dt_or_none(r.get("kickoff_time")),
                    "home_team_id": home,
                    "away_team_id": away,
                    "home_score": _int_or_none(r.get("team_h_score")),
                    "away_score": _int_or_none(r.get("team_a_score")),
                    "status": "finished" if finished else "scheduled",
                    "postponed": False,
                }
            )
        return out

    # -- Gameweek performance + snapshots ------------------------------------
    def _gameweek_files(self, season: str) -> list[int]:
        import os
        import re

        gws_dir = self._fetcher.raw_root / self.provider_name / season / "gws"
        if gws_dir.exists():
            nums = sorted(
                {
                    int(m.group(1))
                    for f in os.listdir(gws_dir)
                    if (m := re.match(r"gw(\d+)\.csv$", f))
                }
            )
            if nums:
                return nums
        return list(range(1, self._max_gameweeks + 1))

    def get_fpl_history(self, season: str) -> Sequence[Mapping[str, object]]:
        out: list[dict[str, object]] = []
        for gw_num in self._gameweek_files(season):
            filename = f"gw{gw_num}.csv"
            url = self._mirror_url(season, f"gws/{filename}")
            try:
                rows = self._load_csv(season, f"gws/gw{gw_num}", filename, url)
            except Exception:  # noqa: BLE001 -- blank gameweek file is not fatal
                continue
            for r in rows:
                pid = str(r.get("element", ""))
                if not pid:
                    continue
                out.append(self._history_row(season, gw_num, r))
        return out

    def _history_row(self, season: str, gw_num: int, r: Mapping[str, Any]) -> dict[str, object]:
        value = _int_or_none(r.get("value"))
        return {
            "provider_player_id": str(r.get("element", "")),
            "season_name": season,
            "gameweek": gw_num,
            "total_points": _int_or_none(r.get("total_points")),
            "minutes": _int_or_none(r.get("minutes")),
            "goals_scored": _int_or_none(r.get("goals_scored")),
            "assists": _int_or_none(r.get("assists")),
            "clean_sheets": _int_or_none(r.get("clean_sheets")),
            "goals_conceded": _int_or_none(r.get("goals_conceded")),
            "own_goals": _int_or_none(r.get("own_goals")),
            "penalties_saved": _int_or_none(r.get("penalties_saved")),
            "penalties_missed": _int_or_none(r.get("penalties_missed")),
            "yellow_cards": _int_or_none(r.get("yellow_cards")),
            "red_cards": _int_or_none(r.get("red_cards")),
            "saves": _int_or_none(r.get("saves")),
            "bonus": _int_or_none(r.get("bonus")),
            "bps": _int_or_none(r.get("bps")),
            "influence": _float_or_none(r.get("influence")),
            "creativity": _float_or_none(r.get("creativity")),
            "threat": _float_or_none(r.get("threat")),
            "ict_index": _float_or_none(r.get("ict_index")),
            "expected_goals": _float_or_none(r.get("expected_goals")),
            "expected_assists": _float_or_none(r.get("expected_assists")),
            "expected_goal_involvements": _float_or_none(r.get("expected_goal_involvements")),
            "expected_goals_conceded": _float_or_none(r.get("expected_goals_conceded")),
            "value": value,
            "transfers_balance": _int_or_none(r.get("transfers_balance")),
            "selected": _int_or_none(r.get("selected")),
            "transfers_in": _int_or_none(r.get("transfers_in")),
            "transfers_out": _int_or_none(r.get("transfers_out")),
            "price": (value / 10.0) if value is not None else None,
            "selected_by_percent": None,  # not available per-GW in mirror
            "form": None,
            "points_per_game": None,
            "ep_this": None,
            "ep_next": None,
        }

    def get_fpl_snapshots(
        self, season: str, gameweek: int | None = None
    ) -> Sequence[Mapping[str, object]]:
        out: list[dict[str, object]] = []
        for gw_num in self._gameweek_files(season):
            if gameweek is not None and gw_num != gameweek:
                continue
            filename = f"gw{gw_num}.csv"
            url = self._mirror_url(season, f"gws/{filename}")
            try:
                rows = self._load_csv(season, f"gws/gw{gw_num}", filename, url)
            except Exception:  # noqa: BLE001
                continue
            for r in rows:
                pid = str(r.get("element", ""))
                if not pid:
                    continue
                row = self._history_row(season, gw_num, r)
                # Snapshot event_time = gameweek-end kickoff time. This is NOT
                # pre-deadline state (HISTORICAL_OUTCOME_ONLY).
                out.append(
                    {
                        "provider_player_id": pid,
                        "gameweek": gw_num,
                        "event_time": _dt_or_none(r.get("kickoff_time")),
                        "price": row["price"],
                        "selected_by_percent": None,
                        "transfers_in_event": row["transfers_in"],
                        "transfers_out_event": row["transfers_out"],
                        "transfers_in_season": None,
                        "transfers_out_season": None,
                        "total_points": row["total_points"],
                        "form": None,
                        "points_per_game": None,
                        "form_rank": None,
                        "points_per_game_rank": None,
                        "selected_rank": None,
                        "ep_this": None,
                        "ep_next": None,
                    }
                )
        return out

    def get_player_match_stats(self, season: str, player_id: str) -> Sequence[Mapping[str, object]]:
        raise NotImplementedError("Player-match stats are exposed by the football-stats provider")

    def get_team_match_stats(self, season: str, team_id: str) -> Sequence[Mapping[str, object]]:
        raise NotImplementedError("Team-match stats are exposed by the football-stats provider")


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dt_or_none(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

