import httpx
import respx

from fpl_intelligence.collectors.official_fpl import OfficialFPLDataProvider


@respx.mock
def test_bootstrap_static_contract() -> None:
    route = respx.get("https://fantasy.premierleague.com/api/bootstrap-static/").mock(
        return_value=httpx.Response(200, json={"teams": [], "elements": []})
    )
    provider = OfficialFPLDataProvider("https://fantasy.premierleague.com")
    result = provider.get_bootstrap_static()
    assert result["teams"] == []
    assert route.called


@respx.mock
def test_fixtures_contract() -> None:
    route = respx.get("https://fantasy.premierleague.com/api/fixtures/").mock(
        return_value=httpx.Response(200, json=[])
    )
    provider = OfficialFPLDataProvider("https://fantasy.premierleague.com")
    result = provider.get_fixtures()
    assert result == []
    assert route.called
