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

Policy notes: automatic diagnosis is gated by entry age (24 h) and a persisted
6 h cooldown, and a repair issue is only raised after two consecutive stale
classifications (``diag_streak``) so a genuinely quiet direction does not nag.
The options-flow check is user-initiated: it bypasses the cooldown and counts
toward (and can complete) the same streak.
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
    ATTR_DIAG_NEXT_OK_AT,
    ATTR_DIAG_STREAK,
    ATTR_ROUTE_GTFS_ID,
    CONF_DIRECTION,
    CONF_DIRECTION_NAME,
    CONF_ROUTE,
    CONF_ROUTE_NAME,
    CONF_ROUTE_TYPE,
    CONF_ROUTE_TYPE_NAME,
    CONF_STOP,
    CONF_STOP_NAME,
    DOMAIN,
    STALE_STREAK_THRESHOLD,
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

_WHAT_LABELS = {
    "direction": "direction",
    "route": "route",
    "stop": "stop",
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
    if what == "stop":
        return (
            "The configured stop id no longer matches this stop name on the "
            "route."
        )
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


def entry_mapping(entry):
    """The ids stored in the entry (display only, never written as names)."""
    data = entry.data
    return {
        "route": _text(data.get(CONF_ROUTE)),
        "direction": _text(data.get(CONF_DIRECTION)),
        "stop": _text(data.get(CONF_STOP)),
        "gtfs": _text(data.get(ATTR_ROUTE_GTFS_ID)),
    }


def mapping_placeholders(old, new, route_name, detail):
    """Placeholders for a proposed/aid id mapping (issues and flow steps)."""
    old = old or {}
    new = new or {}
    return {
        "route_name": _text(route_name),
        "old_route_id": _text(old.get("route")),
        "new_route_id": _text(new.get("route")),
        "old_direction_id": _text(old.get("direction")),
        "new_direction_id": _text(new.get("direction")),
        "old_stop_id": _text(old.get("stop")),
        "new_stop_id": _text(new.get("stop")),
        "route_gtfs_id": _text(new.get("gtfs") or old.get("gtfs")),
        "detail": detail or "",
    }


def fix_placeholders(entry, candidate, detail):
    """Placeholders for the fixable issue and its confirm step."""
    return mapping_placeholders(
        entry_mapping(entry),
        candidate,
        entry.data.get(CONF_ROUTE_NAME),
        detail,
    )


def _mapping_complete(mapping):
    """True when a mapping carries every id needed to apply it."""
    mapping = mapping or {}
    return bool(
        mapping.get("route")
        and mapping.get("direction")
        and mapping.get("stop")
    )


def _audit_resolution(audit):
    """Convert an ``audit_identity`` result to the resolve_candidate shape."""
    audit = audit or {}
    mapping = audit.get("mapping") or {}
    return {
        "resolved": audit.get("status") in ("changed", "stale")
        and _mapping_complete(mapping),
        "what": _text(audit.get("what")) or "unknown",
        "candidate": mapping,
        "reason": _text(audit.get("reason")),
    }


def manual_placeholders(entry, audit):
    """Placeholders for the non-fixable (manual instructions) presentation."""
    audit = audit or {}
    resolution = _audit_resolution(audit)
    what = _text(audit.get("what")) or "unknown"
    detail = _detail_text(what, resolution) or _text(audit.get("reason"))
    return {
        "entry_title": str(entry.title),
        "route_name": _text(entry.data.get(CONF_ROUTE_NAME)),
        "what": _WHAT_LABELS.get(what, what),
        "detail": detail,
        "configured": _configured_ids(entry),
        "candidate": _candidate_text(resolution),
        "remedy": REMEDY_TEXT,
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
        placeholders = fix_placeholders(
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


def async_persist_entry_data(hass, entry, updates):
    """Merge ``updates`` into a fresh copy of entry.data and write once."""
    if not updates:
        return
    data = dict(entry.data)
    data.update(updates)
    hass.config_entries.async_update_entry(entry, data=data)


def _entry_ready(entry):
    """True when the entry is loaded and enabled (safe to repair)."""
    return (
        entry is not None
        and entry.state is ConfigEntryState.LOADED
        and getattr(entry, "disabled_by", None) is None
    )


def _next_cooldown_stamp():
    return (dt_util.utcnow() + EMPTY_DIAG_COOLDOWN).isoformat()


def _persisted_streak(entry):
    """Return the persisted consecutive-stale diagnosis counter."""
    try:
        return max(0, int(entry.data.get(ATTR_DIAG_STREAK) or 0))
    except (TypeError, ValueError):
        return 0


async def _async_audit_entry(hass, entry):
    """Build a live connector and audit this entry's stored ids."""
    try:
        connector = _connector_from_entry(hass, entry)
    except KeyError:
        return None, {
            "status": "abstain",
            "what": "unknown",
            "mapping": entry_mapping(entry),
            "reason": "the config entry is missing required settings",
        }
    try:
        audit = await connector.audit_identity()
    except PTVApiError as err:
        _LOGGER.debug("PTV: identity audit failed: %s", err)
        audit = {
            "status": "abstain",
            "what": "unknown",
            "mapping": entry_mapping(entry),
            "reason": "the timetable could not be checked",
        }
    return connector, audit


async def async_run_identity_audit(hass, entry) -> dict:
    """User-initiated id audit: probe, stamp, track the streak, sync issues.

    Manual runs bypass the automatic cooldown (the user asked explicitly) and
    count toward the persisted stale streak.  A run that completes a pending
    streak raises or refreshes the fixable issue; a single stale run is still
    offered as a one-click fix in the options flow without raising the issue
    yet.  An abstain raises the manual issue immediately.  A healthy audit
    creates nothing and does **not** delete an existing issue (only a healthy
    poll clears it).  All state is written in one bounded entry.data write.
    """
    connector, audit = await _async_audit_entry(hass, entry)
    status = audit.get("status")
    actionable = status in ("changed", "stale")
    streak = _persisted_streak(entry) + 1 if actionable else 0

    updates = {
        ATTR_DIAG_NEXT_OK_AT: _next_cooldown_stamp(),
        ATTR_DIAG_STREAK: streak,
    }
    mapping = audit.get("mapping") or {}
    gtfs_id = mapping.get("gtfs") or (
        connector.gtfs_id if connector is not None else None
    )
    if gtfs_id and _text(entry.data.get(ATTR_ROUTE_GTFS_ID)) != _text(gtfs_id):
        updates[ATTR_ROUTE_GTFS_ID] = _text(gtfs_id)

    if actionable and streak >= STALE_STREAK_THRESHOLD:
        async_create_stale_ids_issue(
            hass,
            entry,
            what=audit.get("what", "unknown"),
            resolution=_audit_resolution(audit),
        )
    elif status == "abstain":
        async_create_stale_ids_issue(
            hass,
            entry,
            what=audit.get("what", "unknown"),
            resolution=_audit_resolution(audit),
        )
    async_persist_entry_data(hass, entry, updates)
    return audit


async def async_apply_stale_id_fix(hass, entry, candidate=None) -> str:
    """Re-verify live, apply verified ids, and schedule the reload.

    Shared by the repair issue flow and the options flow, so the apply logic
    exists in exactly one place.  Returns one of ``applied``, ``unchanged``,
    ``failed``, ``entry_not_found`` or ``entry_not_loaded``.

    ``candidate`` is accepted for interface stability (the proposal the user
    saw); the fresh audit is always authoritative.
    """
    if entry is None:
        return "entry_not_found"
    if not _entry_ready(entry):
        return "entry_not_loaded"

    connector, audit = await _async_audit_entry(hass, entry)
    status = audit.get("status")
    mapping = audit.get("mapping") or {}
    new = {
        "route": _text(mapping.get("route")),
        "direction": _text(mapping.get("direction")),
        "stop": _text(mapping.get("stop")),
    }
    stored = {
        "route": _text(entry.data.get(CONF_ROUTE)),
        "direction": _text(entry.data.get(CONF_DIRECTION)),
        "stop": _text(entry.data.get(CONF_STOP)),
    }

    updates = {ATTR_DIAG_NEXT_OK_AT: _next_cooldown_stamp()}
    gtfs_id = mapping.get("gtfs") or (
        connector.gtfs_id if connector is not None else None
    )
    if gtfs_id and _text(entry.data.get(ATTR_ROUTE_GTFS_ID)) != _text(gtfs_id):
        updates[ATTR_ROUTE_GTFS_ID] = _text(gtfs_id)

    if status == "healthy" or (
        status in ("changed", "stale") and new == stored
    ):
        # Verified against the live API and already current: no id write.
        async_persist_entry_data(hass, entry, updates)
        return "unchanged"

    if status not in ("changed", "stale") or not _mapping_complete(mapping):
        # The live timetable no longer supports a confident fix: leave manual
        # instructions behind and do not touch the ids.
        async_create_stale_ids_issue(
            hass,
            entry,
            what=audit.get("what", "unknown"),
            resolution=_audit_resolution(audit),
        )
        async_persist_entry_data(hass, entry, updates)
        return "failed"

    updates.update(
        {
            CONF_ROUTE: new["route"],
            CONF_DIRECTION: new["direction"],
            CONF_STOP: new["stop"],
        }
    )
    # Names (and every other key) are copied verbatim: entity unique ids keep
    # pointing at the same entities.  One write, then the single reload path.
    async_persist_entry_data(hass, entry, updates)
    hass.config_entries.async_schedule_reload(entry.entry_id)
    return "applied"


class StaleIdsRepairFlow(RepairsFlow):
    """Confirm-only flow that applies verified timetable ids."""

    def __init__(self, data):
        """Initialise from the repair issue data."""
        self._entry_id = _text(data.get("entry_id"))
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
        placeholders = fix_placeholders(
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
        """Apply via the single shared verified-apply helper."""
        outcome = await async_apply_stale_id_fix(
            self.hass, entry, self._candidate
        )
        if outcome == "entry_not_found":
            return self.async_abort(reason="entry_not_found")
        if outcome == "entry_not_loaded":
            return self.async_abort(reason="entry_not_loaded")
        if outcome == "failed":
            # The helper replaced the issue with manual instructions; show the
            # form error so the user knows why nothing was written.
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                errors={"base": "verification_failed"},
                description_placeholders=fix_placeholders(
                    entry,
                    {},
                    "the timetable changed again while this fix was open",
                ),
            )
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the fix flow for a stale-ids issue."""
    return StaleIdsRepairFlow(data or {})
