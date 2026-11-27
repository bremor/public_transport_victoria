"""Record sanitised PTV Timetable API fixtures for the regression suite.

The PTV API needs a developer id and an HMAC-SHA1 signature derived from a
private API key, so this script is *never* run by CI and never stores a
signed URL.  Instead it:

1. reads credentials from a env file (``PTV_SECRETS_FILE``, default
   ``/Users/ataylor/devel/ptv/.secrets/ptv.env``),
2. requests a fixed, well-known CBD capture list (Flinders Street,
   Frankston line, tram 96, one bus route -- never the operator's own
   configured home stops),
3. writes only the JSON response bodies to ``tests/fixtures/``,
4. derives the two error-variant fixtures locally, and
5. scrubs every written file, asserting the api key and devid byte strings
   do not appear anywhere.

Only ``path -> status`` lines are printed.  Signed URLs, the api key and the
devid are never printed.

Usage::

    PTV_SECRETS_FILE=/path/to/ptv.env python3 tests/capture_fixtures.py

If the secrets file is missing the script prints ``skipped (no credentials)``
and exits 0, so it is safe to invoke from a test job by accident.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://timetableapi.ptv.vic.gov.au"
DEFAULT_SECRETS_FILE = "/Users/ataylor/devel/ptv/.secrets/ptv.env"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

ROUTE_TYPES_PATH = "/v3/route_types"
ROUTES_PATH = "/v3/routes?route_types={}"
DIRECTIONS_PATH = "/v3/directions/route/{}"
STOPS_PATH = "/v3/stops/route/{}/route_type/{}"
DEPARTURES_PATH = (
    "/v3/departures/route_type/{}/stop/{}/route/{}"
    "?direction_id={}&max_results={}"
)
RUNS_PATH = "/v3/runs/{}"

# Well-known CBD targets.  These are public landmarks, chosen deliberately so
# fixtures never describe the operator's own configured home stops.
TRAIN_ROUTE_NAME = "Frankston"
TRAIN_STOP_NAME = "Flinders Street"
TRAM_ROUTE_NUMBER = "96"
BUS_ROUTE_NUMBER = "220"
MAX_RESULTS = 5


def build_url(devid: str, api_key: str, request: str) -> str:
    """Build a signed PTV URL exactly like the connector's build_URL."""
    request = request + ("&" if "?" in request else "?")
    raw = request + "devid={}".format(devid)
    signature = hmac.new(
        api_key.encode("utf-8"), raw.encode("utf-8"), hashlib.sha1
    ).hexdigest()
    return BASE_URL + raw + "&signature={}".format(signature)


def load_credentials(path: Path) -> dict:
    """Read KEY=VALUE pairs from the env file.  Values are never printed."""
    credentials = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        credentials[key.strip()] = value.strip().strip("'\"")
    return credentials


class Client:
    """Tiny stdlib HTTP client that reports only path and status."""

    def __init__(self, devid: str, api_key: str):
        self._devid = devid
        self._api_key = api_key
        self.log = []

    def get(self, path: str):
        """GET ``path`` and return (status, parsed JSON or None).

        The signed URL lives only in this local variable; it is never logged
        or written to disk.
        """
        url = build_url(self._devid, self._api_key, path)
        request = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": "ptv-fixtures"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = response.status
                body = response.read()
        except urllib.error.HTTPError as error:  # still record the status
            status = error.code
            body = error.read() or b"{}"
        print("{} -> {}".format(path, status))
        self.log.append({"path": path, "status": status})
        try:
            return status, json.loads(body.decode("utf-8"))
        except ValueError:
            return status, None


def _write(name: str, payload) -> Path:
    path = FIXTURE_DIR / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def _pick(mapping, needle):
    for key, value in mapping.items():
        if needle.lower() in str(value).lower():
            return key, value
    raise LookupError("no entry matching {!r}".format(needle))


