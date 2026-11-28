"""Regression tests for the PTV connector against recorded fixtures.

No network and no credentials: ``aiohttp`` is replaced by a tiny fake that
routes on the request path and returns fixture JSON.  The connector is imported
from the real ``homeassistant`` package pinned by
``pytest-homeassistant-custom-component``.

P1.3 introduced typed API errors and P1.4 made every request use one shared
session with an explicit timeout, parsed JSON inside the response context and
replaced silent stale-data handling with typed failures.  P1.5 makes the
DataUpdateCoordinator the only polling layer: ``prepare()`` performs no I/O and
there is no throttle between refresh requests.  The tests below pin that
guaranteed behaviour.
"""

import copy
import datetime
import inspect
import json
import logging
import sys
import types
from pathlib import Path

import aiohttp
import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryNotReady

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
# Bounded empty-result diagnosis probes (see EMPTY_DIAG_COOLDOWN).
EMPTY_PROBE_PATH = "/v3/departures/route_type/0/stop/1071/route/6?max_results=1"
ROUTE_CHECK_PATH = "/v3/directions/route/6"


# ---------------------------------------------------------------------------
# Module loading (real homeassistant; no Throttle shim after P1.5)
# ---------------------------------------------------------------------------


from custom_components.public_transport_victoria.PublicTransportVictoria import (  # noqa: E402
    public_transport_victoria as connector_module,
)


Connector = connector_module.Connector

from custom_components.public_transport_victoria import sensor as sensor_module  # noqa: E402


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

    async def json(self, content_type=None):
        return self._payload

    def __await__(self):
        async def _resolve():
            return self

        return _resolve().__await__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _explode(*args, **kwargs):
    raise AssertionError("aiohttp.ClientSession must never be constructed")


class FakeAPI:
    """Route fake HTTP responses by request path (signature stripped)."""

    def __init__(self):
        self.routes = {}
        self.raises = {}
        self.requests = []
        self.get_calls = []
        self.session_resolutions = 0
        self.default_status = 404
        self.session = FakeSession(self)
        self.namespace = types.SimpleNamespace(
            ClientSession=_explode, ClientTimeout=lambda **kwargs: None
        )

    def add(self, path, payload, status=200):
        self.routes[path] = (status, payload)
        return self

    def set_status(self, path, status, payload=None):
        self.routes[path] = (status, payload if payload is not None else {})
        return self

    def raise_on(self, path, err):
        """Make requests for ``path`` raise ``err`` from ``get``."""
        self.raises[path] = err
        return self

    def respond(self, url, kwargs):
        path = url.split("timetableapi.ptv.vic.gov.au", 1)[1]
        # build_URL appends devid then signature; drop them to key the route.
        path = path.split("devid=")[0].rstrip("?&")
        self.requests.append(path)
        self.get_calls.append((path, kwargs))
        if path in self.raises:
            raise self.raises[path]
        status, payload = self.routes.get(path, (self.default_status, {}))
        return FakeResponse(status, copy.deepcopy(payload))


class FakeSession:
    """Minimal ``aiohttp.ClientSession`` stand-in for one connector."""

    def __init__(self, api):
        self._api = api

    def get(self, url, **kwargs):
        return self._api.respond(url, kwargs)


@pytest.fixture
def fake_api(monkeypatch):
    api = FakeAPI()
    monkeypatch.setattr(connector_module, "aiohttp", api.namespace)

    def _get_session(_hass):
        api.session_resolutions += 1
        return api.session

    monkeypatch.setattr(connector_module, "async_get_clientsession", _get_session)
    return api


def make_hass():
    return types.SimpleNamespace(config=types.SimpleNamespace(time_zone="UTC"))


def expected_departure(row):
    """Format a fixture row the way the connector does (hass TZ is UTC)."""
    utc = row.get("estimated_departure_utc") or row["scheduled_departure_utc"]
    return datetime.datetime.strptime(utc, "%Y-%m-%dT%H:%M:%SZ").strftime(
        "%I:%M %p"
    )


