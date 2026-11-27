# Public Transport Victoria

A Home Assistant integration that shows upcoming departure times for Public Transport Victoria (PTV) services — trains, trams, buses, V/Line trains and night buses — for the stop, line and direction you configure.

## Installation

### HACS

1. In Home Assistant, open **HACS**.
2. Open the three-dot menu and choose **Custom repositories**.
3. Enter `https://github.com/bremor/public_transport_victoria` and select **Integration** as the category, then add the repository.
4. Install **Public Transport Victoria** from HACS.
5. Restart Home Assistant.
6. Go to **Settings → Devices & Services**, choose **Add Integration**, and search for **Public Transport Victoria**.

### Manual

Copy the `custom_components/public_transport_victoria` directory from this repository into the `custom_components` directory inside your Home Assistant configuration directory, so that the files sit under `config/custom_components/public_transport_victoria/`. Restart Home Assistant, then add the integration from **Settings → Devices & Services** as described above.

## Configuration

Adding the integration asks for your PTV **Dev ID** and **API key**. Both are issued by Public Transport Victoria through the [PTV Timetable API](https://www.vic.gov.au/public-transport-timetable-api).

The config flow then asks you to choose, in order:

1. Route type — Train, Tram, Bus, V/Line Train or Night Bus.
2. Route (line or service).
3. Direction.
4. Stop.

Each completed flow creates one config entry for that stop, line and direction. You can add the integration more than once to monitor several stops, lines or directions; later entries reuse the Dev ID and API key already stored by the first entry.

### API key status

> **Note:** PTV developer key registrations have been slow or stalled at times — see [issue #38](https://github.com/bremor/public_transport_victoria/issues/38). Existing Dev ID / API key credentials continue to work. New registrations may take an extended period before being approved.

## Data and updates

Each config entry creates five sensors, one for each of the next five departures. A sensor's state is the departure time in 12-hour local time, for example `12:34 PM`.

Each sensor's attributes contain the raw departure row returned by the PTV API for that departure, including the estimated and scheduled departure times, the run ID, and other fields supplied by the API.

Departure data is refreshed by a coordinator approximately every 10 minutes. Manual refreshes, such as a call to `homeassistant.update_entity`, are subject to an internal 2-minute throttle: requests made sooner than that are ignored.

## Troubleshooting

To enable debug logging for the integration, add the following to your YAML configuration and restart Home Assistant:

```yaml
logger:
  default: warning
  logs:
    custom_components.public_transport_victoria: debug
```

Then reproduce the problem and check the Home Assistant log for lines from `custom_components.public_transport_victoria`. Remember to remove or reduce the debug logging afterwards.

## Attribution and licence

Departure data is provided by [Public Transport Victoria](https://www.ptv.vic.gov.au/) and is used under the [Creative Commons Attribution 4.0 International licence (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/).

Data © Public Transport Victoria.
