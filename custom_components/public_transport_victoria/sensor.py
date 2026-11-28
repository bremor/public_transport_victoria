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

from .const import ATTRIBUTION, DOMAIN
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

# Empty-poll policy (0.8.5).  A repair issue is only raised while there is
# evidence this entry was healthy recently; otherwise an empty poll is far more
# likely to be a quiet period than a stale id.
HEALTHY_EVIDENCE_WINDOW = datetime.timedelta(hours=48)
DIAG_COOLDOWN = EMPTY_DIAG_COOLDOWN

# Config-entry data keys written by the coordinator.  All three are bounded to
# churn events (or a single backfill); names backing entity unique ids are
# never touched.
ATTR_LAST_DEPARTURE_AT = "last_departure_at"
ATTR_DIAG_NEXT_OK_AT = "diag_next_ok_at"
ATTR_ROUTE_GTFS_ID = "route_gtfs_id"

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
    a repair issue is raised or cleared.  Entry writes happen only on churn
    events, so a healthy poll never writes to the config entry.
    """

    def __init__(self, hass, connector, config_entry=None):
        """Initialize the coordinator."""
        self.connector = connector
        self._last_healthy_at = None
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
        if meta.get("kept_rows", 0):
            self._async_handle_healthy()
        else:
            await self._async_handle_empty()
        return self.connector.departures  # Return the latest data

    def _async_handle_healthy(self):
        """Record real departures and clear any stale-ids issue."""
        self._last_healthy_at = dt_util.utcnow()
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

    def _healthy_evidence(self, entry_data):
        """Return the most recent healthy-poll timestamp known for this entry."""
        candidates = []
        if self._last_healthy_at is not None:
            candidates.append(self._last_healthy_at)
        persisted = self._parse_entry_time(
            entry_data.get(ATTR_LAST_DEPARTURE_AT)
        )
        if persisted is not None:
            candidates.append(persisted)
        if not candidates:
            return None
        return max(candidates)

    def _persist_entry_data(self, entry, updates):
        """Merge ``updates`` into a fresh copy of entry.data and write once."""
        async_persist_entry_data(self.hass, entry, updates)

    async def _async_handle_empty(self):
        """Apply the quiet gate + cooldown, diagnose, then repair if needed."""
        entry = self.config_entry
        if entry is None:
            return
        now = dt_util.utcnow()
        entry_data = entry.data

        healthy_at = self._healthy_evidence(entry_data)
        if (
            healthy_at is None
            or now - healthy_at > HEALTHY_EVIDENCE_WINDOW
        ):
            _LOGGER.debug(
                "PTV: empty departures for entry %s with no healthy poll in "
                "the last %s hours - assuming a quiet period",
                entry.entry_id,
                HEALTHY_EVIDENCE_WINDOW.total_seconds() // 3600,
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
        resolution = None
        if what in REPAIR_ACTIONS:
            resolution = await self.connector.resolve_candidate()

        updates = {}
        gtfs_id = self.connector.gtfs_id
        if gtfs_id is not None and entry_data.get(ATTR_ROUTE_GTFS_ID) != gtfs_id:
            updates[ATTR_ROUTE_GTFS_ID] = gtfs_id
        last_departure_at = healthy_at.isoformat()
        if entry_data.get(ATTR_LAST_DEPARTURE_AT) != last_departure_at:
            updates[ATTR_LAST_DEPARTURE_AT] = last_departure_at
        updates[ATTR_DIAG_NEXT_OK_AT] = (now + DIAG_COOLDOWN).isoformat()
        self._persist_entry_data(entry, updates)

        if what in REPAIR_ACTIONS:
            async_create_stale_ids_issue(
                self.hass, entry, what=what, resolution=resolution
            )
        else:
            _LOGGER.debug(
                "PTV: empty departures for entry %s are not stale ids (%s)",
                entry.entry_id,
                what,
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