async def prepare_and_update(connector):
    """Run P1.5's synchronous path setup, then the coordinator-owned fetch."""
    connector.prepare()
    await connector.async_update()


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


def make_coordinator(hass, connector):
    """Build a coordinator as if created during config-entry setup.

    Production relies on Home Assistant's ``current_entry`` context to attach
    the config entry; tests set the same state explicitly so the real
    ``async_config_entry_first_refresh`` retry semantics are exercised.
    """
    coordinator = sensor_module.PublicTransportVictoriaDataUpdateCoordinator(
        hass, connector
    )
    coordinator.config_entry = types.SimpleNamespace(
        state=ConfigEntryState.SETUP_IN_PROGRESS,
        pref_disable_polling=False,
    )
    return coordinator


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
async def test_async_route_types_401_raises_auth_error(fake_api):
    """P1.3: a rejected credential raises a typed error instead of ``None``."""
    fake_api.set_status(ROUTE_TYPES_PATH, 401)
    connector = make_connector()

    with pytest.raises(connector_module.PTVAuthError):
        await connector.async_route_types()


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
async def test_async_routes_500_raises_service_error(fake_api):
    """P1.3: a server error raises a typed error instead of ``{}``."""
    fake_api.set_status(ROUTES_PATH, 500)
    connector = make_connector()

    with pytest.raises(connector_module.PTVServiceError):
        await connector.async_routes(0)


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

    await prepare_and_update(connector)

    assert len(connector.departures) == len(RECORDED_DEPARTURES)
    first = connector.departures[0]
    assert first["estimated_departure_utc"] is not None
    assert first["is_express"] is True  # runs.json has express_stop_count 12

    # Every row is formatted from the estimate, falling back to the schedule.
    for row in connector.departures:
        assert row["departure"] == expected_departure(row)
        # P1.4: the timestamp schema is stable; both keys always exist.
        assert "estimated_departure_utc" in row
        assert "scheduled_departure_utc" in row

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
    fake_api.add(EMPTY_PROBE_PATH, {"departures": []})
    connector = make_connector()

    await prepare_and_update(connector)

    assert connector.departures == []
    # No run lookups; the empty poll only adds the bounded diagnosis probes
    # (probe A is empty, so the route-existence probe B follows).
    assert fake_api.requests == [
        DEPARTURES_PATH,
        EMPTY_PROBE_PATH,
        ROUTE_CHECK_PATH,
    ]


@pytest.mark.asyncio
async def test_async_update_non_200_after_success_raises_and_keeps_previous_data(
    fake_api,
):
    """P1.4: a later non-200 raises a typed error and preserves old data.

    The coordinator must treat the poll as failed rather than publishing stale
    departures as a successful update.
    """
    wire_happy_path(fake_api)
    connector = make_connector()
    await prepare_and_update(connector)
    before = list(connector.departures)

    fake_api.set_status(REQUEST_DEPARTURES, 502)
    with pytest.raises(connector_module.PTVServiceError):
        await connector.async_update()

    assert connector.departures == before


@pytest.mark.asyncio
async def test_async_update_initial_non_200_raises_service_error(fake_api):
    """P1.4: the first fetch fails with a typed error and leaves no partial data."""
    wire_happy_path(fake_api)
    fake_api.set_status(REQUEST_DEPARTURES, 500)
    connector = make_connector()

    with pytest.raises(connector_module.PTVServiceError):
        await prepare_and_update(connector)

    assert connector.departures == []


@pytest.mark.asyncio
async def test_async_update_rows_without_any_timestamp_are_dropped(fake_api):
    """P1.4: an absent timestamp is not a KeyError; unusable rows are dropped."""
    wire_happy_path(fake_api)
    fake_api.add(
        REQUEST_DEPARTURES, fixture("departures-200-missing-fields.json")
    )
    connector = make_connector()

    await prepare_and_update(connector)

    assert connector.departures == []
    # Dropped rows must not trigger per-row run lookups.
    assert fake_api.requests == [DEPARTURES_PATH]


