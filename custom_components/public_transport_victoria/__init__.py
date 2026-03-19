"""Public Transport Victoria integration."""
import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_ID
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DIRECTION, CONF_DIRECTION_NAME, CONF_FILTER_EXPRESS,
    CONF_ROUTE, CONF_ROUTE_NAME, CONF_ROUTE_TYPE, CONF_ROUTE_TYPE_NAME,
    CONF_STOP, CONF_STOP_NAME, DOMAIN,
)
from .coordinator import PtvDataUpdateCoordinator
from .PublicTransportVictoria.public_transport_victoria import Connector

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor", "device_tracker"]


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Public Transport Victoria component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Public Transport Victoria from a config entry."""
    connector = Connector(
        hass,
        entry.data[CONF_ID],
        entry.data[CONF_API_KEY],
        entry.data.get(CONF_ROUTE_TYPE),
        entry.data.get(CONF_ROUTE),
        entry.data.get(CONF_DIRECTION),
        entry.data[CONF_STOP],
        entry.data.get(CONF_ROUTE_TYPE_NAME),
        entry.data.get(CONF_ROUTE_NAME),
        entry.data.get(CONF_DIRECTION_NAME),
        entry.data[CONF_STOP_NAME],
        entry.data.get(CONF_FILTER_EXPRESS, False),
    )
    # _init() calls async_update() to pre-populate connector.departures
    await connector._init()

    coordinator = PtvDataUpdateCoordinator(hass, connector)
    # First refresh re-uses data already fetched by _init() (throttle prevents double call)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "connector": connector,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, component)
                for component in PLATFORMS
            ]
        )
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
