"""Phase 20.0 — weekly assistant brief: template sections + strict JSON parse.

The LLM path is exercised through the deterministic template fallback and the
section parser; no network, no keys required.
"""

from __future__ import annotations

import pytest

from fpl_intelligence.api.routes.assistant import (
    SECTION_KEYS,
    SECTION_TITLES,
    _cache_key,
    _parse_sections,
    _template_sections,
)


def _facts(**overrides) -> dict:
    base = {
        "gameweek": 8,
        "entry_size": 15,
        "bank": 2.5,
        "free_transfers": 1,
        "captain": {
            "name": "Haaland",
            "xpts": 7.4,
            "alternatives": [{"name": "Salah", "xpts": 6.9}],
        },
        "transfer_action": "roll",
        "transfer_reason": "no upgrade clears the bar",
        "transfer_ins": [],
        "transfer_outs": [],
        "prediction_source": "model-backtest",
        "fixture_lines": ["Haaland: WHU(H)2, EVE(A)3"],
        "squad_swing": 1.5,
        "targets": ["BOU (avg FDR 2.0)"],
        "news_lines": ["No BBC headlines matched your squad."],
        "grade_line": "3 graded calls · 67% hits · net +4 pts",
    }
    base.update(overrides)
    return base


class TestTemplateSections:
    def test_all_six_sections_present(self):
        sections = _template_sections(_facts())
        assert set(sections) == set(SECTION_KEYS)

    def test_titles_cover_every_section(self):
        assert len(SECTION_TITLES) == len(SECTION_KEYS)
        assert set(SECTION_TITLES.values()) == {
            "SQUAD STATUS", "CAPTAIN", "TRANSFERS",
            "FIXTURE SWINGS", "NEWS FLAGS", "LAST WEEK GRADE",
        }

    def test_captain_names_real_player_with_xpts(self):
        sections = _template_sections(_facts())
        assert "Haaland" in sections["captain"]
        assert "7.4" in sections["captain"]
        assert "Salah" in sections["captain"]

    def test_roll_stance_wording(self):
        sections = _template_sections(_facts())
        assert "Roll" in sections["transfers"]

    def test_transfer_action_lists_ins_and_outs(self):
        sections = _template_sections(
            _facts(
                transfer_action="Free Transfer",
                transfer_ins=["Gordon"],
                transfer_outs=["Mbeumo"],
            )
        )
        assert "Gordon" in sections["transfers"]
        assert "Mbeumo" in sections["transfers"]

    def test_swing_sign_is_disclosed(self):
        easy = _template_sections(_facts(squad_swing=1.5))
        hard = _template_sections(_facts(squad_swing=-2.1))
        assert "+1.5" in easy["fixture_swings"]
        assert "-2.1" in hard["fixture_swings"]

    def test_news_and_grade_flow_through(self):
        sections = _template_sections(_facts())
        assert "No BBC headlines matched your squad." in sections["news_flags"]
        assert "+4 pts" in sections["last_week_grade"]


class TestParseSections:
    JSON_OK = (
        '{"squad_status":"a","captain":"b","transfers":"c",'
        '"fixture_swings":"d","news_flags":"e","last_week_grade":"f"}'
    )

    def test_parses_clean_json(self):
        parsed = _parse_sections(self.JSON_OK)
        assert parsed is not None
        assert parsed["captain"] == "b"
        assert len(parsed) == 6

    def test_tolerates_code_fence(self):
        raw = "```json\n" + self.JSON_OK + "\n```"
        assert _parse_sections(raw) is not None

    def test_rejects_incomplete_sections(self):
        raw = '{"squad_status":"a"}'
        assert _parse_sections(raw) is None

    def test_rejects_prose_without_json(self):
        assert _parse_sections("I could not produce JSON, sorry.") is None


class TestCacheKey:
    def test_same_inputs_same_key(self):
        a = _cache_key("42", 8, [1, 2, 3])
        b = _cache_key("42", 8, [3, 2, 1])  # order-insensitive
        assert a == b

    def test_different_gw_different_key(self):
        assert _cache_key("42", 8, [1]) != _cache_key("42", 9, [1])

    @pytest.mark.parametrize("sid_a,sid_b", [("42", "43")])
    def test_different_session_different_key(self, sid_a: str, sid_b: str):
        assert _cache_key(sid_a, 8, [1]) != _cache_key(sid_b, 8, [1])