@pytest.mark.asyncio
async def test_async_update_drops_only_rows_without_timestamps(fake_api):
    """P1.4: one unusable row is dropped while the rest are kept."""
    wire_happy_path(fake_api)
    rows = copy.deepcopy(RECORDED_DEPARTURES[:2])
    # Row 0 loses both timestamps: unusable.
    del rows[0]["estimated_departure_utc"]
    del rows[0]["scheduled_departure_utc"]
    # Row 1 loses only the estimate: keep it, falling back to the schedule.
    del rows[1]["estimated_departure_utc"]
    fake_api.add(REQUEST_DEPARTURES, {"departures": rows})

    connector = make_connector()
    await prepare_and_update(connector)

    assert len(connector.departures) == 1
    kept = connector.departures[0]
    assert kept["run_id"] == rows[1]["run_id"]
    assert kept["estimated_departure_utc"] is None
    assert kept["scheduled_departure_utc"] == rows[1]["scheduled_departure_utc"]
    assert kept["departure"] == expected_departure(rows[1])
    # Only the kept row is looked up; the dropped row never reaches /runs.
    assert [
        path for path in fake_api.requests if path.startswith("/v3/runs/")
    ] == ["/v3/runs/{}".format(rows[1]["run_id"])]


@pytest.mark.asyncio
async def test_async_update_derived_mixed_fixture(fake_api):
    """The derived mixed fixture exercises both estimate branches in one call."""
    wire_happy_path(fake_api)
    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-mixed.json"))
    connector = make_connector()

    await prepare_and_update(connector)

    estimates = [row["estimated_departure_utc"] for row in connector.departures]
    assert estimates[0] is None
    assert any(value is not None for value in estimates)


# ---------------------------------------------------------------------------
# P1.7: only the configured route's departures are kept (issue #34)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_route", [6, "6"])
async def test_async_update_filters_to_configured_route(
    fake_api, caplog, configured_route
):
    """Mixed-route rows are filtered before any run lookup or processing.

    The fixture rows use numeric ``route_id`` values.  One kept row carries a
    string ``route_id`` so both int- and string-configured routes prove the
    normalised comparison, and the drop is logged as a count only.
    """
    wire_happy_path(fake_api)
    rows = copy.deepcopy(RECORDED_DEPARTURES[:4])
    rows[0]["route_id"] = 6           # matches as an int
    rows[1]["route_id"] = "6"         # matches as a string
    rows[2]["route_id"] = 11          # different route
    del rows[3]["route_id"]           # missing provenance
    fake_api.add(REQUEST_DEPARTURES, {"departures": rows})
    connector = make_connector(route=configured_route)

    caplog.set_level(logging.DEBUG)
    await prepare_and_update(connector)

    assert [row["run_id"] for row in connector.departures] == [
        rows[0]["run_id"],
        rows[1]["run_id"],
    ]
    assert all(row["route_id"] in (6, "6") for row in connector.departures)
    # Run lookups only happen for the kept rows; the dropped rows never reach
    # the /v3/runs endpoint.
    assert fake_api.requests == [
        DEPARTURES_PATH,
        "/v3/runs/{}".format(rows[0]["run_id"]),
        "/v3/runs/{}".format(rows[1]["run_id"]),
    ]
    assert (
        "filtered 2 departure row(s) not matching the configured route"
        in caplog.text
    )
    # Count-only logging: no dropped ids or payloads leak into the log.
    assert str(rows[2]["run_id"]) not in caplog.text
    assert str(rows[3]["run_id"]) not in caplog.text


