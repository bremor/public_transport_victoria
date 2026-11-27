"""Typed connector error tests introduced by P1.3.

The four API methods the config flow calls (route types, routes, directions,
stops) now raise ``PTVAuthError``, ``PTVConnectionError`` or ``PTVServiceError``
instead of returning ``None``/``{}``.  ``aiohttp`` is replaced by a tiny fake,
so there is no network and no credentials.
"""

import asyncio
import sys
import types
from pathlib import Path

import aiohttp
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from custom_components.public_transport_victoria.PublicTransportVictoria import (  # noqa: E402
    public_transport_victoria as connector_module,
)

Connector = connector_module.Connector

CANARY_DEVID = "1234567"
CANARY_API_KEY = "CANARY-not-a-real-key-0001"


class FakeResponse:
    """Response usable both with ``await`` and ``async with``."""

    def __init__(self, status, payload=None, json_error=None):
        self.status = status
        self._payload = payload
        self._json_error = json_error

    async def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload

    def __await__(self):
        async def _resolve():
            return self

        return _resolve().__await__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    def __init__(self, api):
        self._api = api

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def get(self, url, **kwargs):
        return self._api.get(url)


class FakeAPI:
    """Return one fixed response, or raise one fixed exception, per request."""

    def __init__(self, response):
        self.response = response
        self.requests = []
        self.namespace = types.SimpleNamespace(
            ClientSession=self._session, ClientTimeout=lambda **kwargs: None
        )

    def _session(self, **kwargs):
        return FakeSession(self)

    def get(self, url):
        self.requests.append(url)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


@pytest.fixture
def install_api(monkeypatch):
    """Install a fake ``aiohttp`` module returning ``response``."""

    def _install(response):
        api = FakeAPI(response)
        monkeypatch.setattr(connector_module, "aiohttp", api.namespace)
        return api

    return _install


def make_connector(**overrides):
    kwargs = dict(
        hass=types.SimpleNamespace(config=types.SimpleNamespace(time_zone="UTC")),
        id=CANARY_DEVID,
        api_key=CANARY_API_KEY,
        route_type=0,
        route=6,
        direction=5,
        stop=1071,
    )
    kwargs.update(overrides)
    return Connector(**kwargs)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_route_types_auth_status_raises_auth_error(install_api, status):
    install_api(FakeResponse(status, {}))
    connector = make_connector()

    with pytest.raises(connector_module.PTVAuthError):
        await connector.async_route_types()


@pytest.mark.asyncio
async def test_directions_auth_status_raises_auth_error(install_api):
    install_api(FakeResponse(403, {}))
    connector = make_connector()

    with pytest.raises(connector_module.PTVAuthError):
        await connector.async_directions(6)


# ---------------------------------------------------------------------------
# Connectivity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_types_client_error_raises_connection_error(install_api):
    install_api(aiohttp.ClientError("boom"))
    connector = make_connector()

    with pytest.raises(connector_module.PTVConnectionError):
        await connector.async_route_types()


@pytest.mark.asyncio
async def test_routes_timeout_raises_connection_error(install_api):
    install_api(asyncio.TimeoutError())
    connector = make_connector()

    with pytest.raises(connector_module.PTVConnectionError):
        await connector.async_routes(0)


@pytest.mark.asyncio
async def test_stops_server_disconnect_raises_connection_error(install_api):
    install_api(aiohttp.ServerDisconnectedError())
    connector = make_connector()

    with pytest.raises(connector_module.PTVConnectionError):
        await connector.async_stops(6)


# ---------------------------------------------------------------------------
# Service / payload problems
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 429, 500, 502, 503])
async def test_route_types_non_auth_error_status_raises_service_error(
    install_api, status
):
    install_api(FakeResponse(status, {}))
    connector = make_connector()

    with pytest.raises(connector_module.PTVServiceError):
        await connector.async_route_types()


@pytest.mark.asyncio
async def test_routes_500_raises_service_error(install_api):
    install_api(FakeResponse(500, {}))
    connector = make_connector()

    with pytest.raises(connector_module.PTVServiceError):
        await connector.async_routes(0)


@pytest.mark.asyncio
async def test_route_types_missing_key_raises_service_error(install_api):
    install_api(FakeResponse(200, {"unexpected": []}))
    connector = make_connector()

    with pytest.raises(connector_module.PTVServiceError):
        await connector.async_route_types()


@pytest.mark.asyncio
async def test_directions_payload_not_an_object_raises_service_error(install_api):
    install_api(FakeResponse(200, []))
    connector = make_connector()

    with pytest.raises(connector_module.PTVServiceError):
        await connector.async_directions(6)


@pytest.mark.asyncio
async def test_stops_unreadable_json_raises_service_error(install_api):
    install_api(FakeResponse(200, json_error=ValueError("bad json")))
    connector = make_connector()

    with pytest.raises(connector_module.PTVServiceError):
        await connector.async_stops(6)


# ---------------------------------------------------------------------------
# Empty (but valid) responses are still returned as empty mappings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_route_types_returns_empty_mapping(install_api):
    install_api(FakeResponse(200, {"route_types": []}))
    connector = make_connector()

    assert await connector.async_route_types() == {}


@pytest.mark.asyncio
async def test_empty_routes_returns_empty_mapping(install_api):
    install_api(FakeResponse(200, {"routes": []}))
    connector = make_connector(route_type=None)

    assert await connector.async_routes(0) == {}
    assert connector.route_type == 0


@pytest.mark.asyncio
async def test_empty_directions_and_stops_return_empty_mappings(install_api):
    install_api(FakeResponse(200, {"directions": []}))
    connector = make_connector()
    assert await connector.async_directions(6) == {}

    install_api(FakeResponse(200, {"stops": []}))
    assert await connector.async_stops(6) == {}


def test_typed_errors_share_a_base_class():
    assert issubclass(connector_module.PTVAuthError, connector_module.PTVApiError)
    assert issubclass(
        connector_module.PTVConnectionError, connector_module.PTVApiError
    )
    assert issubclass(connector_module.PTVServiceError, connector_module.PTVApiError)
