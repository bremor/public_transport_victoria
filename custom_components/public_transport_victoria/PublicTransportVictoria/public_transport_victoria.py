"""Public Transport Victoria API connector."""
import aiohttp
import asyncio
import datetime
import hmac
import logging
import re
from hashlib import sha1

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.dt import get_time_zone

from .redact import sanitise_request


BASE_URL = "https://timetableapi.ptv.vic.gov.au"
DEPARTURES_PATH = "/v3/departures/route_type/{}/stop/{}/route/{}?direction_id={}&max_results={}"
DIRECTIONS_PATH = "/v3/directions/route/{}"
MAX_RESULTS = 5
ROUTE_TYPES_PATH = "/v3/route_types"
ROUTES_PATH = "/v3/routes?route_types={}"
STOPS_PATH = "/v3/stops/route/{}/route_type/{}"
# One tiny single-record response, used to backfill the GTFS id of the
# configured route (the route's numeric id is the only durable identity we
# store, but GTFS ids survive id churn).
ROUTE_PATH = "/v3/routes/{}?route_types={}"

# Live probing showed the departures endpoint answers HTTP 200 with an empty
# list for stale direction, route and stop ids alike (issue #50), so an empty
# poll alone cannot tell "no service right now" from "stale configured ids".
# When a poll keeps no rows the coordinator may ask the connector to gather
# evidence: one un-filtered departures probe (and, only if that is empty too,
# one tiny directions call) separates the two cases.
EMPTY_DEPARTURES_PATH = (
    "/v3/departures/route_type/{}/stop/{}/route/{}?max_results=1"
)

# Persistent empties (for example an overnight quiet period) must not repeat
# the diagnosis on every poll.  The coordinator persists this cooldown in the
# config entry, so it survives reloads and restarts.
EMPTY_DIAG_COOLDOWN = datetime.timedelta(hours=6)

# One explicit timeout for every PTV request.  The API is a plain JSON
# service, so a whole request that takes longer than 30 seconds is stalled.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(
    total=30, connect=10, sock_connect=10, sock_read=20
)

_LOGGER = logging.getLogger(__name__)


class PTVApiError(Exception):
    """Base class for Public Transport Victoria API failures."""


class PTVAuthError(PTVApiError):
    """The PTV API rejected the developer ID or API key."""


class PTVConnectionError(PTVApiError):
    """The PTV API could not be reached."""


class PTVServiceError(PTVApiError):
    """The PTV API returned an error status or an unusable response."""


# Captured at import time so tests can replace the module-level ``aiohttp``
# name with a stub without affecting this classification.
_CONNECTION_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, OSError)
_AUTH_STATUSES = (401, 403)


def _check_status(response, operation):
    """Raise a typed error unless the response was a successful HTTP 200."""
    status = getattr(response, "status", None)
    if status == 200:
        return
    if status in _AUTH_STATUSES:
        raise PTVAuthError(
            "PTV API rejected the credentials for {} (HTTP {})".format(
                operation, status
            )
        )
    raise PTVServiceError(
        "PTV API returned HTTP {} for {}".format(status, operation)
    )


async def _read_json(response, operation):
    """Return a JSON object body, raising PTVServiceError when unusable."""
    try:
        # The API may label JSON responses with a non-standard content type.
        payload = await response.json(content_type=None)
    except Exception as err:
        raise PTVServiceError(
            "PTV API returned an unreadable response for {}".format(operation)
        ) from err
    if not isinstance(payload, dict):
        raise PTVServiceError(
            "PTV API returned an unexpected response for {}".format(operation)
        )
    return payload


def _require_list(payload, key, operation):
    """Return ``payload[key]`` as a list or raise PTVServiceError."""
    value = payload.get(key)
    if not isinstance(value, list):
        raise PTVServiceError(
            "PTV API response for {} is missing '{}'".format(operation, key)
        )
    return value