@pytest.mark.asyncio
async def test_async_update_all_rows_filtered_is_empty_success(fake_api, caplog, hass):
    """Every row for another route is an empty, successful update."""
    wire_happy_path(fake_api)
    rows = copy.deepcopy(RECORDED_DEPARTURES[:2])
    rows[0]["route_id"] = 11
    del rows[1]["route_id"]
    fake_api.add(REQUEST_DEPARTURES, {"departures": rows})
    fake_api.add(EMPTY_PROBE_PATH, {"departures": []})
    connector = make_connector()
    connector.prepare()
    coordinator = make_coordinator(hass, connector)

    caplog.set_level(logging.DEBUG)
    await coordinator.async_config_entry_first_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data == []
    assert connector.departures == []
    # Empty is a valid result, not a failure: no run lookups were attempted,
    # only the bounded empty-result diagnosis probes.
    assert fake_api.requests == [
        DEPARTURES_PATH,
        EMPTY_PROBE_PATH,
        ROUTE_CHECK_PATH,
    ]
    assert (
        "filtered 2 departure row(s) not matching the configured route"
        in caplog.text
    )


# ---------------------------------------------------------------------------
# #50: an empty poll is diagnosed (stale ids vs genuinely no service)
# ---------------------------------------------------------------------------


def warning_messages(caplog, needle):
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and needle in record.getMessage()
    ]


@pytest.mark.asyncio
async def test_empty_departures_probe_without_direction_warns_stale_direction(
    fake_api, caplog
):
    """Empty + rows without the direction filter == stale direction id."""
    wire_happy_path(fake_api)
    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-empty.json"))
    fake_api.add(
        EMPTY_PROBE_PATH, {"departures": copy.deepcopy(RECORDED_DEPARTURES[:1])}
    )
    connector = make_connector()

    caplog.set_level(logging.DEBUG)
    await prepare_and_update(connector)

    assert connector.departures == []
    # Exactly one extra GET, shaped like the departures call minus direction.
    assert fake_api.requests == [DEPARTURES_PATH, EMPTY_PROBE_PATH]
    assert "direction_id" not in EMPTY_PROBE_PATH
    assert "max_results=1" in EMPTY_PROBE_PATH
    # Probe rows are never processed, so no run lookups are attempted.
    assert not any(path.startswith("/v3/runs/") for path in fake_api.requests)
    assert warning_messages(caplog, "direction id looks stale") == [
        "PTV: departures are empty for route 6 direction 5 stop 1071, but the "
        "route has departures without the direction filter - the direction "
        "id looks stale; re-add the config entry"
    ]


@pytest.mark.asyncio
async def test_empty_departures_and_no_directions_warns_stale_route(
    fake_api, caplog
):
    """Probe A empty + the route has no directions == stale route id."""
    wire_happy_path(fake_api)
    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-empty.json"))
    fake_api.add(EMPTY_PROBE_PATH, {"departures": []})
    fake_api.add(ROUTE_CHECK_PATH, {"directions": []})
    connector = make_connector()

    caplog.set_level(logging.DEBUG)
    await prepare_and_update(connector)

    assert connector.departures == []
    assert fake_api.requests == [
        DEPARTURES_PATH,
        EMPTY_PROBE_PATH,
        ROUTE_CHECK_PATH,
    ]
    assert warning_messages(caplog, "route id looks stale") == [
        "PTV: departures are empty for route 6 direction 5 stop 1071 and the "
        "route has no directions - the route id looks stale; re-add the "
        "config entry"
    ]
    assert not warning_messages(caplog, "direction id looks stale")


@pytest.mark.asyncio
async def test_empty_departures_with_existing_route_logs_info_not_warning(
    fake_api, caplog
):
    """Probe A empty + the route still exists == a quiet period, not a fault."""
    wire_happy_path(fake_api)  # the directions fixture is non-empty
    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-empty.json"))
    fake_api.add(EMPTY_PROBE_PATH, {"departures": []})
    connector = make_connector()

    caplog.set_level(logging.DEBUG)
    await prepare_and_update(connector)

    assert connector.departures == []
    assert fake_api.requests == [
        DEPARTURES_PATH,
        EMPTY_PROBE_PATH,
        ROUTE_CHECK_PATH,
    ]
    infos = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    ]
    assert (
        "PTV: no departures currently scheduled for route 6 stop 1071 "
        "direction 5 (probe A returned nothing) - may be a quiet period"
    ) in infos
    assert not warning_messages(caplog, "PTV:")


