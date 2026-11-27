"""Redaction helpers for Public Transport Victoria debug logging.

The PTV Timetable API authenticates every request with a developer id
(``devid``) and an HMAC-SHA1 ``signature`` derived from the user's private
API key.  Home Assistant users routinely attach debug logs to bug reports,
so neither the signed URL nor the config-entry payload (which holds the
``api_key`` and developer ``id``) may be written to the log verbatim.

This module contains just enough logic to make logging safe:

* :func:`sanitise_request` keeps only the request path, dropping the query
  string that carries ``devid`` and ``signature``.
* :func:`redact_config` returns a copy of a mapping with credential-looking
  values replaced by a length marker.

Nothing here changes how requests are built or signed; it is used only to
produce log-safe strings.  Standard library only, no imports.
"""

SENSITIVE_KEY_PARTS = ("api", "key", "id", "signature", "token")


def _redact_value(value):
    """Return a placeholder that records the value's length but not itself."""
    try:
        length = len(value)
    except TypeError:
        length = len(str(value))
    return "<redacted,len{}>".format(length)


def _is_sensitive(key):
    """Return True when a mapping key looks like it holds a credential."""
    lowered = str(key).lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def sanitise_request(request):
    """Return the path part of ``request`` with any query string removed.

    ``"/v3/departures/route_type/0/stop/1?direction_id=3&max_results=5"``
    becomes ``"/v3/departures/route_type/0/stop/1"``.  The query string is
    where ``devid`` and ``signature`` live, so dropping it makes the value
    safe to log.
    """
    if not isinstance(request, str):
        return "<redacted>"
    path = request.split("?", 1)[0]
    return path.split("#", 1)[0]


def redact_config(data):
    """Return a log-safe copy of ``data`` with credential values hidden.

    Values whose keys contain "api", "key", "id", "signature" or "token"
    (case-insensitive) are replaced with ``"<redacted,lenN>"`` where ``N`` is
    the original length.  Every other key passes through unchanged.  The
    input mapping is never mutated.
    """
    if not isinstance(data, dict):
        return {}

    redacted = {}
    for key, value in data.items():
        if _is_sensitive(key):
            redacted[key] = _redact_value(value)
        elif isinstance(value, dict):
            redacted[key] = redact_config(value)
        elif isinstance(value, (list, tuple)):
            redacted[key] = [
                redact_config(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            redacted[key] = value
    return redacted
