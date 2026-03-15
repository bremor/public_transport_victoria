"""Constants for the Public Transport Victoria integration."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

CONF_DIRECTION = "direction"
CONF_DIRECTION_NAME = "direction_name"
CONF_ROUTE = "route"
CONF_ROUTE_NAME = "route_name"
CONF_ROUTE_TYPE = "route_type"
CONF_ROUTE_TYPE_NAME = "route_type_name"
CONF_STOP = "stop"
CONF_STOP_NAME = "stop_name"
DOMAIN = "public_transport_victoria"
ATTRIBUTION = "Licensed from Public Transport Victoria under a Creative Commons Attribution 4.0 International Licence"
DEFAULT_DETAILS_LIMIT = 1

ROUTE_TYPE_ICONS: dict[int, str] = {
    0: "mdi:train",
    1: "mdi:tram",
    2: "mdi:bus",
    3: "mdi:van-passenger",
    4: "mdi:ferry",
}
DEFAULT_ICON = "mdi:transit-connection"


def get_device_info(connector) -> DeviceInfo:
    """Return shared device info for all entities of a configured stop."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{connector.route}-{connector.direction}-{connector.stop}")},
        name=f"{connector.route_name} line {connector.stop_name} to {connector.direction_name}",
        manufacturer="Public Transport Victoria",
        model=f"{connector.stop_name} to {connector.direction_name}",
    )