@pytest.mark.asyncio
async def test_empty_diagnosis_cools_down_until_a_non_empty_poll(fake_api, caplog):
    """Persistent empties stay quiet; any non-empty poll re-arms the warning."""
    wire_happy_path(fake_api)
    empty = fixture("departures-200-empty.json")
    fake_api.add(REQUEST_DEPARTURES, empty)
    fake_api.add(
        EMPTY_PROBE_PATH, {"departures": copy.deepcopy(RECORDED_DEPARTURES[:1])}
    )
    connector = make_connector()

    caplog.set_level(logging.DEBUG)
    await prepare_and_update(connector)
    assert fake_api.requests == [DEPARTURES_PATH, EMPTY_PROBE_PATH]
    assert len(warning_messages(caplog, "direction id looks stale")) == 1

    # Second empty poll inside the cooldown: no probes, no second warning.
    await connector.async_update()
    assert fake_api.requests.count(DEPARTURES_PATH) == 2
    assert fake_api.requests.count(EMPTY_PROBE_PATH) == 1
    assert len(warning_messages(caplog, "direction id looks stale")) == 1

    # Any non-empty poll re-arms the diagnosis...
    fake_api.add(
        REQUEST_DEPARTURES, fixture("departures-frankston-flinders.json")
    )
    await connector.async_update()
    assert len(connector.departures) == len(RECORDED_DEPARTURES)

    # ...so the next dry spell probes and warns again.
    fake_api.add(REQUEST_DEPARTURES, empty)
    await connector.async_update()
    assert connector.departures == []
    assert fake_api.requests.count(EMPTY_PROBE_PATH) == 2
    assert len(warning_messages(caplog, "direction id looks stale")) == 2


@pytest.mark.asyncio
async def test_all_rows_filtered_out_probes_and_warns_about_route_id(
    fake_api, caplog
):
    """Rows exist for the stop but none for the configured route."""
    wire_happy_path(fake_api)
    rows = copy.deepcopy(RECORDED_DEPARTURES[:2])
    rows[0]["route_id"] = 11
    del rows[1]["route_id"]
    fake_api.add(REQUEST_DEPARTURES, {"departures": rows})
    fake_api.add(EMPTY_PROBE_PATH, {"departures": copy.deepcopy(rows)})
    connector = make_connector()

    caplog.set_level(logging.DEBUG)
    await prepare_and_update(connector)

    assert connector.departures == []
    assert fake_api.requests == [DEPARTURES_PATH, EMPTY_PROBE_PATH]
    assert warning_messages(caplog, "none match the configured route id") == [
        "PTV: departures exist for this stop but none match the configured "
        "route id (through-running or stale route id) - re-add the config "
        "entry naming route 6"
    ]


@pytest.mark.asyncio
async def test_empty_probe_failure_never_fails_the_update(fake_api, caplog):
    """A probe's own failure is a debug log; the empty result still succeeds."""
    wire_happy_path(fake_api)
    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-empty.json"))
    fake_api.raise_on(
        EMPTY_PROBE_PATH, connector_module.PTVConnectionError("probe boom")
    )
    connector = make_connector()

    caplog.set_level(logging.DEBUG)
    await prepare_and_update(connector)

    assert connector.departures == []
    assert fake_api.requests == [DEPARTURES_PATH, EMPTY_PROBE_PATH]
    assert "PTV: empty-result departure probe failed" in caplog.text
    assert not warning_messages(caplog, "PTV:")


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


