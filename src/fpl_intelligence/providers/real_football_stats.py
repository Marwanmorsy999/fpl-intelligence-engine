"""Real football (team + advanced) statistics provider.

Secondary real adapter. Whereas :class:`RealFPLProvider` exposes FPL gameweek
performance (including per-player xG/xA), this provider exposes **team-level
match statistics** derived from the same real fixtures + gameweek data, and is
the extension point where richer advanced providers (Understat/FBref-style team
shots, possession, big chances) should be plugged in.

Team-level fields the FPL source does not contain (shots, shots on target,
possession, big chances) are reported as ``None`` rather than fabricated --
this is the honest missing-data representation required by Phase 4.75.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from fpl_intelligence.domain.environment import (
    DataEnvironment,
    SourceProvenance,
)
from fpl_intelligence.providers.real_fpl import (
    RealFPLProvider,
    _float_or_none,
    _int_or_none,
)


class RealFootballStatsProvider:
    """Real team-match stats provider built on the FPL mirror data."""

    schema_version = "v1"

    def __init__(self, fpl: RealFPLProvider | None = None) -> None:
        self._fpl = fpl or RealFPLProvider()
        self._cache: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self.environment = DataEnvironment.REAL

    @property
    def provider_name(self) -> str:
        return "real_football"

    @property
    def provenance(self) -> SourceProvenance:
        return SourceProvenance(
            provider=self.provider_name,
            environment=DataEnvironment.REAL,
            source_name="Derived from vaastav/Fantasy-Premier-League fixtures + gameweek xG",
            url="https://github.com/vaastav/Fantasy-Premier-League",
            access_method="public HTTP (raw.githubusercontent.com); team stats derived locally",
            retrieval_date=datetime.now(UTC).date().isoformat(),
            license_notes=(
                "Derived from the public vaastav/Fantasy-Premier-League FPL mirror. "
                "Team advanced fields (shots/possession) are not present in the source."
            ),
            fields_provided=[
                "goals_scored",
                "goals_conceded",
                "expected_goals",
                "expected_goals_conceded",
                "is_home",
            ],
            seasons_covered=[],
            known_limitations=[
                "shots / shots_on_target / possession / big chances unavailable",
                "team xG is the sum of player expected_goals from the FPL gameweek data",
            ],
        )

    def _team_match_records(self, season: str) -> dict[str, list[dict[str, object]]]:
        """Build team-match rows for a season, keyed by provider team id.

        Team xG per Gameweek = sum of that team's players' expected_goals in the
        Gameweek (real data). Team goals come from the fixture scores.
        """
        if season in self._cache:
            return dict(self._cache[season])

        # (team_id, gw) -> summed player expected_goals / conceded.
        team_xg: dict[tuple[int, int], float] = {}
        team_xgc: dict[tuple[int, int], float] = {}
        provider_dir = self._fpl._fetcher.raw_root / self._fpl.provider_name / season / "gws"
        gw_files = sorted(provider_dir.glob("gw*.csv")) if provider_dir.exists() else []
        for gw_path in gw_files:
            try:
                rows = list(csv.DictReader(io.StringIO(gw_path.read_text(encoding="utf-8"))))
            except OSError:  # pragma: no cover
                continue
            for r in rows:
                team = _int_or_none(r.get("team"))
                if team is None:
                    continue
                gw = _int_or_none(r.get("round")) or 0
                xg = _float_or_none(r.get("expected_goals")) or 0.0
                xgc = _float_or_none(r.get("expected_goals_conceded")) or 0.0
                team_xg[(team, gw)] = team_xg.get((team, gw), 0.0) + xg
                team_xgc[(team, gw)] = team_xgc.get((team, gw), 0.0) + xgc

        def _xg(team: int, gw: int, what: dict[tuple[int, int], float]) -> float | None:
            return round(what.get((team, gw), 0.0), 3)

        out: dict[str, list[dict[str, object]]] = {}
        for f in self._fpl.get_fixtures(season):
            gw = _int_or_none(f.get("gameweek")) or 0
            home = int(f["home_team_id"])
            away = int(f["away_team_id"])
            hs = _int_or_none(f.get("home_score")) or 0
            as_ = _int_or_none(f.get("away_score")) or 0
            fwid = f["provider_fixture_id"]
            out.setdefault(str(home), []).append(
                {
                    "provider_team_id": str(home),
                    "provider_fixture_id": fwid,
                    "is_home": True,
                    "goals_scored": hs,
                    "goals_conceded": as_,
                    "expected_goals": _xg(home, gw, team_xg),
                    "expected_goals_conceded": _xg(home, gw, team_xgc),
                    "shots": None,
                    "shots_on_target": None,
                    "possession": None,
                }
            )
            out.setdefault(str(away), []).append(
                {
                    "provider_team_id": str(away),
                    "provider_fixture_id": fwid,
                    "is_home": False,
                    "goals_scored": as_,
                    "goals_conceded": hs,
                    "expected_goals": _xg(away, gw, team_xg),
                    "expected_goals_conceded": _xg(away, gw, team_xgc),
                    "shots": None,
                    "shots_on_target": None,
                    "possession": None,
                }
            )
        self._cache[season] = out
        return dict(out)

    def get_seasons(self) -> Sequence[Mapping[str, object]]:
        return self._fpl.get_seasons()

    def get_teams(self, season: str) -> Sequence[Mapping[str, object]]:
        return self._fpl.get_teams(season)

    def get_players(self, season: str) -> Sequence[Mapping[str, object]]:
        return self._fpl.get_players(season)

    def get_fixtures(self, season: str) -> Sequence[Mapping[str, object]]:
        return self._fpl.get_fixtures(season)

    def get_player_match_stats(self, season: str, player_id: str) -> Sequence[Mapping[str, object]]:
        raise NotImplementedError(
            "Player match-level granularity is not present in the FPL mirror; "
            "plug an Understat/FBref adapter here for shot-level player stats."
        )

    def get_team_match_stats(self, season: str, team_id: str) -> Sequence[Mapping[str, object]]:
        return self._team_match_records(season).get(str(team_id), [])

    def get_fpl_history(self, season: str) -> Sequence[Mapping[str, object]]:
        return self._fpl.get_fpl_history(season)

    def get_fpl_snapshots(
        self, season: str, gameweek: int | None = None
    ) -> Sequence[Mapping[str, object]]:
        return self._fpl.get_fpl_snapshots(season, gameweek)
