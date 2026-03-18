"""Sensor platform for Public Transport Victoria."""
import datetime
import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
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
        entities.append(DeparturePlatformSensor(coordinator, config_entry, slot))

    async_add_entities(entities)


class DepartureSensor(PtvDepartureEntity, SensorEntity):
    """Departure time — device_class: timestamp so HA renders 'in X min' natively.

    Attributes surface whether the time is real-time or scheduled, and when
    real-time is available, how many minutes early or late the service is.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_departure_{self._slot}"

    @property
    def name(self) -> str:
        return f"{self._device_label} {DEPARTURE_NAMES[self._slot]}"

    @property
    def native_value(self) -> datetime.datetime | None:
        """Return the best available departure time as a timezone-aware datetime.

        Prefers estimated (real-time) over scheduled when available.
        """
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

        is_realtime = dep.get("estimated_departure_utc") is not None
        attrs: dict = {
            "is_realtime": is_realtime,
            "scheduled_departure_utc": dep.get("scheduled_departure_utc"),
            "estimated_departure_utc": dep.get("estimated_departure_utc"),
            "at_platform": dep.get("at_platform"),
            "departure_note": dep.get("departure_note"),
            "stop_id": dep.get("stop_id"),
            "route_id": dep.get("route_id"),
            "run_ref": dep.get("run_ref"),
            "disruption_ids": dep.get("disruption_ids"),
            "attribution": ATTRIBUTION,
        }

        # When real-time data is available, compute variance from schedule.
        # variance_minutes: signed int for automations (positive=late, negative=early)
        # punctuality: human-readable string for display
        if is_realtime and dep.get("scheduled_departure_utc"):
            try:
                estimated = datetime.datetime.strptime(
                    dep["estimated_departure_utc"], "%Y-%m-%dT%H:%M:%SZ"
                )
                scheduled = datetime.datetime.strptime(
                    dep["scheduled_departure_utc"], "%Y-%m-%dT%H:%M:%SZ"
                )
                variance = int((estimated - scheduled).total_seconds() / 60)
                attrs["variance_minutes"] = variance
                if variance == 0:
                    attrs["punctuality"] = "on time"
                elif variance > 0:
                    attrs["punctuality"] = f"{variance} min late"
                else:
                    attrs["punctuality"] = f"{abs(variance)} min early"
            except (ValueError, TypeError):
                pass

        return attrs


class DeparturePlatformSensor(PtvDepartureEntity, SensorEntity):
    """Platform number for a departure.

    Only Metro Train (route_type 0) and V/Line (route_type 3) have platforms.
    For trams and buses this entity is disabled by default — the stop name
    already encodes the boarding location for those modes.
    """

    _attr_icon = "mdi:sign-direction"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

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