# ---------------------------------------------------------------------------
# P1.4: one shared session, one explicit timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_injected_session_serves_every_request(fake_api):
    """Every request goes through the one injected session object.

    ``aiohttp.ClientSession`` is monkeypatched to explode by the fixture, so a
    request that tried to build its own session would fail loudly here.
    """
    wire_happy_path(fake_api)
    connector = make_connector(session=fake_api.session)

    await connector.async_route_types()
    await connector.async_routes(0)
    await connector.async_directions(6)
    await connector.async_stops(6)
    await prepare_and_update(connector)

    assert fake_api.session_resolutions == 0
    assert len(fake_api.get_calls) == 5 + len(RECORDED_DEPARTURES)


@pytest.mark.asyncio
async def test_get_session_uses_the_real_ha_shared_session(hass):
    """The production path must call the real helper without awaiting it.

    ``async_get_clientsession`` is a synchronous callback in supported Home
    Assistant releases; awaiting it would raise on a real install.
    """
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    connector = make_connector()
    connector.hass = hass

    session = connector._get_session()

    assert session is async_get_clientsession(hass)
    assert connector._get_session() is session


@pytest.mark.asyncio
async def test_resolved_shared_session_is_cached(fake_api):
    """The HA session is resolved once and reused for every later request."""
    wire_happy_path(fake_api)
    connector = make_connector()

    await prepare_and_update(connector)
    await connector.async_route_types()

    assert fake_api.session_resolutions == 1


@pytest.mark.asyncio
async def test_every_request_passes_the_module_timeout(fake_api):
    wire_happy_path(fake_api)
    connector = make_connector()

    await connector.async_route_types()
    await connector.async_routes(0)
    await connector.async_directions(6)
    await connector.async_stops(6)
    await prepare_and_update(connector)

    assert connector_module.REQUEST_TIMEOUT.total == 30
    assert fake_api.get_calls
    for path, kwargs in fake_api.get_calls:
        assert kwargs.get("timeout") is connector_module.REQUEST_TIMEOUT, path


# ---------------------------------------------------------------------------
# P1.4: typed failures from async_update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, connector_module.PTVAuthError),
        (403, connector_module.PTVAuthError),
        (500, connector_module.PTVServiceError),
        (502, connector_module.PTVServiceError),
    ],
)
async def test_async_update_status_errors_are_typed(fake_api, status, error):
    wire_happy_path(fake_api)
    fake_api.set_status(REQUEST_DEPARTURES, status)
    connector = make_connector()

    with pytest.raises(error):
        await prepare_and_update(connector)

    assert connector.departures == []


@pytest.mark.asyncio
async def test_async_update_connection_error_is_typed(fake_api):
    wire_happy_path(fake_api)
    fake_api.raise_on(REQUEST_DEPARTURES, aiohttp.ClientError("boom"))
    connector = make_connector()

    with pytest.raises(connector_module.PTVConnectionError):
        await prepare_and_update(connector)


# ---------------------------------------------------------------------------
# P1.4: the coordinator translates typed errors into UpdateFailed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_returns_departures_after_success(fake_api, hass):
    wire_happy_path(fake_api)
    connector = make_connector()
    connector.prepare()
    coordinator = sensor_module.PublicTransportVictoriaDataUpdateCoordinator(
        hass, connector
    )

    data = await coordinator._async_update_data()

    assert data == connector.departures
    assert len(data) == len(RECORDED_DEPARTURES)


@pytest.mark.asyncio
async def test_coordinator_marks_typed_failure_as_update_failed(fake_api, hass):
    wire_happy_path(fake_api)
    connector = make_connector()
    connector.prepare()
    coordinator = sensor_module.PublicTransportVictoriaDataUpdateCoordinator(
        hass, connector
    )
    previous = list(connector.departures)

    fake_api.set_status(REQUEST_DEPARTURES, 500)
    with pytest.raises(sensor_module.UpdateFailed) as excinfo:
        await coordinator._async_update_data()

    assert "HTTP 500" in str(excinfo.value)
    # The coordinator failure does not destroy the last good connector data.
    assert connector.departures == previous