def normalize_name(value):
    """Normalise a PTV name for tolerant, deterministic matching.

    Bus stop names can carry trailing spaces and direction names often carry a
    trailing parenthetical ("Frankston (Station)").  Abbreviations such as
    "St" vs "Street" are deliberately NOT expanded: an ambiguous name must
    abstain rather than guess.
    """
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"\s*\([^()]*\)\s*$", "", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class Connector:
    """Public Transport Victoria connector."""

    manufacturer = "Demonstration Corp"

    def __init__(self, hass, id, api_key, route_type=None, route=None,
                 direction=None, stop=None, route_type_name=None,
                 route_name=None, direction_name=None, stop_name=None,
                 session=None):
        """Init Public Transport Victoria connector.

        ``session`` is a test seam: production callers leave it unset and the
        connector resolves Home Assistant's shared aiohttp session lazily.
        """
        self.hass = hass
        self.id = id
        self.api_key = api_key
        self.route_type = route_type
        self.route = route
        self.direction = direction
        self.stop = stop
        self.route_type_name = route_type_name
        self.route_name = route_name
        self.direction_name = direction_name
        self.stop_name = stop_name
        self._session = session
        self._resolved_session = None
        # Numeric route id is what the config entry stores; the GTFS route id
        # is the id that survives PTV's id churn.  Backfilled lazily (one tiny
        # call) by resolve_candidate so the coordinator can persist it.
        self.gtfs_id = None
        # Evidence captured by every poll for the coordinator's empty-poll
        # policy: how many rows the API returned and how many passed the
        # client-side route filter.
        self.last_result_meta = {"raw_rows": 0, "kept_rows": 0}
        # The last diagnose_empty() verdict, consumed by resolve_candidate().
        self.last_diagnosis = None
        # Distinct route ids seen in the last raw poll (route-mismatch evidence).
        self._last_raw_route_ids = []
        # Always defined so a failing first fetch leaves a usable value.
        self.departures = []

    def _get_session(self):
        """Return the shared aiohttp session, resolving it once.

        ``async_get_clientsession`` is a synchronous callback in the supported
        Home Assistant releases: it returns the shared session, creating it on
        first use.  It must still be called from the event loop.
        """
        if self._resolved_session is None:
            if self._session is not None:
                self._resolved_session = self._session
            else:
                self._resolved_session = async_get_clientsession(self.hass)
        return self._resolved_session

    async def _async_get_json(self, url, operation):
        """GET ``url`` and return its JSON body while the response is open.

        The status is checked and the body is consumed inside the response
        context so the response is never used after it has been released.
        """
        try:
            async with self._get_session().get(
                url, timeout=REQUEST_TIMEOUT
            ) as response:
                _check_status(response, operation)
                return await _read_json(response, operation)
        except _CONNECTION_ERRORS as err:
            raise PTVConnectionError(
                "Could not reach the PTV API for {}".format(operation)
            ) from err

    def prepare(self):
        """Compute the departures request path.

        This performs no network I/O.  The DataUpdateCoordinator owns the only
        initial fetch, so setup cannot leave a second, throttled request behind.
        """
        self.departures_path = DEPARTURES_PATH.format(
            self.route_type, self.stop, self.route, self.direction, MAX_RESULTS
        )

    async def async_route_types(self):
        """Get route types from Public Transport Victoria API."""
        url = build_URL(self.id, self.api_key, ROUTE_TYPES_PATH)

        response = await self._async_get_json(url, "route types")

        route_types = {}
        for r in _require_list(response, "route_types", "route types"):
            route_types[str(r["route_type"])] = r["route_type_name"]

        _LOGGER.debug("Fetched route types: %d", len(route_types))
        return route_types

    async def async_routes(self, route_type):
        """Get routes from Public Transport Victoria API."""
        url = build_URL(self.id, self.api_key, ROUTES_PATH.format(route_type))

        response = await self._async_get_json(url, "routes")

        route_list = []
        for r in _require_list(response, "routes", "routes"):
            route_number = r.get("route_number", "")
            try:
                sort_key = int(route_number) if route_number else float('inf')
            except ValueError:
                sort_key = (1, route_number)

            route_list.append((
                r["route_id"],
                sort_key,
                f"{route_number} - {r['route_name']}" if route_number else r["route_name"]
            ))

        def sort_key(x):
            sort_val = x[1]
            if isinstance(sort_val, tuple):
                return sort_val
            return (0, sort_val)

        route_list.sort(key=sort_key)

        routes = {str(route_id): display_name for route_id, _, display_name in route_list}

        self.route_type = route_type
        return routes

    async def async_directions(self, route):
        """Get directions from Public Transport Victoria API."""
        url = build_URL(self.id, self.api_key, DIRECTIONS_PATH.format(route))

        response = await self._async_get_json(url, "directions")

        directions = {}
        for r in _require_list(response, "directions", "directions"):
            directions[str(r["direction_id"])] = r["direction_name"]

        self.route = route

        _LOGGER.debug("Fetched directions: %d", len(directions))
        return directions

    async def async_stops(self, route):
        """Get stops from Public Transport Victoria API."""
        url = build_URL(self.id, self.api_key, STOPS_PATH.format(route, self.route_type))

        response = await self._async_get_json(url, "stops")

        stops = {}
        for r in _require_list(response, "stops", "stops"):
            stops[str(r["stop_id"])] = r["stop_name"]

        self.route = route

        _LOGGER.debug("Fetched stops: %d", len(stops))
        return stops

    async def _async_route_directions(self, route):
        """Return the raw directions records for ``route`` (no state change)."""
        url = build_URL(self.id, self.api_key, DIRECTIONS_PATH.format(route))
        response = await self._async_get_json(url, "directions check")
        return _require_list(response, "directions", "directions check")

    async def _async_route_stops(self, route):
        """Return the raw stops records for ``route`` (no state change)."""
        url = build_URL(
            self.id, self.api_key, STOPS_PATH.format(route, self.route_type)
        )
        response = await self._async_get_json(url, "route stops check")
        return _require_list(response, "stops", "route stops check")

    async def _async_route_candidates(self):
        """Return every route of this connector's route type."""
        url = build_URL(
            self.id, self.api_key, ROUTES_PATH.format(self.route_type)
        )
        response = await self._async_get_json(url, "routes check")
        return _require_list(response, "routes", "routes check")

    async def _diagnose_pure_empty(self):
        """Run probe A, then probe B, for an empty poll with no raw rows."""
        probe_path = EMPTY_DEPARTURES_PATH.format(
            self.route_type, self.stop, self.route
        )
        try:
            probe = await self._async_get_json(
                build_URL(self.id, self.api_key, probe_path),
                "departures without the direction filter",
            )
            probe_rows = _require_list(
                probe, "departures", "departures without the direction filter"
            )
        except PTVApiError as err:
            _LOGGER.debug("PTV: empty-result departure probe failed: %s", err)
            return {"what": "unknown", "reason": "departure probe failed"}

        if probe_rows:
            return {"what": "direction", "reason": "direction id stale"}

        try:
            route_directions = await self._async_route_directions(self.route)
        except PTVApiError as err:
            _LOGGER.debug("PTV: empty-result route check failed: %s", err)
            return {"what": "route", "reason": "route check failed"}

        if not route_directions:
            return {"what": "route", "reason": "route has no directions"}

        return {"what": "quiet", "reason": "no departures currently scheduled"}

    async def diagnose_empty(self) -> dict:
        """Explain why the last poll kept no departures (evidence only).

        Called only by the coordinator, which owns the quiet-period gate and
        the persisted cooldown.  The returned ``what`` is ``direction``,
        ``route``, ``route_mismatch``, ``quiet``, ``unknown`` or ``healthy``.
        Probe failures are downgraded to debug logs and never raised, so a
        diagnosis can never fail the update.
        """
        meta = self.last_result_meta or {}
        if meta.get("kept_rows", 0):
            result = {"what": "healthy"}
        elif meta.get("raw_rows", 0):
            result = {"what": "route_mismatch"}
        else:
            result = await self._diagnose_pure_empty()
        self.last_diagnosis = result
        return result

    @staticmethod
    def _match_names(target, options):
        """Match ``target`` against ``(id, name)`` pairs, exactly then loosely.

        Returns ``(id, reason)``; ``id`` is None when nothing or more than one
        option matched, and ``reason`` explains why.
        """
        exact = [
            option_id
            for option_id, name in options
            if name is not None and name == target
        ]
        if not exact:
            wanted = normalize_name(target)
            if wanted:
                exact = [
                    option_id
                    for option_id, name in options
                    if normalize_name(name) == wanted
                ]
        unique = list(dict.fromkeys(exact))
        if len(unique) == 1:
            return unique[0], ""
        if not unique:
            return None, "no entry matched the configured name"
        return None, "several entries matched the configured name"

    async def _async_fetch_route_record(self):
        """Return the configured route's single-record response, or None.

        Any typed API failure is downgraded to a debug log: an audit or a GTFS
        backfill must never fail the caller.  The record is verified to carry
        the configured numeric route id.
        """
        if self.route is None:
            return None
        path = ROUTE_PATH.format(self.route, self.route_type)
        try:
            response = await self._async_get_json(
                build_URL(self.id, self.api_key, path), "route check"
            )
        except PTVApiError as err:
            _LOGGER.debug("PTV: route record check failed: %s", err)
            return None
        route = response.get("route")
        if not isinstance(route, dict):
            return None
        return route

    async def _async_backfill_gtfs(self):
        """Capture the configured route's GTFS id with one tiny call."""
        if self.gtfs_id is not None or self.route is None:
            return
        route = await self._async_fetch_route_record()
        if route is None or str(route.get("route_id")) != str(self.route):
            return
        gtfs_id = route.get("route_gtfs_id")
        if gtfs_id:
            self.gtfs_id = str(gtfs_id)

    async def _resolve_direction(self, result):
        """Find the direction id whose name matches the configured one."""
        try:
            directions = await self._async_route_directions(self.route)
        except PTVApiError as err:
            _LOGGER.debug("PTV: direction resolution failed: %s", err)
            result["reason"] = "could not fetch the route's directions"
            return result

        direction_id, reason = self._match_names(
            self.direction_name,
            [
                (str(d.get("direction_id")), d.get("direction_name"))
                for d in directions
            ],
        )
        if direction_id is None:
            result["reason"] = reason
            return result

        candidate = result["candidate"]
        candidate["route"] = None if self.route is None else str(self.route)
        candidate["direction"] = direction_id
        candidate["stop"] = None if self.stop is None else str(self.stop)
        result["resolved"] = True
        result["reason"] = "matched the configured direction name"
        return result

    async def _async_resolve_route_candidate(self):
        """Find a verified replacement route/direction/stop, or abstain.

        Returns ``(candidate, reason)``; ``candidate`` is None when the route
        list could not be fetched, no route matched, the match was ambiguous,
        or the stop/direction names did not verify on the candidate route.
        """
        try:
            routes = await self._async_route_candidates()
        except PTVApiError as err:
            _LOGGER.debug("PTV: route resolution failed: %s", err)
            return None, "could not fetch the route list"

        if self.gtfs_id is not None:
            matches = [
                r for r in routes
                if str(r.get("route_gtfs_id")) == str(self.gtfs_id)
            ]
            basis = "GTFS id {}".format(self.gtfs_id)
        else:
            wanted = normalize_name(self.route_name)
            matches = [
                r for r in routes
                if wanted and normalize_name(r.get("route_name")) == wanted
            ]
            basis = "normalised route name"

        route_ids = list(
            dict.fromkeys(str(r.get("route_id")) for r in matches)
        )
        if len(route_ids) != 1:
            if not route_ids:
                return None, "no route matched by {}".format(basis)
            return None, "several routes matched by {}".format(basis)

        candidate_route = route_ids[0]
        route_record = matches[0]

        try:
            stops = await self._async_route_stops(candidate_route)
            directions = await self._async_route_directions(candidate_route)
        except PTVApiError as err:
            _LOGGER.debug("PTV: candidate route verification failed: %s", err)
            return None, "could not verify the candidate route"

        stop_id, reason = self._match_names(
            self.stop_name,
            [(str(s.get("stop_id")), s.get("stop_name")) for s in stops],
        )
        if stop_id is None:
            return None, (
                "the configured stop name is not unambiguous on the "
                "candidate route: " + reason
            )

        direction_id, reason = self._match_names(
            self.direction_name,
            [
                (str(d.get("direction_id")), d.get("direction_name"))
                for d in directions
            ],
        )
        if direction_id is None:
            return None, "the configured direction name: " + reason

        return (
            {
                "route": candidate_route,
                "direction": direction_id,
                "stop": stop_id,
                "gtfs": route_record.get("route_gtfs_id") or self.gtfs_id,
            },
            "",
        )

    async def _resolve_route(self, result):
        """Fill ``result`` from a verified route/direction/stop candidate."""
        candidate, reason = await self._async_resolve_route_candidate()
        if candidate is None:
            result["reason"] = reason
            return result
        result["candidate"].update(candidate)
        result["resolved"] = True
        result["reason"] = "matched the configured names on route {}".format(
            candidate["route"]
        )
        return result

    async def audit_identity(self) -> dict:
        """Audit the stored ids against PTV's live timetable data.

        Unlike :meth:`resolve_candidate` this needs no empty poll: it is the
        user-initiated "check my ids now" path.  Status is ``healthy`` (all ids
        match), ``changed`` (route alive but direction/stop moved), ``stale``
        (route id/GTFS identity gone but a verified replacement exists) or
        ``abstain`` (API failure or ambiguous names; no fix proposed).  The
        mapping always carries the ids to display, never to write directly.
        """
        stored = {
            "route": None if self.route is None else str(self.route),
            "direction": (
                None if self.direction is None else str(self.direction)
            ),
            "stop": None if self.stop is None else str(self.stop),
            "gtfs": self.gtfs_id,
        }
        result = {
            "status": "abstain",
            "what": "unknown",
            "mapping": dict(stored),
            "reason": "",
        }

        route_record = await self._async_fetch_route_record()
        route_alive = (
            route_record is not None
            and str(route_record.get("route_id")) == stored["route"]
        )
        if route_alive:
            live_gtfs = route_record.get("route_gtfs_id")
            if (
                self.gtfs_id
                and live_gtfs
                and str(live_gtfs) != str(self.gtfs_id)
            ):
                # Same numeric slot, different GTFS identity: the route moved.
                route_alive = False
            elif live_gtfs:
                self.gtfs_id = str(live_gtfs)
                result["mapping"]["gtfs"] = self.gtfs_id

        if not route_alive:
            result["what"] = "route"
            candidate, reason = await self._async_resolve_route_candidate()
            if candidate is None:
                result["reason"] = reason
                return result
            candidate["gtfs"] = candidate.get("gtfs") or self.gtfs_id
            result.update(
                status="stale",
                mapping=candidate,
                reason="the route id has changed",
            )
            return result

        try:
            directions = await self._async_route_directions(self.route)
            stops = await self._async_route_stops(self.route)
        except PTVApiError as err:
            _LOGGER.debug("PTV: identity audit failed: %s", err)
            result["reason"] = "the timetable could not be checked"
            return result

        direction_id, reason = self._match_names(
            self.direction_name,
            [
                (str(d.get("direction_id")), d.get("direction_name"))
                for d in directions
            ],
        )
        if direction_id is None:
            result["what"] = "direction"
            result["reason"] = "the configured direction name: " + reason
            return result

        stop_id, reason = self._match_names(
            self.stop_name,
            [(str(s.get("stop_id")), s.get("stop_name")) for s in stops],
        )
        if stop_id is None:
            result["what"] = "stop"
            result["reason"] = "the configured stop name: " + reason
            return result

        result["mapping"].update(
            {
                "route": stored["route"],
                "direction": direction_id,
                "stop": stop_id,
            }
        )
        if (
            direction_id == stored["direction"]
            and stop_id == stored["stop"]
        ):
            result.update(
                status="healthy", reason="all configured ids match"
            )
        else:
            result.update(
                status="changed",
                what=(
                    "direction"
                    if direction_id != stored["direction"]
                    else "stop"
                ),
                reason="the direction or stop id has changed",
            )
        return result

    async def resolve_candidate(self) -> dict:
        """Describe a transparent replacement for stale configured ids.

        Informational only: the ids are shown in a repair issue and are never
        written back to the config entry.  Any failure or ambiguity returns
        ``resolved`` False with a reason.
        """
        diagnosis = self.last_diagnosis or {}
        what = diagnosis.get("what", "unknown")
        result = {
            "resolved": False,
            "what": what,
            "candidate": {
                "route": None,
                "direction": None,
                "stop": None,
                "gtfs": None,
            },
            "reason": diagnosis.get("reason", ""),
        }
        if what in ("healthy", "quiet"):
            if what == "quiet":
                result["reason"] = "no departures currently scheduled"
            return result
        if what == "unknown":
            return result

        await self._async_backfill_gtfs()
        result["candidate"]["gtfs"] = self.gtfs_id

        if what == "route_mismatch":
            ids = [str(route_id) for route_id in self._last_raw_route_ids]
            if len(ids) <= 3:
                result["reason"] = (
                    "every row was for route id(s) {}".format(
                        ", ".join(ids) if ids else "(unknown)"
                    )
                )
            else:
                result["reason"] = "every row was for one of {} other route ids".format(
                    len(ids)
                )
            return result

        if what == "direction":
            return await self._resolve_direction(result)
        if what == "route":
            return await self._resolve_route(result)
        return result

    async def async_update(self):
        """Update the departure information.

        The new list is built locally and assigned only once it is complete, so
        a failure keeps the previous departures intact.  Failures raise a typed
        :class:`PTVApiError` subclasses instead of being silently ignored.
        """
        url = build_URL(self.id, self.api_key, self.departures_path)

        response = await self._async_get_json(url, "departures")
        rows = _require_list(response, "departures", "departures")
        raw_rows = len(rows)
        _LOGGER.debug("Fetched departures: %d", raw_rows)

        # Only keep rows for the configured route (issue #34).  Live captures
        # show the route-scoped endpoint already filters correctly, but this
        # guards against upstream data quirks and avoids run lookups for rows
        # that would never be shown.  Rows without a route_id have unknown
        # provenance and are dropped.  Direction is deliberately NOT filtered:
        # same-route rows can legitimately carry adjacent direction ids during
        # through-running and terminations, so route-level matching is the
        # approved scope.  IDs are compared as strings because the config flow
        # stores strings while responses may use either type.
        configured_route = None if self.route is None else str(self.route)
        filtered = 0
        kept_rows = []
        for row in rows:
            route_id = row.get("route_id")
            if (
                configured_route is None
                or route_id is None
                or str(route_id) != configured_route
            ):
                filtered += 1
                continue
            kept_rows.append(row)
        if filtered:
            _LOGGER.debug(
                "PTV: filtered %d departure row(s) not matching the configured "
                "route",
                filtered,
            )

        # Evidence for the coordinator's empty-poll policy, plus the route ids
        # that were actually returned (route-mismatch detail).  This is set
        # before normalisation so it reflects what the API sent.
        self._last_raw_route_ids = sorted(
            {
                str(row.get("route_id"))
                for row in rows
                if row.get("route_id") is not None
            }
        )
        self.last_result_meta = {
            "raw_rows": raw_rows,
            "kept_rows": len(kept_rows),
        }
        rows = kept_rows

        # Normalise first: every kept row must have both timestamp attributes,
        # and a row with no usable time at all is dropped rather than aborting
        # the whole update.
        usable = []
        dropped = 0
        for row in rows:
            estimated = row.get("estimated_departure_utc")
            scheduled = row.get("scheduled_departure_utc")
            timestamp = estimated or scheduled
            if not timestamp:
                dropped += 1
                continue
            row["estimated_departure_utc"] = estimated
            row["scheduled_departure_utc"] = scheduled
            usable.append((row, row.get("run_id"), timestamp))

        if dropped:
            _LOGGER.warning(
                "PTV: dropping %d departure row(s) with no timestamps", dropped
            )

        # One run lookup per kept departure (batched lookups are follow-up work).
        run_ids = [run_id for _, run_id, _ in usable if run_id is not None]
        run_infos = await asyncio.gather(
            *[self.async_run(run_id) for run_id in run_ids]
        )
        run_info_by_id = dict(zip(run_ids, run_infos))

        departures = []
        for row, run_id, timestamp in usable:
            run_info = run_info_by_id.get(run_id) if run_id is not None else None
            row["departure"] = convert_utc_to_local(timestamp, self.hass)
            row["is_express"] = (
                run_info.get("express_stop_count", 0) > 0 if run_info else False
            )
            departures.append(row)

        self.departures = departures
        _LOGGER.debug("Processed %d departures", len(self.departures))

    async def async_run(self, run_id):
        """Get run information from Public Transport Victoria API.

        Run information only decorates a departure with express data, so an
        API-level failure returns ``None`` instead of failing the whole update.
        Connection failures still propagate to the caller.
        """
        url = build_URL(self.id, self.api_key, f"/v3/runs/{run_id}")

        try:
            response = await self._async_get_json(url, "run")
        except (PTVAuthError, PTVServiceError) as err:
            _LOGGER.debug("Run lookup unavailable: %s", err)
            return None

        _LOGGER.debug("Fetched run info: found=%s", bool(response.get("runs")))
        if response.get("runs") and len(response["runs"]) > 0:
            return response["runs"][0]
        return None


def build_URL(id, api_key, request):
    request = request + ('&' if ('?' in request) else '?')
    raw = request + 'devid={}'.format(id)
    hashed = hmac.new(api_key.encode('utf-8'), raw.encode('utf-8'), sha1)
    signature = hashed.hexdigest()
    url = BASE_URL + raw + '&signature={}'.format(signature)
    _LOGGER.debug("Requesting PTV API path: %s", sanitise_request(request))
    return url

def convert_utc_to_local(utc, hass):
    """Convert UTC to Home Assistant local time."""
    d = datetime.datetime.strptime(utc, "%Y-%m-%dT%H:%M:%SZ")
    # Get the Home Assistant configured time zone
    local_tz = get_time_zone(hass.config.time_zone)
    # Convert the time to the Home Assistant time zone
    d = d.replace(tzinfo=datetime.timezone.utc).astimezone(local_tz)
    return d.strftime("%I:%M %p")
