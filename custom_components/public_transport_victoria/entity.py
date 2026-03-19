"""Base entity classes shared across sensor, binary_sensor and device_tracker."""
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class PtvEntity(CoordinatorEntity):
    """Base class for all PTV entities — provides shared DeviceInfo."""

    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._connector = coordinator.connector

    @property
    def _device_label(self) -> str:
        """Short label used as the entity name prefix.

        Returns "Route · Stop" when a route filter is configured, or just
        "Stop" in stop-only mode (no route filter set).
        """
        if self._connector.route_name:
            return f"{self._connector.route_name} · {self._connector.stop_name}"
        return self._connector.stop_name

    @property
    def device_info(self) -> DeviceInfo:
        if self._connector.route_name:
            device_name = f"{self._connector.route_name} · {self._connector.stop_name}"
        else:
            device_name = self._connector.stop_name

        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=device_name,
            manufacturer="Public Transport Victoria",
            model=self._connector.route_type_name or self._connector.route_type,
        )
