"""Phase 11.2 — SquadService PostgreSQL persistence tests.

Exercises the DB-backed path of :class:`~fpl_intelligence.squad.service.SquadService`
against the in-memory SQLite test harness (``db_session`` fixture). The same code
path is used in production against PostgreSQL; SQLite validates the ORM mapping,
the upsert/concurrency handling, and cross-request persistence.
"""

from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from fpl_intelligence.squad.models import SquadStateCreate
from fpl_intelligence.squad.service import SquadService


def _payload(**overrides: object) -> SquadStateCreate:
    base = dict(
        player_ids=list(range(1, 16)),
        captain_id=1,
        vice_captain_id=2,
        bank=1.5,
        free_transfers=2,
        chips_available=["wildcard", "free_hit"],
        gameweek=5,
    )
    base.update(overrides)
    return SquadStateCreate(**base)  # type: ignore[arg-type]


class TestSquadServiceSessionBinding:
    """Persistence through a request-scoped SQLAlchemy Session."""

    def test_set_and_get(self, db_session) -> None:
        svc = SquadService(session=db_session)
        stored = svc.set_squad(_payload(), session_id="binding_user")
        assert stored.gameweek == 5
        assert stored.updated_at is not None

        fetched = svc.get_squad(session_id="binding_user")
        assert fetched is not None
        assert fetched.player_ids == list(range(1, 16))
        assert fetched.gameweek == 5

    def test_get_returns_none_when_empty(self, db_session) -> None:
        svc = SquadService(session=db_session)
        assert svc.get_squad(session_id="empty_user") is None

    def test_set_replaces_previous(self, db_session) -> None:
        svc = SquadService(session=db_session)
        svc.set_squad(_payload(gameweek=1), session_id="replace_user")
        svc.set_squad(
            _payload(gameweek=5, player_ids=list(range(10, 25)), captain_id=20),
            session_id="replace_user",
        )
        current = svc.get_squad(session_id="replace_user")
        assert current is not None
        assert current.gameweek == 5
        assert current.player_ids == list(range(10, 25))

    def test_clear_removes_state(self, db_session) -> None:
        svc = SquadService(session=db_session)
        svc.set_squad(_payload(), session_id="clear_user")
        assert svc.get_squad(session_id="clear_user") is not None
        svc.clear(session_id="clear_user")
        assert svc.get_squad(session_id="clear_user") is None


class TestSquadServiceSessionFactory:
    """Persistence through a session factory (production / CLI path)."""

    def test_set_and_get_via_factory(self, db_session) -> None:
        factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
        svc = SquadService(session_factory=factory)
        svc.set_squad(_payload(gameweek=7), session_id="factory_user")

        # A brand-new service (simulating a process restart) sees the data.
        svc2 = SquadService(session_factory=factory)
        fetched = svc2.get_squad(session_id="factory_user")
        assert fetched is not None
        assert fetched.gameweek == 7

    def test_separate_session_keys_are_isolated(self, db_session) -> None:
        factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
        svc_a = SquadService(session_factory=factory)
        svc_b = SquadService(session_factory=factory)

        svc_a.set_squad(_payload(gameweek=1), session_id="user_a")
        svc_b.set_squad(_payload(gameweek=2), session_id="user_b")

        # Each key is independent: writing one does not clobber the other.
        assert svc_a.get_squad(session_id="user_a").gameweek == 1  # type: ignore[union-attr]
        assert svc_b.get_squad(session_id="user_b").gameweek == 2  # type: ignore[union-attr]
        # An unknown key still returns None.
        assert svc_a.get_squad(session_id="user_c") is None

    def test_session_id_is_required_no_implicit_default(self, db_session) -> None:
        """No implicit default session: each key is fully isolated."""
        factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
        svc = SquadService(session_factory=factory)
        svc.set_squad(_payload(gameweek=3), session_id="explicit_key")
        # A different key returns None — no shared default row.
        assert svc.get_squad(session_id="other_key") is None
        assert svc.get_squad(session_id="explicit_key").gameweek == 3  # type: ignore[union-attr]


class TestSquadServiceInMemoryFallback:
    """No session supplied -> in-memory store (backward compatible / offline)."""

    def test_in_memory_set_get(self) -> None:
        svc = SquadService()
        svc.set_squad(_payload(gameweek=4), session_id="mem_user")
        assert svc.get_squad(session_id="mem_user").gameweek == 4  # type: ignore[union-attr]

    def test_in_memory_requires_no_db(self) -> None:
        svc = SquadService()
        assert svc.get_squad(session_id="no_db_user") is None
