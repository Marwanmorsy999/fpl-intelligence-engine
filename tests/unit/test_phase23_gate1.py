"""Phase 23 Gate 1 — EDGE & DOPAMINE tests (v2.3.0-edge-dopamine).

L1 league killer   : classic-league parsing, default pick (most members),
                     picker state, standings/picks cache assembly.
L2 web push        : VAPID subscribe upsert, per-trigger gating, bell log +
                     unread counts independent of browser permission,
                     gone-subscription deactivation.
L3 price engine    : pure now_cost diff detection + moves payload + chips.
L4 matchday pings  : stat-diff event detection with per-event dedupe keys and
                     the captain-delta message format.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fpl_intelligence.api.main import app
from fpl_intelligence.db.session import get_db
from fpl_intelligence.leagues.service import (
    ownership_insights,
    parse_entry_leagues,
    pick_default_league,
    projected_edge_lines,
)
from fpl_intelligence.notifications.webpush import (
    NotificationLogDB,
    PushSubscriptionDB,
    dispatch,
    unread_count,
)
from fpl_intelligence.prices.service import detect_moves


@pytest.fixture()
def client(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    yield TestClient(app), db_session
    app.dependency_overrides.pop(get_db, None)


# --------------------------------------------------------------------------- #
# L1 — LEAGUE KILLER
# --------------------------------------------------------------------------- #


ENTRY_LEAGUES_PAYLOAD = {
    "classic": [
        {
            "id": 123456,
            "name": "The Big League",
            "entry_count": 4200,
            "entry_rank": 42,
            "entry_last_rank": 51,
            "type": "public",
        },
        {
            "id": 999,
            "name": "Mates Only",
            "entry_count": 12,
            "entry_rank": 3,
            "type": "private",
        },
    ],
    "h2h": [
        {"id": 555, "name": "H2H Cup", "entry_count": 50},
        {"id": 777777, "name": "H2H should be ignored", "entry_count": 1000},
    ],
}


class TestLeagueKiller:
    def test_parse_extracts_only_classic_leagues(self):
        leagues = parse_entry_leagues(ENTRY_LEAGUES_PAYLOAD)
        ids = {lg["league_id"] for lg in leagues}
        assert ids == {123456, 999}
        private = next(lg for lg in leagues if lg["league_id"] == 999)
        assert private["private"] is True

    def test_default_pick_is_most_members(self):
        leagues = parse_entry_leagues(ENTRY_LEAGUES_PAYLOAD)
        assert pick_default_league(leagues)["league_id"] == 123456

    def test_picker_choice_is_persisted(self, db_session):
        from fpl_intelligence.leagues.models import EntryLeagueDB, LeagueSelectionDB

        now = datetime.now(UTC)
        for lg in parse_entry_leagues(ENTRY_LEAGUES_PAYLOAD):
            db_session.add(
                EntryLeagueDB(
                    entry_id=2295006,
                    league_id=lg["league_id"],
                    league_name=lg["name"],
                    member_count=lg.get("member_count"),
                    entry_rank=lg.get("entry_rank"),
                    private=lg.get("private", False),
                    discovered_at=now,
                )
            )
        db_session.commit()

        tc = TestClient(app)
        app.dependency_overrides[get_db] = lambda: db_session
        try:
            resp = tc.post(
                "/api/v1/league/select",
                json={"session_id": "2295006", "league_id": 999},
            )
            assert resp.status_code == 200, resp.text
            row = db_session.get(LeagueSelectionDB, "2295006")
            assert row is not None and row.league_id == 999
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_ownership_insights_prefer_missing_players(self):
        picks_map = {"a": [1, 2, 3], "b": [1, 2], "c": [1]}
        names = {1: "Cherki", 2: "Haaland", 3: "Saliba"}
        insights = ownership_insights({4}, picks_map, names, top_n=3)
        assert insights[0]["web_name"] == "Cherki"
        assert insights[0]["rival_owners"] == 3
        assert insights[0]["user_owns"] is False
        assert "you don't" in insights[0]["line"]

    def test_projected_edge_lines_sorted_sharpest_first(self):
        edge = projected_edge_lines(
            rec_xi=[1],
            xpts_by_element={1: 8.0},
            rival_xis={"a": [2], "b": [3]},
            rival_names={"a": "SuperBata", "b": "FPL Legend"},
        )
        assert edge["your_xpts"] == 8.0
        gaps = [line["gap"] for line in edge["lines"]]
        assert gaps == sorted(gaps)
        best = edge["lines"][0]
        assert best["gap"] >= edge["lines"][-1]["gap"]

    def test_league_page_registered(self):
        html_paths = {}
        from fpl_intelligence.web.dashboard import _PAGES

        html_paths = dict(_PAGES)
        assert html_paths.get("/league") == "league.html"


# --------------------------------------------------------------------------- #
# L2 — SELF-HOSTED WEB PUSH
# --------------------------------------------------------------------------- #


class TestWebPush:
    def test_subscribe_upserts_with_triggers(self, client):
        tc, db = client
        body = {
            "session_id": "2295006",
            "endpoint": "https://push.example.com/sub/abc",
            "keys": {"p256dh": "B" * 40, "auth": "A" * 20},
            "triggers": {"goals": True, "prices": False, "brief": True, "graded": False},
        }
        resp = tc.post("/api/v1/push/subscribe", json=body)
        assert resp.status_code == 200, resp.text
        assert resp.json()["triggers"]["goals"] is True
        assert resp.json()["triggers"]["prices"] is False

        # Upsert same endpoint updates rather than duplicating.
        body["triggers"]["prices"] = True
        tc.post("/api/v1/push/subscribe", json=body)
        subs = db.execute(select(PushSubscriptionDB)).scalars().all()
        assert len(subs) == 1
        assert subs[0].triggers["prices"] is True

    def test_dispatch_logs_bell_even_without_vapid(self, client, monkeypatch):
        tc, db = client
        monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)

        result = dispatch(db, "2295006", "graded", "Graded!", "2 calls graded", "/track-record")
        assert result["logged"] is True
        assert result["vapid_configured"] is False
        logs = db.execute(select(NotificationLogDB)).scalars().all()
        assert len(logs) == 1 and logs[0].kind == "graded"

        count = tc.get("/api/v1/push/unread-count?session_id=2295006").json()
        assert count["unread"] == 1
        log = tc.get("/api/v1/push/log?session_id=2295006").json()
        assert log["items"][0]["title"] == "Graded!"

    def test_trigger_gating_blocks_disabled_kind(self, client, monkeypatch):
        tc, db = client
        monkeypatch.setenv("VAPID_PUBLIC_KEY", "k")
        monkeypatch.setenv("VAPID_PRIVATE_KEY", "s")

        db.add(
            PushSubscriptionDB(
                session_id="777",
                endpoint="https://push.example.com/x",
                p256dh="B" * 40,
                auth="A" * 20,
                triggers={"goals": True, "brief": False},
                active=True,
                created_at=datetime.now(UTC),
            )
        )
        db.commit()

        called = []

        def fake_send(subscription, payload):
            called.append(payload)

        monkeypatch.setattr(
            "fpl_intelligence.notifications.webpush.send_webpush", fake_send
        )

        dispatch(db, "777", "brief", "Brief ready", "...")  # trigger off -> no send
        assert called == []
        dispatch(db, "777", "goals", "⚽ Goal", "Haaland +6 (62')")  # on -> sends
        assert len(called) == 1
        assert "Haaland" in called[0]["body"]

    def test_mark_all_read_resets_bell(self, client):
        tc, db = client
        dispatch(db, "888", "test", "t", "b")
        assert unread_count(db, "888") == 1
        resp = tc.post("/api/v1/push/mark-all-read?session_id=888")
        assert resp.json()["marked"] == 1
        assert unread_count(db, "888") == 0

    def test_config_reports_honest_state(self, client, monkeypatch):
        tc, _db = client
        monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
        cfg = tc.get("/api/v1/push/config").json()
        assert cfg["configured"] is False
        assert set(cfg["triggers"]) == {"goals", "prices", "brief", "graded"}

    def test_gone_subscription_deactivates(self, client, monkeypatch):
        _, db = client
        from fpl_intelligence.notifications import webpush as wp

        db.add(
            PushSubscriptionDB(
                session_id="999",
                endpoint="https://push.example.com/gone",
                p256dh="B" * 40,
                auth="A" * 20,
                triggers={"graded": True},
                active=True,
                created_at=datetime.now(UTC),
            )
        )
        db.commit()

        def raise_gone(subscription, payload):
            raise wp.GoneSubscriptionError("410 gone")

        monkeypatch.setenv("VAPID_PUBLIC_KEY", "k")
        monkeypatch.setenv("VAPID_PRIVATE_KEY", "s")
        monkeypatch.setattr(wp, "send_webpush", raise_gone)

        result = wp.dispatch(db, "999", "graded", "x", "y")
        assert result["deactivated"] == 1
        sub = db.scalar(select(PushSubscriptionDB))
        assert sub.active is False


# --------------------------------------------------------------------------- #
# L3 — PRICE ENGINE
# --------------------------------------------------------------------------- #


class TestPriceEngine:
    def test_detect_moves_pure_diff(self):
        today = {1: 56, 2: 120, 3: 90}
        yesterday = {1: 55, 2: 121, 4: 70}  # element 4 absent today -> skipped
        moves = detect_moves(today, yesterday)
        by_el = {m["element_id"]: m for m in moves}
        assert by_el[1]["delta"] == 1
        assert by_el[2]["delta"] == -1
        assert 4 not in by_el
        # Biggest absolute move first.
        assert abs(moves[0]["delta"]) >= abs(moves[-1]["delta"])

    def test_moves_endpoint_honest_empty_state(self, client):
        tc, _db = client
        data = tc.get("/api/v1/prices/moves").json()
        assert data["has_data"] is False
        assert "No price moves recorded yet" in data["note"]

    def test_moves_strip_after_two_snapshot_days(self, client):
        tc, db = client
        from datetime import date, timedelta

        from fpl_intelligence.prices.service import (
            record_price_moves,
            snapshot_prices,
        )

        facts = {
            501: {"now_cost": 55},
            502: {"now_cost": 120},
        }
        snapshot_prices(db, facts, today=date.today() - timedelta(days=2))

        # Next day: 501 rises to 56, 502 falls to 119.
        facts2 = {501: {"now_cost": 56}, 502: {"now_cost": 119}}
        snapshot_prices(db, facts2, today=date.today())

        stored = record_price_moves(db, gameweek=2)
        assert stored == 2

        data = tc.get("/api/v1/prices/moves?limit=5&gameweek=2").json()
        assert data["has_data"] is True
        assert {c["element_id"] for c in data["risers"]} == {501}
        assert {c["element_id"] for c in data["fallers"]} == {502}
        riser = data["risers"][0]
        assert riser["label"] == "+0.1"
        assert riser["new_cost"] == "£5.6m"

        chips = tc.get("/api/v1/prices/chips?player_ids=501,502,999").json()["chips"]
        assert chips["501"] == 1 and chips["502"] == -1 and "999" not in chips

    def test_element_facts_now_cost_column_sealable(self, db_session):
        """The ALTER-based seal keeps the session usable on old prod tables."""
        from sqlalchemy import text as sa_text

        def columns(session):
            pragma = sa_text("PRAGMA table_info(element_facts)")
            return [row[1] for row in session.execute(pragma).all()]

        # sqlite cannot ADD COLUMN IF NOT EXISTS; emulate by checking first.
        if "now_cost" not in columns(db_session):
            alter = sa_text("ALTER TABLE element_facts ADD COLUMN now_cost INTEGER")
            db_session.execute(alter)
            db_session.commit()
        assert "now_cost" in columns(db_session)


# --------------------------------------------------------------------------- #
# L4 — MATCHDAY PINGS
# --------------------------------------------------------------------------- #


def _stats(points, minutes, goals=0, assists=0, red_cards=0):
    return {
        "points": points,
        "minutes": minutes,
        "goals": goals,
        "assists": assists,
        "red_cards": red_cards,
    }


class TestMatchdayPings:
    def test_detect_goal_assist_red_card_events(self):
        current = {10: _stats(6, 62, goals=1, assists=0, red_cards=1)}
        previous = {10: _stats(0, 30)}
        events = __import__(
            "fpl_intelligence.api.routes.live", fromlist=["detect_stat_events"]
        ).detect_stat_events(current, previous, watched_ids={10})
        kinds = sorted(e["kind"] for e in events)
        assert kinds == ["goal", "red_card"]
        goal = next(e for e in events if e["kind"] == "goal")
        assert goal["ordinal"] == 1  # cumulative count = dedupe key
        assert goal["minute"] == 62

    def test_second_goal_gets_ordinal_2(self):
        live = __import__("fpl_intelligence.api.routes.live", fromlist=["detect_stat_events"])
        prev = {10: _stats(5, 45, goals=1)}
        cur = {10: _stats(11, 80, goals=2)}
        events = live.detect_stat_events(cur, prev, watched_ids={10})
        assert [e["ordinal"] for e in events] == [2]

    def test_unwatched_players_never_ping(self):
        live = __import__("fpl_intelligence.api.routes.live", fromlist=["detect_stat_events"])
        cur = {99: _stats(6, 60, goals=1)}
        events = live.detect_stat_events(cur, {}, watched_ids={10})
        assert events == []

    def test_captain_delta_message_format(self):
        live = __import__("fpl_intelligence.api.routes.live", fromlist=["event_message"])
        msg = live.event_message(
            {"element_id": 10, "kind": "goal", "ordinal": 1, "minute": 62, "points_delta": 6},
            {10: "Haaland"},
            captain_id=10,
        )
        assert msg == "⚽ Haaland +6 (62') — captain delta +12"

    def test_non_captain_message_has_no_delta_note(self):
        live = __import__("fpl_intelligence.api.routes.live", fromlist=["event_message"])
        msg = live.event_message(
            {"element_id": 11, "kind": "assist", "ordinal": 1, "minute": 31, "points_delta": 3},
            {11: "Saka"},
            captain_id=None,
        )
        assert msg == "🎯 Saka +3 (31')"
        assert "captain" not in msg

    def test_red_card_captain_flags_without_double(self):
        live = __import__("fpl_intelligence.api.routes.live", fromlist=["event_message"])
        msg = live.event_message(
            {"element_id": 10, "kind": "red_card", "ordinal": 1, "minute": 55, "points_delta": -3},
            {10: "Saliba"},
            captain_id=10,
        )
        assert "CAPTAIN" in msg and "captain delta" not in msg
