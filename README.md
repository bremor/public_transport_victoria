[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)

The `public transport victoria` sensor platform uses the [Public Transport Victoria (PTV)](http://www.bom.gov.au) as a source for forecast meteorological data. This is an updated version of a fork from bremor/bom_forecast

## Manual Installation 
To add Public Transport Victoria to your installation, create this folder structure in your /config directory:
- “custom_components/public_transport_victoria”.

Then, drop the following files into that folder:
- \_\_init__.py
- manifest.json
- sensor.py

## HACS Support
You will need to add this repository manually to HACS, repository URL is https://github.com/bremor/public_transport_victoria 

## Prerequisites
### Developer ID and API Key
Please follow the instructions on http://ptv.vic.gov.au/ptv-timetable-api/ for obtaining a Developer ID and API Key.

## Stop Information
Using this website, https://www.ptv.vic.gov.au/departures, select your details:
1. Mode of transport,
2. Your line or route,
3. Select a direction,
4. Select your stop.

In the following example, route type is `0`, stop_id is `1141`, and direction ID is `16`. You can use these values to build your configuration.
![Here is an example of where to find product id](img/ptv_example.JPG)
## Configuration
Add the following to your `configuration.yaml` file:

```yaml
# Example configuration.yaml entry
sensor:
  - platform: public_transport_victoria
    id: 1234567
    api_key: 357dts35-930b-467c-844d-21d74f15c38a
    stop_id: 1141
    max_results: 4
    direction_id: 1
    route_type: 0
```

Configuration variables:

- **id** (*Required*): The ID is your developer ID that you receive directly from PTV.
- **api_key** (*Required*): The developer key that name you receive directly from PTV.
- **stop_id** (*Required*): The ID of the bus stop, train station, tram station, etc.
- **max_results** (*Optional*): The maximum number of departures you would like to return. Default is 1.
- **direction_id** (*Optional*): Which direction are you travelling in. Default is '{None}' which returns all directions.
- **route_type** (*Optional*): Route type, e.g. train, tram, bus. Default is 0 which is for trains..
- **monitored_conditions** (*Required*): A list of the conditions to monitor.
