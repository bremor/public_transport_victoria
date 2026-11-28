"""Platform for sensor integration."""

import datetime
import logging

from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    CoordinatorEntity,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .const import (
    ATTRIBUTION,
    ATTR_DIAG_NEXT_OK_AT,
    ATTR_DIAG_STREAK,
    ATTR_FIRST_SEEN_AT,
    ATTR_ROUTE_GTFS_ID,
    DOMAIN,
    STALE_STREAK_THRESHOLD,
)
from .PublicTransportVictoria.public_transport_victoria import (
    EMPTY_DIAG_COOLDOWN,
    PTVApiError,
)
from .repairs import (
    async_create_stale_ids_issue,
    async_delete_stale_ids_issue,
    async_persist_entry_data,
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = datetime.timedelta(minutes=10)

# Empty-poll policy (0.8.5).  A brand-new entry should not nag within its
# first day (it may simply be misconfigured), but once it is older than this,
# empties are diagnosed unconditionally: healthy polls write nothing, so
# requiring "healthy evidence" deadlocked long-healthy entries after a restart.
ENTRY_AGE_GATE = datetime.timedelta(hours=24)
DIAG_COOLDOWN = EMPTY_DIAG_COOLDOWN

REPAIR_ACTIONS = ("direction", "route", "route_mismatch")


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Add sensors for passed config_entry in HA."""
    connector = hass.data[DOMAIN][config_entry.entry_id]

    # Create the coordinator to manage polling
    coordinator = PublicTransportVictoriaDataUpdateCoordinator(
        hass, connector, config_entry=config_entry
    )

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    # Create sensors for the first 5 departures
    new_devices = [PublicTransportVictoriaSensor(coordinator, i) for i in range(5)]

    async_add_entities(new_devices)


class PublicTransportVictoriaDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Public Transport Victoria data.

    The connector only gathers evidence.  This coordinator owns the policy:
    when a diagnosis may run, which config-entry stamps are persisted, and when
    a repair issue is raised or cleared.  Entry writes are bounded to diagnosis
    windows (plus a one-time first-seen fallback), so a healthy poll never
    writes to the config entry.
    """

    def __init__(self, hass, connector, config_entry=None):
        """Initialize the coordinator."""
        self.connector = connector
        # A healthy poll observed by this instance resets the persisted streak
        # lazily at the next diagnosis (healthy polls never write).
        self._healthy_since_diag = False
        if config_entry is not None and connector.gtfs_id is None:
            # A previous churn event may have persisted the GTFS id; seeding it
            # here avoids repeating the one-call backfill after a reload.
            stored_gtfs = config_entry.data.get(ATTR_ROUTE_GTFS_ID)
            if stored_gtfs:
                connector.gtfs_id = str(stored_gtfs)
        super().__init__(
            hass,
            _LOGGER,
            name="Public Transport Victoria",
            update_interval=SCAN_INTERVAL,
            config_entry=config_entry,
        )

    async def _async_update_data(self):
        """Fetch data from Public Transport Victoria."""
        _LOGGER.debug("Fetching new data from Public Transport Victoria API.")
        try:
            await self.connector.async_update()
        except PTVApiError as err:
            raise UpdateFailed(
                "Error fetching Public Transport Victoria data: {}".format(err)
            ) from err

        meta = self.connector.last_result_meta or {}
        if self.config_entry is not None:
            self._async_ensure_first_seen(self.config_entry)
        if meta.get("kept_rows", 0):
            self._async_handle_healthy()
        else:
            await self._async_handle_empty()
        return self.connector.departures  # Return the latest data

    def _async_ensure_first_seen(self, entry):
        """Fallback age clock for entries without ``created_at``.

        Real ConfigEntry objects always carry ``created_at``; this only writes
        for unusual entries (or test doubles) so the age gate still works.
        """
        if isinstance(getattr(entry, "created_at", None), datetime.datetime):
            return
        if entry.data.get(ATTR_FIRST_SEEN_AT):
            return
        self._persist_entry_data(
            entry, {ATTR_FIRST_SEEN_AT: dt_util.utcnow().isoformat()}
        )

    def _async_handle_healthy(self):
        """Record a healthy poll, reset the streak lazily, clear the issue.

        No entry writes: the reset is persisted in the next diagnosis window.
        """
        self._healthy_since_diag = True
        entry = self.config_entry
        if entry is None:
            return
        async_delete_stale_ids_issue(self.hass, entry.entry_id)

    @staticmethod
    def _parse_entry_time(value):
        """Parse an ISO timestamp persisted in entry.data, or return None."""
        if not value:
            return None
        parsed = dt_util.parse_datetime(str(value))
        if parsed is None or parsed.tzinfo is None:
            return None
        return parsed

    def _entry_age(self, entry, now):
        """Age of the entry, from created_at or a persisted first-seen stamp."""
        created = getattr(entry, "created_at", None)
        if isinstance(created, datetime.datetime):
            if created.tzinfo is None:
                created = created.replace(tzinfo=datetime.timezone.utc)
            return now - created
        first_seen = self._parse_entry_time(
            entry.data.get(ATTR_FIRST_SEEN_AT)
        )
        if first_seen is not None:
            return now - first_seen
        return None

    @staticmethod
    def _persisted_streak(entry_data):
        """Return the persisted consecutive-stale diagnosis counter."""
        try:
            return max(0, int(entry_data.get(ATTR_DIAG_STREAK) or 0))
        except (TypeError, ValueError):
            return 0

    def _persist_entry_data(self, entry, updates):
        """Merge ``updates`` into a fresh copy of entry.data and write once."""
        async_persist_entry_data(self.hass, entry, updates)

    async def _async_handle_empty(self):
        """Apply the age gate + cooldown, diagnose, and track the streak."""
        entry = self.config_entry
        if entry is None:
            return
        now = dt_util.utcnow()
        entry_data = entry.data

        age = self._entry_age(entry, now)
        if age is None:
            # Fallback for entries without created_at: start the clock now and
            # diagnose on a later poll, not this one.
            self._persist_entry_data(
                entry, {ATTR_FIRST_SEEN_AT: now.isoformat()}
            )
            return
        if age < ENTRY_AGE_GATE:
            _LOGGER.debug(
                "PTV: entry %s is younger than %s hours - not diagnosing yet",
                entry.entry_id,
                ENTRY_AGE_GATE.total_seconds() // 3600,
            )
            return

        next_ok_at = self._parse_entry_time(
            entry_data.get(ATTR_DIAG_NEXT_OK_AT)
        )
        if next_ok_at is not None and now < next_ok_at:
            _LOGGER.debug(
                "PTV: empty departures for entry %s inside the diagnosis "
                "cooldown - skipping probes",
                entry.entry_id,
            )
            return

        diagnosis = await self.connector.diagnose_empty()
        what = diagnosis.get("what", "unknown")
        actionable = what in REPAIR_ACTIONS

        streak = 0 if self._healthy_since_diag else self._persisted_streak(
            entry_data
        )
        streak = streak + 1 if actionable else 0
        self._healthy_since_diag = False

        resolution = None
        if actionable and streak >= STALE_STREAK_THRESHOLD:
            resolution = await self.connector.resolve_candidate()

        updates = {
            ATTR_DIAG_NEXT_OK_AT: (now + DIAG_COOLDOWN).isoformat(),
            ATTR_DIAG_STREAK: streak,
        }
        gtfs_id = self.connector.gtfs_id
        if gtfs_id is not None and entry_data.get(ATTR_ROUTE_GTFS_ID) != gtfs_id:
            updates[ATTR_ROUTE_GTFS_ID] = gtfs_id
        self._persist_entry_data(entry, updates)

        if actionable and streak >= STALE_STREAK_THRESHOLD:
            async_create_stale_ids_issue(
                self.hass, entry, what=what, resolution=resolution
            )
        else:
            _LOGGER.debug(
                "PTV: empty departures for entry %s are not confirmed stale "
                "ids (verdict=%s, streak=%d)",
                entry.entry_id,
                what,
                streak,
            )


class PublicTransportVictoriaSensor(CoordinatorEntity, Entity):
    """Representation of a Public Transport Victoria Sensor."""

    # Home Assistant renders this as the state attribution marker, so reading
    # the attributes never needs to mutate the coordinator's departure data.
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator, number):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._number = number
        self._connector = coordinator.connector

    @property
    def state(self):
        """Return the state of the sensor."""
        if len(self.coordinator.data) > self._number:
            return self.coordinator.data[self._number].get("departure", "No data")
        return "No data"

    @property
    def name(self):
        """Return the name of the sensor."""
        return "{} line to {} from {} {}".format(
            self._connector.route_name,
            self._connector.direction_name,
            self._connector.stop_name,
            self._number,
        )

    @property
    def unique_id(self):
        """Return Unique ID string."""
        return "{} line to {} from {} {}".format(
            self._connector.route_name,
            self._connector.direction_name,
            self._connector.stop_name,
            self._number,
        )

    @property
    def extra_state_attributes(self):
        """Return the state attributes of the sensor.

        A shallow copy keeps coordinator data immutable while preserving every
        raw departure field.  Attribution comes from ``_attr_attribution``.
        """
        if len(self.coordinator.data) > self._number:
            return dict(self.coordinator.data[self._number])
        return {}
