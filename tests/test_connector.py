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
from homeassistant.const import CONF_API_KEY, CONF_ID
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
# One tiny single-record call used to backfill the configured route's GTFS id.
ROUTE_INFO_PATH = "/v3/routes/6?route_types=0"
# A stale direction id (the fixtures only carry 1 and 5).
STALE_DIRECTION = 17
REQUEST_DEPARTURES_STALE = (
    "/v3/departures/route_type/0/stop/1071/route/6"
    "?direction_id=17&max_results=5"
)

ENTRY_ID = "entry-1"
ISSUE_ID = "stale_ptv_ids_{}".format(ENTRY_ID)
MANUAL_TRANSLATION_KEY = "stale_ptv_ids_manual"


# ---------------------------------------------------------------------------
# Module loading (real homeassistant; no Throttle shim after P1.5)
# ---------------------------------------------------------------------------


from custom_components.public_transport_victoria.PublicTransportVictoria import (  # noqa: E402
    public_transport_victoria as connector_module,
)


Connector = connector_module.Connector

from custom_components.public_transport_victoria import sensor as sensor_module  # noqa: E402
from custom_components.public_transport_victoria import repairs as repairs_module  # noqa: E402
from custom_components.public_transport_victoria import config_flow as config_flow_module  # noqa: E402
from custom_components.public_transport_victoria.const import (  # noqa: E402
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


BASE_ENTRY_DATA = {
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
}


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


class FakeEntry(types.SimpleNamespace):
    """Minimal config-entry stand-in for coordinator policy tests.

    It carries the attributes the real ``DataUpdateCoordinator`` touches and
    records nothing else.  Writes go through a fake
    ``hass.config_entries.async_update_entry`` so tests can count them exactly.
    """

    def __init__(self, data=None, entry_id=ENTRY_ID,
                 title="Frankston line to Frankston from Flinders Street Station"):
        super().__init__(
            entry_id=entry_id,
            title=title,
            data=dict(BASE_ENTRY_DATA),
            state=ConfigEntryState.SETUP_IN_PROGRESS,
            pref_disable_polling=False,
        )
        if data:
            self.data.update(data)

    def async_on_unload(self, func):
        """No-op: a fake entry owns no lifecycle callbacks."""
        return None


def make_coordinator(hass, connector, entry=None):
    """Build a coordinator as if created during config-entry setup.

    The coordinator is constructed with the config entry (production path) and
    every config-entry write is recorded on ``coordinator.test_writes``.
    """
    if entry is None:
        entry = FakeEntry()
    writes = []

    def async_update_entry(target, *, data=None, **kwargs):
        writes.append(dict(data))
        target.data = dict(data)
        return True

    hass.config_entries.async_update_entry = async_update_entry
    coordinator = sensor_module.PublicTransportVictoriaDataUpdateCoordinator(
        hass, connector, config_entry=entry
    )
    coordinator.test_entry = entry
    coordinator.test_writes = writes
    return coordinator


def make_entry_loaded(entry):
    """Mark a mock config entry loaded (state is frozen against assignment)."""
    object.__setattr__(entry, "state", ConfigEntryState.LOADED)


def make_connector_from_data(hass, data):
    """Build a connector the way __init__.py would from entry data."""
    connector = Connector(
        hass,
        data[CONF_ID],
        data[CONF_API_KEY],
        data[CONF_ROUTE_TYPE],
        data[CONF_ROUTE],
        data[CONF_DIRECTION],
        data[CONF_STOP],
        data[CONF_ROUTE_TYPE_NAME],
        data[CONF_ROUTE_NAME],
        data[CONF_DIRECTION_NAME],
        data[CONF_STOP_NAME],
    )
    stored_gtfs = data.get("route_gtfs_id")
    if stored_gtfs:
        connector.gtfs_id = str(stored_gtfs)
    connector.prepare()
    return connector


async def setup_repairs(hass):
    """Set up the repairs integration and register this component's platform."""
    from homeassistant.components.repairs import repairs_flow_manager

    assert await async_setup_component(hass, "repairs", {})
    hass.data["repairs"].setdefault("platforms", {})[DOMAIN] = repairs_module
    return repairs_flow_manager(hass)


def create_fixable_issue(hass, entry, *, what="direction", candidate=None):
    """Create a fixable issue directly, as a previous diagnosis would have."""
    candidate = candidate or {
        "route": "6",
        "direction": "5",
        "stop": "1071",
        "gtfs": "2-FKN",
    }
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_ID,
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="stale_ptv_ids",
        translation_placeholders={},
        data={
            "entry_id": entry.entry_id,
            "what": what,
            "candidate": dict(candidate),
        },
    )


