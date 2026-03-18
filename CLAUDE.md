# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Home Assistant custom integration for Public Transport Victoria (PTV). Fetches real-time and scheduled departure data from the PTV API and exposes it as entities grouped under HA devices.

## Development Setup

No build step required — this is a HA custom component. Development workflow:

1. Copy `custom_components/public_transport_victoria/` into your HA `config/custom_components/` directory
2. Restart Home Assistant
3. Add the integration via Settings → Integrations

A Podman compose setup exists for local testing:
```
podman compose up -d   # starts HA stable at http://localhost:8123
```

HACS validation runs via GitHub Actions on every push/PR:
```
.github/workflows/validate.yaml
```

No unit tests exist. To validate, install the integration in a running HA instance.

## Architecture

### Component Layout

```
custom_components/public_transport_victoria/
├── PublicTransportVictoria/
│   └── public_transport_victoria.py   # Connector: HMAC auth + all PTV API calls
├── __init__.py                         # Entry setup/teardown, stores Connector in hass.data
├── config_flow.py                      # Config wizard + OptionsFlowHandler
├── sensor.py                           # DataUpdateCoordinator + departure SensorEntity classes
├── binary_sensor.py                    # (planned) Disruption / is_realtime / is_express entities
├── const.py                            # CONF_* keys, DOMAIN, ATTRIBUTION
└── translations/en.json                # Config flow UI strings
```

### Target Entity Model (Device-per-Entry)

Each config entry maps to one **HA Device** (e.g. *"Upfield line · Moreland Station"*). Entities on that device:

| Entity | Type | Device class | Notes |
|--------|------|-------------|-------|
| `next_departure` … `departure_5` | sensor | `timestamp` | HA renders "in X min" natively |
| `next_platform` | sensor | — | Platform number string |
| `next_minutes_until` | sensor | `duration` | Unit: min, useful for automations/graphs |
| `next_is_express` | binary_sensor | — | True when express_stop_count > 0 |
| `next_is_realtime` | binary_sensor | — | True when estimated_departure_utc present |
| `disrupted` | binary_sensor | `problem` | On = active disruptions on this route |
| `disruption_info` | sensor | — | Disruption title/description text |
| vehicle position | device_tracker | — | Per active run_ref, with lingering |

All entities share the same `DeviceInfo` (keyed on `entry.entry_id`). Debug/ID fields (`run_ref`, `stop_id`, `route_id`, raw UTC times) stay as attributes on the departure sensor rather than becoming entities.

### Data Flow

```
config_flow.py → ConfigEntry {id, api_key, route_type, route, direction, stop}
    ↓
__init__.py → async_setup_entry() → Connector → hass.data[DOMAIN][entry.entry_id]
    ↓
sensor.py → DataUpdateCoordinator (10 min poll)
         → Connector.async_update() → PTV API → connector.departures[]
         → departure sensors + binary sensors + device_tracker all read coordinator.data
```

### Connector (`public_transport_victoria.py`)

- All API calls use HMAC-SHA1 via `build_URL()`
- `async_update()` fetches departures, resolves run info concurrently via `asyncio.gather()`, sets `is_realtime`, `minutes_until`, `is_express` on each departure dict
- Config-flow-only methods: `async_route_types/routes/directions/stops()`
- `@Throttle(MIN_TIME_BETWEEN_UPDATES)` enforces 2-minute minimum between polls
- `InvalidAuth` / `CannotConnect` exceptions defined here and imported by config_flow

### Config Flow (station-first)

`async_step_user` → `async_step_route_types` → `async_step_stop_search` → `async_step_stop_results` → `async_step_filters` → `async_step_filter_direction` (optional)

1. **user** — enter Developer ID and Key; skipped if another entry already has credentials
2. **route_types** — choose transport mode (Train / Tram / Bus / V/Line …); scopes the stop search
3. **stop_search** — type a stop name; searches PTV API scoped to the chosen mode
4. **stop_results** — searchable dropdown of matching stops
5. **filters** — optionally narrow by route; express-only toggle
6. **filter_direction** — optionally narrow by direction (only shown when a specific route is selected)

All dropdowns use `SelectSelector` (HA's native searchable dropdown) so bus routes and long stop lists are filterable by typing.

`OptionsFlowHandler` allows changing route/direction/express filters without deleting the entry.

## Commit Style

- No Claude attribution in commit messages
- No `Co-Authored-By` lines

## Backlog

Priority order for upcoming work:

### 🔴 High

- **Device-per-entry refactor** — each config entry becomes an HA Device; split sensor.py into properly-typed entities (timestamp sensors, binary_sensors for disrupted/is_realtime/is_express, duration sensor for minutes_until); all entities share DeviceInfo keyed on entry_id
- **Resolve disruption IDs** — `disruption_ids` on departures are currently unresolved integers; fetch `/v3/disruptions/{id}` and surface title + description on the departure sensor and the disruption binary_sensor

### 🟡 Medium

- **Stop ordering by sequence** — sort stops by `stop_sequence` from the API response instead of alphabetically
- **Stop-based config mode** — alternative setup path: pick route type → pick stop (no route/direction step); show all departures from that stop across all routes/directions; direction shown as entity attribute/badge rather than a config requirement
- **Vehicle GPS tracking** — `device_tracker` entities keyed by `run_ref` using `vehicle_position` lat/long from the runs API; entities linger for N minutes after a vehicle leaves the departure list so trains don't vanish from the map as they pass the stop
- **Disruptions `binary_sensor`** — `on` when active disruptions exist on the route; `disruption_info` sensor with title/description/URL; feeds off the resolved disruption data above

### 🟢 Lower

- **Journey planner** — multi-leg trip from origin stop to destination; phase 1: show all services from a stop toward a destination across all routes; phase 2: multi-leg interchange matching with configurable interchange time
- **GTFS Schedule for config setup** — download PTV's weekly GTFS Schedule export once during onboarding and use it to drive the stop-search, route, and direction dropdowns instead of making live API calls. Dramatically reduces API usage during setup and allows fully offline initial configuration. https://opendata.transport.vic.gov.au/dataset/gtfs-schedule — dataset contains stops.txt, routes.txt, trips.txt, stop_times.txt, transfers.txt, pathways.txt (accessible stops, platform numbers); published weekly under CC BY 4.0
- **GTFS Realtime feeds** — alternative/supplement to the PTV Timetable API for live updates. Provides: Trip Updates (delays/cancellations), Vehicle Positions (lat/long + congestion level), Service Alerts. Available for Metro Train, Yarra Trams, Bus, V/Line. Protocol Buffer format. https://opendata.transport.vic.gov.au/dataset/gtfs-realtime — could replace `async_update()` for faster/cheaper real-time position data and power the vehicle GPS tracker feature
- **Carriage occupancy** — no capacity/load data in GTFS Realtime (congestion_level field present in the spec but not populated by PTV); no capacity data in GTFS Schedule. May be available via a separate PTV occupancy endpoint if it exists — to investigate
- **GTFS offline/no-API-key mode** — `Connector` becomes an interface with `APIConnector` and `GTFSConnector` implementations; `sensor.py` unchanged; uses GTFS Schedule + Realtime instead of the signed PTV API
- **Stop facilities sensor** — parking, accessibility, lifts from `/v3/stops/{stop_id}/route_type/{route_type}?stop_amenities=true&stop_accessibility=true`
- **Bus route UX** — `SelectSelectorConfig` with search/filter for the hundreds of bus routes
