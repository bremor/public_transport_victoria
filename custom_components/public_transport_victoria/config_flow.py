"""Config flow for Public Transport Victoria integration."""
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_ID
from homeassistant.core import callback

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
from .repairs import (
    async_apply_stale_id_fix,
    async_run_identity_audit,
    entry_mapping,
    fix_placeholders,
    in_diag_cooldown,
    manual_placeholders,
    mapping_placeholders,
)

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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow for this entry."""
        return OptionsFlowHandler()

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


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Manual timetable-id check for a configured entry.

    The audit is deliberately independent of empty polls: users notice stale
    ids first.  It reuses the connector's ``audit_identity`` and the shared
    verified-apply helper, so fixing ids here is exactly as safe as fixing
    them from the repair issue.
    """

    def __init__(self):
        """Initialise the options flow."""
        self._audit = None
        self._candidate = {}
        self._old_mapping = {}
        super().__init__()

    async def async_step_init(self, user_input=None):
        """Offer the single maintenance action."""
        return self.async_show_menu(
            step_id="init", menu_options=["check_ids"]
        )

    async def async_step_check_ids(self, user_input=None):
        """Audit the stored ids against PTV's live data."""
        entry = self.config_entry
        if (
            entry.state is not config_entries.ConfigEntryState.LOADED
            or getattr(entry, "disabled_by", None) is not None
        ):
            return self.async_abort(reason="entry_not_loaded")

        self._old_mapping = entry_mapping(entry)
        if in_diag_cooldown(entry):
            # Shared cooldown: automatic detection and the previous manual
            # run already probed recently.  Do not touch the API.
            return await self.async_step_cooldown()

        audit = await async_run_identity_audit(self.hass, entry)
        self._audit = audit
        status = audit.get("status")
        mapping = audit.get("mapping") or {}
        if (
            status in ("changed", "stale")
            and mapping.get("route")
            and mapping.get("direction")
            and mapping.get("stop")
        ):
            self._candidate = mapping
            return self.async_show_form(
                step_id="confirm_apply",
                data_schema=vol.Schema({}),
                description_placeholders=fix_placeholders(
                    entry, mapping, audit.get("reason", "")
                ),
            )
        if status == "healthy":
            return await self.async_step_healthy()
        return await self.async_step_manual()

    async def async_step_confirm_apply(self, user_input=None):
        """Show the id mapping, then apply it via the shared helper."""
        entry = self.config_entry
        if user_input is None:
            return self.async_show_form(
                step_id="confirm_apply",
                data_schema=vol.Schema({}),
                description_placeholders=fix_placeholders(
                    entry,
                    self._candidate,
                    (self._audit or {}).get("reason", ""),
                ),
            )

        outcome = await async_apply_stale_id_fix(
            self.hass, entry, self._candidate
        )
        if outcome == "entry_not_found":
            return self.async_abort(reason="entry_not_found")
        if outcome == "entry_not_loaded":
            return self.async_abort(reason="entry_not_loaded")
        if outcome == "failed":
            # The shared helper already left manual instructions behind.
            self._audit = {
                "status": "abstain",
                "what": (self._audit or {}).get("what", "unknown"),
                "mapping": self._candidate,
                "reason": "the timetable changed again while this check was open",
            }
            return await self.async_step_manual()
        if outcome == "unchanged":
            return await self.async_step_healthy()
        return await self.async_step_applied()

    async def async_step_healthy(self, user_input=None):
        """Done step: no id changes were detected."""
        if user_input is not None:
            return self._async_done()
        return self.async_show_form(
            step_id="healthy",
            data_schema=vol.Schema({}),
            description_placeholders=fix_placeholders(
                self.config_entry,
                self._old_mapping,
                (self._audit or {}).get("reason", ""),
            ),
        )

    async def async_step_manual(self, user_input=None):
        """Done step: no safe automatic fix; manual instructions."""
        if user_input is not None:
            return self._async_done()
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({}),
            description_placeholders=manual_placeholders(
                self.config_entry, self._audit
            ),
        )

    async def async_step_applied(self, user_input=None):
        """Done step: verified ids were written and a reload was scheduled."""
        if user_input is not None:
            return self._async_done()
        return self.async_show_form(
            step_id="applied",
            data_schema=vol.Schema({}),
            description_placeholders=mapping_placeholders(
                self._old_mapping,
                self._candidate,
                self.config_entry.data.get(CONF_ROUTE_NAME),
                "verified against PTV",
            ),
        )

    async def async_step_cooldown(self, user_input=None):
        """Done step: an id check already ran inside the shared window."""
        if user_input is not None:
            return self._async_done()
        return self.async_show_form(
            step_id="cooldown",
            data_schema=vol.Schema({}),
            description_placeholders={
                "route_name": self.config_entry.data.get(
                    CONF_ROUTE_NAME, ""
                )
            },
        )

    def _async_done(self):
        """Close the options flow without changing entry options."""
        return self.async_create_entry(
            data=dict(self.config_entry.options)
        )
