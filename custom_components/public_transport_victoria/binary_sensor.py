"""Binary sensor platform for Public Transport Victoria."""
import logging

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory

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
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_realtime_{self._slot}"

    @property
    def name(self) -> str:
        return f"{self._device_label} {DEPARTURE_NAMES[self._slot]} real-time"

    @property
    def is_on(self) -> bool | None:
        dep = self._departure
        if dep is None:
            return None
        return bool(dep.get("is_realtime"))


class DepartureExpressBinarySensor(PtvDepartureEntity, BinarySensorEntity):
    """On when the service skips stops (express run)."""

    _attr_icon = "mdi:train-variant"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_express_{self._slot}"

    @property
    def name(self) -> str:
        return f"{self._device_label} {DEPARTURE_NAMES[self._slot]} express"

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
        return f"{self._device_label} disrupted"

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

        attrs: dict = {
            "disruption_count": len(disruptions),
            "most_severe": severity_label,
        }

        # Flatten each disruption into numbered attributes so the HA UI
        # shows them as simple key/value rows rather than a YAML blob.
        for i, d in enumerate(disruptions, start=1):
            prefix = f"disruption_{i}"
            attrs[f"{prefix}_title"] = d.get("title", "")
            attrs[f"{prefix}_type"] = d.get("disruption_type", "")
            attrs[f"{prefix}_severity"] = d.get("severity", "")
            attrs[f"{prefix}_from"] = d.get("from_date")
            attrs[f"{prefix}_to"] = d.get("to_date")
            attrs[f"{prefix}_url"] = d.get("url", "")

        return attrs
