"""Platform for sensor integration."""

import datetime
import logging

from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    CoordinatorEntity,
    UpdateFailed,
)
from .const import ATTRIBUTION, DOMAIN
from .PublicTransportVictoria.public_transport_victoria import PTVApiError

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = datetime.timedelta(minutes=10)

async def async_setup_entry(hass, config_entry, async_add_entities):
    """Add sensors for passed config_entry in HA."""
    connector = hass.data[DOMAIN][config_entry.entry_id]

    # Create the coordinator to manage polling
    coordinator = PublicTransportVictoriaDataUpdateCoordinator(hass, connector)

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    # Create sensors for the first 5 departures
    new_devices = [PublicTransportVictoriaSensor(coordinator, i) for i in range(5)]

    async_add_entities(new_devices)


class PublicTransportVictoriaDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Public Transport Victoria data."""

    def __init__(self, hass, connector):
        """Initialize the coordinator."""
        self.connector = connector
        super().__init__(
            hass,
            _LOGGER,
            name="Public Transport Victoria",
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self):
        """Fetch data from Public Transport Victoria."""
        _LOGGER.debug("Fetching new data from Public Transport Victoria API.")
        try:
            await self.connector.async_update()
        except PTVApiError as err:
            raise UpdateFailed(
                "Error fetching Public Transport Victoria data: {}".format(err)
            ) from err
        return self.connector.departures  # Return the latest data


class PublicTransportVictoriaSensor(CoordinatorEntity, Entity):
    """Representation of a Public Transport Victoria Sensor."""

    # Home Assistant renders this as the state attribution marker, so reading
    # the attributes never needs to mutate the coordinator's departure data.
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator, number):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._number = number
        self._connector = coordinator.connector

    @property
    def state(self):
        """Return the state of the sensor."""
        if len(self.coordinator.data) > self._number:
            return self.coordinator.data[self._number].get("departure", "No data")
        return "No data"

    @property
    def name(self):
        """Return the name of the sensor."""
        return "{} line to {} from {} {}".format(
            self._connector.route_name,
            self._connector.direction_name,
            self._connector.stop_name,
            self._number,
        )

    @property
    def unique_id(self):
        """Return Unique ID string."""
        return "{} line to {} from {} {}".format(
            self._connector.route_name,
            self._connector.direction_name,
            self._connector.stop_name,
            self._number,
        )

    @property
    def extra_state_attributes(self):
        """Return the state attributes of the sensor.

        A shallow copy keeps coordinator data immutable while preserving every
        raw departure field.  Attribution comes from ``_attr_attribution``.
        """
        if len(self.coordinator.data) > self._number:
            return dict(self.coordinator.data[self._number])
        return {}
