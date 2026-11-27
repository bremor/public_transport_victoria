"""Known-vector tests for the connector's PTV URL signing (P1.1 acceptance).

These run against the real ``homeassistant`` package that
``pytest-homeassistant-custom-component`` pins (see ``requirements_test.txt``),
so CI installs exactly what is exercised here.  The connector still imports the
deprecated ``homeassistant.util.Throttle`` decorator; it is replaced with a
passthrough *only* for the duration of the import so tests can call
``async_update`` more than once.  Nothing here touches the network and no real
credential is used.
"""

import hashlib
import hmac
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Fixed canaries for the known-vector assertions.  These are not credentials.
CANARY_DEVID = "1234567"
CANARY_API_KEY = "CANARY-not-a-real-key-0001"
# Independently computed with hmac.new(sha1) over the signed raw string.
CANARY_ROUTE_TYPES_SIGNATURE = "1ca1a261e7997586d4fd090ab7436cd1092afbd6"
CANARY_DEPARTURES_SIGNATURE = "aba9204e5f31782141bc79f9487171ec0b4872ad"
DEPARTURES_PATH = (
    "/v3/departures/route_type/0/stop/1071/route/6"
    "?direction_id=5&max_results=5"
)


def _import_connector():
    """Import the component connector with a non-throttling Throttle shim."""
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


connector = _import_connector()
build_URL = connector.build_URL


def test_build_url_matches_independent_hmac_known_vector():
    """The URL signature equals an independently recomputed HMAC-SHA1."""
    url = build_URL(CANARY_DEVID, CANARY_API_KEY, "/v3/route_types")

    raw = "/v3/route_types?devid={}".format(CANARY_DEVID)
    expected = hmac.new(
        CANARY_API_KEY.encode("utf-8"), raw.encode("utf-8"), hashlib.sha1
    ).hexdigest()

    assert expected == CANARY_ROUTE_TYPES_SIGNATURE
    assert url == (
        "https://timetableapi.ptv.vic.gov.au" + raw + "&signature=" + expected
    )


def test_build_url_known_vector_with_existing_query_string():
    """A request that already has a query string is joined with ``&``."""
    url = build_URL(CANARY_DEVID, CANARY_API_KEY, DEPARTURES_PATH)

    raw = DEPARTURES_PATH + "&devid={}".format(CANARY_DEVID)
    expected = hmac.new(
        CANARY_API_KEY.encode("utf-8"), raw.encode("utf-8"), hashlib.sha1
    ).hexdigest()

    assert expected == CANARY_DEPARTURES_SIGNATURE
    assert url == (
        "https://timetableapi.ptv.vic.gov.au" + raw + "&signature=" + expected
    )


@pytest.mark.parametrize(
    "path", ["/v3/route_types", DEPARTURES_PATH, "/v3/runs/952567"]
)
def test_build_url_has_one_devid_and_one_trailing_signature(path):
    """devid and signature each appear exactly once, signature last."""
    url = build_URL(CANARY_DEVID, CANARY_API_KEY, path)

    assert url.count("devid=") == 1
    assert url.count("signature=") == 1
    assert url.startswith("https://timetableapi.ptv.vic.gov.au/")
    assert url.endswith(
        "&signature="
        + hmac.new(
            CANARY_API_KEY.encode("utf-8"),
            (path + ("&" if "?" in path else "?") + "devid=" + CANARY_DEVID).encode(
                "utf-8"
            ),
            hashlib.sha1,
        ).hexdigest()
    )


def test_build_url_does_not_leak_the_api_key():
    url = build_URL(CANARY_DEVID, CANARY_API_KEY, "/v3/route_types")
    assert CANARY_API_KEY not in url


def test_build_url_logs_only_the_path(caplog):
    """P1.1: build_URL must not log the signed URL, devid or signature."""
    import logging

    caplog.set_level(logging.DEBUG)
    build_URL(CANARY_DEVID, CANARY_API_KEY, DEPARTURES_PATH)

    assert CANARY_API_KEY not in caplog.text
    assert CANARY_DEVID not in caplog.text
    assert "signature=" not in caplog.text


def test_signatures_differ_between_paths():
    """Guards against a signature accidentally signed over a constant."""
    first = build_URL(CANARY_DEVID, CANARY_API_KEY, "/v3/route_types")
    second = build_URL(CANARY_DEVID, CANARY_API_KEY, "/v3/routes?route_types=0")
    assert first.rsplit("signature=", 1)[1] != second.rsplit("signature=", 1)[1]
