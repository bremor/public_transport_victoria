"""Config flow for Public Transport Victoria integration."""
import logging

import voluptuous as vol

from homeassistant import config_entries
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
from .PublicTransportVictoria.public_transport_victoria import (
    Connector,
    PTVAuthError,
    PTVConnectionError,
    PTVServiceError,
)
from .PublicTransportVictoria.redact import redact_config

_LOGGER = logging.getLogger(__name__)

# Every API failure the flow can expect; anything else is an "unknown" error.
API_ERRORS = (PTVAuthError, PTVConnectionError, PTVServiceError)


def _api_error_key(err):
    """Map a typed connector error to a config-flow error key."""
    if isinstance(err, PTVAuthError):
        return "invalid_auth"
    if isinstance(err, PTVConnectionError):
        return "cannot_connect"
    return "service_error"


def _default_option(options, preferred=None):
    """Return a string default that is guaranteed to be one of ``options``."""
    if not options:
        # Callers must abort the flow before building a schema from no options.
        raise ValueError("cannot choose a default from an empty option set")
    if preferred is not None and preferred in options:
        return preferred
    return next(iter(options))


def _selection_schema(key, options, preferred=None):
    """Build a dropdown schema whose values are strings end to end.

    ``vol.Coerce`` keeps a client that submits an integer (the historical
    default behaviour behind issue #40) valid, while every stored value is a
    string, matching the connector's string-keyed option mappings.
    """
    return vol.Schema(
        {
            vol.Required(
                key, default=_default_option(options, preferred)
            ): vol.All(vol.Coerce(str), vol.In(options)),
        }
    )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Public Transport Victoria."""

    VERSION = 1

    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        # Initialize self.data if it doesn't exist
        if not hasattr(self, "data"):
            self.data = {}
        _LOGGER.debug("Entering step user")

        errors = {}

        # Check if there is already a config entry for this integration
        existing_entries = self._async_current_entries()
        if existing_entries:
            _LOGGER.debug("Existing entry found, using existing credentials.")
            entry = existing_entries[0]
            _LOGGER.debug("Existing entry data: %s", redact_config(entry.data))

            # Copy id and api_key to self.data so it persists across steps
            self.data[CONF_ID] = entry.data[CONF_ID]
            self.data[CONF_API_KEY] = entry.data[CONF_API_KEY]

            self.connector = Connector(
                self.hass, entry.data[CONF_ID], entry.data[CONF_API_KEY]
            )
            try:
                self.route_types = await self.connector.async_route_types()
            except API_ERRORS as err:
                _LOGGER.warning(
                    "Could not load route types using the stored credentials: %s",
                    err,
                )
                errors["base"] = _api_error_key(err)
            else:
                return await self.async_step_route_types()

            # Fall through to the credentials form so the user can correct the
            # stored credentials instead of getting an opaque failure.

        # If no existing entry, prompt user for API key and ID
        data_schema = vol.Schema(
            {
                vol.Required(CONF_ID): str,
                vol.Required(CONF_API_KEY): str,
            }
        )

        if user_input is not None:
            try:
                _LOGGER.debug("Received user input: %s", redact_config(user_input))
                # Initialize connector to validate API key and fetch route types
                self.connector = Connector(
                    self.hass, user_input[CONF_ID], user_input[CONF_API_KEY]
                )
                self.route_types = await self.connector.async_route_types()

                if not self.route_types:
                    errors["base"] = "no_options"
                else:
                    # Store the API key and ID in self.data for later steps
                    self.data[CONF_ID] = user_input[CONF_ID]
                    self.data[CONF_API_KEY] = user_input[CONF_API_KEY]
                    _LOGGER.debug(
                        "Stored API key and ID in self.data: %s",
                        redact_config(self.data),
                    )

                    return await self.async_step_route_types()

            except API_ERRORS as err:
                _LOGGER.warning("Could not load route types: %s", err)
                errors["base"] = _api_error_key(err)
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        # Show the form to input the API ID and Key
        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    async def async_step_route_types(self, user_input=None):
        """Handle the route types step."""
        if not self.route_types:
            _LOGGER.warning("Public Transport Victoria returned no route types")
            return self.async_abort(reason="no_options")

        data_schema = _selection_schema(
            CONF_ROUTE_TYPE, self.route_types, preferred="0"
        )

        errors = {}
        if user_input is not None:
            route_type = user_input[CONF_ROUTE_TYPE]
            try:
                self.routes = await self.connector.async_routes(route_type)
            except API_ERRORS as err:
                _LOGGER.warning("Could not load routes: %s", err)
                errors["base"] = _api_error_key(err)
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                self.data[CONF_ROUTE_TYPE] = route_type
                self.data[CONF_ROUTE_TYPE_NAME] = self.route_types[route_type]

                return await self.async_step_routes()

        # If there is no user input or there were errors, show the form again.
        return self.async_show_form(
            step_id="route_types", data_schema=data_schema, errors=errors
        )

    async def async_step_routes(self, user_input=None):
        """Handle the routes step."""
        if not self.routes:
            _LOGGER.warning("Public Transport Victoria returned no routes")
            return self.async_abort(reason="no_options")

        data_schema = _selection_schema(CONF_ROUTE, self.routes)

        errors = {}
        if user_input is not None:
            route = user_input[CONF_ROUTE]
            try:
                self.directions = await self.connector.async_directions(route)
            except API_ERRORS as err:
                _LOGGER.warning("Could not load directions: %s", err)
                errors["base"] = _api_error_key(err)
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                self.data[CONF_ROUTE] = route
                self.data[CONF_ROUTE_NAME] = self.routes[route]

                return await self.async_step_directions()

        # If there is no user input or there were errors, show the form again.
        return self.async_show_form(
            step_id="routes", data_schema=data_schema, errors=errors
        )

    async def async_step_directions(self, user_input=None):
        """Handle the directions step."""
        if not self.directions:
            _LOGGER.warning("Public Transport Victoria returned no directions")
            return self.async_abort(reason="no_options")

        data_schema = _selection_schema(CONF_DIRECTION, self.directions)

        errors = {}
        if user_input is not None:
            direction = user_input[CONF_DIRECTION]
            try:
                self.stops = await self.connector.async_stops(
                    self.data[CONF_ROUTE]
                )
            except API_ERRORS as err:
                _LOGGER.warning("Could not load stops: %s", err)
                errors["base"] = _api_error_key(err)
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                self.data[CONF_DIRECTION] = direction
                self.data[CONF_DIRECTION_NAME] = self.directions[direction]

                return await self.async_step_stops()

        # If there is no user input or there were errors, show the form again.
        return self.async_show_form(
            step_id="directions", data_schema=data_schema, errors=errors
        )

    async def async_step_stops(self, user_input=None):
        """Handle the stops step."""
        if not self.stops:
            _LOGGER.warning("Public Transport Victoria returned no stops")
            return self.async_abort(reason="no_options")

        data_schema = _selection_schema(CONF_STOP, self.stops)

        errors = {}
        if user_input is not None:
            stop = user_input[CONF_STOP]
            self.data[CONF_STOP] = stop
            self.data[CONF_STOP_NAME] = self.stops[stop]

            title = "{} line to {} from {}".format(
                self.data[CONF_ROUTE_NAME],
                self.data[CONF_DIRECTION_NAME],
                self.data[CONF_STOP_NAME]
            )

            return self.async_create_entry(title=title, data=self.data)

        # If there is no user input, show the form.
        return self.async_show_form(
            step_id="stops", data_schema=data_schema, errors=errors
        )
