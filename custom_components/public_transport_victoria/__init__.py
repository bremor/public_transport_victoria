"""Public Transport Victoria integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_ID, Platform
from homeassistant.core import HomeAssistant

from .const import (
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
from .coordinator import PublicTransportVictoriaCoordinator
from .PublicTransportVictoria.public_transport_victoria import Connector

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Public Transport Victoria from a config entry."""
    connector = Connector(
        hass,
        entry.data[CONF_ID],
        entry.data[CONF_API_KEY],
        entry.data[CONF_ROUTE_TYPE],
        entry.data[CONF_ROUTE],
        entry.data[CONF_DIRECTION],
        entry.data[CONF_STOP],
        entry.data[CONF_ROUTE_TYPE_NAME],
        entry.data[CONF_ROUTE_NAME],
        entry.data[CONF_DIRECTION_NAME],
        entry.data[CONF_STOP_NAME],
    )
    await connector._init()

    coordinator = PublicTransportVictoriaCoordinator(hass, connector)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "connector": connector,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
