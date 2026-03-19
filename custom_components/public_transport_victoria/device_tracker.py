"""Device tracker platform for PTV vehicle positions."""
from __future__ import annotations

import datetime
import logging

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import callback

from .const import DOMAIN
from .entity import PtvEntity

_LOGGER = logging.getLogger(__name__)

# How long to keep a tracker visible after its run_ref leaves the departure list.
# Lets the vehicle stay on the map as it passes the stop rather than vanishing.
LINGER_MINUTES = 10


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up vehicle position trackers for a config entry.

    Trackers are created dynamically as new run_refs with GPS data appear
    in coordinator updates.  A single global registry (hass.data[DOMAIN]["run_trackers"])
    prevents duplicate tracker entities when multiple entries cover overlapping
    routes — the first entry to see a run_ref owns its tracker.
    """
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    # Cross-entry registry: run_ref → entry_id that owns the tracker
    global_trackers: dict[str, str] = hass.data[DOMAIN].setdefault("run_trackers", {})
    # run_refs registered by THIS entry so we can free them on unload
    my_run_refs: set[str] = set()

    @callback
    def _check_for_new_trackers() -> None:
        departures = coordinator.data or []
        new_entities = []
        for dep in departures:
            run_ref = dep.get("run_ref")
            pos = dep.get("vehicle_position") or {}
            if not run_ref or not pos.get("latitude"):
                continue
            if run_ref not in global_trackers:
                global_trackers[run_ref] = config_entry.entry_id
                my_run_refs.add(run_ref)
                new_entities.append(
                    PtvVehicleTracker(coordinator, config_entry, run_ref)
                )
        if new_entities:
            async_add_entities(new_entities)

    @callback
    def _cleanup() -> None:
        """Release this entry's run_refs so another entry can reclaim them."""
        for run_ref in my_run_refs:
            global_trackers.pop(run_ref, None)

    config_entry.async_on_unload(
        coordinator.async_add_listener(_check_for_new_trackers)
    )
    config_entry.async_on_unload(_cleanup)
    # Seed from data already loaded during coordinator first refresh
    _check_for_new_trackers()


class PtvVehicleTracker(PtvEntity, TrackerEntity):
    """Live vehicle position tracker keyed by run_ref.

    Lingers for LINGER_MINUTES after the vehicle passes the monitored stop,
    showing the last known GPS position, so it doesn't disappear mid-journey.
    After the linger window expires the entity becomes unavailable.
    """

    _attr_source_type = SourceType.GPS

    def __init__(self, coordinator, config_entry, run_ref: str) -> None:
        super().__init__(coordinator, config_entry)
        self._run_ref = run_ref
        self._lat: float | None = None
        self._lon: float | None = None
        self._bearing: float | None = None
        self._destination: str | None = None
        self._is_express: bool = False
        self._vehicle_description: str | None = None
        self._last_seen: datetime.datetime | None = None
        # Seed immediately so the entity is available as soon as it's registered
        self._update_from_departures(coordinator.data or [])

    @property
    def unique_id(self) -> str:
        # Global across all entries — prevents HA entity registry duplicates
        # when multiple station entries see the same run_ref.
        return f"{DOMAIN}_vehicle_{self._run_ref}"

    @property
    def name(self) -> str:
        if self._destination:
            return f"{self._device_label} to {self._destination}"
        return f"{self._device_label} vehicle {self._run_ref}"

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

    @property
    def extra_state_attributes(self) -> dict:
        attrs: dict = {}
        if self._bearing is not None:
            attrs["bearing"] = self._bearing
        if self._vehicle_description:
            attrs["vehicle_description"] = self._vehicle_description
        if self._is_express:
            attrs["is_express"] = True
        return attrs

    def _update_from_departures(self, departures: list) -> None:
        """Parse position and metadata from a departures list."""
        for dep in departures:
            if dep.get("run_ref") != self._run_ref:
                continue
            pos = dep.get("vehicle_position") or {}
            if pos.get("latitude"):
                self._lat = pos["latitude"]
                self._lon = pos["longitude"]
                self._bearing = pos.get("bearing")
                self._last_seen = datetime.datetime.now(datetime.timezone.utc)
            self._destination = dep.get("destination_name")
            self._is_express = dep.get("is_express", False)
            vd = dep.get("vehicle_descriptor") or {}
            desc = vd.get("description")
            if desc:
                self._vehicle_description = desc
            break
        # run_ref not found → still in linger window or about to go unavailable

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh position and metadata from the latest coordinator data."""
        self._update_from_departures(self.coordinator.data or [])
        self.async_write_ha_state()
