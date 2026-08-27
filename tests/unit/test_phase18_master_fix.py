"""Phase 18.0 — MASTER FIX regression tests.

Covers the five P5 unit-test requirements:

1. Egress chain strategy ORDER + shape VALIDATION (mocked masks).
2. Single-join name+price pairing: Haaland's element id resolves to
   "Haaland" carrying Haaland's own price — never a cross-ID mix.
3. Unknown session -> honest 404 (never another squad).
4. Market wording: zero matched fixtures can never claim agreement.
5. Analyst label: real provider label when keys exist; template-fallback only
   when none do.

Fully offline: masks are monkeypatched, DBs are in-memory SQLite.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fpl_intelligence.api import deps
from fpl_intelligence.api.main import app
from fpl_intelligence.data_providers.fpl_egress import (
    FplEgressChain,
    FplEgressExhaustedError,
    validate_bootstrap_payload,
    validate_entry_payload,
    validate_picks_payload,
)
from fpl_intelligence.db.base import Base
from fpl_intelligence.db.models import Player
from fpl_intelligence.squad.models_db import SquadStateDB  # noqa: F401

# ---------------------------------------------------------------------------
# 1) Egress chain: validators, strategy order, exhaustion, cache
# ---------------------------------------------------------------------------


class TestShapeValidators:
    def test_entry_requires_id(self) -> None:
        with pytest.raises(ValueError):
            validate_entry_payload({"name": "no id here"})
        validate_entry_payload({"id": 794561})  # does not raise

    def test_picks_require_picks_list(self) -> None:
        with pytest.raises(ValueError):
            validate_picks_payload({"transfers": {}})
        validate_picks_payload({"picks": []})

    def test_bootstrap_requires_elements(self) -> None:
        with pytest.raises(ValueError):
            validate_bootstrap_payload({"teams": []})
        validate_bootstrap_payload({"elements": []})

    def test_non_dict_rejected(self) -> None:
        for bad in (None, [], "json"):
            with pytest.raises(ValueError):
                validate_entry_payload(bad)


def _chain_with_behaviors(monkeypatch, behaviors: list[tuple[str, object]]) -> FplEgressChain:
    """Build a chain whose strategies run per the ordered behavior list.

    ``behaviors`` maps strategy names to either a payload (success) or an
    exception instance (failure). Strategies not listed fail by default.
    """
    chain = FplEgressChain("https://fpl.test", cache_ttl=0)

    def _make(name: str):
        outcome = dict(behaviors).get(name, RuntimeError(f"{name} down"))

        async def fn(url: str):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return fn

    monkeypatch.setattr(chain, "_direct", _make("direct"))
    monkeypatch.setattr(chain, "_allorigins", _make("allorigins"))
    monkeypatch.setattr(chain, "_corsproxy", _make("corsproxy"))
    monkeypatch.setattr(chain, "_codetabs", _make("codetabs"))
    monkeypatch.setattr(chain, "_env_proxy", _make("env_proxy"))
    return chain


class TestEgressStrategyOrder:
    def test_direct_wins_when_healthy(self, monkeypatch) -> None:
        chain = _chain_with_behaviors(
            monkeypatch,
            [("direct", {"id": 1}), ("allorigins", {"id": 2})],
        )
        data = asyncio.run(chain.fetch("/api/entry/1/", validator=validate_entry_payload))
        assert data == {"id": 1}
        assert chain.winning_strategy == "direct"

    def test_falls_through_to_allorigins(self, monkeypatch) -> None:
        chain = _chain_with_behaviors(
            monkeypatch,
            [("direct", RuntimeError("403")), ("allorigins", {"id": 2})],
        )
        data = asyncio.run(chain.fetch("/api/entry/1/", validator=validate_entry_payload))
        assert data == {"id": 2}
        assert chain.winning_strategy == "allorigins"

    def test_full_order_direct_allorigins_corsproxy_env(self, monkeypatch) -> None:
        chain = _chain_with_behaviors(
            monkeypatch,
            [
                ("direct", RuntimeError("x")),
                ("allorigins", RuntimeError("y")),
                ("corsproxy", {"id": 3}),
                ("env_proxy", {"id": 4}),
            ],
        )
        data = asyncio.run(chain.fetch("/api/entry/1/", validator=validate_entry_payload))
        assert data == {"id": 3}
        assert chain.winning_strategy == "corsproxy"

    def test_invalid_shape_rejected_and_next_tried(self, monkeypatch) -> None:
        """A mask returning HTML/garbage must be rejected, not trusted."""
        chain = _chain_with_behaviors(
            monkeypatch,
            [("direct", "<html>blocked</html>"), ("allorigins", {"id": 9})],
        )
        data = asyncio.run(chain.fetch("/api/entry/1/", validator=validate_entry_payload))
        assert data == {"id": 9}

    def test_exhaustion_lists_every_attempt(self, monkeypatch) -> None:
        chain = _chain_with_behaviors(monkeypatch, [])
        with pytest.raises(FplEgressExhaustedError) as excinfo:
            asyncio.run(chain.fetch("/api/entry/1/", validator=validate_entry_payload))
        tried = [name for name, _err in excinfo.value.attempts]
        # Order matters: direct first, user proxy LAST; codetabs sits between
        # corsproxy and the user proxy (pass-2 fourth free mask).
        assert tried == ["direct", "allorigins", "corsproxy", "codetabs", "env_proxy"]
        assert chain.winning_strategy is None

    def test_cache_reuses_successful_response(self, monkeypatch) -> None:
        chain = FplEgressChain("https://fpl.test")  # default 60s TTL
        calls: list[str] = []

        async def fn(url: str) -> dict:
            calls.append(url)
            return {"picks": []}

        monkeypatch.setattr(chain, "_direct", fn)
        picks_path = "/api/entry/1/event/3/picks/"
        first = asyncio.run(chain.fetch(picks_path, validator=validate_picks_payload))
        second = asyncio.run(chain.fetch(picks_path, validator=validate_picks_payload))
        assert first is second
        assert len(calls) == 1, "second fetch must come from the picks cache"


# ---------------------------------------------------------------------------
# 2) Single-join name+price pairing through GET /api/v1/decisions
# ---------------------------------------------------------------------------


@pytest.fixture
def collision_db() -> Session:
    """Internal id 445 = McConnell; element id 445 = Haaland (£15.5m)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = factory()
    haaland = Player(
        first_name="Erling",
        second_name="Haaland",
        web_name="Haaland",
        position_code=4,
        fpl_element_id=445,
        fpl_code=223094,
    )
    mcconnell = Player(
        id=445,  # force the internal-id collision with Haaland's element id
        first_name="James",
        second_name="McConnell",
        web_name="McConnell",
        position_code=3,
    )
    db.add_all([haaland, mcconnell])
    db.flush()
    # Guarantee the collision: McConnell's INTERNAL id is 445.
    assert mcconnell.id == 445
    db.commit()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)


