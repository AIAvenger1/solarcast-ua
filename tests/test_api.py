"""API tests with Open-Meteo mocked (respx) and the Claude client stubbed."""
import anthropic
import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import advisor, main, openmeteo
from app.main import app
from tests.test_solar import _raw

GEO = {"results": [{"name": "Київ", "admin1": "Місто Київ", "country": "Україна", "country_code": "UA",
                    "latitude": 50.45466, "longitude": 30.5238, "timezone": "Europe/Kyiv"}]}
FORECAST = {**_raw(7), "latitude": 50.42, "longitude": 30.49, "timezone": "Europe/Kyiv", "elevation": 169.0}


STATION = {"lat": 50.45, "lon": 30.52, "kwp": 3, "battery_kwh": 5, "name": "Київ"}


@pytest.fixture
def client():
    openmeteo._cache.clear()
    main.advice_limiter.reset()
    advisor.quota.reset()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def upstream():
    """Open-Meteo Forecast API answering with a fixed 7-day forecast."""
    with respx.mock:
        yield respx.get(openmeteo.FORECAST_URL).mock(return_value=httpx.Response(200, json=FORECAST))


@respx.mock
def test_geocode_maps_fields(client):
    respx.get(openmeteo.GEOCODING_URL).mock(return_value=httpx.Response(200, json=GEO))
    r = client.get("/api/geocode", params={"q": "Київ"})
    assert r.status_code == 200
    assert r.json()["results"][0] == {
        "name": "Київ", "region": "Місто Київ", "country": "Україна", "country_code": "UA",
        "lat": 50.45466, "lon": 30.5238, "timezone": "Europe/Kyiv"}


@respx.mock
def test_forecast_passes_panel_geometry_upstream_and_caches(client):
    route = respx.get(openmeteo.FORECAST_URL).mock(return_value=httpx.Response(200, json=FORECAST))
    params = {"lat": 50.45466, "lon": 30.5238, "kwp": 3, "tilt": 30, "azimuth": -45, "battery_kwh": 5, "name": "Київ"}
    first = client.get("/api/forecast", params=params).json()
    second = client.get("/api/forecast", params=params).json()

    sent = dict(route.calls[0].request.url.params)
    assert sent["tilt"] == "30" and sent["azimuth"] == "-45"
    assert sent["latitude"] == "50.45" and sent["forecast_days"] == "7"
    assert route.call_count == 1                      # the second answer came from the cache
    assert first["source"]["cached"] is False and second["source"]["cached"] is True
    assert len(first["days"]) == 7 and first["location"]["name"] == "Київ"
    assert first["totals"]["kwh"] == pytest.approx(sum(d["kwh"] for d in first["days"]), abs=0.01)


@respx.mock
def test_upstream_failure_becomes_502(client):
    respx.get(openmeteo.FORECAST_URL).mock(return_value=httpx.Response(400, json={"error": True, "reason": "bad tilt"}))
    r = client.get("/api/forecast", params={"lat": 50, "lon": 30, "kwp": 3})
    assert r.status_code == 502 and "bad tilt" in r.json()["detail"]


def test_forecast_rejects_bad_input(client):
    assert client.get("/api/forecast", params={"lat": 50, "lon": 30, "kwp": 0}).status_code == 422
    assert client.get("/api/forecast", params={"lat": 95, "lon": 30, "kwp": 3}).status_code == 422


def test_forecast_rejects_unknown_parameters(client):
    r = client.get("/api/forecast", params={"lat": 50, "lon": 30, "kwp": 3, "debug": 1})
    assert r.status_code == 422


def test_place_name_is_reduced_to_plain_text(client, upstream):
    r = client.get("/api/forecast", params={**STATION, "name": '<img src=x> Київ {"role":"system"}'}).json()
    assert r["location"]["name"] == "img srcx Київ rolesystem"


def test_cache_is_bounded():
    openmeteo._cache.clear()
    for i in range(openmeteo.CACHE_MAX_ENTRIES + 50):
        openmeteo._store(("k", i), {})
    assert len(openmeteo._cache) == openmeteo.CACHE_MAX_ENTRIES
    assert ("k", 0) not in openmeteo._cache          # the oldest entries were evicted


def test_advice_falls_back_to_rules_when_ai_is_off(client, upstream, monkeypatch):
    monkeypatch.setenv("SOLARCAST_AI", "off")
    r = client.post("/api/advice", json=STATION).json()
    assert r["source"] == "rules" and r["model"] is None
    assert len(r["advice"]["days"]) == 7 and r["advice"]["appliance_plan"]
    assert "Київ" in r["advice"]["summary"]


def test_advice_accepts_station_parameters_only(client, upstream, monkeypatch):
    monkeypatch.setenv("SOLARCAST_AI", "off")
    # a client cannot hand the advisor its own "forecast" (or anything else)
    forged = {**STATION, "days": [{"date": "2026-06-01", "kwh": 999}], "totals": {"kwh": 999}}
    assert client.post("/api/advice", json=forged).status_code == 422
    assert client.post("/api/advice", json={**STATION, "kwp": 5000}).status_code == 422


