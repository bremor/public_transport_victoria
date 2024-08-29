"""Public Transport Victoria API connector."""
import aiohttp
import asyncio
import datetime
import hmac
import logging
from hashlib import sha1


from homeassistant.util import Throttle

BASE_URL = "https://timetableapi.ptv.vic.gov.au"
DEPARTURES_PATH = "/v3/departures/route_type/{}/stop/{}/route/{}?direction_id={}&max_results={}"
STOPPING_PATTERNS='/v3/pattern/run/{}/route_type/{}?expand=None&include_skipped_stops=false'
DIRECTIONS_PATH = "/v3/directions/route/{}"
MIN_TIME_BETWEEN_UPDATES = datetime.timedelta(minutes=2)
MAX_RESULTS = 5
ROUTE_TYPES_PATH = "/v3/route_types"
ROUTES_PATH = "/v3/routes?route_types={}"
STOPS_PATH = "/v3/stops/route/{}/route_type/{}"

_LOGGER = logging.getLogger(__name__)

class Connector:
    """Public Transport Victoria connector."""

    manufacturer = "Demonstration Corp"

    def __init__(self, hass, id, api_key, route_type=None, route=None,
                 direction=None, stop=None, destination_stop=None, route_type_name=None,
                 route_name=None, direction_name=None, stop_name=None,destination_stop_name=None):
        """Init Public Transport Victoria connector."""
        self.hass = hass
        self.id = id
        self.api_key = api_key
        self.route_type = route_type
        self.route = route
        self.direction = direction
        self.stop = stop
        self.destination_stop = destination_stop
        self.route_type_name = route_type_name
        self.route_name = route_name
        self.direction_name = direction_name
        self.stop_name = stop_name
        self.destination_stop_name = destination_stop_name

    async def _init(self):
        """Async Init Public Transport Victoria connector."""
        self.departures_path = DEPARTURES_PATH.format(
            self.route_type, self.stop, self.route, self.direction, MAX_RESULTS
        )
        await self.async_update()

    async def async_route_types(self):
        """Get route types from Public Transport Victoria API."""
        url = build_URL(self.id, self.api_key, ROUTE_TYPES_PATH)

        async with aiohttp.ClientSession() as session:
            response = await session.get(url)

        if response is not None and response.status == 200:
            response = await response.json()
            _LOGGER.debug(response)
            route_types = {}
            for r in response["route_types"]:
                route_types[r["route_type"]] = r["route_type_name"]

            return route_types

    async def async_routes(self, route_type):
        """Get routes from Public Transport Victoria API."""
        url = build_URL(self.id, self.api_key, ROUTES_PATH.format(route_type))

        async with aiohttp.ClientSession() as session:
            response = await session.get(url)

        if response is not None and response.status == 200:
            response = await response.json()
            _LOGGER.debug(response)
            routes = {}
            for r in response["routes"]:
                routes[r["route_id"]] = r["route_name"]

            self.route_type = route_type

            return routes

    async def async_directions(self, route):
        """Get directions from Public Transport Victoria API."""
        url = build_URL(self.id, self.api_key, DIRECTIONS_PATH.format(route))

        async with aiohttp.ClientSession() as session:
            response = await session.get(url)

        if response is not None and response.status == 200:
            response = await response.json()
            _LOGGER.debug(response)
            directions = {}
            for r in response["directions"]:
                directions[r["direction_id"]] = r["direction_name"]

            self.route = route

            return directions

    async def async_stops(self, route):
        """Get stops from Public Transport Victoria API."""
        url = build_URL(self.id, self.api_key, STOPS_PATH.format(route, self.route_type))

        async with aiohttp.ClientSession() as session:
            response = await session.get(url)

        if response is not None and response.status == 200:
            response = await response.json()
            _LOGGER.debug(response)
            stops = {}
            for r in response["stops"]:
                stops[r["stop_id"]] = r["stop_name"]

            self.route = route

            return stops

    async def async_stopping_patterns(self,run_ref,stop_id):
        """Get stops from Public Transport Victoria API."""
        url = build_URL(self.id, self.api_key, STOPPING_PATTERNS.format(run_ref, self.route_type))

        async with aiohttp.ClientSession() as session:
            response = await session.get(url)

        if response is not None and response.status == 200:
            response = await response.json()
            _LOGGER.debug(response)
            for r in response["departures"]:
                if stop_id==r['stop_id']:
                    return True
            return False

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    async def async_update(self):
        """Update the departure information."""
        url = build_URL(self.id, self.api_key, self.departures_path)

        async with aiohttp.ClientSession() as session:
            response = await session.get(url)

        if response is not None and response.status == 200:
            response = await response.json()
            _LOGGER.debug(response)
            self.departures = []
            for r in response["departures"]:
                r['stops_at_destinaton']=await self.async_stopping_patterns(r['run_ref'],self.destination_stop)
                r['last_refreshed_at']=datetime.datetime.now()
                if r["estimated_departure_utc"] is not None:
                    r["departure"] = convert_utc_to_local(r["estimated_departure_utc"])
                else:
                    r["departure"] = convert_utc_to_local(r["scheduled_departure_utc"])
                self.departures.append(r)

        for departure in self.departures:
            _LOGGER.debug(departure)

def build_URL(id, api_key, request):
    request = request + ('&' if ('?' in request) else '?')
    raw = request + 'devid={}'.format(id)
    hashed = hmac.new(api_key.encode('utf-8'), raw.encode('utf-8'), sha1)
    signature = hashed.hexdigest()
    url = BASE_URL + raw + '&signature={}'.format(signature)
    _LOGGER.debug(url)
    return url

def convert_utc_to_local(utc):
    d = datetime.datetime.strptime(utc, '%Y-%m-%dT%H:%M:%SZ')
    d = d.replace(tzinfo=datetime.timezone.utc)
    d = d.astimezone()
    return d.strftime('%I:%M %p')
