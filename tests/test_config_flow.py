"""Config-flow regression tests for issue #40 (integer vs string route types).

The config flow is exercised through the real Home Assistant test harness
(``pytest-homeassistant-custom-component``), with the connector's HTTP methods
replaced by stubs so no credentials or network are needed.

The HA harness's ``hass`` fixture is an async fixture that relies on
pytest-asyncio auto mode.  This repository has no ``pytest.ini`` (P1.2 is not
allowed to add one), so run these with ``--asyncio-mode=auto`` -- the test
workflow does.  Without the flag the module skips rather than erroring, so a
plain ``pytest tests`` stays green.

Two shapes of test are present:

* "documents current bug" tests assert the *current* failure (a schema built
  from string keys rejects the integer ``0`` default), and
* a strict ``xfail`` asserts the behaviour P1.3 will deliver (integer and
  string selections are both accepted).  When P1.3 lands, that test will
  xpass and the marker should be removed.
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_ID
from homeassistant.data_entry_flow import FlowResultType

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

# The HA harness normally sets asyncio_mode=auto via its own pytest config;
# this repo has no pytest.ini, so every test here is explicitly async and the
# module only runs when the CLI enables auto mode.
_argv = sys.argv
_ASYNCIO_AUTO = (
    any(arg == "--asyncio-mode=auto" for arg in _argv)
    or any(
        arg == "--asyncio-mode"
        and index + 1 < len(_argv)
        and _argv[index + 1] == "auto"
        for index, arg in enumerate(_argv)
    )
    or "asyncio-mode=auto" in os.environ.get("PYTEST_ADDOPTS", "")
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _ASYNCIO_AUTO,
        reason="needs pytest --asyncio-mode=auto (see .github/workflows/tests.yaml)",
    ),
]


@pytest.fixture(autouse=True)
def _enable_custom_integration(enable_custom_integrations):
    """Make the repo's custom_components tree discoverable by HA."""
    yield


@pytest.fixture
def connector_stubs(monkeypatch):
    """Replace every connector HTTP call the flow makes."""
    stub = AsyncMock(return_value=dict(ROUTE_TYPES))
    monkeypatch.setattr(Connector, "async_route_types", stub)
    monkeypatch.setattr(
        Connector, "async_routes", AsyncMock(return_value=dict(ROUTES))
    )
    monkeypatch.setattr(
        Connector, "async_directions", AsyncMock(return_value=dict(DIRECTIONS))
    )
    monkeypatch.setattr(Connector, "async_stops", AsyncMock(return_value=dict(STOPS)))
    # Creating the entry triggers real setup; keep it to the config-flow layer.
    monkeypatch.setattr(integration, "async_setup_entry", AsyncMock(return_value=True))
    monkeypatch.setattr(
        integration, "async_unload_entry", AsyncMock(return_value=True)
    )
    return stub


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


async def test_route_types_step_default_is_integer_zero(hass, connector_stubs):
    """Documents current bug (#40): the schema default is the integer 0."""
    result = await _start_flow(hass)
    schema = result["data_schema"]

    (key,) = schema.schema

    assert key.default() == 0
    assert isinstance(key.default(), int)


async def test_integer_zero_fails_string_route_type_schema_documents_bug(
    hass, connector_stubs
):
    """Documents current bug (#40): integer 0 is not a member of vol.In.

    The frontend submits the integer default and voluptuous replies with the
    reported ``value must be one of ['0', '1', '2', '3', '4']`` message.
    """
    result = await _start_flow(hass)
    schema = result["data_schema"]

    with pytest.raises(vol.Invalid) as excinfo:
        schema({CONF_ROUTE_TYPE: 0})

    message = str(excinfo.value)
    assert "value must be one of" in message
    for value in ROUTE_TYPES:
        assert "'{}'".format(value) in message


@pytest.mark.xfail(
    strict=True,
    reason="issue #40: the default route-type value is int 0 but vol.In keys "
    "are strings; P1.3 makes both accepted",
)
async def test_integer_zero_selection_is_accepted(hass, connector_stubs):
    """Target behaviour for P1.3: the integer default validates."""
    result = await _start_flow(hass)
    schema = result["data_schema"]

    assert schema({CONF_ROUTE_TYPE: 0})


async def test_string_route_type_passes_and_advances_to_routes(
    hass, connector_stubs
):
    result = await _start_flow(hass)
    schema = result["data_schema"]

    assert schema({CONF_ROUTE_TYPE: "0"})

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ROUTE_TYPE: "0"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "routes"


async def test_full_train_flow_creates_entry_with_string_values(
    hass, connector_stubs
):
    """A complete string-valued Train flow creates a config entry."""
    result = await _start_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ROUTE_TYPE: "0"}
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
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STOP: "1071"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Frankston line to Frankston from Flinders Street Station"
    assert result["data"] == {
        CONF_ID: CREDENTIALS[CONF_ID],
        CONF_API_KEY: CREDENTIALS[CONF_API_KEY],
        CONF_ROUTE_TYPE: "0",
        CONF_ROUTE_TYPE_NAME: "Train",
        CONF_ROUTE: "6",
        CONF_ROUTE_NAME: "Frankston",
        CONF_DIRECTION: "5",
        CONF_DIRECTION_NAME: "Frankston",
        CONF_STOP: "1071",
        CONF_STOP_NAME: "Flinders Street Station",
    }