@pytest.mark.asyncio
async def test_coordinator_refresh_marks_failure_after_success(fake_api, hass):
    """End-to-end: one good poll, then a failing poll flips the success flag."""
    wire_happy_path(fake_api)
    connector = make_connector()
    # P1.5: entry setup only computes the path; the coordinator owns the fetch.
    connector.prepare()
    coordinator = sensor_module.PublicTransportVictoriaDataUpdateCoordinator(
        hass, connector
    )

    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    assert len(coordinator.data) == len(RECORDED_DEPARTURES)

    fake_api.set_status(REQUEST_DEPARTURES, 500)
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False


@pytest.mark.asyncio
async def test_empty_departures_are_a_successful_empty_result(fake_api, hass):
    """Acceptance: an empty list is valid data, not a coordinator failure."""
    wire_happy_path(fake_api)
    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-empty.json"))
    connector = make_connector()
    connector.prepare()
    coordinator = make_coordinator(hass, connector)

    await coordinator.async_config_entry_first_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data == []


# ---------------------------------------------------------------------------
# P1.5: one polling layer, no throttle, immutable coordinator data
# ---------------------------------------------------------------------------


def test_prepare_only_computes_the_path_without_network_io(fake_api):
    """Entry setup must not fetch: prepare() is synchronous and I/O free."""
    connector = make_connector()

    assert not inspect.iscoroutinefunction(Connector.prepare)
    connector.prepare()

    assert connector.departures_path == DEPARTURES_PATH
    assert connector.departures == []
    assert fake_api.requests == []
    assert fake_api.get_calls == []
    assert fake_api.session_resolutions == 0


def test_no_throttle_layer_remains_in_the_connector():
    """The coordinator is the only polling boundary."""
    assert not hasattr(connector_module, "Throttle")
    assert not hasattr(connector_module, "MIN_TIME_BETWEEN_UPDATES")


@pytest.mark.asyncio
async def test_entry_setup_prepares_without_fetching(fake_api):
    """Config-entry setup performs zero HTTP calls; the platform fetches once."""
    import custom_components.public_transport_victoria as integration
    from custom_components.public_transport_victoria.const import (
        CONF_DIRECTION,
        CONF_DIRECTION_NAME,
        CONF_ROUTE,
        CONF_ROUTE_NAME,
        CONF_ROUTE_TYPE,
        CONF_ROUTE_TYPE_NAME,
        CONF_STOP,
        CONF_STOP_NAME,
        DOMAIN,
    )
    from homeassistant.const import CONF_API_KEY, CONF_ID

    forwarded = []

    async def forward_entry_setups(entry, platforms):
        forwarded.append((entry, platforms))

    hass = types.SimpleNamespace(
        data={},
        config_entries=types.SimpleNamespace(
            async_forward_entry_setups=forward_entry_setups
        ),
    )
    entry = types.SimpleNamespace(
        entry_id="entry-1",
        data={
            CONF_ID: CANARY_DEVID,
            CONF_API_KEY: CANARY_API_KEY,
            CONF_ROUTE_TYPE: 0,
            CONF_ROUTE: 6,
            CONF_DIRECTION: 5,
            CONF_STOP: 1071,
            CONF_ROUTE_TYPE_NAME: "Train",
            CONF_ROUTE_NAME: "Frankston",
            CONF_DIRECTION_NAME: "Frankston",
            CONF_STOP_NAME: "Flinders Street Station",
        },
    )

    assert await integration.async_setup(hass, {}) is True
    assert await integration.async_setup_entry(hass, entry) is True

    connector = hass.data[DOMAIN]["entry-1"]
    assert connector.departures_path == DEPARTURES_PATH
    assert connector.departures == []
    assert fake_api.requests == []
    assert fake_api.get_calls == []
    assert forwarded == [(entry, ["sensor"])]


