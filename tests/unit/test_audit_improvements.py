"""Audit pass 2 (2026-08-27) — regression tests for the P2 fixes.

1. Egress TEXT mode + fourth free mask (respx-mocked, fully offline):
   direct text success · JSON-mode failure on a healthy HTML 200 ·
   codetabs fallback · ``_strategies()`` order · text exhaustion.
2. ``GET /api/v1/players`` — bootstrap catalog price fallback.
3. ``GET /api/v1/players/search`` — typo tolerance, filters, sorts, and the
   xPTS-aware score blend (asserted against the 4-dp rounding, abs=1e-4).
4. Transfer ledger — reverse-pair dedupe (the live "swap listed twice" bug).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fpl_intelligence.api import deps
from fpl_intelligence.data_providers.fpl_egress import (
    FplEgressChain,
    FplEgressExhaustedError,
)
from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import (
    Gameweek,
    Player,
    PlayerGameweekPerformance,
    PlayerTeamMembership,
    Season,
    Team,
)
from fpl_intelligence.sync.materialized_models import PredictionCurrentDB
from fpl_intelligence.transfers.models import TransferLogDB
from fpl_intelligence.transfers.service import persist_ledger

# --------------------------------------------------------------------------- #
# 1) Egress text mode + codetabs mask (respx — no real network)
# --------------------------------------------------------------------------- #

_HTML = "<html><script>var playersData = [...];</script></html>"


class TestEgressTextMode:
    @respx.mock
    async def test_fetch_text_direct_success(self) -> None:
        """A healthy 200 HTML page must be returned as raw text (direct)."""
        respx.route(host="understat.test").respond(200, content=_HTML)
        chain = FplEgressChain("https://understat.test", cache_ttl=0)
        text = await chain.fetch_text("/league/EPL/2026")
        assert text == _HTML
        assert chain.winning_strategy == "direct"

    @respx.mock
    async def test_fetch_json_mode_rejects_healthy_html(self) -> None:
        """The JSON chain discards a healthy HTML 200 on EVERY strategy.

        This is the root cause behind the permanent stale
        "page reachable but no playersData block" Sources status.
        """
        respx.route(host="understat.test").respond(200, content=_HTML)
        respx.route(host="api.allorigins.win").respond(200, content=_HTML)
        respx.route(host="corsproxy.io").respond(200, content=_HTML)
        respx.route(host="api.codetabs.com").respond(200, content=_HTML)
        chain = FplEgressChain("https://understat.test", cache_ttl=0)
        with pytest.raises(FplEgressExhaustedError) as excinfo:
            await chain.fetch("/league/EPL/2026")
        tried = [name for name, _err in excinfo.value.attempts]
        assert tried == ["direct", "allorigins", "corsproxy", "codetabs", "env_proxy"]

    @respx.mock
    async def test_fetch_text_codetabs_fallback(self) -> None:
        """direct + allorigins + corsproxy blocked → codetabs (4th free mask)."""
        respx.route(host="understat.test").respond(403)
        respx.route(host="api.allorigins.win").respond(500)
        respx.route(host="corsproxy.io").respond(500)
        respx.route(host="api.codetabs.com").respond(200, content="<html>via codetabs</html>")
        chain = FplEgressChain("https://understat.test", cache_ttl=0)
        text = await chain.fetch_text("/league/EPL/2026")
        assert text == "<html>via codetabs</html>"
        assert chain.winning_strategy == "codetabs"

    def test_strategies_order_includes_codetabs(self) -> None:
        chain = FplEgressChain("https://fpl.test")
        assert [name for name, _fn in chain._strategies()] == [
            "direct",
            "allorigins",
            "corsproxy",
            "codetabs",
            "env_proxy",
        ]
        assert [name for name, _fn in chain._mask_strategies()] == [
            "allorigins",
            "corsproxy",
            "codetabs",
            "env_proxy",
        ]

    @respx.mock
    async def test_fetch_text_exhaustion_lists_every_strategy(self) -> None:
        respx.route(host="understat.test").respond(403)
        respx.route(host="api.allorigins.win").respond(500)
        respx.route(host="corsproxy.io").respond(500)
        respx.route(host="api.codetabs.com").respond(500)
        chain = FplEgressChain("https://understat.test", cache_ttl=0)
        with pytest.raises(FplEgressExhaustedError) as excinfo:
            await chain.fetch_text("/league/EPL/2026")
        tried = [name for name, _err in excinfo.value.attempts]
        assert tried == ["direct", "allorigins", "corsproxy", "codetabs", "env_proxy"]
        assert chain.winning_strategy is None


# --------------------------------------------------------------------------- #
# 2) + 3) Players endpoints (TestClient + in-memory SQLite + stub catalog)
# --------------------------------------------------------------------------- #


@pytest.fixture
def players_db() -> Session:
    """Four players: two with perf rows (price), two without (catalog fallback)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = factory()

    season = Season(code="2026/27", display_name="2026/27")
    db.add(season)
    db.flush()
    gw2 = Gameweek(season_id=season.id, provider_event_id=2, name="GW2")
    db.add(gw2)
    db.flush()
    liv = Team(id=13, name="Liverpool", short_name="LIV")
    che = Team(id=4, name="Chelsea", short_name="CHE")
    db.add_all([liv, che])
    db.flush()

    haaland = Player(
        first_name="Erling", second_name="Haaland", web_name="Haaland",
        position_code=4, fpl_element_id=445, fpl_code=223094,
    )
    salah = Player(
        first_name="Mohamed", second_name="Salah", web_name="Salah",
        position_code=4, fpl_element_id=1, fpl_code=108069,
    )
    kante = Player(
        first_name="N'Golo", second_name="Kante", web_name="Kante",
        position_code=3, fpl_element_id=7, fpl_code=110,
    )
    pickford = Player(
        first_name="Jordan", second_name="Pickford", web_name="Pickford",
        position_code=1, fpl_element_id=9, fpl_code=11000,
    )
    db.add_all([haaland, salah, kante, pickford])
    db.flush()

    # Only Salah + Pickford have a gameweek price snapshot; Haaland + Kante
    # must fall back to the bootstrap catalog price.
    db.add(PlayerGameweekPerformance(
        player_id=salah.id, gameweek_id=gw2.id, season_id=season.id,
        team_id=liv.id, price=13.0,
    ))
    db.add(PlayerGameweekPerformance(
        player_id=pickford.id, gameweek_id=gw2.id, season_id=season.id,
        team_id=liv.id, price=5.5,
    ))
    db.add(PlayerTeamMembership(player_id=salah.id, team_id=liv.id, season_id=season.id))
    db.add(PlayerTeamMembership(player_id=kante.id, team_id=che.id, season_id=season.id))
    db.add(PlayerTeamMembership(player_id=pickford.id, team_id=liv.id, season_id=season.id))

    # xPTS: newest gameweek with rows is GW2 (element-id space). The GW1 row
    # must be ignored (latest gameweek wins).
    now = datetime.now(UTC)
    db.add(PredictionCurrentDB(gameweek=2, element_id=445, expected_points=8.0, computed_at=now))
    db.add(PredictionCurrentDB(gameweek=2, element_id=1, expected_points=9.5, computed_at=now))
    db.add(PredictionCurrentDB(gameweek=2, element_id=9, expected_points=6.5, computed_at=now))
    db.add(PredictionCurrentDB(gameweek=1, element_id=445, expected_points=1.0, computed_at=now))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)


