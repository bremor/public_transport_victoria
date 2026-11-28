"""Repair issues and fix flow for stale PTV timetable ids.

PTV answers HTTP 200 with an empty departure list for stale numeric route,
direction and stop ids (issue #50), so an empty sensor looks exactly like a
quiet period.  When a churn event is established, the coordinator raises one
repair issue per config entry.

Two tiers are used:

* ``stale_ptv_ids`` -- fixable.  The replacement ids were resolved uniquely and
  verified (stop and direction names still match), so the repair flow can
  write the numeric ids and reload the entry in one click.
* ``stale_ptv_ids_manual`` -- not fixable.  The replacement was ambiguous or
  could not be verified, so the issue carries manual instructions (delete and
  re-add the entry; the config flow fetches current ids).

Entity names (which back entity unique ids) are never rewritten by either
path.  This module is also the integration's ``repairs`` platform: Home
Assistant discovers :func:`async_create_fix_flow` for the fixable issues.
"""

import logging

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_ID
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DIRECTION,
    CONF_DIRECTION_NAME,
    CONF_ROUTE,
    CONF_ROUTE_NAME,
    CONF_ROUTE_TYPE,
    CONF_ROUTE_TYPE_NAME,
    CONF_STOP,
    CONF_STOP_NAME,
    DOMAIN,
)
from .PublicTransportVictoria.public_transport_victoria import (
    EMPTY_DIAG_COOLDOWN,
    Connector,
    PTVApiError,
)

_LOGGER = logging.getLogger(__name__)

TRANSLATION_KEY = "stale_ptv_ids"
MANUAL_TRANSLATION_KEY = "stale_ptv_ids_manual"
LEARN_MORE_URL = (
    "https://github.com/bremor/public_transport_victoria/issues/50"
)
ISSUE_PREFIX = "stale_ptv_ids_"

ATTR_LAST_DEPARTURE_AT = "last_departure_at"
ATTR_DIAG_NEXT_OK_AT = "diag_next_ok_at"
ATTR_ROUTE_GTFS_ID = "route_gtfs_id"

_WHAT_LABELS = {
    "direction": "direction",
    "route": "route",
    "route_mismatch": "route mismatch",
}

REMEDY_TEXT = (
    "Delete this entry and add it again: the setup flow fetches the current "
    "timetable ids."
)


def issue_id(entry_id):
    """Return the repair issue id for one config entry."""
    return ISSUE_PREFIX + entry_id


def _text(value):
    """Render an id or name for a translation placeholder."""
    return "" if value is None else str(value)


def _configured_ids(entry):
    """Describe the ids stored in the config entry (display only)."""
    data = entry.data
    return "route {}, direction {}, stop {}".format(
        _text(data.get(CONF_ROUTE)) or "unknown",
        _text(data.get(CONF_DIRECTION)) or "unknown",
        _text(data.get(CONF_STOP)) or "unknown",
    )


def _detail_text(what, resolution):
    """One sentence describing what was detected (manual tier)."""
    if what == "direction":
        return (
            "Departures exist for this stop and route, but not for the "
            "configured direction id."
        )
    if what == "route":
        return "The configured route id no longer exists."
    if what == "route_mismatch":
        reason = (resolution or {}).get("reason")
        return (
            "There are departures at this stop, but none for the configured "
            "route id ({})."
        ).format(reason or "other route ids were returned")
    return ""


def _candidate_text(resolution):
    """Describe the informational replacement, or say none was found."""
    resolution = resolution or {}
    candidate = resolution.get("candidate") or {}
    if resolution.get("resolved"):
        parts = []
        if candidate.get("route") is not None:
            parts.append("route {}".format(candidate["route"]))
        if candidate.get("direction") is not None:
            parts.append("direction {}".format(candidate["direction"]))
        if candidate.get("gtfs"):
            parts.append("GTFS id {}".format(candidate["gtfs"]))
        if parts:
            return "A working replacement looks like {}.".format(
                ", ".join(parts)
            )
    return (
        "No unambiguous replacement was found automatically, so the setup "
        "flow is the reliable source."
    )


def _fix_placeholders(entry, candidate, detail):
    """Placeholders for the fixable issue and its confirm step."""
    data = entry.data
    candidate = candidate or {}
    return {
        "route_name": _text(data.get(CONF_ROUTE_NAME)),
        "old_route_id": _text(data.get(CONF_ROUTE)),
        "new_route_id": _text(candidate.get("route")),
        "old_direction_id": _text(data.get(CONF_DIRECTION)),
        "new_direction_id": _text(candidate.get("direction")),
        "old_stop_id": _text(data.get(CONF_STOP)),
        "new_stop_id": _text(candidate.get("stop")),
        "route_gtfs_id": _text(candidate.get("gtfs")),
        "detail": detail or "",
    }


def _is_fixable(resolution):
    """True only for a unique, verified, complete replacement."""
    resolution = resolution or {}
    candidate = resolution.get("candidate") or {}
    return bool(
        resolution.get("resolved")
        and candidate.get("route")
        and candidate.get("direction")
        and candidate.get("stop")
    )


def _issue_data(entry, what, candidate):
    """Issue data handed to the repair flow (entry id + display candidate)."""
    return {
        "entry_id": entry.entry_id,
        "what": what,
        "candidate": {
            key: _text((candidate or {}).get(key))
            for key in ("route", "direction", "stop", "gtfs")
        },
    }


