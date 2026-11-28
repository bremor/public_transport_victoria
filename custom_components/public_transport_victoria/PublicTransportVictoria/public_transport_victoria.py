"""Public Transport Victoria API connector."""
import aiohttp
import asyncio
import datetime
import hmac
import logging
import time
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

# Live probing showed the departures endpoint answers HTTP 200 with an empty
# list for stale direction, route and stop ids alike (issue #50), so an empty
# poll alone cannot tell "no service right now" from "stale configured ids".
# When a poll keeps no rows, one un-filtered departures probe (and, only if
# that is empty too, one tiny directions call) separates the two cases.
EMPTY_DEPARTURES_PATH = (
    "/v3/departures/route_type/{}/stop/{}/route/{}?max_results=1"
)

# Persistent empties (for example an overnight quiet period) must not repeat
# the diagnosis on every poll; a non-empty poll re-arms it.
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
        # When the last empty-result diagnosis was logged (monotonic seconds),
        # or None when a non-empty poll re-armed the diagnosis.
        self._last_empty_diag = None
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

    def _log_empty_diag(self, level, message, *args):
        """Emit one empty-result diagnosis and start the cooldown."""
        _LOGGER.log(level, message, *args)
        self._last_empty_diag = time.monotonic()

    async def _async_diagnose_empty(self, had_rows):
        """Explain an empty departure list with at most two cheap probes.

        Probe A drops the direction filter: rows coming back mean the
        configured direction id is the likely culprit.  Probe B asks the tiny
        directions endpoint whether the configured route still exists at all.
        Both probes are only issued on an empty poll, at most once per
        :data:`EMPTY_DIAG_COOLDOWN`, and their own failures are downgraded to
        debug logs so a diagnosis never fails the update.  Logs carry ids and
        counts only, never payloads or signed URLs.
        """
        now = time.monotonic()
        if (
            self._last_empty_diag is not None
            and now - self._last_empty_diag
            < EMPTY_DIAG_COOLDOWN.total_seconds()
        ):
            # Already diagnosed during this empty spell; stay quiet.
            return

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
        except (PTVAuthError, PTVConnectionError, PTVServiceError) as err:
            _LOGGER.debug("PTV: empty-result departure probe failed: %s", err)
            return

        if probe_rows:
            configured_route = None if self.route is None else str(self.route)
            if (
                had_rows
                and configured_route is not None
                and not any(
                    str(row.get("route_id")) == configured_route
                    for row in probe_rows
                )
            ):
                self._log_empty_diag(
                    logging.WARNING,
                    "PTV: departures exist for this stop but none match the "
                    "configured route id (through-running or stale route id) "
                    "- re-add the config entry naming route %s",
                    self.route,
                )
                return
            self._log_empty_diag(
                logging.WARNING,
                "PTV: departures are empty for route %s direction %s stop "
                "%s, but the route has departures without the direction "
                "filter - the direction id looks stale; re-add the config "
                "entry",
                self.route,
                self.direction,
                self.stop,
            )
            return

        try:
            response = await self._async_get_json(
                build_URL(
                    self.id, self.api_key, DIRECTIONS_PATH.format(self.route)
                ),
                "directions route check",
            )
            route_directions = _require_list(
                response, "directions", "directions route check"
            )
        except (PTVAuthError, PTVConnectionError, PTVServiceError) as err:
            _LOGGER.debug("PTV: empty-result route check failed: %s", err)
            return

        if not route_directions:
            self._log_empty_diag(
                logging.WARNING,
                "PTV: departures are empty for route %s direction %s stop "
                "%s and the route has no directions - the route id looks "
                "stale; re-add the config entry",
                self.route,
                self.direction,
                self.stop,
            )
            return

        self._log_empty_diag(
            logging.INFO,
            "PTV: no departures currently scheduled for route %s stop %s "
            "direction %s (probe A returned nothing) - may be a quiet period",
            self.route,
            self.stop,
            self.direction,
        )

    async def async_update(self):
        """Update the departure information.

        The new list is built locally and assigned only once it is complete, so
        a failure keeps the previous departures intact.  Failures raise a typed
        :class:`PTVApiError` subclasses instead of being silently ignored.
        """
        url = build_URL(self.id, self.api_key, self.departures_path)

        response = await self._async_get_json(url, "departures")
        rows = _require_list(response, "departures", "departures")
        _LOGGER.debug("Fetched departures: %d", len(rows))

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

        if not kept_rows:
            # An empty poll is ambiguous (no service vs stale ids).  Diagnose
            # it with bounded probes; a non-empty poll re-arms the diagnosis.
            await self._async_diagnose_empty(bool(rows))
        else:
            self._last_empty_diag = None
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
