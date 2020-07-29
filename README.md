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

## Requirements
### Developer ID and API Key
Please follow the instructions on http://ptv.vic.gov.au/ptv-timetable-api/ for obtaining a Developer ID and API Key

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
```

To get the Product ID for any BOM city:
- Go to [this](http://www.bom.gov.au/nsw/observations/map.shtml) website and search for "City Forecast", or "Town Forecast".
- The Product ID for your city will be in the left most column, or at the bottom of the page, and will look like "IDV10450"

![Here is an example of where to find product id](bom_forecast_product.png)

NOTE: The product id will be DIFFERENT to the one you use for the Core BOM Sensor Configuration. The numbers will not be the same.

Configuration variables:

- **product_id** (*Optional*): The Product ID string as identified from the BOM website.  If not given, defaults to the closest city.
- **name** (*Optional*): The name you would like to give to the weather forecast.
- **forecast_days** (*Optional*): The number of days of forecast you would like, maximum is 6. If not given, defaults to 6.
- **rest_of_today** (*Optional*): Would you like to create a sensor for the forecast for the rest of today. Defaults to true.
- **friendly** (*Optional*): Friendly mode will only create one sensor per day of forecast, and will have all the forecast information as sensor attributes. Defaults to false.
- **friendly_state_format** (*Optional*): Friendly state format allows you to format the state of your forecast sensors when in friendly mode. For example, '{min} to {max}, {summary}' will display the state as '10 to 25, Cloudy'. Defaults to '{summary}'.
- **monitored_conditions** (*Required*): A list of the conditions to monitor.
