"""Sensor platform for Public Transport Victoria."""
import datetime
import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.util.dt import get_time_zone

from .const import ATTRIBUTION, DOMAIN
from .entity import DEPARTURE_NAMES, PtvDepartureEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up departure sensors for a config entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    entities = []
    for slot in range(5):
        entities.append(DepartureSensor(coordinator, config_entry, slot))
        entities.append(DepartureMinutesSensor(coordinator, config_entry, slot))
        entities.append(DeparturePlatformSensor(coordinator, config_entry, slot))

    async_add_entities(entities)


class DepartureSensor(PtvDepartureEntity, SensorEntity):
    """Departure time — device_class: timestamp so HA renders 'in X min' natively."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_departure_{self._slot}"

    @property
    def name(self) -> str:
        return f"{self._device_label} {DEPARTURE_NAMES[self._slot]}"

    @property
    def native_value(self) -> datetime.datetime | None:
        """Return the departure time as a timezone-aware datetime."""
        dep = self._departure
        if dep is None:
            return None
        utc_str = dep.get("estimated_departure_utc") or dep.get("scheduled_departure_utc")
        if not utc_str:
            return None
        dt = datetime.datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
        return dt.astimezone(get_time_zone(self.hass.config.time_zone))

    @property
    def extra_state_attributes(self) -> dict:
        dep = self._departure
        if dep is None:
            return {}
        return {
            "stop_id": dep.get("stop_id"),
            "route_id": dep.get("route_id"),
            "run_id": dep.get("run_id"),
            "run_ref": dep.get("run_ref"),
            "direction_id": dep.get("direction_id"),
            "scheduled_departure_utc": dep.get("scheduled_departure_utc"),
            "estimated_departure_utc": dep.get("estimated_departure_utc"),
            "at_platform": dep.get("at_platform"),
            "departure_note": dep.get("departure_note"),
            "disruption_ids": dep.get("disruption_ids"),
            "attribution": ATTRIBUTION,
        }


class DepartureMinutesSensor(PtvDepartureEntity, SensorEntity):
    """Minutes until departure — numeric sensor useful for automations and graphs."""

    _attr_native_unit_of_measurement = "min"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:clock-outline"

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_minutes_{self._slot}"

    @property
    def name(self) -> str:
        return f"{self._device_label} {DEPARTURE_NAMES[self._slot]} minutes"

    @property
    def native_value(self) -> int | None:
        dep = self._departure
        if dep is None:
            return None
        return dep.get("minutes_until")


class DeparturePlatformSensor(PtvDepartureEntity, SensorEntity):
    """Platform number for a departure.

    Only Metro Train (route_type 0) and V/Line (route_type 3) have platforms.
    For trams and buses this entity is disabled by default — the stop name
    already encodes the boarding location for those modes.
    """

    _attr_icon = "mdi:sign-direction"

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_platform_{self._slot}"

    @property
    def name(self) -> str:
        return f"{self._device_label} {DEPARTURE_NAMES[self._slot]} platform"

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Enable by default only for modes that have platforms (trains)."""
        return self._connector.route_type in ("0", "3")

    @property
    def native_value(self) -> str | None:
        dep = self._departure
        if dep is None:
            return None
        platform = dep.get("platform_number")
        return str(platform) if platform is not None else None
