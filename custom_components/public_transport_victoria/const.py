"""Constants for the Public Transport Victoria integration."""

CONF_DIRECTION = "direction"
CONF_DIRECTION_NAME = "direction_name"
CONF_ROUTE = "route"
CONF_ROUTE_NAME = "route_name"
CONF_ROUTE_TYPE = "route_type"
CONF_ROUTE_TYPE_NAME = "route_type_name"
CONF_STOP = "stop"
CONF_STOP_NAME = "stop_name"
DOMAIN = "public_transport_victoria"
ATTRIBUTION = "Licensed from Public Transport Victoria under a Creative Commons Attribution 4.0 International Licence"

# Config-entry data keys written by the diagnosis policy.  Names that back
# entity unique ids are never written.
ATTR_DIAG_NEXT_OK_AT = "diag_next_ok_at"
ATTR_DIAG_STREAK = "diag_streak"
ATTR_FIRST_SEEN_AT = "first_seen_at"
ATTR_ROUTE_GTFS_ID = "route_gtfs_id"

# A stale classification must repeat this many diagnosis windows in a row
# before a repair issue is raised (anti-false-positive for genuinely quiet
# directions).  A manual options-flow check counts toward the streak.
STALE_STREAK_THRESHOLD = 2
