"""Base entity classes shared across sensor and binary_sensor platforms."""
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

# Human-readable slot names — numeric prefixes keep entities sorted correctly in HA
DEPARTURE_NAMES = [
    "1st departure",
    "2nd departure",
    "3rd departure",
    "4th departure",
    "5th departure",
]


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
        # When no route filter is set, the device is just the stop.
        # When a route is configured, show "Route · Stop" for clarity.
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


class PtvDepartureEntity(PtvEntity):
    """Base class for per-departure-slot entities (slots 0–4)."""

    def __init__(self, coordinator, config_entry, slot: int):
        super().__init__(coordinator, config_entry)
        self._slot = slot

    @property
    def _departure(self) -> dict | None:
        """Return the departure dict for this slot, or None if unavailable."""
        data = self.coordinator.data
        if data and len(data) > self._slot:
            return data[self._slot]
        return None

    @property
    def available(self) -> bool:
        return self._departure is not None