async def add_real_entry(hass, data=None):
    """Add a loaded MockConfigEntry for flow tests."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**BASE_ENTRY_DATA, **(data or {})},
        entry_id=ENTRY_ID,
        title="Frankston line to Frankston from Flinders Street Station",
    )
    entry.add_to_hass(hass)
    make_entry_loaded(entry)
    return entry


async def churn_on_real_entry(fake_api, hass):
    """Healthy poll then a stale-direction churn on a real config entry."""
    fake_api.add(
        REQUEST_DEPARTURES_STALE,
        fixture("departures-frankston-flinders.json"),
    )
    fake_api.add(
        ROUTE_INFO_PATH,
        {"route": {"route_id": 6, "route_gtfs_id": "2-FKN"}},
    )
    fake_api.add(DIRECTIONS_PATH, fixture("directions-frankston.json"))
    fake_api.add(STOPS_PATH, fixture("stops-frankston.json"))
    entry = await add_real_entry(hass, {CONF_DIRECTION: STALE_DIRECTION})
    connector = make_connector(
        direction=STALE_DIRECTION,
        route_name="Frankston",
        direction_name="Frankston",
        stop_name="Flinders Street Station",
    )
    connector.prepare()
    coordinator = sensor_module.PublicTransportVictoriaDataUpdateCoordinator(
        hass, connector, config_entry=entry
    )
    await coordinator._async_update_data()  # healthy poll
    assert issue_for(hass) is None

    fake_api.add(
        REQUEST_DEPARTURES_STALE, fixture("departures-200-empty.json")
    )
    fake_api.add(
        EMPTY_PROBE_PATH, {"departures": copy.deepcopy(RECORDED_DEPARTURES[:1])}
    )
    await coordinator._async_update_data()  # churn event
    return entry, coordinator


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
    connector = make_connector()

    await prepare_and_update(connector)

    assert connector.departures == []
    # 0.8.5: an update only records evidence; the coordinator decides whether
    # to diagnose.  No probe is issued from async_update itself.
    assert connector.last_result_meta == {"raw_rows": 0, "kept_rows": 0}
    assert fake_api.requests == [DEPARTURES_PATH]


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
    assert connector.last_result_meta == {"raw_rows": 2, "kept_rows": 0}
    # Empty is a valid result, not a failure: no run lookups were attempted.
    # With no healthy evidence yet the coordinator does not probe (0.8.5).
    assert fake_api.requests == [DEPARTURES_PATH]
    assert (
        "filtered 2 departure row(s) not matching the configured route"
        in caplog.text
    )


# ---------------------------------------------------------------------------
# #50: empty-poll evidence (connector) and repair policy (coordinator)
# ---------------------------------------------------------------------------


def issue_for(hass):
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ID)


@pytest.mark.asyncio
async def test_diagnose_empty_reports_stale_direction_from_probe(fake_api):
    """Empty + rows without the direction filter == stale direction id."""
    wire_happy_path(fake_api)
    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-empty.json"))
    fake_api.add(
        EMPTY_PROBE_PATH, {"departures": copy.deepcopy(RECORDED_DEPARTURES[:1])}
    )
    connector = make_connector()
    await prepare_and_update(connector)

    result = await connector.diagnose_empty()

    assert result == {"what": "direction", "reason": "direction id stale"}
    assert fake_api.requests == [DEPARTURES_PATH, EMPTY_PROBE_PATH]
    assert "direction_id" not in EMPTY_PROBE_PATH
    assert "max_results=1" in EMPTY_PROBE_PATH
    # Probe rows are never processed, so no run lookups are attempted.
    assert not any(path.startswith("/v3/runs/") for path in fake_api.requests)


@pytest.mark.asyncio
async def test_diagnose_empty_reports_stale_route_without_directions(fake_api):
    """Probe A empty + the route has no directions == stale route id."""
    wire_happy_path(fake_api)
    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-empty.json"))
    fake_api.add(EMPTY_PROBE_PATH, {"departures": []})
    fake_api.add(ROUTE_CHECK_PATH, {"directions": []})
    connector = make_connector()
    await prepare_and_update(connector)

    result = await connector.diagnose_empty()

    assert result == {"what": "route", "reason": "route has no directions"}
    assert fake_api.requests == [
        DEPARTURES_PATH,
        EMPTY_PROBE_PATH,
        ROUTE_CHECK_PATH,
    ]


@pytest.mark.asyncio
async def test_diagnose_empty_reports_quiet_when_route_exists(fake_api):
    """Probe A empty + the route still exists == a quiet period, not a fault."""
    wire_happy_path(fake_api)  # the directions fixture is non-empty
    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-empty.json"))
    fake_api.add(EMPTY_PROBE_PATH, {"departures": []})
    connector = make_connector()
    await prepare_and_update(connector)

    result = await connector.diagnose_empty()

    assert result == {
        "what": "quiet",
        "reason": "no departures currently scheduled",
    }


@pytest.mark.asyncio
async def test_diagnose_empty_reports_route_mismatch_without_probes(fake_api):
    """Rows for other routes are a mismatch; no departure probe is needed."""
    wire_happy_path(fake_api)
    rows = copy.deepcopy(RECORDED_DEPARTURES[:2])
    rows[0]["route_id"] = 11
    del rows[1]["route_id"]
    fake_api.add(REQUEST_DEPARTURES, {"departures": rows})
    connector = make_connector()
    await prepare_and_update(connector)

    assert connector.last_result_meta == {"raw_rows": 2, "kept_rows": 0}
    result = await connector.diagnose_empty()

    assert result == {"what": "route_mismatch"}
    assert fake_api.requests == [DEPARTURES_PATH]


@pytest.mark.asyncio
async def test_diagnose_empty_probe_failure_abstains(fake_api, caplog):
    """A failed probe cannot establish stale ids, so the verdict abstains."""
    wire_happy_path(fake_api)
    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-empty.json"))
    fake_api.raise_on(
        EMPTY_PROBE_PATH, connector_module.PTVConnectionError("probe boom")
    )
    connector = make_connector()
    await prepare_and_update(connector)

    caplog.set_level(logging.DEBUG)
    result = await connector.diagnose_empty()

    assert result["what"] == "unknown"
    assert fake_api.requests == [DEPARTURES_PATH, EMPTY_PROBE_PATH]
    assert "PTV: empty-result departure probe failed" in caplog.text


@pytest.mark.asyncio
async def test_diagnose_empty_is_healthy_when_rows_are_kept(fake_api):
    wire_happy_path(fake_api)
    connector = make_connector()
    await prepare_and_update(connector)

    assert connector.last_result_meta == {
        "raw_rows": len(RECORDED_DEPARTURES),
        "kept_rows": len(RECORDED_DEPARTURES),
    }
    assert await connector.diagnose_empty() == {"what": "healthy"}


# ---------------------------------------------------------------------------
# resolve_candidate: informational replacement ids (never persisted here)
# ---------------------------------------------------------------------------


async def empty_direction_connector(fake_api):
    """Connector whose last diagnosis is a stale direction id."""
    wire_happy_path(fake_api)
    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-empty.json"))
    fake_api.add(
        EMPTY_PROBE_PATH, {"departures": copy.deepcopy(RECORDED_DEPARTURES[:1])}
    )
    connector = make_connector(
        route_name="Frankston",
        direction_name="Frankston",
        stop_name="Flinders Street Station",
    )
    await prepare_and_update(connector)
    await connector.diagnose_empty()
    return connector


@pytest.mark.asyncio
async def test_resolve_candidate_backfills_gtfs_id_with_one_small_call(fake_api):
    connector = await empty_direction_connector(fake_api)
    fake_api.add(
        ROUTE_INFO_PATH,
        {"route": {"route_id": 6, "route_gtfs_id": "2-FKN"}},
    )

    result = await connector.resolve_candidate()

    assert connector.gtfs_id == "2-FKN"
    assert result["resolved"] is True
    assert result["what"] == "direction"
    assert result["candidate"] == {
        "route": "6",
        "direction": "5",
        "stop": "1071",
        "gtfs": "2-FKN",
    }
    assert fake_api.requests.count(ROUTE_INFO_PATH) == 1

    # A second resolution in the same process reuses the captured GTFS id.
    await connector.resolve_candidate()
    assert fake_api.requests.count(ROUTE_INFO_PATH) == 1


@pytest.mark.asyncio
async def test_resolve_candidate_abstains_on_ambiguous_direction_name(fake_api):
    connector = await empty_direction_connector(fake_api)
    fake_api.add(
        ROUTE_INFO_PATH,
        {"route": {"route_id": 6, "route_gtfs_id": "2-FKN"}},
    )
    fake_api.add(
        ROUTE_CHECK_PATH,
        {
            "directions": [
                {"direction_id": 1, "direction_name": "Frankston "},
                {"direction_id": 2, "direction_name": "frankston"},
            ]
        },
    )

    result = await connector.resolve_candidate()

    assert result["resolved"] is False
    assert result["candidate"]["direction"] is None
    assert result["candidate"]["stop"] is None
    assert "several" in result["reason"]


@pytest.mark.asyncio
async def test_resolve_candidate_route_by_unique_gtfs_match(fake_api):
    connector = make_connector(
        route_name="Frankston",
        direction_name="Frankston",
        stop_name="Flinders Street Station",
    )
    connector.gtfs_id = "2-FKN"
    connector.last_diagnosis = {"what": "route"}
    fake_api.add(
        ROUTES_PATH,
        {
            "routes": [
                {
                    "route_id": 99,
                    "route_gtfs_id": "2-FKN",
                    "route_name": "Frankston",
                    "route_number": "6",
                },
                {
                    "route_id": 100,
                    "route_gtfs_id": "2-STY",
                    "route_name": "Stony Point",
                    "route_number": "",
                },
            ]
        },
    )
    fake_api.add(
        "/v3/stops/route/99/route_type/0", fixture("stops-frankston.json")
    )
    fake_api.add(
        "/v3/directions/route/99", fixture("directions-frankston.json")
    )

    result = await connector.resolve_candidate()

    assert result["resolved"] is True
    assert result["candidate"] == {
        "route": "99",
        "direction": "5",
        "stop": "1071",
        "gtfs": "2-FKN",
    }


@pytest.mark.asyncio
async def test_resolve_candidate_route_abstains_on_duplicate_gtfs(fake_api):
    connector = make_connector(
        route_name="Frankston",
        direction_name="Frankston",
        stop_name="Flinders Street Station",
    )
    connector.gtfs_id = "2-FKN"
    connector.last_diagnosis = {"what": "route"}
    fake_api.add(
        ROUTES_PATH,
        {
            "routes": [
                {"route_id": 99, "route_gtfs_id": "2-FKN", "route_name": "A"},
                {"route_id": 100, "route_gtfs_id": "2-FKN", "route_name": "B"},
            ]
        },
    )

    result = await connector.resolve_candidate()

    assert result["resolved"] is False
    assert result["candidate"]["route"] is None
    assert "several routes" in result["reason"]


# ---------------------------------------------------------------------------
# 0.8.5 coordinator policy: quiet gate, persisted cooldown, repair lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_poll_without_healthy_evidence_stays_quiet(fake_api, hass):
    wire_happy_path(fake_api)
    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-empty.json"))
    connector = make_connector()
    connector.prepare()
    coordinator = make_coordinator(hass, connector)

    await coordinator._async_update_data()

    assert coordinator.test_writes == []
    assert fake_api.requests == [DEPARTURES_PATH]
    assert issue_for(hass) is None


@pytest.mark.asyncio
async def test_healthy_poll_clears_issue_and_writes_nothing(fake_api, hass):
    wire_happy_path(fake_api)
    connector = make_connector()
    connector.prepare()
    coordinator = make_coordinator(hass, connector)
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_ID,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="stale_ptv_ids",
        translation_placeholders={},
    )
    assert issue_for(hass) is not None

    await coordinator._async_update_data()

    assert issue_for(hass) is None
    assert coordinator.test_writes == []
    assert coordinator._last_healthy_at is not None


@pytest.mark.asyncio
async def test_churn_after_healthy_poll_raises_fixable_issue_and_persists_once(
    fake_api, hass
):
    fake_api.add(
        REQUEST_DEPARTURES_STALE,
        fixture("departures-frankston-flinders.json"),
    )
    fake_api.add(
        ROUTE_INFO_PATH,
        {"route": {"route_id": 6, "route_gtfs_id": "2-FKN"}},
    )
    fake_api.add(DIRECTIONS_PATH, fixture("directions-frankston.json"))
    connector = make_connector(
        direction=STALE_DIRECTION,
        route_name="Frankston",
        direction_name="Frankston",
        stop_name="Flinders Street Station",
    )
    connector.prepare()
    entry = FakeEntry(data={CONF_DIRECTION: STALE_DIRECTION})
    coordinator = make_coordinator(hass, connector, entry=entry)
    await coordinator._async_update_data()  # healthy poll
    assert coordinator.test_writes == []

    fake_api.add(
        REQUEST_DEPARTURES_STALE, fixture("departures-200-empty.json")
    )
    fake_api.add(
        EMPTY_PROBE_PATH, {"departures": copy.deepcopy(RECORDED_DEPARTURES[:1])}
    )
    await coordinator._async_update_data()  # churn event

    issue = issue_for(hass)
    assert issue is not None
    assert issue.issue_id == ISSUE_ID
    assert issue.is_fixable is True
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "stale_ptv_ids"
    assert issue.learn_more_url.endswith("/issues/50")
    assert issue.data["entry_id"] == ENTRY_ID
    assert issue.data["what"] == "direction"
    assert issue.data["candidate"] == {
        "route": "6",
        "direction": "5",
        "stop": "1071",
        "gtfs": "2-FKN",
    }
    placeholders = issue.translation_placeholders
    assert placeholders["route_name"] == "Frankston"
    assert placeholders["old_route_id"] == "6"
    assert placeholders["new_route_id"] == "6"
    assert placeholders["old_direction_id"] == "17"
    assert placeholders["new_direction_id"] == "5"
    assert placeholders["old_stop_id"] == "1071"
    assert placeholders["new_stop_id"] == "1071"
    assert placeholders["route_gtfs_id"] == "2-FKN"
    assert placeholders["detail"] == "matched the configured direction name"

    # Exactly one config-entry write per churn event, carrying the three
    # bounded stamps; configured ids and names are untouched.
    assert len(coordinator.test_writes) == 1
    written = coordinator.test_writes[0]
    assert written["route_gtfs_id"] == "2-FKN"
    assert written["last_departure_at"]
    assert written["diag_next_ok_at"]
    assert written[CONF_DIRECTION] == STALE_DIRECTION
    assert written[CONF_ROUTE] == 6
    assert written[CONF_STOP] == 1071
    assert written["route_name"] == "Frankston"
    assert written["direction_name"] == "Frankston"
    assert written["stop_name"] == "Flinders Street Station"
    assert fake_api.requests.count(ROUTE_INFO_PATH) == 1

    # A healthy poll clears the issue without writing to the entry again.
    fake_api.add(
        REQUEST_DEPARTURES_STALE,
        fixture("departures-frankston-flinders.json"),
    )
    await coordinator._async_update_data()

    assert issue_for(hass) is None
    assert len(coordinator.test_writes) == 1


@pytest.mark.asyncio
async def test_abstained_resolution_raises_manual_issue(fake_api, hass):
    """Ambiguous names cannot be auto-fixed: manual instructions instead."""
    fake_api.add(
        REQUEST_DEPARTURES_STALE,
        fixture("departures-frankston-flinders.json"),
    )
    fake_api.add(
        ROUTE_INFO_PATH,
        {"route": {"route_id": 6, "route_gtfs_id": "2-FKN"}},
    )
    fake_api.add(DIRECTIONS_PATH, fixture("directions-frankston.json"))
    connector = make_connector(
        direction=STALE_DIRECTION,
        route_name="Frankston",
        direction_name="Frankston",
        stop_name="Flinders Street Station",
    )
    connector.prepare()
    entry = FakeEntry(data={CONF_DIRECTION: STALE_DIRECTION})
    coordinator = make_coordinator(hass, connector, entry=entry)
    await coordinator._async_update_data()  # healthy

    fake_api.add(
        REQUEST_DEPARTURES_STALE, fixture("departures-200-empty.json")
    )
    fake_api.add(
        EMPTY_PROBE_PATH, {"departures": copy.deepcopy(RECORDED_DEPARTURES[:1])}
    )
    # Two names normalise to the configured one: resolution must abstain.
    fake_api.add(
        ROUTE_CHECK_PATH,
        {
            "directions": [
                {"direction_id": 1, "direction_name": "Frankston "},
                {"direction_id": 2, "direction_name": "frankston"},
            ]
        },
    )
    await coordinator._async_update_data()

    issue = issue_for(hass)
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.translation_key == MANUAL_TRANSLATION_KEY
    placeholders = issue.translation_placeholders
    assert placeholders["what"] == "direction"
    assert placeholders["configured"] == "route 6, direction 17, stop 1071"
    assert "Delete this entry" in placeholders["remedy"]
    assert "several entries matched" in placeholders["candidate"] or (
        "No unambiguous replacement" in placeholders["candidate"]
    )
    assert len(coordinator.test_writes) == 1
    assert coordinator.test_writes[0][CONF_DIRECTION] == STALE_DIRECTION


@pytest.mark.asyncio
async def test_real_config_entry_write_path_persists_bounded_stamps(fake_api, hass):
    """The production write path (real ConfigEntries manager) works end to end."""
    wire_happy_path(fake_api)
    fake_api.add(
        ROUTE_INFO_PATH,
        {"route": {"route_id": 6, "route_gtfs_id": "2-FKN"}},
    )
    entry = MockConfigEntry(
        domain=DOMAIN, data=dict(BASE_ENTRY_DATA), entry_id=ENTRY_ID
    )
    entry.add_to_hass(hass)
    connector = make_connector(direction_name="Frankston")
    connector.prepare()
    coordinator = sensor_module.PublicTransportVictoriaDataUpdateCoordinator(
        hass, connector, config_entry=entry
    )
    await coordinator._async_update_data()  # healthy
    assert "last_departure_at" not in entry.data

    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-empty.json"))
    fake_api.add(
        EMPTY_PROBE_PATH, {"departures": copy.deepcopy(RECORDED_DEPARTURES[:1])}
    )
    await coordinator._async_update_data()  # churn event

    assert entry.data["route_gtfs_id"] == "2-FKN"
    assert entry.data["last_departure_at"]
    assert entry.data["diag_next_ok_at"]
    assert entry.data[CONF_ROUTE_NAME] == "Frankston"
    assert entry.data[CONF_DIRECTION_NAME] == "Frankston"
    assert entry.data[CONF_STOP_NAME] == "Flinders Street Station"
    assert issue_for(hass) is not None


@pytest.mark.asyncio
async def test_cooldown_is_persisted_and_respected_after_reload(fake_api, hass):
    wire_happy_path(fake_api)
    fake_api.add(
        ROUTE_INFO_PATH,
        {"route": {"route_id": 6, "route_gtfs_id": "2-FKN"}},
    )
    connector = make_connector(direction_name="Frankston")
    connector.prepare()
    coordinator = make_coordinator(hass, connector)
    await coordinator._async_update_data()  # healthy

    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-empty.json"))
    fake_api.add(
        EMPTY_PROBE_PATH, {"departures": copy.deepcopy(RECORDED_DEPARTURES[:1])}
    )
    await coordinator._async_update_data()  # churn: one write, one issue
    assert len(coordinator.test_writes) == 1
    assert issue_for(hass) is not None

    # Reload: a new connector and coordinator over the same entry data.
    connector2 = make_connector(direction_name="Frankston")
    connector2.prepare()
    coordinator2 = make_coordinator(
        hass, connector2, entry=coordinator.test_entry
    )
    before = len(fake_api.requests)

    await coordinator2._async_update_data()

    assert fake_api.requests[before:] == [DEPARTURES_PATH]
    assert coordinator2.test_writes == []
    assert issue_for(hass) is not None


@pytest.mark.asyncio
async def test_quiet_empty_poll_raises_no_issue(fake_api, hass):
    wire_happy_path(fake_api)
    connector = make_connector()
    connector.prepare()
    coordinator = make_coordinator(hass, connector)
    await coordinator._async_update_data()  # healthy

    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-empty.json"))
    fake_api.add(EMPTY_PROBE_PATH, {"departures": []})
    await coordinator._async_update_data()  # probe A empty, route exists

    assert issue_for(hass) is None
    # The cooldown stamp is still bounded to one write per diagnosis window.
    assert len(coordinator.test_writes) == 1
    assert "diag_next_ok_at" in coordinator.test_entry.data


@pytest.mark.asyncio
async def test_route_mismatch_issue_lists_observed_route_ids(fake_api, hass):
    wire_happy_path(fake_api)
    connector = make_connector()
    connector.prepare()
    coordinator = make_coordinator(hass, connector)
    await coordinator._async_update_data()  # healthy

    rows = copy.deepcopy(RECORDED_DEPARTURES[:2])
    rows[0]["route_id"] = 11
    del rows[1]["route_id"]
    fake_api.add(REQUEST_DEPARTURES, {"departures": rows})
    await coordinator._async_update_data()

    issue = issue_for(hass)
    assert issue is not None
    placeholders = issue.translation_placeholders
    assert placeholders["what"] == "route mismatch"
    assert "route id(s) 11" in placeholders["detail"]
    assert len(coordinator.test_writes) == 1


# ---------------------------------------------------------------------------
# 0.8.5 repair flow: fixable issues apply verified ids in one click
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repair_flow_applies_verified_ids(fake_api, hass):
    entry, _coordinator = await churn_on_real_entry(fake_api, hass)
    issue = issue_for(hass)
    assert issue is not None
    assert issue.is_fixable is True

    names = (CONF_ROUTE_NAME, CONF_DIRECTION_NAME, CONF_STOP_NAME)
    names_before = {key: entry.data[key] for key in names}
    data_before = dict(entry.data)

    writes = []
    real_update = hass.config_entries.async_update_entry

    def record_update(target, *, data=None, **kwargs):
        writes.append(dict(data))
        return real_update(target, data=data)

    hass.config_entries.async_update_entry = record_update
    reloads = []
    hass.config_entries.async_schedule_reload = reloads.append

    manager = await setup_repairs(hass)
    result = await manager.async_init(
        DOMAIN, context={"source": "repairs"}, data={"issue_id": ISSUE_ID}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["errors"] is None
    assert result["description_placeholders"]["old_direction_id"] == "17"
    assert result["description_placeholders"]["new_direction_id"] == "5"
    assert result["description_placeholders"]["old_stop_id"] == "1071"
    assert result["description_placeholders"]["new_stop_id"] == "1071"

    result = await manager.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_ROUTE] == "6"
    assert entry.data[CONF_DIRECTION] == "5"
    assert entry.data[CONF_STOP] == "1071"
    assert entry.data["route_gtfs_id"] == "2-FKN"
    assert entry.data["diag_next_ok_at"]
    assert entry.data["last_departure_at"] == data_before["last_departure_at"]
    assert {key: entry.data[key] for key in names} == names_before
    assert writes == [dict(entry.data)]
    assert reloads == [ENTRY_ID]
    # The repairs flow manager auto-deletes a fixable issue on completion.
    assert issue_for(hass) is None


@pytest.mark.asyncio
async def test_repair_flow_is_idempotent_when_candidate_matches_stored(
    fake_api, hass
):
    wire_happy_path(fake_api)
    entry = await add_real_entry(hass, {"route_gtfs_id": "2-FKN"})
    create_fixable_issue(hass, entry)
    data_before = dict(entry.data)
    reloads = []
    hass.config_entries.async_schedule_reload = reloads.append

    manager = await setup_repairs(hass)
    result = await manager.async_init(
        DOMAIN, context={"source": "repairs"}, data={"issue_id": ISSUE_ID}
    )
    result = await manager.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert {
        key: value
        for key, value in entry.data.items()
        if key != "diag_next_ok_at"
    } == data_before
    assert entry.data["diag_next_ok_at"]
    assert reloads == []
    assert issue_for(hass) is None


@pytest.mark.asyncio
async def test_repair_flow_downgrades_issue_when_reverification_fails(
    fake_api, hass
):
    wire_happy_path(fake_api)
    entry = await add_real_entry(hass, {"route_gtfs_id": "2-FKN"})
    create_fixable_issue(hass, entry, what="route")
    data_before = dict(entry.data)
    reloads = []
    hass.config_entries.async_schedule_reload = reloads.append
    # The live route list no longer yields a unique candidate.
    fake_api.add(ROUTES_PATH, {"routes": []})

    manager = await setup_repairs(hass)
    result = await manager.async_init(
        DOMAIN, context={"source": "repairs"}, data={"issue_id": ISSUE_ID}
    )
    result = await manager.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "verification_failed"}
    issue = issue_for(hass)
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.translation_key == MANUAL_TRANSLATION_KEY
    assert {
        key: value
        for key, value in entry.data.items()
        if key != "diag_next_ok_at"
    } == data_before
    assert reloads == []


@pytest.mark.asyncio
async def test_repair_flow_aborts_when_entry_not_loaded(fake_api, hass):
    wire_happy_path(fake_api)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**BASE_ENTRY_DATA, "route_gtfs_id": "2-FKN"},
        entry_id=ENTRY_ID,
    )
    entry.add_to_hass(hass)  # remains NOT_LOADED
    create_fixable_issue(hass, entry)
    data_before = dict(entry.data)
    reloads = []
    hass.config_entries.async_schedule_reload = reloads.append

    manager = await setup_repairs(hass)
    result = await manager.async_init(
        DOMAIN, context={"source": "repairs"}, data={"issue_id": ISSUE_ID}
    )
    assert result["type"] is FlowResultType.FORM
    result = await manager.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"
    assert dict(entry.data) == data_before
    assert reloads == []
    # Aborting is not a fix: the fixable issue is still there.
    issue = issue_for(hass)
    assert issue is not None
    assert issue.is_fixable is True


@pytest.mark.asyncio
async def test_post_fix_reload_cooldown_blocks_new_diagnosis(fake_api, hass):
    entry, _coordinator = await churn_on_real_entry(fake_api, hass)
    reloads = []
    hass.config_entries.async_schedule_reload = reloads.append

    manager = await setup_repairs(hass)
    result = await manager.async_init(
        DOMAIN, context={"source": "repairs"}, data={"issue_id": ISSUE_ID}
    )
    result = await manager.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert reloads == [ENTRY_ID]
    assert issue_for(hass) is None
    data_after_fix = dict(entry.data)

    # The scheduled reload rebuilds the coordinator from the new entry data.
    connector2 = make_connector_from_data(hass, entry.data)
    coordinator2 = sensor_module.PublicTransportVictoriaDataUpdateCoordinator(
        hass, connector2, config_entry=entry
    )
    fake_api.add(REQUEST_DEPARTURES, fixture("departures-200-empty.json"))
    before = len(fake_api.requests)

    await coordinator2._async_update_data()

    # The persisted cooldown is honoured by the new instance: no probes,
    # no new issue, and no entry write.
    assert fake_api.requests[before:] == [DEPARTURES_PATH]
    assert dict(entry.data) == data_after_fix
    assert issue_for(hass) is None


# ---------------------------------------------------------------------------
# 0.8.5 options flow: manual "check timetable IDs now"
# ---------------------------------------------------------------------------


def instrument_entry(hass):
    """Record data writes (not options writes) and reload requests."""
    writes = []
    reloads = []
    real_update = hass.config_entries.async_update_entry

    def record_update(target, *, data=None, **kwargs):
        if data is not None:
            writes.append(dict(data))
            return real_update(target, data=data, **kwargs)
        return real_update(target, **kwargs)

    hass.config_entries.async_update_entry = record_update
    hass.config_entries.async_schedule_reload = reloads.append
    return writes, reloads


async def start_options_flow(hass):
    """Open the options flow and choose the id check."""
    register_config_flow(hass)
    result = await hass.config_entries.options.async_init(ENTRY_ID)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "check_ids"}
    )


def register_config_flow(hass):
    """Register the config-flow handler for the options-flow manager.

    The HA test harness has no installed integration package, so the options
    manager cannot load ``public_transport_victoria.config_flow`` through the
    loader.  Marking the module loaded is the same seam the manager checks.
    """
    from homeassistant import loader
    from homeassistant.config_entries import HANDLERS

    HANDLERS.register(DOMAIN)(config_flow_module.ConfigFlow)
    hass.data.setdefault(loader.DATA_COMPONENTS, {})[
        "{}.config_flow".format(DOMAIN)
    ] = config_flow_module


def wire_healthy_audit(fake_api):
    """Responses needed by an audit: route record, directions, stops."""
    fake_api.add(
        ROUTE_INFO_PATH,
        {"route": {"route_id": 6, "route_gtfs_id": "2-FKN"}},
    )
    fake_api.add(DIRECTIONS_PATH, fixture("directions-frankston.json"))
    fake_api.add(STOPS_PATH, fixture("stops-frankston.json"))


@pytest.mark.asyncio
async def test_options_flow_menu_offers_single_check(fake_api, hass):
    entry = await add_real_entry(hass)
    assert config_flow_module.ConfigFlow.async_supports_options_flow(entry)

    register_config_flow(hass)
    result = await hass.config_entries.options.async_init(ENTRY_ID)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["check_ids"]


@pytest.mark.asyncio
async def test_options_flow_healthy_audit_stamps_cooldown_only(fake_api, hass):
    wire_healthy_audit(fake_api)
    entry = await add_real_entry(hass, {"route_gtfs_id": "2-FKN"})
    create_fixable_issue(hass, entry)
    issue_before = issue_for(hass)
    names = (CONF_ROUTE_NAME, CONF_DIRECTION_NAME, CONF_STOP_NAME)
    names_before = {key: entry.data[key] for key in names}
    writes, reloads = instrument_entry(hass)

    result = await start_options_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "healthy"
    assert result["description_placeholders"]["old_route_id"] == "6"
    assert result["description_placeholders"]["new_route_id"] == "6"
    # One bounded write: the cooldown stamp (GTFS was already stored).
    assert len(writes) == 1
    assert writes[0]["diag_next_ok_at"]
    assert writes[0]["route_gtfs_id"] == "2-FKN"
    assert {key: entry.data[key] for key in names} == names_before
    assert reloads == []
    # A healthy audit must never delete an existing issue; only a poll does.
    assert issue_for(hass) is issue_before
    # No departures request: the audit is independent of polling.
    assert DEPARTURES_PATH not in fake_api.requests

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.asyncio
async def test_options_flow_changed_direction_applies_verified_ids(fake_api, hass):
    wire_healthy_audit(fake_api)
    entry = await add_real_entry(
        hass, {CONF_DIRECTION: STALE_DIRECTION, "route_gtfs_id": "2-FKN"}
    )
    names = (CONF_ROUTE_NAME, CONF_DIRECTION_NAME, CONF_STOP_NAME)
    names_before = {key: entry.data[key] for key in names}
    writes, reloads = instrument_entry(hass)

    result = await start_options_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm_apply"
    assert result["description_placeholders"]["old_direction_id"] == "17"
    assert result["description_placeholders"]["new_direction_id"] == "5"
    # The audit stamped the cooldown and raised the fixable issue.
    assert len(writes) == 1
    issue = issue_for(hass)
    assert issue is not None and issue.is_fixable is True

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "applied"
    assert entry.data[CONF_ROUTE] == "6"
    assert entry.data[CONF_DIRECTION] == "5"
    assert entry.data[CONF_STOP] == "1071"
    assert entry.data["route_gtfs_id"] == "2-FKN"
    assert entry.data["diag_next_ok_at"]
    assert {key: entry.data[key] for key in names} == names_before
    # One audit write + one apply write, both bounded.
    assert len(writes) == 2
    assert reloads == [ENTRY_ID]
    # The options flow leaves issue cleanup to the next healthy poll.
    assert issue_for(hass) is not None

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.asyncio
async def test_options_flow_abstain_reports_manual_and_nonfixable_issue(
    fake_api, hass
):
    # Route record missing and no unique route-list match: abstain.
    fake_api.add(ROUTES_PATH, {"routes": []})
    entry = await add_real_entry(hass, {"route_gtfs_id": "2-FKN"})
    writes, reloads = instrument_entry(hass)

    result = await start_options_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"
    assert result["description_placeholders"]["remedy"]
    issue = issue_for(hass)
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.translation_key == MANUAL_TRANSLATION_KEY
    assert len(writes) == 1
    assert writes[0]["diag_next_ok_at"]
    assert reloads == []

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.asyncio
async def test_options_flow_cooldown_reports_without_probing(fake_api, hass):
    until = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=1)
    ).isoformat()
    entry = await add_real_entry(hass, {"diag_next_ok_at": until})
    writes, reloads = instrument_entry(hass)
    before = len(fake_api.requests)

    result = await start_options_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "cooldown"
    assert len(fake_api.requests) == before
    assert writes == []
    assert reloads == []
    assert issue_for(hass) is None

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.asyncio
async def test_options_flow_aborts_when_entry_not_loaded(fake_api, hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data=dict(BASE_ENTRY_DATA), entry_id=ENTRY_ID
    )
    entry.add_to_hass(hass)  # stays NOT_LOADED
    before = len(fake_api.requests)
    writes, reloads = instrument_entry(hass)

    result = await start_options_flow(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"
    assert len(fake_api.requests) == before
    assert writes == []
    assert reloads == []


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


def test_stale_ids_repair_translations_are_mirrored():
    """strings.json and translations/en.json must carry the same issue text."""
    component = ROOT / "custom_components" / "public_transport_victoria"
    strings = json.loads((component / "strings.json").read_text())
    english = json.loads(
        (component / "translations" / "en.json").read_text()
    )

    assert strings["issues"] == english["issues"]

    fixable = strings["issues"]["stale_ptv_ids"]
    fix_text = fixable["title"] + fixable["description"]
    confirm = fixable["fix_flow"]["step"]["confirm"]
    fix_text += confirm["title"] + confirm["description"]
    for placeholder in (
        "route_name",
        "old_route_id",
        "new_route_id",
        "old_direction_id",
        "new_direction_id",
        "old_stop_id",
        "new_stop_id",
        "route_gtfs_id",
        "detail",
    ):
        assert "{" + placeholder + "}" in fix_text
    assert fixable["fix_flow"]["error"]["verification_failed"]
    assert set(fixable["fix_flow"]["abort"]) == {
        "entry_not_found",
        "entry_not_loaded",
    }

    manual = strings["issues"]["stale_ptv_ids_manual"]
    assert "fix_flow" not in manual
    manual_text = manual["title"] + manual["description"]
    for placeholder in (
        "entry_title",
        "what",
        "detail",
        "configured",
        "candidate",
        "remedy",
    ):
        assert "{" + placeholder + "}" in manual_text

    assert strings["options"] == english["options"]
    options = strings["options"]["step"]
    assert set(options) >= {
        "init",
        "confirm_apply",
        "healthy",
        "applied",
        "manual",
        "cooldown",
    }
    assert options["init"]["menu_options"]["check_ids"]
    confirm = options["confirm_apply"]["title"] + options["confirm_apply"]["description"]
    for placeholder in (
        "route_name",
        "old_route_id",
        "new_route_id",
        "old_direction_id",
        "new_direction_id",
        "old_stop_id",
        "new_stop_id",
        "route_gtfs_id",
        "detail",
    ):
        assert "{" + placeholder + "}" in confirm
    assert set(strings["options"]["abort"]) == {
        "entry_not_found",
        "entry_not_loaded",
    }
