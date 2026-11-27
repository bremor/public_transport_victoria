"""Config-flow regression tests for issue #40 and config-flow API failures.

The config flow is exercised through the real Home Assistant test harness
(``pytest-homeassistant-custom-component``), with the connector's HTTP methods
replaced by stubs so no credentials or network are needed.

The connector's option mappings are string-keyed (``"0": "Train"``), so the
flow must keep selection values string-typed end to end.  ``pytest.ini`` sets
``asyncio_mode = auto`` for the harness's async fixtures.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_ID
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
import custom_components.public_transport_victoria as integration  # noqa: E402
from custom_components.public_transport_victoria.PublicTransportVictoria.public_transport_victoria import (  # noqa: E402
    Connector,
    PTVAuthError,
    PTVConnectionError,
    PTVServiceError,
)

# The route_types payload the live API returns (string keys after conversion).
ROUTE_TYPES = {
    "0": "Train",
    "1": "Tram",
    "2": "Bus",
    "3": "Vline",
    "4": "Night Bus",
}
ROUTES = {"6": "Frankston"}
DIRECTIONS = {"5": "Frankston"}
STOPS = {"1071": "Flinders Street Station"}

CREDENTIALS = {CONF_ID: "1234567", CONF_API_KEY: "CANARY-not-a-real-key-0001"}
EXISTING_CREDENTIALS = {
    CONF_ID: "7654321",
    CONF_API_KEY: "CANARY-not-a-real-key-0002",
}

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _enable_custom_integration(enable_custom_integrations):
    """Make the repo's custom_components tree discoverable by HA."""
    yield


@pytest.fixture
def connector_stubs(monkeypatch):
    """Replace every connector HTTP call the flow makes."""
    stubs = SimpleNamespace(
        route_types=AsyncMock(return_value=dict(ROUTE_TYPES)),
        routes=AsyncMock(return_value=dict(ROUTES)),
        directions=AsyncMock(return_value=dict(DIRECTIONS)),
        stops=AsyncMock(return_value=dict(STOPS)),
    )
    monkeypatch.setattr(Connector, "async_route_types", stubs.route_types)
    monkeypatch.setattr(Connector, "async_routes", stubs.routes)
    monkeypatch.setattr(Connector, "async_directions", stubs.directions)
    monkeypatch.setattr(Connector, "async_stops", stubs.stops)
    # Creating the entry triggers real setup; keep it to the config-flow layer.
    monkeypatch.setattr(integration, "async_setup_entry", AsyncMock(return_value=True))
    monkeypatch.setattr(
        integration, "async_unload_entry", AsyncMock(return_value=True)
    )
    return stubs


async def _start_flow(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(CREDENTIALS)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "route_types"
    return result


async def _complete_flow(hass, result, route_type="0"):
    """Advance a flow from the route_types step to a created entry."""
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ROUTE_TYPE: route_type}
    )
    assert result["step_id"] == "routes"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ROUTE: "6"}
    )
    assert result["step_id"] == "directions"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DIRECTION: "5"}
    )
    assert result["step_id"] == "stops"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STOP: "1071"}
    )


# ---------------------------------------------------------------------------
# Issue #40: string-typed selections
# ---------------------------------------------------------------------------


async def test_route_types_step_default_is_a_valid_string_key(hass, connector_stubs):
    """The default must be one of the string keys, not the integer 0."""
    result = await _start_flow(hass)
    schema = result["data_schema"]

    (key,) = schema.schema

    assert key.default() == "0"
    assert isinstance(key.default(), str)
    # The bug was that the default was rejected by vol.In.
    assert schema({CONF_ROUTE_TYPE: key.default()})


async def test_route_type_schema_accepts_strings_and_coerces_integers(
    hass, connector_stubs
):
    """String IDs validate; a legacy integer submission is coerced, not rejected."""
    result = await _start_flow(hass)
    schema = result["data_schema"]

    assert schema({CONF_ROUTE_TYPE: "0"}) == {CONF_ROUTE_TYPE: "0"}
    assert schema({CONF_ROUTE_TYPE: 0}) == {CONF_ROUTE_TYPE: "0"}

    with pytest.raises(vol.Invalid):
        schema({CONF_ROUTE_TYPE: "9"})


async def test_integer_route_type_submission_advances_to_routes(
    hass, connector_stubs
):
    """Regression for #40: the historical integer default now completes."""
    result = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ROUTE_TYPE: 0}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "routes"
    connector_stubs.routes.assert_awaited_once_with("0")


