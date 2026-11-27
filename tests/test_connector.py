"""Regression tests for the PTV connector against recorded fixtures.

No network and no credentials: ``aiohttp`` is replaced by a tiny fake that
routes on the request path and returns fixture JSON.  The connector is imported
from the real ``homeassistant`` package pinned by
``pytest-homeassistant-custom-component``.

Several tests are labelled "documents current behaviour" or "documents
current bug": they pin behaviour that P1.3/P1.4/P1.5 will deliberately
change, so later PRs can prove the change is intentional.
"""

import copy
import datetime
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
FIXTURES = Path(__file__).resolve().parent / "fixtures"

CANARY_DEVID = "1234567"
CANARY_API_KEY = "CANARY-not-a-real-key-0001"

ROUTE_TYPES_PATH = "/v3/route_types"
ROUTES_PATH = "/v3/routes?route_types=0"
DIRECTIONS_PATH = "/v3/directions/route/6"
STOPS_PATH = "/v3/stops/route/6/route_type/0"
DEPARTURES_PATH = (
    "/v3/departures/route_type/0/stop/1071/route/6"
    "?direction_id=5&max_results=5"
)
REQUEST_DEPARTURES = DEPARTURES_PATH


# ---------------------------------------------------------------------------
# Module loading (real homeassistant, non-throttling shim)
# ---------------------------------------------------------------------------


def _import_connector():
    """Import the connector with a non-throttling Throttle shim for the import."""
    import homeassistant.util as ha_util

    def passthrough_throttle(minimum):
        def decorator(func):
            return func

        return decorator

    original = ha_util.Throttle
    ha_util.Throttle = passthrough_throttle
    try:
        from custom_components.public_transport_victoria.PublicTransportVictoria import (
            public_transport_victoria as module,
        )
    finally:
        ha_util.Throttle = original
    return module


connector_module = _import_connector()
Connector = connector_module.Connector


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


# Derived from the recording so re-capturing fixtures does not churn tests.
RECORDED_DEPARTURES = fixture("departures-frankston-flinders.json")["departures"]
RUN_ID_WITH_INFO = RECORDED_DEPARTURES[0]["run_id"]
RUN_PATH = "/v3/runs/{}".format(RUN_ID_WITH_INFO)


# ---------------------------------------------------------------------------
# Fake aiohttp
# ---------------------------------------------------------------------------


