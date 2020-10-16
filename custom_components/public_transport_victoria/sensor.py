"""Platform for sensor integration."""
import logging

from homeassistant.helpers.entity import Entity
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# See cover.py for more details.
# Note how both entities for each roller sensor (battry and illuminance) are added at
# the same time to the same list. This way only a single async_add_devices call is
# required.
async def async_setup_entry(hass, config_entry, async_add_devices):
    """Add sensors for passed config_entry in HA."""
    connector = hass.data[DOMAIN][config_entry.entry_id]

    new_devices = []
    for n in range(5):
        new_devices.append(Sensor(connector, n))
    if new_devices:
        async_add_devices(new_devices)


# This base class shows the common properties and methods for a sensor as used in this
# example. See each sensor for further details about properties and methods that
# have been overridden.
class Sensor(Entity):
    """Representation of a Public Transport Victoria Sensor."""

    def __init__(self, connector, number):
        """Initialize the sensor."""
        self._connector = connector
        self._number = number

    # To link this entity to the cover device, this property must return an
    # identifiers value matching that used in the cover, but no other information such
    # as name. If name is returned, this entity will then also become a device in the
    # HA UI.
    #@property
    #def device_info(self):
    #    """Return information to link this entity with the correct device."""
    #    #return {"identifiers": {(DOMAIN, self._roller.roller_id)}}
    #    return {"identifiers": {(DOMAIN, "test_123")}}

    # This property is important to let HA know if this entity is online or not.
    # If an entity is offline (return False), the UI will refelect this.
#    @property
#    def available(self) -> bool:
#        """Return True if roller and hub is available."""
#        #return self._roller.online and self._roller.hub.online
#        return True

#    async def async_added_to_hass(self):
#        """Run when this Entity has been added to HA."""
#        # Sensors should also register callbacks to HA when their state changes
#        #self._roller.register_callback(self.async_write_ha_state)
#        pass

#    async def async_will_remove_from_hass(self):
#        """Entity being removed from hass."""
#        # The opposite of async_added_to_hass. Remove any registered call backs here.
#        #self._roller.remove_callback(self.async_write_ha_state)
#        pass

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
    def device_state_attributes(self):
        """Return the state attributes of the device."""
        return self._connector.departures[self._number]

    async def async_update(self):
        """Return the state attributes of the device."""
        await self._connector.async_update()

