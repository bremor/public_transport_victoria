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
You will need to add this repository manually to HACS, repository URL is https://github.com/bremor 

## Configuration
Either install the package [weather.yaml](https://github.com/DavidFW1960/bom_forecast/blob/master/weather.yaml) (see below under alternate installation)

Or manually, add the following to your `configuration.yaml` file:

```yaml
# Example configuration.yaml entry
sensor:
  - platform: bom_forecast
    product_id: !secret my_bom_product_id
    name: !secret my_bom_name         #In my examples I use Gosford
    forecast_days: 6
    rest_of_today: true
    friendly: false
    friendly_state_format: '{max}, {summary}'
    monitored_conditions:
      - 'max'
      - 'min'
      - 'chance_of_rain'
      - 'possible_rainfall'
      - 'summary'
      - 'detailed_summary'
      - 'icon'
      - 'uv_alert'
      - 'fire_danger'
```

This example uses the secrets.yaml file for the product_id and name. Up to you if you want to do this or not.
If you use the package below you will still need to edit in the correct details.

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

## Alternate Install
Download my [weather.yaml](https://github.com/DavidFW1960/bom_forecast/blob/master/weather.yaml) package and install in your /config/packages folder. Make sure your configuration.yaml includes packages folder. You will also need to download [bom_icons.zip](https://github.com/DavidFW1960/bom_forecast/blob/master/bom_icons.zip) in order to be able to create the lovelace cards looking like this:

![BOM Forecast Card](bom_forecast.png)

The package also 'fixes' the problem whereby the maximum and minimum temperatures are no longer reported by BOM as it will use todays max and the minimum for the day when these values are not returned. It also includes the current text which is used in my [custom_card for BOM](https://github.com/DavidFW1960/bom-weather-card).

BOM also changes the icon displayed for some of their icons at night so there is in the package an automation to take care of that at 6pm.
The Lovelace card, [lovelace.yaml](https://github.com/DavidFW1960/bom_forecast/blob/master/lovelace.yaml) should be cut/pasted into your lovelace where you want to see the card.
