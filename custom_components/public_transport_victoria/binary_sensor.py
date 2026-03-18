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


_SEVERITY_ORDER = {"severe": 3, "moderate": 2, "minor": 1}


class RouteDisruptedBinarySensor(PtvEntity, BinarySensorEntity):
    """On when there are active disruptions on this route.

    Device-level sensor — one per configured route/stop entry.
    Disruption details (title, description, type, severity, url) are resolved
    from the PTV disruptions API and exposed as attributes.
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
        return bool(self._connector.disruptions)

    @property
    def extra_state_attributes(self) -> dict:
        disruptions = self._connector.disruptions
        if not disruptions:
            return {}

        # Determine the worst severity across all active disruptions
        worst = max(
            (_SEVERITY_ORDER.get(d.get("severity", "minor"), 1) for d in disruptions),
            default=0,
        )
        severity_label = {3: "severe", 2: "moderate", 1: "minor"}.get(worst, "none")

        return {
            "disruption_count": len(disruptions),
            "most_severe": severity_label,
            "disruptions": [
                {
                    "title": d.get("title", ""),
                    "description": d.get("description", ""),
                    "type": d.get("disruption_type", ""),
                    "severity": d.get("severity", ""),
                    "url": d.get("url", ""),
                    "from": d.get("from_date"),
                    "to": d.get("to_date"),
                }
                for d in disruptions
            ],
        }
