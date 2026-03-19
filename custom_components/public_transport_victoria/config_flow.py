"""Config flow for Public Transport Victoria integration."""
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY, CONF_ID
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_DIRECTION,
    CONF_DIRECTION_NAME,
    CONF_FILTER_EXPRESS,
    CONF_ROUTE,
    CONF_ROUTE_NAME,
    CONF_ROUTE_TYPE,
    CONF_ROUTE_TYPE_NAME,
    CONF_STOP,
    CONF_STOP_NAME,
    DOMAIN,
)
from .PublicTransportVictoria.public_transport_victoria import (
    CannotConnect,
    Connector,
    InvalidAuth,
)

_LOGGER = logging.getLogger(__name__)

# Sentinel used in optional-filter dropdowns to mean "no filter / show everything"
_ALL = "__all__"

_ALL_ROUTES_OPTION = SelectOptionDict(value=_ALL, label="— All routes —")
_ALL_DIRECTIONS_OPTION = SelectOptionDict(value=_ALL, label="— All directions —")


def _select(options: dict[str, str], *, prepend: list[SelectOptionDict] | None = None) -> SelectSelector:
    """Build a searchable SelectSelector from a {value: label} dict."""
    opts: list[SelectOptionDict] = list(prepend or [])
    opts += [SelectOptionDict(value=k, label=v) for k, v in options.items()]
    return SelectSelector(
        SelectSelectorConfig(options=opts, mode=SelectSelectorMode.DROPDOWN)
    )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Public Transport Victoria.

    Setup sequence:
      1. user             — enter Developer ID and Key (skipped when re-adding if creds exist)
      2. route_types      — choose transport mode (Train / Tram / Bus / V/Line …)
      3. stop_search      — type a stop/station name to search within that mode
      4. stop_results     — searchable dropdown of matching stops
      5. filters          — optionally narrow by route; express-only toggle
      6. filter_direction — optionally narrow by direction (shown only when a route is picked)
    """

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return OptionsFlowHandler(config_entry)

    # ------------------------------------------------------------------
    # Step 1 — credentials
    # ------------------------------------------------------------------

    async def async_step_user(self, user_input=None):
        """Handle the initial credentials step.

        If credentials already exist in another entry, reuse them and jump
        straight to mode selection so the user doesn't need to retype their key.
        """
        if not hasattr(self, "data"):
            self.data = {}

        existing = self._async_current_entries()
        if existing:
            entry = existing[0]
            self.data[CONF_ID] = entry.data[CONF_ID]
            self.data[CONF_API_KEY] = entry.data[CONF_API_KEY]
            self.connector = Connector(
                self.hass, entry.data[CONF_ID], entry.data[CONF_API_KEY]
            )
            self._route_types = await self.connector.async_route_types()
            return await self.async_step_route_types()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_ID): str,
                vol.Required(CONF_API_KEY): str,
            }
        )

        errors = {}
        if user_input is not None:
            try:
                self.connector = Connector(
                    self.hass, user_input[CONF_ID], user_input[CONF_API_KEY]
                )
                # Validate credentials and fetch available modes in one call
                self._route_types = await self.connector.async_route_types()

                self.data[CONF_ID] = user_input[CONF_ID]
                self.data[CONF_API_KEY] = user_input[CONF_API_KEY]
                return await self.async_step_route_types()

            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during credential validation")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    # ------------------------------------------------------------------
    # Step 2 — transport mode
    # ------------------------------------------------------------------

    async def async_step_route_types(self, user_input=None):
        """Let the user pick a transport mode before searching for a stop."""
        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_ROUTE_TYPE,
                    default=next(iter(self._route_types)),
                ): _select(self._route_types),
            }
        )

        if user_input is not None:
            rt = user_input[CONF_ROUTE_TYPE]
            self.data[CONF_ROUTE_TYPE] = rt
            self.data[CONF_ROUTE_TYPE_NAME] = self._route_types[rt]
            return await self.async_step_stop_search()

        return self.async_show_form(step_id="route_types", data_schema=data_schema)

    # ------------------------------------------------------------------
    # Step 3 — stop name search
    # ------------------------------------------------------------------

    async def async_step_stop_search(self, user_input=None):
        """Show a text-input form; search PTV on submit and advance to results."""
        data_schema = vol.Schema(
            {
                vol.Required("stop_name"): str,
            }
        )

        errors = {}
        if user_input is not None:
            term = user_input["stop_name"].strip()
            if not term:
                errors["base"] = "search_term_empty"
            else:
                try:
                    dropdown, meta = await self.connector.async_search_stops(
                        term, route_type=self.data[CONF_ROUTE_TYPE]
                    )
                    if not dropdown:
                        errors["base"] = "no_stops_found"
                    else:
                        self._stop_dropdown = dropdown
                        self._stop_meta = meta
                        return await self.async_step_stop_results()
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected exception during stop search")
                    errors["base"] = "unknown"

        return self.async_show_form(
            step_id="stop_search", data_schema=data_schema, errors=errors
        )

    # ------------------------------------------------------------------
    # Step 4 — pick a stop from the search results
    # ------------------------------------------------------------------

    async def async_step_stop_results(self, user_input=None):
        """Let the user pick one stop from the search results (searchable dropdown)."""
        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_STOP, default=next(iter(self._stop_dropdown))
                ): _select(self._stop_dropdown),
            }
        )

        errors = {}
        if user_input is not None:
            key = user_input[CONF_STOP]
            m = self._stop_meta[key]
            self.data[CONF_STOP] = m["stop_id"]
            self.data[CONF_STOP_NAME] = m["stop_name"]
            self.data[CONF_ROUTE_TYPE] = m["route_type"]
            self.data[CONF_ROUTE_TYPE_NAME] = m["route_type_name"]
            return await self.async_step_setup_mode()

        return self.async_show_form(
            step_id="stop_results", data_schema=data_schema, errors=errors
        )

    # ------------------------------------------------------------------
    # Step 4b — choose monitoring mode
    # ------------------------------------------------------------------

    async def async_step_setup_mode(self, user_input=None):
        """Choose between stop-based (all departures) and route-filtered monitoring.

        Stop-based: creates the entry immediately — all routes and directions from
        this stop will appear as departure sensors.

        Route-filtered: proceeds to the route + direction selection steps.
        """
        data_schema = vol.Schema(
            {
                vol.Required("mode", default="all"): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value="all", label="All departures from this stop"),
                            SelectOptionDict(value="route", label="Filter by route and direction"),
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )

        errors = {}
        if user_input is not None:
            if user_input["mode"] == "all":
                return self._create_entry()
            try:
                self._routes = await self.connector.async_routes(
                    self.data[CONF_ROUTE_TYPE], stop_id=self.data[CONF_STOP]
                )
                return await self.async_step_filters()
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception loading routes for filter step")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="setup_mode", data_schema=data_schema, errors=errors
        )

    # ------------------------------------------------------------------
    # Step 5 — optional filters: route + express
    # ------------------------------------------------------------------

    async def async_step_filters(self, user_input=None):
        """Offer optional route filter and express-only toggle."""
        data_schema = vol.Schema(
            {
                vol.Required(CONF_ROUTE, default=_ALL): _select(
                    self._routes, prepend=[_ALL_ROUTES_OPTION]
                ),
                vol.Required(CONF_FILTER_EXPRESS, default=False): bool,
            }
        )

        errors = {}
        if user_input is not None:
            route = user_input[CONF_ROUTE]
            self.data[CONF_FILTER_EXPRESS] = user_input[CONF_FILTER_EXPRESS]

            if route != _ALL:
                self.data[CONF_ROUTE] = route
                self.data[CONF_ROUTE_NAME] = self._routes[route]
                try:
                    self._directions = await self.connector.async_directions(route)
                    return await self.async_step_filter_direction()
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected exception loading directions")
                    errors["base"] = "unknown"
            else:
                return self._create_entry()

        return self.async_show_form(
            step_id="filters", data_schema=data_schema, errors=errors
        )

    # ------------------------------------------------------------------
    # Step 6 — optional direction filter
    # ------------------------------------------------------------------

    async def async_step_filter_direction(self, user_input=None):
        """Optionally narrow departures to a specific direction."""
        data_schema = vol.Schema(
            {
                vol.Required(CONF_DIRECTION, default=_ALL): _select(
                    self._directions, prepend=[_ALL_DIRECTIONS_OPTION]
                ),
            }
        )

        if user_input is not None:
            direction = user_input[CONF_DIRECTION]
            if direction != _ALL:
                self.data[CONF_DIRECTION] = direction
                self.data[CONF_DIRECTION_NAME] = self._directions[direction]
            return self._create_entry()

        return self.async_show_form(
            step_id="filter_direction", data_schema=data_schema
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_entry(self):
        """Build the entry title and create the config entry."""
        stop = self.data[CONF_STOP_NAME]
        route = self.data.get(CONF_ROUTE_NAME, "")
        direction = self.data.get(CONF_DIRECTION_NAME, "")

        if route and direction:
            title = f"{stop} · {route} → {direction}"
        elif route:
            title = f"{stop} · {route}"
        else:
            title = stop

        return self.async_create_entry(title=title, data=self.data)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Allow changing route/direction/express filters without re-adding the entry.

    The stop and route_type are fixed; only the filters can be updated.
    Changes are saved back into config_entry.data so the Connector in
    __init__.py works unchanged.
    """

    def __init__(self, config_entry):
        """Initialise the options flow."""
        self.config_entry = config_entry
        self.data = dict(config_entry.data)
        self.connector = None
        self._routes = {}
        self._directions = {}

    async def async_step_init(self, user_input=None):
        """Bootstrap: create a temporary Connector and load routes."""
        self.connector = Connector(
            self.hass,
            self.data[CONF_ID],
            self.data[CONF_API_KEY],
        )
        self._routes = await self.connector.async_routes(
            self.data[CONF_ROUTE_TYPE], stop_id=self.data.get(CONF_STOP)
        )
        return await self.async_step_filters()

    async def async_step_filters(self, user_input=None):
        """Show the route + express filter form, pre-filled with current values."""
        current_route = self.data.get(CONF_ROUTE, _ALL)
        if current_route not in self._routes:
            current_route = _ALL

        data_schema = vol.Schema(
            {
                vol.Required(CONF_ROUTE, default=current_route): _select(
                    self._routes, prepend=[_ALL_ROUTES_OPTION]
                ),
                vol.Required(
                    CONF_FILTER_EXPRESS,
                    default=self.data.get(CONF_FILTER_EXPRESS, False),
                ): bool,
            }
        )

        errors = {}
        if user_input is not None:
            route = user_input[CONF_ROUTE]
            self.data[CONF_FILTER_EXPRESS] = user_input[CONF_FILTER_EXPRESS]

            # Clear old route/direction values so stale data doesn't linger
            self.data.pop(CONF_ROUTE, None)
            self.data.pop(CONF_ROUTE_NAME, None)
            self.data.pop(CONF_DIRECTION, None)
            self.data.pop(CONF_DIRECTION_NAME, None)

            if route != _ALL:
                self.data[CONF_ROUTE] = route
                self.data[CONF_ROUTE_NAME] = self._routes[route]
                try:
                    self._directions = await self.connector.async_directions(route)
                    return await self.async_step_filter_direction()
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected exception loading directions in options")
                    errors["base"] = "unknown"
            else:
                return self._save_and_finish()

        return self.async_show_form(
            step_id="filters", data_schema=data_schema, errors=errors
        )

    async def async_step_filter_direction(self, user_input=None):
        """Optionally narrow to a direction."""
        current_dir = self.data.get(CONF_DIRECTION, _ALL)
        if current_dir not in self._directions:
            current_dir = _ALL

        data_schema = vol.Schema(
            {
                vol.Required(CONF_DIRECTION, default=current_dir): _select(
                    self._directions, prepend=[_ALL_DIRECTIONS_OPTION]
                ),
            }
        )

        if user_input is not None:
            direction = user_input[CONF_DIRECTION]
            if direction != _ALL:
                self.data[CONF_DIRECTION] = direction
                self.data[CONF_DIRECTION_NAME] = self._directions[direction]
            return self._save_and_finish()

        return self.async_show_form(
            step_id="filter_direction", data_schema=data_schema
        )

    def _save_and_finish(self):
        """Persist updated data back into the config entry and trigger reload."""
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=self.data
        )
        return self.async_create_entry(title="", data={})
