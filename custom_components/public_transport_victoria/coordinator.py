"""DataUpdateCoordinator for Public Transport Victoria."""
import datetime
import logging

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = datetime.timedelta(minutes=10)


class PtvDataUpdateCoordinator(DataUpdateCoordinator):
    """Manages polling the PTV API and distributing data to all entities."""

    def __init__(self, hass, connector):
        self.connector = connector
        super().__init__(
            hass,
            _LOGGER,
            name="Public Transport Victoria",
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self):
        """Fetch departures from the PTV API via the connector."""
        _LOGGER.debug("Fetching new data from Public Transport Victoria API.")
        await self.connector.async_update()
        return self.connector.departures