class FakeResponse:
    """Response usable both with ``await`` and ``async with``."""

    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    def __await__(self):
        async def _resolve():
            return self

        return _resolve().__await__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeAPI:
    """Route fake HTTP responses by request path (signature stripped)."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        self.default_status = 404
        self.namespace = types.SimpleNamespace(
            ClientSession=self._session, ClientTimeout=lambda **kwargs: None
        )

    def add(self, path, payload, status=200):
        self.routes[path] = (status, payload)
        return self

    def set_status(self, path, status, payload=None):
        self.routes[path] = (status, payload if payload is not None else {})
        return self

    def _session(self, **kwargs):
        return FakeSession(self)

    def respond(self, url):
        path = url.split("timetableapi.ptv.vic.gov.au", 1)[1]
        # build_URL appends devid then signature; drop them to key the route.
        path = path.split("devid=")[0].rstrip("?&")
        self.requests.append(path)
        status, payload = self.routes.get(path, (self.default_status, {}))
        return FakeResponse(status, copy.deepcopy(payload))


class FakeSession:
    def __init__(self, api):
        self._api = api

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def get(self, url, **kwargs):
        return self._api.respond(url)


@pytest.fixture
def fake_api(monkeypatch):
    api = FakeAPI()
    monkeypatch.setattr(connector_module, "aiohttp", api.namespace)
    return api


def make_hass():
    return types.SimpleNamespace(config=types.SimpleNamespace(time_zone="UTC"))


def expected_departure(row):
    """Format a fixture row the way the connector does (hass TZ is UTC)."""
    utc = row["estimated_departure_utc"] or row["scheduled_departure_utc"]
    return datetime.datetime.strptime(utc, "%Y-%m-%dT%H:%M:%SZ").strftime(
        "%I:%M %p"
    )


def make_connector(**overrides):
    kwargs = dict(
        hass=make_hass(),
        id=CANARY_DEVID,
        api_key=CANARY_API_KEY,
        route_type=0,
        route=6,
        direction=5,
        stop=1071,
    )
    kwargs.update(overrides)
    return Connector(**kwargs)


def wire_happy_path(api, *, run_status=200, departures_status=200):
    api.add(ROUTE_TYPES_PATH, fixture("route_types.json"))
    api.add(ROUTES_PATH, fixture("routes-train.json"))
    api.add(DIRECTIONS_PATH, fixture("directions-frankston.json"))
    api.add(STOPS_PATH, fixture("stops-frankston.json"))
    api.add(
        REQUEST_DEPARTURES,
        fixture("departures-frankston-flinders.json"),
        status=departures_status,
    )
    api.add(RUN_PATH, fixture("runs.json"), status=run_status)
    return api


# ---------------------------------------------------------------------------
# Route types / routes / directions / stops
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_route_types_returns_string_keyed_dict(fake_api):
    fake_api.add(ROUTE_TYPES_PATH, fixture("route_types.json"))
    connector = make_connector()

    result = await connector.async_route_types()

    assert result == {"0": "Train", "1": "Tram", "2": "Bus", "3": "Vline", "4": "Night Bus"}
    assert all(isinstance(key, str) for key in result)


@pytest.mark.asyncio
async def test_async_route_types_non_200_returns_none_documents_current(fake_api):
    """Documents current behaviour: a failed fetch returns ``None``, which the
    config flow then treats as an empty mapping (P1.3 will make this explicit).
    """
    fake_api.set_status(ROUTE_TYPES_PATH, 401)
    connector = make_connector()

    assert await connector.async_route_types() is None


@pytest.mark.asyncio
async def test_async_routes_sorts_numeric_before_non_numeric(fake_api):
    payload = {
        "routes": [
            {"route_id": 30, "route_number": "906", "route_name": "Werribee"},
            {"route_id": 11, "route_number": "1", "route_name": "East Coburg"},
            {"route_id": 12, "route_number": "", "route_name": "Unnumbered"},
            {"route_id": 13, "route_number": "Skybus", "route_name": "Skybus"},
        ]
    }
    fake_api.add(ROUTES_PATH, payload)
    connector = make_connector(route_type=None)

    result = await connector.async_routes(0)

    assert list(result) == ["11", "30", "12", "13"]
    assert result["11"] == "1 - East Coburg"
    assert result["12"] == "Unnumbered"
    assert result["13"] == "Skybus - Skybus"
    assert connector.route_type == 0


@pytest.mark.asyncio
async def test_async_routes_fixture_mapping(fake_api):
    fake_api.add(ROUTES_PATH, fixture("routes-train.json"))
    connector = make_connector(route_type=None)

    result = await connector.async_routes(0)

    routes = fixture("routes-train.json")["routes"]
    assert set(result) == {str(route["route_id"]) for route in routes}
    # Train routes carry an empty route_number, so the name is used alone.
    assert result["6"] == "Frankston"


@pytest.mark.asyncio
async def test_async_routes_non_200_returns_empty_dict(fake_api):
    fake_api.set_status(ROUTES_PATH, 500)
    connector = make_connector()

    assert await connector.async_routes(0) == {}


@pytest.mark.asyncio
async def test_async_directions_and_stops_map_string_keys(fake_api):
    fake_api.add(DIRECTIONS_PATH, fixture("directions-frankston.json"))
    fake_api.add(STOPS_PATH, fixture("stops-frankston.json"))
    connector = make_connector()

    directions = await connector.async_directions(6)
    stops = await connector.async_stops(6)

    assert directions["5"] == "Frankston"
    assert {str(d["direction_id"]) for d in fixture("directions-frankston.json")["directions"]} == set(directions)
    assert stops["1071"] == "Flinders Street Station"
    assert len(stops) == len(fixture("stops-frankston.json")["stops"])
    assert all(isinstance(key, str) for key in stops)
    assert connector.route == 6


# ---------------------------------------------------------------------------
# Departures: async_update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_update_happy_path_mixed_estimates(fake_api):
    wire_happy_path(fake_api)
    connector = make_connector()

    await connector._init()

    assert len(connector.departures) == len(RECORDED_DEPARTURES)
    first = connector.departures[0]
    assert first["estimated_departure_utc"] is not None
    assert first["is_express"] is True  # runs.json has express_stop_count 12

    # Every row is formatted from the estimate, falling back to the schedule.
    for row in connector.departures:
        assert row["departure"] == expected_departure(row)

    # The recording deliberately includes a null estimate (scheduled-only) row.
    assert any(
        row["estimated_departure_utc"] is None for row in connector.departures
    )
    # Only the first run id has a run fixture; the rest must be non-express.
    others = [
        row for row in connector.departures if row["run_id"] != RUN_ID_WITH_INFO
    ]
    assert all(row["is_express"] is False for row in others)

    # One departures request plus one run request per departure (N+1 today).
    assert fake_api.requests[0] == DEPARTURES_PATH
    assert len(fake_api.requests) == 1 + len(RECORDED_DEPARTURES)
    assert (
        sum(1 for path in fake_api.requests if path.startswith("/v3/runs/"))
        == len(RECORDED_DEPARTURES)
    )


@pytest.mark.asyncio
async def test_async_update_200_empty_departures_is_empty_list(fake_api):
    wire_happy_path(fake_api)
    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-empty.json"))
    connector = make_connector()

    await connector._init()

    assert connector.departures == []
    assert fake_api.requests == [DEPARTURES_PATH]  # no run lookups


@pytest.mark.asyncio
async def test_async_update_non_200_after_success_keeps_stale_data_documents_current(
    fake_api,
):
    """Documents current behaviour (P1.4 will raise an error instead).

    A later non-200 response leaves the previous departure list untouched and
    raises nothing, so the coordinator reports success with stale data.
    """
    wire_happy_path(fake_api)
    connector = make_connector()
    await connector._init()
    before = list(connector.departures)

    fake_api.set_status(REQUEST_DEPARTURES, 502)
    await connector.async_update()

    assert connector.departures == before


@pytest.mark.asyncio
async def test_async_update_initial_non_200_raises_documents_current_bug(fake_api):
    """Documents current bug (P1.5 will turn this into a retryable failure).

    Before the first success ``self.departures`` is never assigned, and the
    unconditional logging line then raises ``AttributeError``.
    """
    wire_happy_path(fake_api)
    fake_api.set_status(REQUEST_DEPARTURES, 500)
    connector = make_connector()

    with pytest.raises(AttributeError):
        await connector._init()


@pytest.mark.asyncio
async def test_async_update_missing_scheduled_field_raises_keyerror_documents_bug(
    fake_api,
):
    """Documents current bug (P1.4 will handle this gracefully).

    A row that omits ``scheduled_departure_utc`` entirely is not the same as a
    JSON null; the fallback branch raises ``KeyError``.
    """
    wire_happy_path(fake_api)
    fake_api.add(
        REQUEST_DEPARTURES, fixture("departures-200-missing-fields.json")
    )
    connector = make_connector()

    with pytest.raises(KeyError):
        await connector._init()


@pytest.mark.asyncio
async def test_async_update_derived_mixed_fixture(fake_api):
    """The derived mixed fixture exercises both estimate branches in one call."""
    wire_happy_path(fake_api)
    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-mixed.json"))
    connector = make_connector()

    await connector._init()

    estimates = [row["estimated_departure_utc"] for row in connector.departures]
    assert estimates[0] is None
    assert any(value is not None for value in estimates)


@pytest.mark.asyncio
async def test_async_run_returns_none_when_absent(fake_api):
    fake_api.set_status(RUN_PATH, 404)
    connector = make_connector()

    assert await connector.async_run(RUN_ID_WITH_INFO) is None


@pytest.mark.asyncio
async def test_async_run_returns_first_run(fake_api):
    fake_api.add(RUN_PATH, fixture("runs.json"))
    connector = make_connector()

    run = await connector.async_run(RUN_ID_WITH_INFO)

    assert run["run_id"] == RUN_ID_WITH_INFO
    assert run["express_stop_count"] == 12


def test_convert_utc_to_local_is_deterministic():
    assert (
        connector_module.convert_utc_to_local("2026-11-27T06:40:00Z", make_hass())
        == "06:40 AM"
    )