@pytest.mark.parametrize(
    ("route_type", "route_type_name"),
    [("0", "Train"), ("1", "Tram"), ("2", "Bus")],
)
async def test_train_tram_bus_flows_create_string_valued_entries(
    hass, connector_stubs, route_type, route_type_name
):
    """A complete flow creates an entry whose stored IDs are all strings."""
    result = await _start_flow(hass)
    result = await _complete_flow(hass, result, route_type)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == (
        "Frankston line to Frankston from Flinders Street Station"
    )
    assert result["data"] == {
        CONF_ID: CREDENTIALS[CONF_ID],
        CONF_API_KEY: CREDENTIALS[CONF_API_KEY],
        CONF_ROUTE_TYPE: route_type,
        CONF_ROUTE_TYPE_NAME: route_type_name,
        CONF_ROUTE: "6",
        CONF_ROUTE_NAME: "Frankston",
        CONF_DIRECTION: "5",
        CONF_DIRECTION_NAME: "Frankston",
        CONF_STOP: "1071",
        CONF_STOP_NAME: "Flinders Street Station",
    }
    assert all(
        isinstance(value, str)
        for key, value in result["data"].items()
        if key != CONF_ID
    )


# ---------------------------------------------------------------------------
# API failures at the credentials step
# ---------------------------------------------------------------------------


async def test_empty_route_types_shows_no_options_error(hass, connector_stubs):
    """An empty (but successful) route-type response is a deliberate error."""
    connector_stubs.route_types.return_value = {}

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(CREDENTIALS)
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "no_options"}


@pytest.mark.parametrize(
    ("error", "expected_key"),
    [
        (PTVAuthError("bad credentials"), "invalid_auth"),
        (PTVConnectionError("offline"), "cannot_connect"),
        (PTVServiceError("bad response"), "service_error"),
    ],
)
async def test_route_types_api_failure_shows_typed_error(
    hass, connector_stubs, error, expected_key
):
    """Typed connector failures become deliberate form errors, not crashes."""
    connector_stubs.route_types.side_effect = error

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(CREDENTIALS)
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected_key}


# ---------------------------------------------------------------------------
# Existing-entry credential carry-over
# ---------------------------------------------------------------------------


async def test_existing_entry_credentials_are_reused(hass, connector_stubs):
    """The first existing entry's credentials are carried into the new entry."""
    MockConfigEntry(
        domain=DOMAIN, data=dict(EXISTING_CREDENTIALS), title="Existing"
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "route_types"
    connector_stubs.route_types.assert_awaited_once()

    result = await _complete_flow(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ID] == EXISTING_CREDENTIALS[CONF_ID]
    assert result["data"][CONF_API_KEY] == EXISTING_CREDENTIALS[CONF_API_KEY]


async def test_existing_entry_route_types_failure_shows_typed_error(
    hass, connector_stubs
):
    """A failure while loading stored-credential route types is user-readable."""
    MockConfigEntry(
        domain=DOMAIN, data=dict(EXISTING_CREDENTIALS), title="Existing"
    ).add_to_hass(hass)
    connector_stubs.route_types.side_effect = PTVAuthError("bad credentials")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


# ---------------------------------------------------------------------------
# Empty option sets at later steps
# ---------------------------------------------------------------------------


async def test_empty_routes_aborts_with_no_options(hass, connector_stubs):
    connector_stubs.routes.return_value = {}
    result = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ROUTE_TYPE: "0"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_options"


async def test_empty_directions_aborts_with_no_options(hass, connector_stubs):
    connector_stubs.directions.return_value = {}
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ROUTE_TYPE: "0"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ROUTE: "6"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_options"


async def test_empty_stops_aborts_with_no_options(hass, connector_stubs):
    connector_stubs.stops.return_value = {}
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ROUTE_TYPE: "0"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ROUTE: "6"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DIRECTION: "5"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_options"


async def test_route_step_auth_failure_redisplays_form_with_error(
    hass, connector_stubs
):
    """A typed failure at a later step re-shows that step with the error."""
    connector_stubs.routes.side_effect = PTVAuthError("bad credentials")
    result = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ROUTE_TYPE: "0"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "route_types"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_direction_step_connection_failure_redisplays_form_with_error(
    hass, connector_stubs
):
    connector_stubs.directions.side_effect = PTVConnectionError("offline")
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ROUTE_TYPE: "0"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ROUTE: "6"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "routes"
    assert result["errors"] == {"base": "cannot_connect"}
