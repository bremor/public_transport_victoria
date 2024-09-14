"""Public Transport Victoria integration."""
import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_ID
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DIRECTION, CONF_DIRECTION_NAME, CONF_ROUTE, CONF_ROUTE_NAME,
    CONF_ROUTE_TYPE, CONF_ROUTE_TYPE_NAME, CONF_STOP, CONF_STOP_NAME, 
    CONF_DESTINATION_STOP,CONF_DESTINATION_STOP_NAME,DOMAIN
)
from .PublicTransportVictoria.public_transport_victoria import Connector

PLATFORMS = ["sensor"]


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Public Transport Victoria component."""
    hass.data.setdefault(DOMAIN, {})

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Public Transport Victoria from a config entry."""
    connector = Connector(hass,
                          entry.data[CONF_ID],
                          entry.data[CONF_API_KEY],
                          entry.data[CONF_ROUTE_TYPE],
                          entry.data[CONF_ROUTE],
                          entry.data[CONF_DIRECTION],
                          entry.data[CONF_STOP],
                          entry.data[CONF_DESTINATION_STOP],
                          entry.data[CONF_ROUTE_TYPE_NAME],
                          entry.data[CONF_ROUTE_NAME],
                          entry.data[CONF_DIRECTION_NAME],
                          entry.data[CONF_STOP_NAME],
                          entry.data[CONF_DESTINATION_STOP_NAME],
    )
    await connector._init()

    hass.data[DOMAIN][entry.entry_id] = connector

    # This creates each HA object for each platform your device requires.
    # It's done by calling the `async_setup_entry` function in each platform module.
    for component in PLATFORMS:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(entry, component)
        )

    return True


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
