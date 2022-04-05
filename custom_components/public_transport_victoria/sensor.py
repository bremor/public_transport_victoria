"""Platform for sensor integration."""
import datetime
import logging

from homeassistant.helpers.entity import Entity
from .const import (
    ATTRIBUTION, DOMAIN,
)
from homeassistant.const import ATTR_ATTRIBUTION

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = datetime.timedelta(minutes=10)

async def async_setup_entry(hass, config_entry, async_add_devices):
    """Add sensors for passed config_entry in HA."""
    connector = hass.data[DOMAIN][config_entry.entry_id]

    new_devices = []
    for n in range(5):
        new_devices.append(Sensor(connector, n))
    if new_devices:
        async_add_devices(new_devices)


class Sensor(Entity):
    """Representation of a Public Transport Victoria Sensor."""

    def __init__(self, connector, number):
        """Initialize the sensor."""
        self._connector = connector
        self._number = number

    # The value of this sensor.
    @property
    def state(self):
        """Return the state of the sensor."""
        return self._connector.departures[self._number]["departure"]

    # The name of this entity, as displayed in the entity UI.
    @property
    def name(self):
        """Return the name of the sensor."""
        return "{} line to {} from {} {}".format(
            self._connector.route_name,
            self._connector.direction_name,
            self._connector.stop_name,
            self._number
        )

    # A unique_id for this entity with in this domain.
    @property
    def unique_id(self):
        """Return Unique ID string."""
        return "{} line to {} from {} {}".format(
            self._connector.route_name,
            self._connector.direction_name,
            self._connector.stop_name,
            self._number
        )

    @property
    def extra_state_attributes(self):
        """Return the state attributes of the sensor."""
        attr = self._connector.departures[self._number]
        attr[ATTR_ATTRIBUTION] = ATTRIBUTION
        return attr

    async def async_update(self):
        """Return the state attributes of the device."""
        _LOGGER.debug("Update has been called")
        await self._connector.async_update()

