"""Sensor platform for Public Transport Victoria."""
import datetime
import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.util.dt import get_time_zone

from .const import ATTRIBUTION, DOMAIN
from .entity import PtvEntity

_LOGGER = logging.getLogger(__name__)

# Route types that have platform numbers and should use PlatformDepartureSensor.
# Trains (0) and V/Line (3) both allocate platform numbers at stations.
# Trams (1), Bus (2), Night Bus (4) use stop IDs rather than platforms.
_PLATFORM_ROUTE_TYPES = {"0", "3"}
_MAX_PLATFORMS = 10
_SLOTS = 5


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _parse_utc(utc_str: str | None, hass) -> datetime.datetime | None:
    """Parse a PTV UTC timestamp string and return a timezone-aware datetime."""
    if not utc_str:
        return None
    dt = datetime.datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc
    )
    return dt.astimezone(get_time_zone(hass.config.time_zone))


def _build_departure_attributes(dep: dict, hass) -> dict:
    """Build the extra_state_attributes dict for a departure dict.

    Shared by PlatformDepartureSensor and SlotDepartureSensor so attribute
    behaviour is identical across train and non-train modes.
    """
    is_realtime = dep.get("estimated_departure_utc") is not None

    attrs: dict = {
        "cancelled":               dep.get("cancelled", False),
        "is_realtime":             is_realtime,
        "is_express":              dep.get("is_express", False),
        "destination":             dep.get("destination_name", ""),
        "platform":                dep.get("platform_number"),
        "scheduled_departure_utc": dep.get("scheduled_departure_utc"),
        "estimated_departure_utc": dep.get("estimated_departure_utc"),
        "at_platform":             dep.get("at_platform"),
        "departure_note":          dep.get("departure_note"),
        "stop_id":                 dep.get("stop_id"),
        "route_id":                dep.get("route_id"),
        "run_ref":                 dep.get("run_ref"),
        "disruption_ids":          dep.get("disruption_ids"),
        "attribution":             ATTRIBUTION,
    }

    # Vehicle descriptor — only include fields the API actually populated
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
        attrs["vehicle_latitude"]  = vp.get("latitude")
        attrs["vehicle_longitude"] = vp.get("longitude")
        attrs["vehicle_bearing"]   = vp.get("bearing")

    # Stopping pattern — upcoming stops and any express-skipped stops
    pattern = dep.get("stopping_pattern") or {}
    if pattern.get("upcoming"):
        attrs["upcoming_stops"] = pattern["upcoming"]
    if pattern.get("skipped"):
        attrs["skipped_stops"] = pattern["skipped"]

    # Punctuality — only when real-time data and scheduled time both available
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


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------