@pytest.fixture
def catalog_stub(monkeypatch: pytest.MonkeyPatch):
    """Deterministic bootstrap catalog via the documented monkeypatch seam."""
    from fpl_intelligence.api.routes import players as players_route
    from fpl_intelligence.prediction import live_provider as live_provider_mod

    catalog = {
        445: {"web_name": "Haaland", "price": 15.5, "position": 4, "team": 15, "team_short": "MCI", "selected_by_percent": 60.5},
        1: {"web_name": "Salah", "price": 13.0, "position": 4, "team": 13, "team_short": "LIV", "selected_by_percent": 99.0},
        7: {"web_name": "Kante", "price": 4.5, "position": 3, "team": 4, "team_short": "CHE", "selected_by_percent": 5.0},
        9: {"web_name": "Pickford", "price": 5.5, "position": 1, "team": 13, "team_short": "LIV", "selected_by_percent": 50.0},
    }
    monkeypatch.setattr(live_provider_mod, "load_player_catalog", lambda path=None: catalog)
    players_route._reset_catalog_cache()
    yield catalog
    players_route._reset_catalog_cache()


@pytest.fixture
def players_client(players_db: Session, catalog_stub: dict) -> TestClient:
    from fpl_intelligence.api.main import app

    def _override_db() -> Session:
        yield players_db

    app.dependency_overrides[deps._get_db_session] = _override_db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


