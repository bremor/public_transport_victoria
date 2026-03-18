"""Public Transport Victoria API connector."""
import aiohttp
import datetime
import hmac
import logging
from hashlib import sha1

from homeassistant.util import Throttle
from homeassistant.util.dt import get_time_zone


class InvalidAuth(Exception):
    """Raised when the PTV API rejects the Developer ID or Key."""


class CannotConnect(Exception):
    """Raised when the PTV API cannot be reached."""


BASE_URL = "https://timetableapi.ptv.vic.gov.au"
# Departure paths — most specific to least specific
DEPARTURES_PATH = "/v3/departures/route_type/{}/stop/{}/route/{}?direction_id={}&max_results={}"
DEPARTURES_ROUTE_STOP_PATH = "/v3/departures/route_type/{}/stop/{}/route/{}?max_results={}"
DEPARTURES_STOP_PATH = "/v3/departures/route_type/{}/stop/{}?max_results={}"
# Appended to runtime departures paths to get run/vehicle data inline,
# eliminating the need for separate per-departure run API calls.
_DEPARTURES_EXPAND = "&expand=Run&expand=VehicleDescriptor&expand=VehiclePosition"
DIRECTIONS_PATH = "/v3/directions/route/{}"
DISRUPTIONS_PATH = "/v3/disruptions/route/{}"
MIN_TIME_BETWEEN_UPDATES = datetime.timedelta(minutes=2)
MAX_RESULTS = 5
ROUTE_TYPES_PATH = "/v3/route_types"
ROUTES_PATH = "/v3/routes?route_types={}"
SEARCH_PATH = "/v3/search/{}?include_outlets=false&include_addresses=false"
STOP_DETAILS_PATH = "/v3/stops/{}/route_type/{}?stop_location=false&stop_amenities=false&stop_accessibility=false&stop_contact=false&stop_ticket=false&gtfs=false&stop_staffing=false&stop_disruptions=false"
STOPS_PATH = "/v3/stops/route/{}/route_type/{}?direction_id={}"

# Human-readable mode names for stop search results display
_ROUTE_TYPE_NAMES = {
    "0": "Train",
    "1": "Tram",
    "2": "Bus",
    "3": "V/Line",
    "4": "Night Bus",
}

# Maps disruption_type substrings (case-insensitive) to a severity tier.
# First match wins — more specific strings should come first.
_SEVERITY_MAP = [
    ("suspension",       "severe"),
    ("suspended",        "severe"),
    ("replacement bus",  "severe"),
    ("bus replacement",  "severe"),
    ("tram replacement", "severe"),
    ("major disruption", "severe"),
    ("planned works",    "moderate"),
    ("major delay",      "moderate"),
    ("disruption",       "moderate"),
    ("reduced frequency","moderate"),
    ("stop closed",      "minor"),
    ("minor delay",      "minor"),
    ("elevator",         "minor"),
    ("escalator",        "minor"),
    ("information",      "minor"),
]

def _classify_severity(disruption_type: str) -> str:
    """Return 'severe', 'moderate', or 'minor' based on disruption_type text."""
    lower = disruption_type.lower()
    for keyword, severity in _SEVERITY_MAP:
        if keyword in lower:
            return severity
    return "moderate"

_LOGGER = logging.getLogger(__name__)

