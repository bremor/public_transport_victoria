"""Tests for the PTV logging redaction helpers.

These tests deliberately load the redact module by file path: the component
package imports ``homeassistant`` and ``aiohttp``, which are not installed in
this environment, so a normal import would drag those in.  ``redact.py`` is
standard-library only by design.
"""

import importlib.util
from pathlib import Path

import pytest

REDACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components/public_transport_victoria/PublicTransportVictoria/redact.py"
)


def _load_redact():
    spec = importlib.util.spec_from_file_location("ptv_redact", REDACT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


redact = _load_redact()


def test_redact_module_is_importable_by_path():
    assert hasattr(redact, "sanitise_request")
    assert hasattr(redact, "redact_config")


def test_sanitise_request_strips_query_string():
    request = (
        "/v3/departures/route_type/0/stop/1/route/2"
        "?direction_id=3&max_results=5"
    )
    assert (
        redact.sanitise_request(request)
        == "/v3/departures/route_type/0/stop/1/route/2"
    )


def test_sanitise_request_keeps_path_without_query():
    assert redact.sanitise_request("/v3/routes") == "/v3/routes"
    assert redact.sanitise_request("/v3/route_types") == "/v3/route_types"


def test_sanitise_request_handles_build_url_trailing_ampersand():
    # build_URL appends "&" or "?" to the request before signing it, so the
    # value handed to the logger may end with a bare separator.
    assert redact.sanitise_request("/v3/routes?route_types=0&") == "/v3/routes"
    assert redact.sanitise_request("/v3/route_types?") == "/v3/route_types"


@pytest.mark.parametrize(
    "path_with_query",
    [
        "/v3/departures/route_type/0/stop/1?direction_id=3&max_results=5&devid=1234&signature=abc123",
        "/v3/departures/route_type/0/stop/1?devid=1234&signature=abc123",
        "/v3/runs/42?devid=1234&signature=deadbeef",
        "/v3/route_types?devid=1234&signature=deadbeef",
    ],
)
def test_sanitise_request_never_leaks_query_parameters(path_with_query):
    sanitised = redact.sanitise_request(path_with_query)
    assert "=" not in sanitised
    assert "devid" not in sanitised
    assert "signature" not in sanitised


def test_sanitise_request_non_string_is_redacted():
    assert redact.sanitise_request(None) == "<redacted>"


def test_redact_config_hides_credential_keys():
    data = {
        "id": "1234",
        "api_key": "SECRETKEY123",
        "signature": "deadbeef",
        "devId": "5678",
        "API_KEY": "ANOTHERKEY",
    }
    result = redact.redact_config(data)

    for key in data:
        assert result[key].startswith("<redacted")
    assert result["api_key"] == "<redacted,len12>"
    assert result["id"] == "<redacted,len4>"


def test_redact_config_preserves_non_credential_keys():
    data = {
        "api_key": "SECRETKEY123",
        "id": "1234",
        "stop_name": "Flinders St",
        "route": "1",
    }
    result = redact.redact_config(data)

    assert result["stop_name"] == "Flinders St"
    assert result["route"] == "1"
    assert result["api_key"] != "SECRETKEY123"


def test_redact_config_does_not_mutate_original():
    data = {"api_key": "SECRETKEY123", "id": "1234", "stop_name": "Flinders St"}
    original = dict(data)

    redact.redact_config(data)

    assert data == original


def test_redact_config_redacts_nested_dicts():
    data = {"entry": {"api_key": "SECRETKEY123", "stop_name": "Flinders St"}}
    result = redact.redact_config(data)

    assert "SECRETKEY123" not in str(result)
    assert result["entry"]["stop_name"] == "Flinders St"


def test_redact_config_canary_never_leaks_secrets():
    canary = {"id": "P4SSW0RDID", "api_key": "SECRETKEY123", "stop_name": "Flinders St"}
    result = redact.redact_config(canary)

    rendered = str(result)
    assert "SECRETKEY" not in rendered
    assert "P4SSW0RDID" not in rendered
    assert "<redacted" in rendered
    assert result["stop_name"] == "Flinders St"


def test_redact_config_non_dict_returns_empty_mapping():
    assert redact.redact_config(None) == {}
    assert redact.redact_config("SECRETKEY123") == {}