@pytest.mark.asyncio
async def test_successive_connector_updates_always_fetch(fake_api):
    """Without a throttle, two updates issue two separate GET bursts."""
    wire_happy_path(fake_api)
    connector = make_connector()

    await prepare_and_update(connector)
    assert fake_api.requests.count(REQUEST_DEPARTURES) == 1

    await connector.async_update()
    assert fake_api.requests.count(REQUEST_DEPARTURES) == 2


@pytest.mark.asyncio
async def test_each_coordinator_refresh_fetches_again(fake_api, hass):
    """Regression for the live defect: refreshes are never suppressed.

    Three consecutive coordinator refreshes (scheduled or manual, both use the
    same path) must each issue exactly one departures request.
    """
    wire_happy_path(fake_api)
    connector = make_connector()
    connector.prepare()
    coordinator = make_coordinator(hass, connector)

    assert fake_api.requests.count(REQUEST_DEPARTURES) == 0

    for expected in (1, 2, 3):
        await coordinator.async_refresh()
        assert coordinator.last_update_success is True
        assert fake_api.requests.count(REQUEST_DEPARTURES) == expected


@pytest.mark.asyncio
async def test_manual_entity_update_issues_a_fetch(fake_api, hass):
    """``homeassistant.update_entity`` reaches the API without being swallowed."""
    wire_happy_path(fake_api)
    connector = make_connector()
    connector.prepare()
    coordinator = make_coordinator(hass, connector)
    await coordinator.async_config_entry_first_refresh()

    entity = sensor_module.PublicTransportVictoriaSensor(coordinator, 0)
    entity.hass = hass
    before = fake_api.requests.count(REQUEST_DEPARTURES)

    await entity.async_update()
    await hass.async_block_till_done()

    assert fake_api.requests.count(REQUEST_DEPARTURES) == before + 1


@pytest.mark.asyncio
async def test_first_refresh_failure_is_retryable_and_recovers(fake_api, hass):
    """Startup failure stays retryable; a later success restores availability."""
    wire_happy_path(fake_api)
    fake_api.set_status(REQUEST_DEPARTURES, 500)
    connector = make_connector()
    connector.prepare()
    coordinator = make_coordinator(hass, connector)
    entity = sensor_module.PublicTransportVictoriaSensor(coordinator, 0)

    with pytest.raises(ConfigEntryNotReady):
        await coordinator.async_config_entry_first_refresh()

    assert coordinator.last_update_success is False
    assert coordinator.data is None
    assert entity.available is False
    assert connector.departures == []

    fake_api.add(
        REQUEST_DEPARTURES, fixture("departures-frankston-flinders.json")
    )
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert entity.available is True
    assert len(coordinator.data) == len(RECORDED_DEPARTURES)


@pytest.mark.asyncio
async def test_attribute_reads_do_not_mutate_coordinator_data(fake_api, hass):
    """Attributes are a fresh copy; attribution is entity metadata."""
    wire_happy_path(fake_api)
    connector = make_connector()
    connector.prepare()
    coordinator = make_coordinator(hass, connector)
    await coordinator.async_config_entry_first_refresh()
    snapshot = copy.deepcopy(coordinator.data)
    entity = sensor_module.PublicTransportVictoriaSensor(coordinator, 0)

    first = entity.extra_state_attributes
    second = entity.extra_state_attributes

    assert coordinator.data == snapshot
    assert first == second
    assert first is not coordinator.data[0]
    assert "attribution" not in first
    assert "attribution" not in coordinator.data[0]
    assert entity.attribution == sensor_module.ATTRIBUTION

    first["injected"] = True
    assert "injected" not in coordinator.data[0]


def test_convert_utc_to_local_is_deterministic():
    assert (
        connector_module.convert_utc_to_local("2026-11-27T06:40:00Z", make_hass())
        == "06:40 AM"
    )
