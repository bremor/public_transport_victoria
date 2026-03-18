"""Binary sensor platform for Public Transport Victoria."""
import logging

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .const import DOMAIN
from .entity import DEPARTURE_NAMES, PtvDepartureEntity, PtvEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up binary sensors for a config entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    entities = []
    for slot in range(5):
        entities.append(DepartureRealtimeBinarySensor(coordinator, config_entry, slot))
        entities.append(DepartureExpressBinarySensor(coordinator, config_entry, slot))

    entities.append(RouteDisruptedBinarySensor(coordinator, config_entry))

    async_add_entities(entities)


class DepartureRealtimeBinarySensor(PtvDepartureEntity, BinarySensorEntity):
    """On when the departure time shown is real-time (not just the timetable)."""

    _attr_icon = "mdi:satellite-uplink"

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_realtime_{self._slot}"

    @property
    def name(self) -> str:
        return f"{self._connector.route_name} · {self._connector.stop_name} {DEPARTURE_NAMES[self._slot]} real-time"

    @property
    def is_on(self) -> bool | None:
        dep = self._departure
        if dep is None:
            return None
        return bool(dep.get("is_realtime"))


class DepartureExpressBinarySensor(PtvDepartureEntity, BinarySensorEntity):
    """On when the service skips stops (express run)."""

    _attr_icon = "mdi:train-variant"

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_express_{self._slot}"

    @property
    def name(self) -> str:
        return f"{self._connector.route_name} · {self._connector.stop_name} {DEPARTURE_NAMES[self._slot]} express"

    @property
    def is_on(self) -> bool | None:
        dep = self._departure
        if dep is None:
            return None
        return bool(dep.get("is_express"))


class RouteDisruptedBinarySensor(PtvEntity, BinarySensorEntity):
    """On when any current departure on this route has active disruptions.

    This is a device-level sensor — one per configured route/stop entry.
    Will be enhanced to resolve disruption IDs to titles/descriptions in a
    follow-up (see backlog: 'Resolve disruption IDs').
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:alert-circle-outline"

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_disrupted"

    @property
    def name(self) -> str:
        return f"{self._connector.route_name} · {self._connector.stop_name} disrupted"

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        if not data:
            return False
        return any(dep.get("disruption_ids") for dep in data)

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        if not data:
            return {}
        # Collect the unique disruption IDs across all current departures
        ids = set()
        for dep in data:
            for d_id in (dep.get("disruption_ids") or []):
                ids.add(d_id)
        return {"disruption_ids": sorted(ids)} if ids else {}
