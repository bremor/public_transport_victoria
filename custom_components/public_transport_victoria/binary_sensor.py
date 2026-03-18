"""Binary sensor platform for Public Transport Victoria."""
import logging

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .const import DOMAIN
from .entity import PtvEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up binary sensors for a config entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    async_add_entities([RouteDisruptedBinarySensor(coordinator, config_entry)])


_SEVERITY_ORDER = {"severe": 3, "moderate": 2, "minor": 1}


class RouteDisruptedBinarySensor(PtvEntity, BinarySensorEntity):
    """On when there are active disruptions on this route.

    Device-level sensor — one per configured route/stop entry.
    Disruption details are exposed as numbered flat attributes
    (disruption_1_title, disruption_1_type, …).
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

        worst = max(
            (_SEVERITY_ORDER.get(d.get("severity", "minor"), 1) for d in disruptions),
            default=0,
        )
        severity_label = {3: "severe", 2: "moderate", 1: "minor"}.get(worst, "none")

        attrs: dict = {
            "disruption_count": len(disruptions),
            "most_severe": severity_label,
        }

        for i, d in enumerate(disruptions, start=1):
            prefix = f"disruption_{i}"
            attrs[f"{prefix}_title"] = d.get("title", "")
            attrs[f"{prefix}_type"] = d.get("disruption_type", "")
            attrs[f"{prefix}_severity"] = d.get("severity", "")
            attrs[f"{prefix}_from"] = d.get("from_date")
            attrs[f"{prefix}_to"] = d.get("to_date")
            attrs[f"{prefix}_url"] = d.get("url", "")

        return attrs