def async_create_stale_ids_issue(hass, entry, *, what, resolution):
    """Create or refresh this entry's stale-timetable-ids repair issue."""
    resolution = resolution or {}
    candidate = resolution.get("candidate") or {}
    fixable = _is_fixable(resolution)

    if fixable:
        placeholders = _fix_placeholders(
            entry, candidate, resolution.get("reason", "")
        )
        _LOGGER.warning(
            "PTV: raising a fixable repair issue for entry %s: stale %s ids",
            entry.entry_id,
            _WHAT_LABELS.get(what, str(what)),
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id(entry.entry_id),
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=TRANSLATION_KEY,
            translation_placeholders=placeholders,
            data=_issue_data(entry, what, candidate),
            learn_more_url=LEARN_MORE_URL,
        )
        return

    placeholders = {
        "entry_title": str(entry.title),
        "what": _WHAT_LABELS.get(what, str(what)),
        "detail": _detail_text(what, resolution),
        "configured": _configured_ids(entry),
        "candidate": _candidate_text(resolution),
        "remedy": REMEDY_TEXT,
    }
    _LOGGER.warning(
        "PTV: raising a manual repair issue for entry %s: stale %s ids",
        entry.entry_id,
        _WHAT_LABELS.get(what, str(what)),
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry.entry_id),
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=MANUAL_TRANSLATION_KEY,
        translation_placeholders=placeholders,
        data=_issue_data(entry, what, candidate),
        learn_more_url=LEARN_MORE_URL,
    )


def async_delete_stale_ids_issue(hass, entry_id):
    """Delete this entry's stale-timetable-ids repair issue, if any."""
    ir.async_delete_issue(hass, DOMAIN, issue_id(entry_id))


def _connector_from_entry(hass, entry):
    """Build a throwaway connector from config-entry data for re-verification."""
    connector = Connector(
        hass,
        entry.data[CONF_ID],
        entry.data[CONF_API_KEY],
        entry.data[CONF_ROUTE_TYPE],
        entry.data[CONF_ROUTE],
        entry.data[CONF_DIRECTION],
        entry.data[CONF_STOP],
        entry.data[CONF_ROUTE_TYPE_NAME],
        entry.data[CONF_ROUTE_NAME],
        entry.data[CONF_DIRECTION_NAME],
        entry.data[CONF_STOP_NAME],
    )
    stored_gtfs = entry.data.get(ATTR_ROUTE_GTFS_ID)
    if stored_gtfs:
        connector.gtfs_id = str(stored_gtfs)
    return connector


class StaleIdsRepairFlow(RepairsFlow):
    """Confirm-only flow that applies verified timetable ids."""

    def __init__(self, data):
        """Initialise from the repair issue data."""
        self._entry_id = _text(data.get("entry_id"))
        self._what = _text(data.get("what")) or "unknown"
        self._candidate = dict(data.get("candidate") or {})
        super().__init__()

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Handle the first step of a fix flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Show the proposed id change, then apply it on submit."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        placeholders = _fix_placeholders(
            entry, self._candidate, "Review the change before applying it."
        )
        if user_input is not None:
            return await self._async_apply(entry)
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders=placeholders,
        )

    async def _async_apply(self, entry) -> FlowResult:
        """Re-verify live, write the ids, and schedule a reload."""
        if (
            entry.state is not ConfigEntryState.LOADED
            or getattr(entry, "disabled_by", None) is not None
        ):
            return self.async_abort(reason="entry_not_loaded")

        resolution = await self._async_resolve(entry)
        candidate = resolution.get("candidate") or {}
        if not _is_fixable(resolution):
            # The live timetable no longer supports a confident fix.  Replace
            # the fixable issue with manual instructions and show the error.
            async_create_stale_ids_issue(
                self.hass, entry, what=self._what, resolution=resolution
            )
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                errors={"base": "verification_failed"},
                description_placeholders=_fix_placeholders(
                    entry, candidate, resolution.get("reason", "")
                ),
            )

        stored = {
            "route": _text(entry.data.get(CONF_ROUTE)),
            "direction": _text(entry.data.get(CONF_DIRECTION)),
            "stop": _text(entry.data.get(CONF_STOP)),
        }
        new = {
            "route": _text(candidate.get("route")),
            "direction": _text(candidate.get("direction")),
            "stop": _text(candidate.get("stop")),
        }
        if new == stored:
            # Resolved to the ids already stored: no write, just heal/delete.
            return self.async_create_entry(data={})

        data = dict(entry.data)
        data.update(
            {
                CONF_ROUTE: new["route"],
                CONF_DIRECTION: new["direction"],
                CONF_STOP: new["stop"],
                ATTR_DIAG_NEXT_OK_AT: (
                    dt_util.utcnow() + EMPTY_DIAG_COOLDOWN
                ).isoformat(),
            }
        )
        gtfs_id = candidate.get("gtfs") or entry.data.get(
            ATTR_ROUTE_GTFS_ID
        )
        if gtfs_id:
            data[ATTR_ROUTE_GTFS_ID] = str(gtfs_id)

        # Names (and every other key) are copied verbatim: entity unique ids
        # keep pointing at the same entities.
        self.hass.config_entries.async_update_entry(entry, data=data)
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data={})

    async def _async_resolve(self, entry):
        """Re-run the connector's resolution against the live API."""
        try:
            connector = _connector_from_entry(self.hass, entry)
        except KeyError:
            return {
                "resolved": False,
                "what": self._what,
                "candidate": {},
                "reason": "the config entry is missing required settings",
            }
        connector.last_diagnosis = {"what": self._what}
        try:
            return await connector.resolve_candidate()
        except PTVApiError as err:
            _LOGGER.debug("PTV: repair re-verification failed: %s", err)
            return {
                "resolved": False,
                "what": self._what,
                "candidate": {},
                "reason": "the timetable could not be checked",
            }


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the fix flow for a stale-ids issue."""
    return StaleIdsRepairFlow(data or {})
