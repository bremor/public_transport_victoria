import async_timeout
import asyncio
import datetime
import hmac
import json
import logging
import requests

from hashlib import sha1
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import Throttle

_LOGGER = logging.getLogger(__name__)
DEFAULT_TIMEOUT = 10
MIN_TIME_BETWEEN_UPDATES = datetime.timedelta(minutes=1)
URL = "https://timetableapi.ptv.vic.gov.au"
URI_DEPARTURES = "/v3/departures/route_type/{}/stop/{}?max_results={}"
URI_DIRECTIONS = "/v3/directions/{}"
URI_ROUTES = "/v3/routes/{}"
URI_STOPS = "/v3/stops/{}/route_type/{}"

class PyPublicTransportVictoria():

    def __init__(self, hass, direction_id, id, key, max_results, route_type, stop_id):
        self._data = None
        self._hass = hass
        self._direction_id = direction_id
        self._direction_names = {}
        self._id = id
        self._key = key
        self._max_results = max_results
        self._route_names = {}
        self._route_type = route_type
        self._stop_id = stop_id
        self._stop_name = None

        s = URI_DEPARTURES.format(self._route_type, self._stop_id, self._max_results)
        if self._direction_id:
            s = s + "&direction_id={}".format(self._direction_id)
        self._url_departures = self.build_URL(s)
        _LOGGER.error(self._url_departures)

    def get_state(self, index):
        if self._data:
            scheduled_departure_utc = self._data['departures'][index]['scheduled_departure_utc']
            return self.convert_utc_to_local(scheduled_departure_utc)

    def get_direction_name(self, index):
        if self._data:
            direction_id = self._data['departures'][index]['direction_id']
            if direction_id in self._direction_names:
                return self._direction_names[direction_id]

    def get_estimated_departure_utc(self, index):
        if self._data:
            return self._data['departures'][index]['estimated_departure_utc']

    def get_platform_number(self, index):
        if self._data:
            return self._data['departures'][index]['platform_number']

    def get_route_name(self, index):
        if self._data:
            route_id = self._data['departures'][index]['route_id']
            if route_id in self._route_names:
                return self._route_names[route_id]

    def get_scheduled_departure_utc(self, index):
        if self._data:
            return self._data['departures'][index]['scheduled_departure_utc']

    def get_stop_name(self):
        return self._stop_name

    def build_URL(self, request):
        request = request + ('&' if ('?' in request) else '?')
        raw = request + 'devid={}'.format(self._id)
        hashed = hmac.new(self._key.encode('utf-8'), raw.encode('utf-8'), sha1)
        signature = hashed.hexdigest()
        return URL + raw + '&signature={}'.format(signature)

    def convert_utc_to_local(self, utc):
        d = datetime.datetime.strptime(utc, '%Y-%m-%dT%H:%M:%SZ')
        d = d.replace(tzinfo=datetime.timezone.utc)
        d = d.astimezone()
        return d.strftime('%I:%M %p')

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    async def async_get_data(self):
        session = async_get_clientsession(self._hass)
        with async_timeout.timeout(DEFAULT_TIMEOUT):
            response = await session.get(self._url_departures)
        self._data = await response.json()

        for index in range(0, self._max_results):
            route_id = self._data['departures'][index]['route_id']
            if route_id not in self._route_names:
                s = URI_ROUTES.format(route_id)
                url_routes = self.build_URL(s) 
                with async_timeout.timeout(DEFAULT_TIMEOUT):
                    response = await session.get(url_routes)
                data = await response.json()
                self._route_names[route_id] = data['route']['route_name']

            direction_id = self._data['departures'][index]['direction_id']
            if direction_id not in self._direction_names:
                s = URI_DIRECTIONS.format(direction_id)
                url_directions = self.build_URL(s) 
                with async_timeout.timeout(DEFAULT_TIMEOUT):
                    response = await session.get(url_directions)
                data = await response.json()
                self._direction_names[direction_id] = data['directions'][0]['direction_name']

        if self._stop_name is None:
            s = URI_STOPS.format(self._stop_id, self._route_type)
            url_stops = self.build_URL(s) 
            with async_timeout.timeout(DEFAULT_TIMEOUT):
                response = await session.get(url_stops)
            data = await response.json()
            self._stop_name = data['stop']['stop_name']
            _LOGGER.error(self._stop_name)