def capture(client: Client) -> dict:
    """Perform the documented capture list and write the fixtures."""
    written = {}

    status, route_types = client.get(ROUTE_TYPES_PATH)
    written["route_types.json"] = _write("route_types.json", route_types)

    type_ids = {
        str(item["route_type"]): item["route_type_name"]
        for item in route_types["route_types"]
    }
    type_by_name = {name: int(ident) for ident, name in type_ids.items()}

    # One route list per mode: Train, Tram and Bus.
    routes_by_mode = {}
    for mode in ("Train", "Tram", "Bus"):
        route_type = type_by_name[mode]
        filename = "routes-{}.json".format(mode.lower())
        _, payload = client.get(ROUTES_PATH.format(route_type))
        written[filename] = _write(filename, payload)
        routes_by_mode[mode] = {
            str(route["route_id"]): route for route in payload["routes"]
        }

    train_route_id, train_route = _pick(
        {rid: r["route_name"] for rid, r in routes_by_mode["Train"].items()},
        TRAIN_ROUTE_NAME,
    )
    tram_route_id, _ = _pick(
        {rid: r.get("route_number", "") for rid, r in routes_by_mode["Tram"].items()},
        TRAM_ROUTE_NUMBER,
    )
    bus_route_id, _ = _pick(
        {rid: r.get("route_number", "") for rid, r in routes_by_mode["Bus"].items()},
        BUS_ROUTE_NUMBER,
    )

    train_type = type_by_name["Train"]

    # Directions and stops for the train route; departures for one stop.
    _, directions = client.get(DIRECTIONS_PATH.format(train_route_id))
    written["directions-frankston.json"] = _write(
        "directions-frankston.json", directions
    )

    _, stops_payload = client.get(
        STOPS_PATH.format(train_route_id, train_type)
    )
    written["stops-frankston.json"] = _write(
        "stops-frankston.json", stops_payload
    )

    stop_id, _ = _pick(
        {str(s["stop_id"]): s["stop_name"] for s in stops_payload["stops"]},
        TRAIN_STOP_NAME,
    )

    # Prefer the outbound direction (named after the line) because citybound
    # trains terminate at Flinders Street, so that direction returns an empty
    # departure list at this stop.
    direction_names = {
        str(d["direction_id"]): d["direction_name"]
        for d in directions["directions"]
    }
    candidates = []
    for needle in (TRAIN_ROUTE_NAME, "City"):
        try:
            candidates.append(_pick(direction_names, needle)[0])
        except LookupError:
            pass
    candidates.extend(did for did in direction_names if did not in candidates)

    departures = {"departures": []}
    for direction_id in candidates:
        departures_path = DEPARTURES_PATH.format(
            train_type, stop_id, train_route_id, direction_id, MAX_RESULTS
        )
        _, departures = client.get(departures_path)
        if departures.get("departures"):
            break
    written["departures-frankston-flinders.json"] = _write(
        "departures-frankston-flinders.json", departures
    )

    # One run lookup for express details.
    runs_payload = None
    for row in departures.get("departures") or []:
        if row.get("run_id") is not None:
            _, runs_payload = client.get(RUNS_PATH.format(row["run_id"]))
            break
    if runs_payload is not None:
        written["runs.json"] = _write("runs.json", runs_payload)

    # ---- locally derived variants (no extra API calls) -------------------

    # A guaranteed mix of estimated and scheduled-only rows, derived from the
    # recording.  Live data is often all-estimated or all-null depending on
    # the time of day, and the connector's fallback branch needs both.
    mixed = json.loads(json.dumps(departures))
    rows = mixed.get("departures") or []
    if rows:
        rows[0]["estimated_departure_utc"] = None
        if len(rows) > 1:
            rows[1]["estimated_departure_utc"] = rows[1].get(
                "estimated_departure_utc", rows[1].get("scheduled_departure_utc")
            )
    written["departures-200-mixed.json"] = _write(
        "departures-200-mixed.json", mixed
    )

    # Error variant: HTTP 200 with an empty but valid departures list.
    written["departures-200-empty.json"] = _write(
        "departures-200-empty.json", {"departures": []}
    )

    # Malformed variant: rows that omit scheduled_departure_utc entirely (not
    # null) while the estimate is null.  Current code raises KeyError; P1.4
    # will make this graceful.
    malformed = json.loads(json.dumps(departures))
    for row in malformed.get("departures") or []:
        row["estimated_departure_utc"] = None
        row.pop("scheduled_departure_utc", None)
    written["departures-200-missing-fields.json"] = _write(
        "departures-200-missing-fields.json", malformed
    )

    # Capture metadata: statuses only, no URLs and no credentials.
    written["_manifest.json"] = _write(
        "_manifest.json",
        {
            "captured_at": datetime.datetime.now(datetime.timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "source": "PTV Timetable API v3 live capture, well-known CBD targets",
            "requests": client.log,
            "targets": {
                "train_route": train_route,
                "tram_route_number": TRAM_ROUTE_NUMBER,
                "bus_route_number": BUS_ROUTE_NUMBER,
                "train_stop": TRAIN_STOP_NAME,
            },
            "derived_fixtures": [
                "departures-200-mixed.json",
                "departures-200-empty.json",
                "departures-200-missing-fields.json",
            ],
        },
    )
    return written


def scrub(written: dict, secrets: dict) -> None:
    """Assert no credential value appears in any written fixture byte."""
    needles = [
        value
        for key, value in secrets.items()
        if key in ("PTV_API_KEY", "PTV_DEVID")
    ]
    needles = [value.encode("utf-8") for value in needles if value]

    for name, path in written.items():
        data = path.read_bytes()
        for needle in needles:
            if needle in data:
                raise SystemExit(
                    "secret leak detected in {} (value length {})".format(
                        name, len(needle)
                    )
                )
    print(
        "scrub verified: {} file(s), {} credential value(s) absent".format(
            len(written), len(needles)
        )
    )


def main() -> int:
    secrets_file = Path(
        os.environ.get("PTV_SECRETS_FILE", DEFAULT_SECRETS_FILE)
    ).expanduser()
    if not secrets_file.is_file():
        print("skipped (no credentials)")
        return 0

    secrets = load_credentials(secrets_file)
    if not secrets.get("PTV_DEVID") or not secrets.get("PTV_API_KEY"):
        print("skipped (no credentials)")
        return 0

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    client = Client(secrets["PTV_DEVID"], secrets["PTV_API_KEY"])

    written = capture(client)
    scrub(written, secrets)

    for name, path in sorted(written.items()):
        print("wrote {}".format(path.relative_to(FIXTURE_DIR.parent.parent)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