class TestPlayersPriceFallback:
    def test_players_price_falls_back_to_catalog(self, players_client: TestClient) -> None:
        resp = players_client.get("/api/v1/players")
        assert resp.status_code == 200
        rows = {r["web_name"]: r for r in resp.json()}
        # Haaland: NO gameweek perf row → bootstrap catalog price (kills £—).
        assert rows["Haaland"]["price"] == pytest.approx(15.5)
        # Kante: no perf row → catalog price too.
        assert rows["Kante"]["price"] == pytest.approx(4.5)
        # Salah: perf-row price wins over the catalog.
        assert rows["Salah"]["price"] == pytest.approx(13.0)
        # fpl_element_id is the canonical id — never invented.
        assert rows["Haaland"]["fpl_element_id"] == 445
        assert rows["Haaland"]["code"] == 223094


class TestPlayersSearch:
    def test_search_typo_tolerant_and_score_blend(self, players_client: TestClient) -> None:
        """'haland' (transposition) finds Haaland ranked FIRST; score = blend."""
        resp = players_client.get("/api/v1/players/search", params={"q": "haland"})
        assert resp.status_code == 200
        hits = resp.json()
        # The transposed name ranks first (0.92 relevance); "Salah" is a
        # weaker partial match that still clears the 0.45 floor.
        assert [h["web_name"] for h in hits] == ["Haaland", "Salah"]
        top = hits[0]
        # xPTS from the NEWEST gameweek with predictions (GW2: 8.0, not GW1: 1.0).
        assert top["xpts"] == pytest.approx(8.0)
        expected = round(0.7 * top["relevance"] + 0.3 * min(1.0, 8.0 / 10.0), 4)
        assert top["score"] == pytest.approx(expected, abs=1e-4)
        # Catalog enrichment on the hit.
        assert top["team_short"] == "MCI"
        assert top["ownership_pct"] == pytest.approx(60.5)

    def test_search_filters_and_sort(self, players_client: TestClient) -> None:
        """position / team / max_price filters + all four sorts (q matches Salah+Kante)."""
        base = {"q": "slah kante"}

        resp = players_client.get("/api/v1/players/search", params={**base, "position": 3})
        assert [h["web_name"] for h in resp.json()] == ["Kante"]
        resp = players_client.get("/api/v1/players/search", params={**base, "position": 4})
        assert [h["web_name"] for h in resp.json()] == ["Salah"]

        resp = players_client.get("/api/v1/players/search", params={**base, "team": 13})
        assert [h["web_name"] for h in resp.json()] == ["Salah"]

        resp = players_client.get("/api/v1/players/search", params={**base, "max_price": 5.0})
        assert [h["web_name"] for h in resp.json()] == ["Kante"]

        # sort=price → descending: Salah £13.0m before Kante £4.5m (None last)
        resp = players_client.get("/api/v1/players/search", params={**base, "sort": "price"})
        assert [h["web_name"] for h in resp.json()] == ["Salah", "Kante"]
        # sort=xpts → Salah (9.5) before Kante (no prediction row → last)
        resp = players_client.get("/api/v1/players/search", params={**base, "sort": "xpts"})
        assert [h["web_name"] for h in resp.json()] == ["Salah", "Kante"]
        # sort=ownership → Salah (99%) before Kante (5%)
        resp = players_client.get("/api/v1/players/search", params={**base, "sort": "ownership"})
        assert [h["web_name"] for h in resp.json()] == ["Salah", "Kante"]
        # default sort=relevance → Salah (higher blended score) first
        resp = players_client.get("/api/v1/players/search", params=base)
        assert [h["web_name"] for h in resp.json()] == ["Salah", "Kante"]

    def test_search_cutoff_and_limit(self, players_client: TestClient) -> None:
        # Unrelated query: everything below the 0.45 relevance floor.
        resp = players_client.get("/api/v1/players/search", params={"q": "zzzz"})
        assert resp.status_code == 200
        assert resp.json() == []
        # Empty query is honest: no results, not an error.
        assert players_client.get("/api/v1/players/search", params={"q": ""}).json() == []
        # limit trims the list.
        resp = players_client.get("/api/v1/players/search", params={"q": "slah kante", "limit": 1})
        assert len(resp.json()) == 1
        assert resp.json()[0]["web_name"] == "Salah"


