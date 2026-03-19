"""Device tracker platform for PTV vehicle positions."""
from __future__ import annotations

import datetime
import logging

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_MAX_TRACKERS, DEFAULT_MAX_TRACKERS, DOMAIN
from .entity import PtvEntity

_LOGGER = logging.getLogger(__name__)

# How long to keep a tracker alive after its run_ref leaves the departure list.
LINGER_MINUTES = 10


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up vehicle position trackers for a config entry.

    Trackers are created dynamically as run_refs with GPS data appear.
    A cross-entry global registry prevents duplicate entities when multiple
    entries cover overlapping routes.

    On the first setup of any entry in a session, stale tracker entities from
    previous HA sessions are cleared so the registry stays clean.
    """
    data        = hass.data[DOMAIN][config_entry.entry_id]
    coordinator = data["coordinator"]
    connector   = data["connector"]

    # One-time per HA session: wipe tracker entities left over from previous runs.
    # Run_refs change daily so yesterday's trackers are always stale.
    if not hass.data[DOMAIN].get("trackers_cleaned"):
        hass.data[DOMAIN]["trackers_cleaned"] = True
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415
        registry = er.async_get(hass)
        stale = [
            ent for ent in registry.entities.values()
            if ent.domain == "device_tracker" and ent.platform == DOMAIN
        ]
        for ent in stale:
            _LOGGER.debug("Removing stale tracker from previous session: %s", ent.entity_id)
            registry.async_remove(ent.entity_id)

    # Cross-entry registry: run_ref → entry_id that owns the tracker
    global_trackers: dict[str, str] = hass.data[DOMAIN].setdefault("run_trackers", {})
    my_run_refs: set[str] = set()

    @callback
    def _check_for_new_trackers() -> None:
        max_trackers = config_entry.options.get(CONF_MAX_TRACKERS, DEFAULT_MAX_TRACKERS)
        departures = coordinator.data or []
        new_entities = []
        for dep in departures:
            run_ref = dep.get("run_ref")
            pos = dep.get("vehicle_position") or {}
            if not run_ref or not pos.get("latitude"):
                continue
            if run_ref in global_trackers:
                continue
            if len(my_run_refs) >= max_trackers:
                break
            global_trackers[run_ref] = config_entry.entry_id
            my_run_refs.add(run_ref)
            new_entities.append(
                PtvVehicleTracker(coordinator, config_entry, run_ref, connector, global_trackers)
            )
        if new_entities:
            async_add_entities(new_entities)

    @callback
    def _cleanup() -> None:
        for run_ref in my_run_refs:
            global_trackers.pop(run_ref, None)

    config_entry.async_on_unload(
        coordinator.async_add_listener(_check_for_new_trackers)
    )
    config_entry.async_on_unload(_cleanup)
    _check_for_new_trackers()


class PtvVehicleTracker(PtvEntity, TrackerEntity):
    """Live vehicle position tracker keyed by run_ref.

    Lingers for LINGER_MINUTES after the vehicle leaves the departure list,
    then self-removes from the entity registry so stale entries don't accumulate.

    State is the destination string ("to Upfield") rather than a zone name,
    overriding the default TrackerEntity "not_home" / "away" behaviour.
    """

    _attr_source_type = SourceType.GPS

    def __init__(
        self,
        coordinator,
        config_entry,
        run_ref: str,
        connector,
        global_trackers: dict[str, str],
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._run_ref          = run_ref
        self._global_trackers  = global_trackers
        self._lat: float | None              = None
        self._lon: float | None              = None
        self._bearing: float | None          = None
        self._destination: str | None        = None
        self._is_express: bool               = False
        self._vehicle_description: str | None = None
        self._operator: str | None           = None
        self._air_conditioned: bool | None   = None
        self._low_floor: bool | None         = None
        self._platform_number: str | None    = None
        self._scheduled_departure: str | None = None
        self._estimated_departure: str | None = None
        self._last_seen: datetime.datetime | None = None
        # Seed immediately so entity is available as soon as it registers
        self._update_from_departures(coordinator.data or [])

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def unique_id(self) -> str:
        # Global — no entry_id — so the same vehicle isn't duplicated across entries
        return f"ptv_{self._connector.route_type}_{self._run_ref}"

    @property
    def name(self) -> str:
        rt = self._connector.route_type_name or "Vehicle"
        if self._destination:
            return f"{rt} to {self._destination}"
        return f"{rt} {self._run_ref}"

    @property
    def device_info(self) -> DeviceInfo:
        """Each tracker gets its own HA device, linked to the stop device via via_device."""
        rt = self._connector.route_type_name or "Vehicle"
        dest = self._destination or self._run_ref
        return DeviceInfo(
            identifiers={(DOMAIN, f"vehicle_{self._run_ref}")},
            name=f"{rt} to {dest}",
            manufacturer="Public Transport Victoria",
            model=self._vehicle_description or rt,
            via_device=(DOMAIN, self._config_entry.entry_id),
        )

    # ------------------------------------------------------------------
    # TrackerEntity interface
    # ------------------------------------------------------------------

    @property
    def location_name(self) -> str:
        """Return destination as state — overrides TrackerEntity zone-based 'not_home'."""
        if self._destination:
            return f"to {self._destination}"
        return "active"

    @property
    def latitude(self) -> float | None:
        return self._lat

    @property
    def longitude(self) -> float | None:
        return self._lon

    @property
    def location_accuracy(self) -> int:
        return 0

    @property
    def available(self) -> bool:
        if self._last_seen is None:
            return False
        elapsed = (
            datetime.datetime.now(datetime.timezone.utc) - self._last_seen
        ).total_seconds()
        return elapsed < LINGER_MINUTES * 60

    # ------------------------------------------------------------------
    # Attributes
    # ------------------------------------------------------------------

    @property
    def extra_state_attributes(self) -> dict:
        attrs: dict = {"run_ref": self._run_ref}
        if self._destination:
            attrs["destination"] = self._destination
        if self._bearing is not None:
            attrs["bearing"] = self._bearing
        if self._vehicle_description:
            attrs["vehicle_description"] = self._vehicle_description
        if self._operator:
            attrs["operator"] = self._operator
        if self._air_conditioned is not None:
            attrs["air_conditioned"] = self._air_conditioned
        if self._low_floor is not None:
            attrs["low_floor"] = self._low_floor
        if self._is_express:
            attrs["is_express"] = True
        if self._platform_number is not None:
            attrs["last_seen_platform"] = self._platform_number
        if self._connector.route_name:
            attrs["route"] = self._connector.route_name
        if self._scheduled_departure:
            attrs["scheduled_departure"] = self._scheduled_departure
        if self._estimated_departure:
            attrs["estimated_departure"] = self._estimated_departure
        return attrs

    # ------------------------------------------------------------------
    # Data helpers
    # ------------------------------------------------------------------

    def _update_from_departures(self, departures: list) -> None:
        for dep in departures:
            if dep.get("run_ref") != self._run_ref:
                continue
            pos = dep.get("vehicle_position") or {}
            if pos.get("latitude"):
                self._lat       = pos["latitude"]
                self._lon       = pos["longitude"]
                self._bearing   = pos.get("bearing")
                self._last_seen = datetime.datetime.now(datetime.timezone.utc)
            self._destination          = dep.get("destination_name")
            self._is_express           = dep.get("is_express", False)
            self._platform_number      = dep.get("platform_number")
            self._scheduled_departure  = dep.get("scheduled_departure_utc")
            self._estimated_departure  = dep.get("estimated_departure_utc")
            vd = dep.get("vehicle_descriptor") or {}
            if vd.get("description"):
                self._vehicle_description = vd["description"]
            if vd.get("operator"):
                self._operator = vd["operator"]
            if vd.get("air_conditioned") is not None:
                self._air_conditioned = vd["air_conditioned"]
            if vd.get("low_floor") is not None:
                self._low_floor = vd["low_floor"]
            break

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_from_departures(self.coordinator.data or [])
        if not self.available and self._last_seen is not None:
            # Past linger window — remove from entity and device registries
            self._self_remove()
        self.async_write_ha_state()

    def _self_remove(self) -> None:
        """Remove this entity from the HA entity registry and free the run_ref slot."""
        self._global_trackers.pop(self._run_ref, None)
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415
        registry = er.async_get(self.hass)
        if self.entity_id and registry.async_get(self.entity_id):
            _LOGGER.debug("Vehicle tracker expired, removing: %s", self.entity_id)
            registry.async_remove(self.entity_id)
