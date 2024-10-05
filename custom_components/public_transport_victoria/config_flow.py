"""Config flow for Public Transport Victoria integration."""
import logging

import voluptuous as vol

from homeassistant import config_entries, exceptions
from homeassistant.const import CONF_API_KEY, CONF_ID

from .const import (
    CONF_DIRECTION, 
    CONF_DIRECTION_NAME, 
    CONF_ROUTE, 
    CONF_ROUTE_NAME,
    CONF_ROUTE_TYPE, 
    CONF_ROUTE_TYPE_NAME, 
    CONF_STOP, 
    CONF_STOP_NAME, 
    DOMAIN
)
from .PublicTransportVictoria.public_transport_victoria import Connector

_LOGGER = logging.getLogger(__name__)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Public Transport Victoria."""

    VERSION = 1

    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        # Initialize self.data if it doesn't exist
        if not hasattr(self, "data"):
            self.data = {}
        _LOGGER.debug("Initialized self.data: %s", self.data)

        # Check if there is already a config entry for this integration
        existing_entries = self._async_current_entries()
        if existing_entries:
            _LOGGER.debug("Existing entry found, using existing credentials.")
            entry = existing_entries[0]
            _LOGGER.debug("Existing entry data: %s", entry.data)

            # Copy id and api_key to self.data so it persists across steps
            self.data[CONF_ID] = entry.data[CONF_ID]
            self.data[CONF_API_KEY] = entry.data[CONF_API_KEY]
            _LOGGER.debug("Carried over API key and ID into self.data: %s", self.data)

            self.connector = Connector(
                self.hass, entry.data[CONF_ID], entry.data[CONF_API_KEY]
            )
            self.route_types = await self.connector.async_route_types()
            return await self.async_step_route_types()

        # If no existing entry, prompt user for API key and ID
        data_schema = vol.Schema(
            {
                vol.Required(CONF_ID): str,
                vol.Required(CONF_API_KEY): str,
            }
        )

        errors = {}
        if user_input is not None:
            try:
                _LOGGER.debug("Received user input: %s", user_input)
                # Initialize connector to validate API key and fetch route types
                self.connector = Connector(
                    self.hass, user_input[CONF_ID], user_input[CONF_API_KEY]
                )
                self.route_types = await self.connector.async_route_types()

                if not self.route_types:
                    raise CannotConnect

                # Store the API key and ID in self.data for use in subsequent steps
                self.data[CONF_ID] = user_input[CONF_ID]
                self.data[CONF_API_KEY] = user_input[CONF_API_KEY]
                _LOGGER.debug("Stored API key and ID in self.data: %s", self.data)

                return await self.async_step_route_types()

            except CannotConnect:
                _LOGGER.error("Cannot connect to Public Transport Victoria API.")
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        # Show the form to input the API ID and Key
        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    async def async_step_route_types(self, user_input=None):
        """Handle the route types step."""
        data_schema = vol.Schema({
            vol.Required(CONF_ROUTE_TYPE, default=0): vol.In(self.route_types),
        })

        errors = {}
        if user_input is not None:
            try:
                self.routes = await self.connector.async_routes(
                    user_input[CONF_ROUTE_TYPE]
                )

                self.data[CONF_ROUTE_TYPE] = user_input[CONF_ROUTE_TYPE]
                self.data[CONF_ROUTE_TYPE_NAME] = self.route_types[user_input[CONF_ROUTE_TYPE]]

                return await self.async_step_routes()

            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        # If there is no user input or there were errors, show the form again.
        return self.async_show_form(
            step_id="route_types", data_schema=data_schema, errors=errors
        )

    async def async_step_routes(self, user_input=None):
        """Handle the route types step."""
        data_schema = vol.Schema({
            vol.Required(CONF_ROUTE, default=next(iter(self.routes))): vol.In(self.routes),
        })

        errors = {}
        if user_input is not None:
            try:
                self.directions = await self.connector.async_directions(
                    user_input[CONF_ROUTE]
                )

                self.data[CONF_ROUTE] = user_input[CONF_ROUTE]
                self.data[CONF_ROUTE_NAME] = self.routes[user_input[CONF_ROUTE]]

                return await self.async_step_directions()

            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        # If there is no user input or there were errors, show the form again.
        return self.async_show_form(
            step_id="routes", data_schema=data_schema, errors=errors
        )

    async def async_step_directions(self, user_input=None):
        """Handle the direction types step."""
        data_schema = vol.Schema({
            vol.Required(CONF_DIRECTION, default=next(iter(self.directions))): vol.In(self.directions),
        })

        errors = {}
        if user_input is not None:
            try:
                self.stops = await self.connector.async_stops(
                    self.data[CONF_ROUTE]
                )

                self.data[CONF_DIRECTION] = user_input[CONF_DIRECTION]
                self.data[CONF_DIRECTION_NAME] = self.directions[user_input[CONF_DIRECTION]]

                return await self.async_step_stops()

            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        # If there is no user input or there were errors, show the form again.
        return self.async_show_form(
            step_id="directions", data_schema=data_schema, errors=errors
        )

    async def async_step_stops(self, user_input=None):
        """Handle the stops types step."""
        data_schema = vol.Schema({
            vol.Required(CONF_STOP, default=next(iter(self.stops))): vol.In(self.stops),
        })

        errors = {}
        if user_input is not None:
            try:
                self.data[CONF_STOP] = user_input[CONF_STOP]
                self.data[CONF_STOP_NAME] = self.stops[user_input[CONF_STOP]]

                title = "{} line to {} from {}".format(
                    self.data[CONF_ROUTE_NAME],
                    self.data[CONF_DIRECTION_NAME],
                    self.data[CONF_STOP_NAME]
                )

                return self.async_create_entry(title=title, data=self.data)

            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        # If there is no user input or there were errors, show the form again.
        return self.async_show_form(
            step_id="stops", data_schema=data_schema, errors=errors
        )

class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""