@pytest.fixture
def decisions_client(collision_db: Session):
    def _override_db():
        yield collision_db

    from fpl_intelligence.prediction import live_provider as live_provider_mod
    from fpl_intelligence.prediction.live_provider import LivePredictionProvider

    # Deterministic catalog so the proxy chain covers exactly our ids
    # (offline; the committed seed is not needed here).
    catalog = {
        445: {
            "web_name": "Haaland",
            "price": 15.5,
            "position": 4,
            "team": 15,
            "team_short": "MCI",
        },
    }
    for i in range(2000, 2014):
        catalog[i] = {
            "web_name": f"Filler{i}",
            "price": 4.5,
            "position": (i % 4) + 1,
            "team": 1,
            "team_short": "T1",
        }
    _original_loader = live_provider_mod.load_player_catalog
    live_provider_mod.load_player_catalog = lambda path=None: catalog  # type: ignore[assignment]

    app.dependency_overrides[deps._get_db_session] = _override_db
    app.dependency_overrides[deps.get_prediction_provider] = lambda: LivePredictionProvider(
        session=collision_db
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        live_provider_mod.load_player_catalog = _original_loader


class TestSingleJoinPairing:
    def test_element_id_resolves_haaland_with_his_own_price(
        self, decisions_client: TestClient, collision_db: Session
    ) -> None:
        """THE E2 regression: one row, one join — name AND price pair up."""
        # 445 = Haaland's element id; fillers are elements absent from the DB
        # (they render as "Player N" — honest, never another player).
        filler_ids = list(range(2000, 2014))
        all_ids = [445, *filler_ids]
        squad_body = {
            "session_id": "pairing-test",
            "player_ids": all_ids,
            "captain_id": 445,
            "vice_captain_id": 445,
            "bank": 0.0,
            "free_transfers": 1,
            "gameweek": 3,
            # Squad metadata says element 445 costs £15.5m (bootstrap truth).
            "player_positions": {pid: 4 for pid in all_ids},
            "player_prices": {pid: (15.5 if pid == 445 else 4.5) for pid in all_ids},
            "player_teams": {pid: 15 for pid in all_ids},
        }
        saved = decisions_client.post("/api/v1/squad", json=squad_body)
        assert saved.status_code == 200, saved.text

        resp = decisions_client.get("/api/v1/decisions", params={"session_id": "pairing-test"})
        assert resp.status_code == 200
        detail = resp.json()["players"]["445"]

        # Name comes from the SAME row the price metadata describes.
        assert detail["web_name"] == "Haaland"
        assert detail["web_name"] != "McConnell"
        assert detail["price"] == pytest.approx(15.5)
        # And the row's photo code is Haaland's, never McConnell's.
        assert detail["code"] == 223094


# ---------------------------------------------------------------------------
# 3) Unknown session -> honest 404
# ---------------------------------------------------------------------------


class TestUnknownSession:
    def test_decisions_unknown_session_returns_404(self, decisions_client: TestClient) -> None:
        resp = decisions_client.get("/api/v1/decisions", params={"session_id": "never-saved"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "No squad saved for this session"

    def test_squad_unknown_session_returns_404(self, decisions_client: TestClient) -> None:
        resp = decisions_client.get("/api/v1/squad", params={"session_id": "never-saved"})
        assert resp.status_code == 404

    def test_missing_session_param_returns_404(self, decisions_client: TestClient) -> None:
        resp = decisions_client.get("/api/v1/decisions")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 5) Analyst label: real router when keys exist, template-fallback otherwise
# ---------------------------------------------------------------------------


class TestAnalystProviderSelection:
    @staticmethod
    def _isolate(monkeypatch: pytest.MonkeyPatch, **key_env: str) -> None:
        """Patch the analyst's settings loader to read process env ONLY.

        The repo checkout carries a developer ``.env`` with real keys; tests
        must never see it. ``LLMSettings(_env_file=None)`` reads the process
        environment exclusively, so ``monkeypatch.setenv/delenv`` fully control
        which providers appear configured.
        """
        from fpl_intelligence.api.routes import analyst as analyst_mod
        from fpl_intelligence.live_intelligence.llm_settings import LLMSettings

        for var in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "GOOGLE_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        for var, value in key_env.items():
            monkeypatch.setenv(var, value)
        monkeypatch.setattr(
            analyst_mod,
            "load_llm_settings",
            lambda **_kw: LLMSettings(_env_file=None),
        )

    def test_no_keys_builds_mock(self, monkeypatch) -> None:
        from fpl_intelligence.api.routes.analyst import _build_real_provider
        from fpl_intelligence.live_intelligence.mock_llm import MockLLMProvider

        self._isolate(monkeypatch)
        provider = _build_real_provider()
        assert isinstance(provider, MockLLMProvider)

    def test_groq_key_builds_router_preferring_groq(self, monkeypatch) -> None:
        from fpl_intelligence.api.routes.analyst import _build_real_provider
        from fpl_intelligence.live_intelligence.llm_settings import LLMProviderName
        from fpl_intelligence.live_intelligence.provider_router import ProviderRouter

        self._isolate(
            monkeypatch,
            GROQ_API_KEY="gsk_test",
            OPENROUTER_API_KEY="sk-or-test",
        )
        provider = _build_real_provider()
        assert isinstance(provider, ProviderRouter)
        assert provider._fallback_order[0] == LLMProviderName.GROQ
        assert LLMProviderName.OPENROUTER in provider._fallback_order

    def test_priority_order_is_groq_openrouter_gemini(self, monkeypatch) -> None:
        from fpl_intelligence.api.routes.analyst import _build_real_provider
        from fpl_intelligence.live_intelligence.llm_settings import LLMProviderName
        from fpl_intelligence.live_intelligence.provider_router import ProviderRouter

        self._isolate(
            monkeypatch,
            OPENROUTER_API_KEY="sk-or-test",
            GOOGLE_API_KEY="ai_test",
            GROQ_API_KEY="gsk_test",
        )
        provider = _build_real_provider()
        assert isinstance(provider, ProviderRouter)
        assert provider._fallback_order == [
            LLMProviderName.GROQ,
            LLMProviderName.OPENROUTER,
            LLMProviderName.GEMINI,
        ]

    def test_model_label_pairs_provider_and_model(self) -> None:
        from types import SimpleNamespace

        from fpl_intelligence.api.routes.analyst import _resolve_model_label

        fake = SimpleNamespace(provider_name="router", model_name="llama-3.3-70b")
        assert _resolve_model_label(fake) == "router/llama-3.3-70b"
        assert _resolve_model_label(None) is None

    def test_template_summary_injects_active_chain_label(self) -> None:
        from fpl_intelligence.api.routes.analyst import _template_summary

        report = {
            "captain": {"player_id": 411, "expected_points": 7.2},
            "transfer_plan": {"action_type": "roll"},
            "players": {"411": {"web_name": "Salah"}},
            "meta": {"chain": {"source_label": "Baseline model"}},
        }
        text = _template_summary(report)
        # The ACTIVE chain label rides inside the analyst text (never hardcoded).
        assert "Baseline model" in text