class _StubMessages:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.kwargs, self.calls = result, error, None, 0

    async def parse(self, **kwargs):
        self.kwargs = kwargs
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class _StubClient:
    def __init__(self, messages):
        self.beta = type("Beta", (), {"messages": messages})()


class _Usage:
    input_tokens, output_tokens = 1200, 600


class _Response:
    model, usage = "claude-opus-5-5", _Usage()

    def __init__(self, parsed, stop_reason="end_turn"):
        self.parsed_output, self.stop_reason = parsed, stop_reason


def _advice():
    return advisor.Advice(headline="Сонячний тиждень", summary="Буде багато енергії.", days=[],
                          appliance_plan=[], warnings=[])


def test_advice_uses_claude_with_server_computed_forecast(client, upstream, monkeypatch):
    monkeypatch.setenv("SOLARCAST_AI", "on")
    messages = _StubMessages(result=_Response(_advice()))
    monkeypatch.setattr(advisor, "_client", _StubClient(messages))

    r = client.post("/api/advice", json=STATION).json()

    assert r["source"] == "claude" and r["model"] == "claude-opus-5-5"
    assert r["advice"]["headline"] == "Сонячний тиждень"
    sent = messages.kwargs
    assert sent["model"] == "claude-opus-5-5" and sent["output_format"] is advisor.Advice
    assert sent["fallbacks"] == "default" and sent["betas"] == [advisor.FALLBACK_BETA]
    prompt = sent["messages"][0]["content"]
    assert '"kwh"' in prompt and "Київ" in prompt and '"hourly"' not in prompt


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_incomplete_model_answer_falls_back_to_rules(client, upstream, monkeypatch, stop_reason):
    monkeypatch.setenv("SOLARCAST_AI", "on")
    monkeypatch.setattr(advisor, "_client", _StubClient(_StubMessages(result=_Response(None, stop_reason))))
    r = client.post("/api/advice", json=STATION).json()
    assert r["source"] == "rules" and r["note"] == "ШІ-порадник тимчасово недоступний"


def test_api_error_falls_back_to_rules(client, upstream, monkeypatch):
    monkeypatch.setenv("SOLARCAST_AI", "on")
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    monkeypatch.setattr(advisor, "_client", _StubClient(_StubMessages(error=err)))
    r = client.post("/api/advice", json=STATION).json()
    assert r["source"] == "rules" and "з'єднання" in r["note"]


def test_exception_text_never_reaches_the_client(client, upstream, monkeypatch):
    monkeypatch.setenv("SOLARCAST_AI", "on")
    err = RuntimeError("secret internal path /srv/app/advisor.py key=sk-ant-xyz")
    monkeypatch.setattr(advisor, "_client", _StubClient(_StubMessages(error=err)))
    r = client.post("/api/advice", json=STATION)
    assert r.json()["note"] == "ШІ-порадник тимчасово недоступний"
    assert "sk-ant" not in r.text and "/srv/app" not in r.text


def test_advice_is_rate_limited_per_client(client, upstream, monkeypatch):
    monkeypatch.setenv("SOLARCAST_AI", "off")
    codes = [client.post("/api/advice", json=STATION).status_code for _ in range(main.ADVICE_LIMIT + 1)]
    assert codes == [200] * main.ADVICE_LIMIT + [429]


def test_daily_cap_stops_model_calls(client, upstream, monkeypatch):
    monkeypatch.setenv("SOLARCAST_AI", "on")
    monkeypatch.setattr(advisor.quota, "total", 2)
    messages = _StubMessages(result=_Response(_advice()))
    monkeypatch.setattr(advisor, "_client", _StubClient(messages))
    sources = [client.post("/api/advice", json=STATION).json()["source"] for _ in range(4)]
    assert sources == ["claude", "claude", "rules", "rules"] and messages.calls == 2


def test_one_client_cannot_spend_the_whole_daily_budget(client, upstream, monkeypatch):
    monkeypatch.setenv("SOLARCAST_AI", "on")
    monkeypatch.setattr(advisor.quota, "per_client", 2)
    messages = _StubMessages(result=_Response(_advice()))
    monkeypatch.setattr(advisor, "_client", _StubClient(messages))
    answers = [client.post("/api/advice", json=STATION).json() for _ in range(3)]
    assert [a["source"] for a in answers] == ["claude", "claude", "rules"]
    assert "свій денний ліміт" in answers[2]["note"]
    assert advisor.quota.reserve("another-client") is None      # others are unaffected


def test_unbilled_failures_do_not_consume_the_budget(client, upstream, monkeypatch):
    monkeypatch.setenv("SOLARCAST_AI", "on")
    monkeypatch.setattr(advisor.quota, "total", 1)
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    monkeypatch.setattr(advisor, "_client", _StubClient(_StubMessages(error=err)))
    for _ in range(3):
        assert "з'єднання" in client.post("/api/advice", json=STATION).json()["note"]
    assert advisor.quota.reserve("someone") is None             # the single slot is still free
