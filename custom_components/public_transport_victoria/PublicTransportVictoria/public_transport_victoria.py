"""Public Transport Victoria API connector."""
import aiohttp
import asyncio
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
DEPARTURES_PATH = "/v3/departures/route_type/{}/stop/{}/route/{}?direction_id={}&max_results={}"
DIRECTIONS_PATH = "/v3/directions/route/{}"
MIN_TIME_BETWEEN_UPDATES = datetime.timedelta(minutes=2)
MAX_RESULTS = 5
ROUTE_TYPES_PATH = "/v3/route_types"
ROUTES_PATH = "/v3/routes?route_types={}"
STOPS_PATH = "/v3/stops/route/{}/route_type/{}?direction_id={}"

_LOGGER = logging.getLogger(__name__)

class Connector:
    """Public Transport Victoria connector."""

    manufacturer = "Public Transport Victoria"

    def __init__(self, hass, id, api_key, route_type=None, route=None,
                 direction=None, stop=None, route_type_name=None,
                 route_name=None, direction_name=None, stop_name=None):
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
        self.departures = []

    async def _init(self):
        """Async Init Public Transport Victoria connector."""
        self.departures_path = DEPARTURES_PATH.format(
            self.route_type, self.stop, self.route, self.direction, MAX_RESULTS
        )
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
                    # health != 1 means the API rejected the request (bad credentials)
                    if data.get("health") != 1:
                        raise InvalidAuth(
                            f"API health check failed (health={data.get('health')})"
                        )
                    route_types = {}
                    for r in data["route_types"]:
                        route_types[str(r["route_type"])] = r["route_type_name"]
                    if not route_types:
                        raise InvalidAuth("No route types returned — check Developer ID and Key")
                    return route_types
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect(str(err)) from err

    async def async_routes(self, route_type):
        """Get routes from Public Transport Victoria API."""
        url = build_URL(self.id, self.api_key, ROUTES_PATH.format(route_type))

        timeout = aiohttp.ClientTimeout(
            total=60,
            connect=30,
            sock_read=60,
            sock_connect=30
        )
        
        async with aiohttp.ClientSession(timeout=timeout) as session:
            headers = {
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive'
            }
            async with session.get(url, headers=headers) as response:
                if response is not None and response.status == 200:
                    response = await response.json()
                    
                    route_list = []
                    for r in response["routes"]:
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
                else:
                    return {}

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
                        for r in sorted(data["stops"], key=lambda s: s["stop_name"])
                    }
                    self.route = route
                    return stops

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    async def async_update(self):
        """Update the departure information."""
        url = build_URL(self.id, self.api_key, self.departures_path)

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    _LOGGER.debug(data)
                    departures = data["departures"]

                    run_infos = await asyncio.gather(
                        *[self.async_run(r["run_id"]) for r in departures]
                    )

                    self.departures = []
                    for r, run_info in zip(departures, run_infos):
                        effective_utc = r["estimated_departure_utc"] or r["scheduled_departure_utc"]
                        r["is_realtime"] = r["estimated_departure_utc"] is not None
                        r["departure"] = convert_utc_to_local(effective_utc, self.hass)
                        r["minutes_until"] = minutes_until_departure(effective_utc)
                        r["is_express"] = run_info.get("express_stop_count", 0) > 0 if run_info else False
                        self.departures.append(r)

        for departure in self.departures:
            _LOGGER.debug(departure)

    async def async_run(self, run_id):
        """Get run information from Public Transport Victoria API."""
        url = build_URL(self.id, self.api_key, f"/v3/runs/{run_id}")

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    _LOGGER.debug(data)
                    if data.get("runs"):
                        return data["runs"][0]
        return None

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
