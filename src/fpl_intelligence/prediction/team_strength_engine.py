"""Temporal team-strength engine and derived fixture probabilities.

This module is the single quantitative owner for team strength.  It works on
small immutable ``TeamMatch`` rows, which keeps the estimator deterministic,
testable, and independent of a database session.  ``from_db`` is the only
adapter needed for canonical SQLAlchemy data.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fpl_intelligence.db.models import Fixture, Season, TeamMatchPerformance
from fpl_intelligence.features.temporal import DEFAULT_POLICY, InformationAccessPolicy


@dataclass(frozen=True)
class TeamMatch:
    team_id: int
    fixture_id: int
    season: str
    event_time: datetime
    available_at: datetime | None
    ingested_at: datetime | None
    is_home: bool
    goals_scored: float
    goals_conceded: float
    expected_goals: float | None = None
    expected_goals_conceded: float | None = None


@dataclass(frozen=True)
class TeamStrengthEstimate:
    team_id: int
    cutoff_time: datetime
    attack_strength: float
    defence_strength: float
    home_attack_strength: float
    away_attack_strength: float
    home_defence_strength: float
    away_defence_strength: float
    sample_size: int
    confidence: float
    method: str
    window: int | None = None
    decay: float | None = None

    @property
    def home_strength(self) -> float:
        return self.home_attack_strength

    @property
    def away_strength(self) -> float:
        return self.away_attack_strength

    @property
    def completeness(self) -> float:
        return self.confidence

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {
            "home_strength": self.home_strength,
            "away_strength": self.away_strength,
            "completeness": self.completeness,
        }


@dataclass(frozen=True)
class FixtureProbability:
    fixture_id: int
    cutoff_time: datetime
    expected_home_goals: float
    expected_away_goals: float
    home_win_probability: float
    draw_probability: float
    away_win_probability: float
    home_clean_sheet_probability: float
    away_clean_sheet_probability: float
    home_team_goals_2_plus_probability: float
    home_team_goals_3_plus_probability: float
    away_team_goals_2_plus_probability: float
    away_team_goals_3_plus_probability: float
    model_version: str = "2.0.0"
    feature_version: str = "team-strength-2.0.0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"cutoff_time": self.cutoff_time.isoformat()}


class TeamStrengthEngine:
    """Estimate relative team strengths using only rows known by a cutoff."""

    model_name = "team_strength_engine"
    model_version = "2.0.0"
    feature_version = "team-strength-2.0.0"
    league_average_goals = 1.4

    def __init__(
        self,
        rows: Iterable[TeamMatch] = (),
        policy: InformationAccessPolicy = DEFAULT_POLICY,
        league_average_goals: float = league_average_goals,
    ) -> None:
        self.rows = tuple(rows)
        self.policy = policy
        self.league_average_goals = league_average_goals

    @classmethod
    def from_db(
        cls,
        db: Session,
        season_codes: Iterable[str] | None = None,
        policy: InformationAccessPolicy = DEFAULT_POLICY,
        league_average_goals: float = 1.4,
    ) -> TeamStrengthEngine:
        season_filter = set(season_codes or ())
        seasons = {s.id: s.code for s in db.scalars(select(Season)).all()}
        stmt = select(TeamMatchPerformance, Fixture).join(
            Fixture, Fixture.id == TeamMatchPerformance.fixture_id
        )
        rows: list[TeamMatch] = []
        for performance, fixture in db.execute(stmt):
            season = seasons.get(performance.season_id)
            if season is None or (season_filter and season not in season_filter):
                continue
            if fixture.kickoff_time is None:
                continue
            rows.append(
                TeamMatch(
                    team_id=performance.team_id,
                    fixture_id=performance.fixture_id,
                    season=season,
                    event_time=fixture.kickoff_time,
                    available_at=performance.available_at,
                    ingested_at=performance.ingested_at,
                    is_home=bool(performance.is_home),
                    goals_scored=float(performance.goals_scored or 0),
                    goals_conceded=float(performance.goals_conceded or 0),
                    expected_goals=performance.expected_goals,
                    expected_goals_conceded=performance.expected_goals_conceded,
                )
            )
        return cls(rows, policy, league_average_goals)

    def _prior(self, cutoff: datetime) -> list[TeamMatch]:
        return sorted(
            (
                r
                for r in self.rows
                if r.event_time < cutoff
                and r.available_at is not None
                and r.ingested_at is not None
                and r.available_at <= cutoff
                and r.ingested_at <= cutoff
            ),
            key=lambda r: r.event_time,
        )

    @staticmethod
    def _weighted_mean(values: list[float], weights: list[float]) -> float:
        return sum(v * w for v, w in zip(values, weights, strict=True)) / max(sum(weights), 1e-12)

    def estimate(
        self,
        team_id: int,
        cutoff_time: datetime,
        method: str = "rolling_goals",
        window: int = 5,
        decay: float = 0.9,
    ) -> TeamStrengthEstimate:
        prior = self._prior(cutoff_time)
        team_rows = [r for r in prior if r.team_id == team_id]
        if method in {"rolling_goals", "rolling_xg", "poisson"}:
            team_rows = team_rows[-window:]
        weights = (
            [decay ** (len(team_rows) - i - 1) for i in range(len(team_rows))]
            if method == "ewma"
            else [1.0] * len(team_rows)
        )
        league_home = [r.goals_scored for r in prior if r.is_home]
        league_away = [r.goals_scored for r in prior if not r.is_home]
        league_conceded_home = [r.goals_conceded for r in prior if r.is_home]
        league_conceded_away = [r.goals_conceded for r in prior if not r.is_home]
        avg_home = sum(league_home) / max(len(league_home), 1)
        avg_away = sum(league_away) / max(len(league_away), 1)
        avg_def_home = sum(league_conceded_home) / max(len(league_conceded_home), 1)
        avg_def_away = sum(league_conceded_away) / max(len(league_conceded_away), 1)
        if not team_rows:
            return TeamStrengthEstimate(
                team_id, cutoff_time, 1, 1, 1, 1, 1, 1, 0, 0, method, window, decay
            )

        use_xg = method == "rolling_xg" or (
            method == "poisson"
            and sum(r.expected_goals is not None for r in team_rows) >= len(team_rows) / 2
        )
        scored = [
            float(r.expected_goals if use_xg and r.expected_goals is not None else r.goals_scored)
            for r in team_rows
        ]
        conceded = [
            float(
                r.expected_goals_conceded
                if use_xg and r.expected_goals_conceded is not None
                else r.goals_conceded
            )
            for r in team_rows
        ]
        if method == "poisson":
            # Gamma(1, 1) shrinkage prevents zero-rate teams and is fitted only
            # from the pre-cutoff sample, never from the test fixture.
            weights = [1.0] * len(team_rows)
            scored = scored + [self.league_average_goals]
            conceded = conceded + [self.league_average_goals]
            weights = weights + [1.0]
        attack_rate = self._weighted_mean(scored, weights)
        conceded_rate = self._weighted_mean(conceded, weights)
        home = [i for i, r in enumerate(team_rows) if r.is_home]
        away = [i for i, r in enumerate(team_rows) if not r.is_home]
        home_attack = (
            self._weighted_mean([scored[i] for i in home], [weights[i] for i in home])
            if home
            else avg_home
        )
        away_attack = (
            self._weighted_mean([scored[i] for i in away], [weights[i] for i in away])
            if away
            else avg_away
        )
        home_def = (
            self._weighted_mean([conceded[i] for i in home], [weights[i] for i in home])
            if home
            else avg_def_home
        )
        away_def = (
            self._weighted_mean([conceded[i] for i in away], [weights[i] for i in away])
            if away
            else avg_def_away
        )
        result = TeamStrengthEstimate(
            team_id=team_id,
            cutoff_time=cutoff_time,
            attack_strength=max(0.2, min(2.5, attack_rate / self.league_average_goals)),
            defence_strength=max(
                0.2, min(2.5, self.league_average_goals / max(conceded_rate, 0.1))
            ),
            home_attack_strength=max(0.2, min(2.5, home_attack / max(avg_home, 0.1))),
            away_attack_strength=max(0.2, min(2.5, away_attack / max(avg_away, 0.1))),
            home_defence_strength=max(0.2, min(2.5, avg_def_home / max(home_def, 0.1))),
            away_defence_strength=max(0.2, min(2.5, avg_def_away / max(away_def, 0.1))),
            sample_size=len(team_rows),
            confidence=min(1.0, len(team_rows) / 10.0),
            method=method,
            window=window,
            decay=decay if method == "ewma" else None,
        )
        return result

    def estimate_all(
        self, cutoff_time: datetime, method: str = "poisson", window: int = 5, decay: float = 0.9
    ) -> dict[int, TeamStrengthEstimate]:
        return {
            team_id: self.estimate(team_id, cutoff_time, method, window, decay)
            for team_id in sorted({r.team_id for r in self._prior(cutoff_time)})
        }

    def home_advantage(self, cutoff_time: datetime) -> float:
        prior = self._prior(cutoff_time)
        home = [r.goals_scored for r in prior if r.is_home]
        away = [r.goals_scored for r in prior if not r.is_home]
        return max(
            0.8, min(1.5, (sum(home) / max(len(home), 1)) / max(sum(away) / max(len(away), 1), 0.1))
        )

    def fixture_probability(
        self,
        fixture_id: int,
        cutoff_time: datetime,
        home: TeamStrengthEstimate,
        away: TeamStrengthEstimate,
    ) -> FixtureProbability:
        home_lambda = (
            self.league_average_goals
            * home.home_attack_strength
            / max(away.away_defence_strength, 0.2)
            * self.home_advantage(cutoff_time)
        )
        away_lambda = (
            self.league_average_goals
            * away.away_attack_strength
            / max(home.home_defence_strength, 0.2)
        )
        home_lambda, away_lambda = (
            max(0.05, min(5.0, home_lambda)),
            max(0.05, min(5.0, away_lambda)),
        )
        distribution = {
            (h, a): self._pmf(h, home_lambda) * self._pmf(a, away_lambda)
            for h in range(11)
            for a in range(11)
        }
        total = sum(distribution.values())
        distribution = {key: value / total for key, value in distribution.items()}
        return FixtureProbability(
            fixture_id,
            cutoff_time,
            home_lambda,
            away_lambda,
            sum(p for (h, a), p in distribution.items() if h > a),
            sum(p for (h, a), p in distribution.items() if h == a),
            sum(p for (h, a), p in distribution.items() if h < a),
            sum(p for (h, a), p in distribution.items() if a == 0),
            sum(p for (h, a), p in distribution.items() if h == 0),
            sum(p for (h, _), p in distribution.items() if h >= 2),
            sum(p for (h, _), p in distribution.items() if h >= 3),
            sum(p for (_, a), p in distribution.items() if a >= 2),
            sum(p for (_, a), p in distribution.items() if a >= 3),
        )

    @staticmethod
    def _pmf(goals: int, lam: float) -> float:
        return math.exp(-lam) * lam**goals / math.factorial(goals)

    def metadata(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "model_version": self.model_version,
            "model_type": "poisson_team_strength",
            "feature_version": self.feature_version,
            "hyperparameters": {"league_average_goals": self.league_average_goals},
            "random_seed": None,
        }

    def save(self, artifact_location: str) -> str:
        path = Path(artifact_location)
        path.mkdir(parents=True, exist_ok=True)
        target = path / "metadata.json"
        target.write_text(json.dumps(self.metadata(), sort_keys=True), encoding="utf-8")
        return str(path)

    @classmethod
    def load(cls, artifact_path: str) -> TeamStrengthEngine:
        metadata = json.loads((Path(artifact_path) / "metadata.json").read_text(encoding="utf-8"))
        return cls(league_average_goals=float(metadata["hyperparameters"]["league_average_goals"]))

    # Compatibility with the previous DB-facing API.
    def estimate_team(
        self,
        db: Session,
        team_id: int,
        cutoff_time: datetime,
        method: str = "rolling",
        window: int | None = 5,
    ) -> TeamStrengthEstimate:
        engine = TeamStrengthEngine.from_db(
            db, policy=self.policy, league_average_goals=self.league_average_goals
        )
        mapped = "rolling_goals" if method == "rolling" else "ewma"
        return engine.estimate(team_id, cutoff_time, mapped, window or 5)

    def estimate_all_from_db(
        self, db: Session, cutoff_time: datetime, method: str = "poisson", window: int = 5
    ) -> dict[int, TeamStrengthEstimate]:
        return TeamStrengthEngine.from_db(
            db, policy=self.policy, league_average_goals=self.league_average_goals
        ).estimate_all(cutoff_time, method, window)

    def persist(
        self,
        db: Session,
        estimate: TeamStrengthEstimate,
        season: str,
        feature_version: str | None = None,
    ) -> Any:
        from fpl_intelligence.prediction.models import TeamStrengthRecord

        record = TeamStrengthRecord(
            team_id=estimate.team_id,
            season=season,
            cutoff_time=estimate.cutoff_time,
            feature_version=feature_version or self.feature_version,
            attack_strength=estimate.attack_strength,
            defence_strength=estimate.defence_strength,
            home_strength=estimate.home_strength,
            away_strength=estimate.away_strength,
            home_attack_strength=estimate.home_attack_strength,
            away_attack_strength=estimate.away_attack_strength,
            home_defence_strength=estimate.home_defence_strength,
            away_defence_strength=estimate.away_defence_strength,
            sample_size=estimate.sample_size,
            completeness=estimate.completeness,
        )
        db.add(record)
        db.flush()
        return record


# Public alias retained for callers that used the old class name.
TeamStrengthModel = TeamStrengthEngine