async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up departure sensors for a config entry.

    Trains: one PlatformDepartureSensor per (platform_number, slot).
    Entities are created dynamically as new platforms appear in departure data,
    up to _MAX_PLATFORMS. Slots 0-1 enabled; slots 2-4 disabled by default.

    All other modes: five SlotDepartureSensor entities in arrival-time order.
    Slots 0-1 enabled; slots 2-4 disabled by default.

    One StopInfoSensor is always added regardless of mode.
    """
    data        = hass.data[DOMAIN][config_entry.entry_id]
    coordinator = data["coordinator"]
    connector   = data["connector"]

    is_train = str(connector.route_type) in _PLATFORM_ROUTE_TYPES
    known_keys: set[tuple[str, int]] = set()

    def _check_for_new_platform_sensors() -> None:
        """Create PlatformDepartureSensor entities for any platform not yet seen.

        Called once at setup (from current coordinator data) and then registered
        as a coordinator listener so new platforms discovered at runtime get
        entities created automatically.
        """
        departures = coordinator.data or []

        # Collect unique platform keys in first-seen order, capped at _MAX_PLATFORMS.
        # Skip departures with no platform number — they get no entity.
        seen_platforms: list[str] = []
        for dep in departures:
            p = dep.get("platform_number")
            if not p:
                continue
            p = str(p)
            if p not in seen_platforms:
                seen_platforms.append(p)
            if len(seen_platforms) >= _MAX_PLATFORMS:
                break

        new_entities = []
        for platform_key in seen_platforms:
            for slot in range(_SLOTS):
                key = (platform_key, slot)
                if key not in known_keys:
                    known_keys.add(key)
                    new_entities.append(
                        PlatformDepartureSensor(coordinator, config_entry, platform_key, slot)
                    )

        if new_entities:
            async_add_entities(new_entities)

    if is_train:
        coordinator.async_add_listener(_check_for_new_platform_sensors)
        _check_for_new_platform_sensors()
    else:
        async_add_entities([
            SlotDepartureSensor(coordinator, config_entry, slot)
            for slot in range(_SLOTS)
        ])

    async_add_entities([StopInfoSensor(coordinator, config_entry)])


# ---------------------------------------------------------------------------
# Train sensor: grouped by platform
# ---------------------------------------------------------------------------

class PlatformDepartureSensor(PtvEntity, SensorEntity):
    """Departure sensor grouped by platform — trains only.

    One entity per (platform_number × slot). Platform number is stable in the
    entity name; destination and route info are attributes that update each poll.

    Slots 0-1 are enabled by default; slots 2-4 start disabled in the entity
    registry so the device card isn't cluttered before the user opts in.

    native_value returns None (not unavailable) when this slot has no departure:
    the entity stays visible and shows Unknown state until a service appears.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    _SLOT_LABELS = ["next", "next 1", "next 2", "next 3", "next 4"]

    def __init__(self, coordinator, config_entry, platform_key: str, slot: int):
        super().__init__(coordinator, config_entry)
        self._platform_key = platform_key
        self._slot = slot
        self._attr_entity_registry_enabled_default = slot < 2

    @property
    def unique_id(self) -> str:
        safe = self._platform_key.replace(" ", "_").lower()
        return f"{self._config_entry.entry_id}_platform_{safe}_slot_{self._slot}"

    @property
    def name(self) -> str:
        """Dynamic friendly name — updates each poll to reflect current destination.

        Format: "Platform 1 to Upfield · next"
        Falls back to "Platform 1 · next" when destination isn't yet known.
        The entity_id is fixed at first registration; only the friendly name changes.
        """
        dep = self._departure
        destination = dep.get("destination_name", "") if dep else ""
        prefix = f"Platform {self._platform_key}"
        if destination:
            prefix = f"{prefix} to {destination}"
        return f"{prefix} · {self._SLOT_LABELS[self._slot]}"

    @property
    def available(self) -> bool:
        """Remain available as long as the coordinator is healthy."""
        return self.coordinator.last_update_success

    @property
    def _departure(self) -> dict | None:
        departures = self.coordinator.data or []
        platform_deps = [
            d for d in departures
            if str(d.get("platform_number") or "") == self._platform_key
        ]
        return platform_deps[self._slot] if len(platform_deps) > self._slot else None

    @property
    def native_value(self) -> datetime.datetime | None:
        dep = self._departure
        if dep is None:
            return None
        utc_str = dep.get("estimated_departure_utc") or dep.get("scheduled_departure_utc")
        return _parse_utc(utc_str, self.hass)

    @property
    def extra_state_attributes(self) -> dict:
        dep = self._departure
        return _build_departure_attributes(dep, self.hass) if dep else {}


# ---------------------------------------------------------------------------
# Non-train sensor: ordered by departure time (original slot model)
# ---------------------------------------------------------------------------

class SlotDepartureSensor(PtvEntity, SensorEntity):
    """Departure sensor for non-train modes (tram, bus, V/Line).

    Five sensors in departure-time order. Slots 0-1 enabled; 2-4 disabled.
    Unique IDs preserve the original departure_{slot} format for backward compat.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    _SLOT_LABELS = ["next", "2nd", "3rd", "4th", "5th"]

    def __init__(self, coordinator, config_entry, slot: int):
        super().__init__(coordinator, config_entry)
        self._slot = slot
        self._attr_entity_registry_enabled_default = slot < 2

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_departure_{self._slot}"

    @property
    def name(self) -> str:
        """Dynamic friendly name using current destination.

        Format: "Towards Flinders Street · next"
        Falls back to "{device_label} · next" when destination isn't yet known.
        """
        dep = self._departure
        destination = dep.get("destination_name", "") if dep else ""
        prefix = f"Towards {destination}" if destination else self._device_label
        return f"{prefix} · {self._SLOT_LABELS[self._slot]}"

    @property
    def _departure(self) -> dict | None:
        data = self.coordinator.data
        return data[self._slot] if data and len(data) > self._slot else None

    @property
    def available(self) -> bool:
        return self._departure is not None

    @property
    def native_value(self) -> datetime.datetime | None:
        dep = self._departure
        if dep is None:
            return None
        utc_str = dep.get("estimated_departure_utc") or dep.get("scheduled_departure_utc")
        return _parse_utc(utc_str, self.hass)

    @property
    def extra_state_attributes(self) -> dict:
        dep = self._departure
        return _build_departure_attributes(dep, self.hass) if dep else {}


# ---------------------------------------------------------------------------
# Stop info sensor
# ---------------------------------------------------------------------------

class StopInfoSensor(PtvEntity, SensorEntity):
    """Static stop information — location, zone, amenities, accessibility.

    State: zone string ("Zone 1", "Free Fare Zone") or the stop name as fallback.
    Attributes: latitude/longitude (enables HA map card), plus every amenity
    and accessibility field returned by the PTV stop-details endpoint.

    Data is fetched once when the entity is registered and cached for the
    session; it re-fetches on HA restart since this data rarely changes.
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