# --------------------------------------------------------------------------- #
# 4) Ledger reverse-pair dedupe
# --------------------------------------------------------------------------- #


class TestLedgerReversePairDedupe:
    def test_reverse_pair_updates_instead_of_inserting(self, db_session: Session) -> None:
        """The live bug: 'Cash(#32) ↔ De Cuyper(#115) swap listed twice'.

        A snapshot diff first infers in=115/out=32; a later re-diff of the
        SAME swap arrives with the sides flipped. The second persist must
        UPDATE the existing row (aligning to the newest inference), not add a
        duplicate.
        """
        persist_ledger(
            db_session,
            "794561",
            [{
                "gameweek": 5,
                "transfer_id": None,
                "element_in": 115,
                "element_out": 32,
                "cost": 0,
                "name_in": "De Cuyper",
                "name_out": "Cash",
            }],
            "snapshot-diff (unofficial)",
        )
        written = persist_ledger(
            db_session,
            "794561",
            [{
                "gameweek": 5,
                "transfer_id": None,
                "element_in": 32,
                "element_out": 115,
                "cost": 0,
                "name_in": "Cash",
                "name_out": "De Cuyper",
            }],
            "snapshot-diff (unofficial)",
        )
        assert written == 0, "the reversed duplicate must be folded, not inserted"

        rows = db_session.execute(
            select(TransferLogDB).where(TransferLogDB.entry_id == "794561")
        ).scalars().all()
        assert len(rows) == 1, f"swap listed {len(rows)} times — dedupe failed"
        row = rows[0]
        # Aligned to the NEWEST inference: in=Cash(32), out=De Cuyper(115).
        assert row.element_in == 32
        assert row.element_out == 115
        assert row.name_in == "Cash"
        assert row.name_out == "De Cuyper"
        assert row.transfer_id is None

        # Pre-existing exact-match idempotency must keep working: a repeat of
        # the SAME direction is folded too (and reports 0 rows written).
        same_direction = [{
            "gameweek": 7,
            "transfer_id": None,
            "element_in": 200,
            "element_out": 100,
            "cost": 0,
        }]
        assert persist_ledger(db_session, "794561", same_direction, "snapshot-diff (unofficial)") == 1
        assert persist_ledger(db_session, "794561", same_direction, "snapshot-diff (unofficial)") == 0
        all_rows = db_session.execute(
            select(TransferLogDB).where(TransferLogDB.entry_id == "794561")
        ).scalars().all()
        assert len(all_rows) == 2, "one deduped swap row + one same-direction row"
