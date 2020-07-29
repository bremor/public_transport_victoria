"""Platform for sensor integration."""
import asyncio
import homeassistant.helpers.config_validation as cv
import voluptuous as vol

from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import (
    ATTR_ATTRIBUTION, CONF_ID, CONF_API_KEY, TEMP_CELSIUS)
from homeassistant.helpers.entity import Entity
from .PyPublicTransportVictoria.pypublictransportvictoria import PyPublicTransportVictoria

ATTR_DIRECTION_NAME = "direction_name"
ATTR_ESTIMATED_DEPARTURE_UTC = "estimated_departure_utc"
ATTR_PLATFORM_NUMBER = "platform_number"
ATTR_ROUTE_NAME = "route_name"
ATTR_SCHEDULED_DEPARTURE_UTC = "scheduled_departure_utc"
ATTR_STOP_NAME = "stop_name"

CONF_ATTRIBUTION = "Data provided by ptv.vic.gov.au"
CONF_DIRECTION_ID = "direction_id"
CONF_MAX_RESULTS = "max_results"
CONF_ROUTE_TYPE = "route_type"
CONF_STOP_ID = "stop_id"

DEFAULT_MAX_RESULTS = 1
DEFAULT_NAME = "Public Tranpsort Victoria"
DEFAULT_ROUTE_TYPE = 0

# Validation of the user's configuration
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_ID): cv.string,
    vol.Required(CONF_API_KEY): cv.string,
    vol.Required(CONF_STOP_ID): cv.positive_int,
    vol.Optional(CONF_MAX_RESULTS, default=DEFAULT_MAX_RESULTS): cv.positive_int,
    vol.Optional(CONF_ROUTE_TYPE, default=DEFAULT_ROUTE_TYPE): cv.positive_int,
    vol.Optional(CONF_DIRECTION_ID): cv.positive_int,
})

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the sensor platform."""

    direction_id = config.get(CONF_DIRECTION_ID)
    id = config.get(CONF_ID)
    key = config.get(CONF_API_KEY)
    max_results = config.get(CONF_MAX_RESULTS)
    route_type = config.get(CONF_ROUTE_TYPE)
    stop_id = config.get(CONF_STOP_ID)

    ptv_api = PyPublicTransportVictoria(hass, direction_id, id, key, max_results, route_type, stop_id)
    
    for index in range(0, max_results):
        async_add_entities([PublicTransportVictoriaSensor(ptv_api, index)])

class PublicTransportVictoriaSensor(Entity):
    """Representation of a Sensor."""

    def __init__(self, ptv_api, index):
        """Initialize the sensor."""
        self._index = index
        self._ptv_api = ptv_api
        self._state = None

    @property
    def name(self):
        """Return the name of the sensor."""
        return 'Example Temperature'

    @property
    def device_state_attributes(self):
        """Return device specific state attributes."""
        attr = {
            ATTR_ATTRIBUTION: CONF_ATTRIBUTION,
            ATTR_DIRECTION_NAME: self._ptv_api.get_direction_name(self._index),
            ATTR_ESTIMATED_DEPARTURE_UTC: self._ptv_api.get_estimated_departure_utc(self._index),
            ATTR_PLATFORM_NUMBER: self._ptv_api.get_platform_number(self._index),
            ATTR_ROUTE_NAME: self._ptv_api.get_route_name(self._index),
            ATTR_SCHEDULED_DEPARTURE_UTC: self._ptv_api.get_scheduled_departure_utc(self._index),
            ATTR_STOP_NAME: self._ptv_api.get_stop_name(),
        }

        return attr

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._ptv_api.get_state(self._index)

    async def async_update(self):
        """Fetch new state data for the sensor.

        This is the only method that should fetch new data for Home Assistant.
        """
        # https://github.com/home-assistant/core/blob/9a40d5b7edd1687d1258b520e78ee94ee148f717/homeassistant/components/thethingsnetwork/sensor.py
        await self._ptv_api.async_get_data()