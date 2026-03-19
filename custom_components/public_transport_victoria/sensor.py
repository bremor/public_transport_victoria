"""Sensor platform for Public Transport Victoria."""
import datetime
import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.util.dt import get_time_zone

from .const import ATTRIBUTION, DOMAIN
from .entity import DEPARTURE_NAMES, PtvDepartureEntity, PtvEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up departure sensors for a config entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    entities = [
        DepartureSensor(coordinator, config_entry, slot)
        for slot in range(5)
    ]
    entities.append(StopInfoSensor(coordinator, config_entry))
    async_add_entities(entities)


class DepartureSensor(PtvDepartureEntity, SensorEntity):
    """Departure time sensor — one per slot (next, 2nd … 5th).

    Name is dynamic: "Platform 3 to Upfield" when run data is available,
    falling back to "next departure" / "2nd departure" etc. otherwise.

    Attributes include platform, destination, is_realtime, is_express,
    punctuality and variance_minutes so no separate diagnostic entities
    are needed.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_departure_{self._slot}"

    @property
    def name(self) -> str:
        dep = self._departure
        if dep:
            destination = dep.get("destination_name", "")
            platform = dep.get("platform_number")
            if destination and platform is not None:
                return f"{self._device_label} platform {platform} to {destination}"
            if destination:
                return f"{self._device_label} to {destination}"
        return f"{self._device_label} {DEPARTURE_NAMES[self._slot]}"

    @property
    def native_value(self) -> datetime.datetime | None:
        """Return the best available departure time as a timezone-aware datetime."""
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
            "cancelled": dep.get("cancelled", False),
            "is_realtime": is_realtime,
            "is_express": dep.get("is_express", False),
            "destination": dep.get("destination_name", ""),
            "platform": dep.get("platform_number"),
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

        # Vehicle descriptor — air con, low floor, vehicle type, operator
        # Only include fields that actually have a value; None means the API
        # doesn't provide that field for this vehicle/route type.
        vd = dep.get("vehicle_descriptor") or {}
        if vd:
            for vd_key, vd_attr in [
                ("description", "vehicle_description"),
                ("operator",    "operator"),
                ("id",          "vehicle_id"),
            ]:
                val = vd.get(vd_key)
                if val:
                    attrs[vd_attr] = val
            for vd_key, vd_attr in [
                ("air_conditioned", "air_conditioned"),
                ("low_floor",       "low_floor"),
            ]:
                val = vd.get(vd_key)
                if val is not None:
                    attrs[vd_attr] = val

        # Vehicle position — GPS coordinates and bearing
        vp = dep.get("vehicle_position") or {}
        if vp and vp.get("latitude") is not None:
            attrs["vehicle_latitude"] = vp.get("latitude")
            attrs["vehicle_longitude"] = vp.get("longitude")
            attrs["vehicle_bearing"] = vp.get("bearing")

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


class StopInfoSensor(PtvEntity, SensorEntity):
    """Static stop information — location, zone, amenities, accessibility.

    State: zone string ("Zone 1", "Free Fare Zone") or the stop name as fallback.
    Attributes: latitude/longitude (enables HA map card), plus every amenity
    and accessibility field returned by the PTV stop-details endpoint.

    Data is fetched once when the coordinator first runs and cached on the
    entity; it re-fetches when HA restarts since this data rarely changes.
    """

    _attr_icon = "mdi:map-marker"
    _stop_info_cache: dict | None = None

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_stop_info"

    @property
    def name(self) -> str:
        return f"{self._device_label} stop info"

    @property
    def native_value(self) -> str:
        if self._stop_info_cache:
            return self._stop_info_cache.get("zone") or self._connector.stop_name
        return self._connector.stop_name

    @property
    def extra_state_attributes(self) -> dict:
        return self._stop_info_cache or {}

    async def async_added_to_hass(self) -> None:
        """Fetch stop info once when entity is registered; cache for the session."""
        await super().async_added_to_hass()
        if self._stop_info_cache is None:
            self._stop_info_cache = await self._connector.async_stop_info()
            self.async_write_ha_state()