class Connector:
    """Public Transport Victoria connector."""

    manufacturer = "Public Transport Victoria"

    def __init__(self, hass, id, api_key, route_type=None, route=None,
                 direction=None, stop=None, route_type_name=None,
                 route_name=None, direction_name=None, stop_name=None,
                 filter_express=False):
        """Init Public Transport Victoria connector."""
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
        self.filter_express = filter_express
        self.departures = []
        self.disruptions = []

    async def _init(self):
        """Async Init Public Transport Victoria connector.

        Chooses the most specific departure path available:
        - route + direction → filter by both (smallest result set, fastest)
        - route only        → all departures for that route at this stop
        - neither           → all departures at this stop across every route

        The expand suffix requests Run, VehicleDescriptor and VehiclePosition
        data inline so no separate per-departure run API calls are needed.
        """
        if self.route and self.direction:
            base = DEPARTURES_PATH.format(
                self.route_type, self.stop, self.route, self.direction, MAX_RESULTS
            )
        elif self.route:
            base = DEPARTURES_ROUTE_STOP_PATH.format(
                self.route_type, self.stop, self.route, MAX_RESULTS
            )
        else:
            base = DEPARTURES_STOP_PATH.format(
                self.route_type, self.stop, MAX_RESULTS
            )
        self.departures_path = base + _DEPARTURES_EXPAND
        await self.async_update()

    async def async_route_types(self):
        """Get route types from Public Transport Victoria API.

        Returns a dict of route_type_id -> route_type_name on success.
        Raises InvalidAuth if credentials are rejected by the API.
        Raises CannotConnect on network or unexpected errors.
        """
        url = build_URL(self.id, self.api_key, ROUTE_TYPES_PATH)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 401:
                        raise InvalidAuth("API returned 401 Unauthorized")
                    if response.status != 200:
                        raise CannotConnect(f"API returned status {response.status}")
                    data = await response.json()
                    _LOGGER.debug(data)
                    route_types = {}
                    for r in data.get("route_types", []):
                        route_types[str(r["route_type"])] = r["route_type_name"]
                    if not route_types:
                        raise InvalidAuth("No route types returned — check Developer ID and Key")
                    return route_types
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(str(err)) from err

    async def async_search_stops(self, term, route_type=None):
        """Search for stops matching *term* using the PTV search API.

        Returns a dict of composite_key → display_name suitable for vol.In(),
        plus a parallel dict composite_key → metadata for config entry storage.

        composite_key format: "{stop_id}:{route_type}"
        """
        import urllib.parse

        path = SEARCH_PATH.format(urllib.parse.quote(term, safe=""))
        if route_type is not None:
            path += f"&route_types={route_type}"
        url = build_URL(self.id, self.api_key, path)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        _LOGGER.debug("Stop search returned status %s", response.status)
                        return {}, {}
                    data = await response.json()
                    _LOGGER.debug(data)

                    dropdown = {}   # composite_key → "Stop Name [Mode]"
                    meta = {}       # composite_key → {stop_id, stop_name, route_type, route_type_name}

                    for s in data.get("stops", []):
                        rt = str(s.get("route_type", "0"))
                        key = f"{s['stop_id']}:{rt}"
                        mode = _ROUTE_TYPE_NAMES.get(rt, rt)
                        suburb = s.get("stop_suburb", "")
                        display = f"{s['stop_name']}{', ' + suburb if suburb else ''} [{mode}]"
                        dropdown[key] = display
                        meta[key] = {
                            "stop_id": str(s["stop_id"]),
                            "stop_name": s["stop_name"],
                            "route_type": rt,
                            "route_type_name": _ROUTE_TYPE_NAMES.get(rt, rt),
                        }

                    return dropdown, meta
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Stop search failed: %s", err)
            return {}, {}

    async def async_routes(self, route_type, stop_id=None):
        """Get routes from Public Transport Victoria API.

        When *stop_id* is provided, uses the stop-details endpoint
        (/v3/stops/{stop_id}/route_type/{route_type}) which returns the routes
        array for that stop directly — time-independent and a single API call.
        Falls back to all routes for the mode if that call fails.
        """
        def _sort_key(x):
            v = x[1]
            return v if isinstance(v, tuple) else (0, v)

        def _build_routes_dict(raw_routes):
            route_list = []
            for r in raw_routes:
                route_number = r.get("route_number", "")
                try:
                    num_key = int(route_number) if route_number else float("inf")
                except ValueError:
                    num_key = (1, route_number)
                route_list.append((
                    r["route_id"],
                    num_key,
                    f"{route_number} - {r['route_name']}" if route_number else r["route_name"],
                ))
            route_list.sort(key=_sort_key)
            return {str(rid): name for rid, _, name in route_list}

        self.route_type = route_type

        if stop_id is not None:
            url = build_URL(
                self.id, self.api_key,
                STOP_DETAILS_PATH.format(stop_id, route_type),
            )
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as response:
                        if response.status == 200:
                            data = await response.json()
                            routes = data.get("stop", {}).get("routes", [])
                            if routes:
                                return _build_routes_dict(routes)
            except Exception:
                _LOGGER.debug("Stop details failed for stop %s; falling back to all routes", stop_id)

        # No stop_id, or stop details call failed — return all routes for the mode
        url = build_URL(self.id, self.api_key, ROUTES_PATH.format(route_type))
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return {}
                data = await response.json()
        return _build_routes_dict(data.get("routes", []))

    async def async_directions(self, route):
        """Get directions from Public Transport Victoria API."""
        url = build_URL(self.id, self.api_key, DIRECTIONS_PATH.format(route))

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    _LOGGER.debug(data)
                    directions = {}
                    for r in data["directions"]:
                        directions[str(r["direction_id"])] = r["direction_name"]
                    self.route = route
                    return directions

    async def async_stops(self, route, direction):
        """Get stops from Public Transport Victoria API."""
        url = build_URL(self.id, self.api_key, STOPS_PATH.format(route, self.route_type, direction))

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    _LOGGER.debug(data)
                    stops = {
                        str(r["stop_id"]): r["stop_name"]
                        for r in sorted(data["stops"], key=lambda s: s.get("stop_sequence", 0))
                    }
                    self.route = route
                    return stops

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    async def async_update(self):
        """Update the departure information.

        The departures path includes expand=Run,VehicleDescriptor,VehiclePosition
        so all run and vehicle data arrives inline — no separate per-departure
        API calls needed.  Disruptions are fetched concurrently if a route is set.
        """
        url = build_URL(self.id, self.api_key, self.departures_path)

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return
                data = await response.json()
                _LOGGER.debug(data)

        departures = data.get("departures", [])

        # Expanded data: runs keyed by str(run_id); vehicle dicts may be nested
        # inside each run object OR provided as separate top-level dicts.
        runs_map = {str(k): v for k, v in data.get("runs", {}).items()}
        vd_map   = {str(k): v for k, v in data.get("vehicle_descriptors", {}).items()}
        vp_map   = {str(k): v for k, v in data.get("vehicle_positions", {}).items()}

        # Fetch disruptions concurrently while we process departure data
        if self.route:
            self.disruptions = await self.async_disruptions(self.route)
        else:
            self.disruptions = []

        self.departures = []
        for r in departures:
            effective_utc = r["estimated_departure_utc"] or r["scheduled_departure_utc"]
            r["is_realtime"] = r["estimated_departure_utc"] is not None
            r["departure"] = convert_utc_to_local(effective_utc, self.hass)

            run_key = str(r.get("run_id", ""))
            run_info = runs_map.get(run_key, {})

            r["destination_name"] = run_info.get("destination_name", "")
            r["is_express"] = run_info.get("express_stop_count", 0) > 0

            # Vehicle descriptor: nested in run object takes priority over top-level dict
            vd = run_info.get("vehicle_descriptor") or vd_map.get(run_key) or {}
            r["vehicle_descriptor"] = vd if vd else None

            # Vehicle position: same priority order
            vp = run_info.get("vehicle_position") or vp_map.get(run_key) or {}
            r["vehicle_position"] = vp if vp else None

            self.departures.append(r)

        if self.filter_express:
            self.departures = [d for d in self.departures if d.get("is_express", False)]

        for departure in self.departures:
            _LOGGER.debug(departure)

    async def async_disruptions(self, route_id):
        """Fetch active disruptions for the route from the PTV API.

        Returns a list of disruption dicts with resolved title, description,
        type, severity, url, and date range. Returns [] on any error so a
        failed disruption call never breaks departure updates.
        """
        url = build_URL(self.id, self.api_key, DISRUPTIONS_PATH.format(route_id))

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        _LOGGER.debug("Disruptions API returned %s", response.status)
                        return []
                    data = await response.json()
                    _LOGGER.debug(data)
                    # The PTV disruptions response nests lists under category keys
                    # e.g. {"disruptions": {"metro_train": [...], "general": [...]}}
                    disruptions_raw = data.get("disruptions", {})
                    if isinstance(disruptions_raw, dict):
                        all_disruptions = [
                            item
                            for category in disruptions_raw.values()
                            if isinstance(category, list)
                            for item in category
                        ]
                    else:
                        all_disruptions = disruptions_raw

                    disruptions = []
                    for d in all_disruptions:
                        disruptions.append({
                            "disruption_id": d.get("disruption_id"),
                            "title": d.get("title", ""),
                            "disruption_type": d.get("disruption_type", ""),
                            "disruption_status": d.get("disruption_status", ""),
                            "severity": _classify_severity(d.get("disruption_type", "")),
                            "url": d.get("url", ""),
                            "from_date": d.get("from_date"),
                            "to_date": d.get("to_date"),
                        })
                    return disruptions
        except Exception as err:
            _LOGGER.warning("Failed to fetch disruptions for route %s: %s", route_id, err)
            return []


def minutes_until_departure(utc_str):
    """Return whole minutes from now until the given UTC departure string."""
    departure = datetime.datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc
    )
    delta = departure - datetime.datetime.now(datetime.timezone.utc)
    return max(0, int(delta.total_seconds() // 60))


def build_URL(id, api_key, request):
    request = request + ('&' if ('?' in request) else '?')
    raw = request + 'devid={}'.format(id)
    hashed = hmac.new(api_key.encode('utf-8'), raw.encode('utf-8'), sha1)
    signature = hashed.hexdigest()
    url = BASE_URL + raw + '&signature={}'.format(signature)
    _LOGGER.debug(url)
    return url

def convert_utc_to_local(utc, hass):
    """Convert UTC to Home Assistant local time."""
    d = datetime.datetime.strptime(utc, "%Y-%m-%dT%H:%M:%SZ")
    # Get the Home Assistant configured time zone
    local_tz = get_time_zone(hass.config.time_zone)
    # Convert the time to the Home Assistant time zone
    d = d.replace(tzinfo=datetime.timezone.utc).astimezone(local_tz)
    return d.strftime("%I:%M %p")
