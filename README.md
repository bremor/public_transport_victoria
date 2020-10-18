[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)

The `public transport victoria` sensor platform uses the [Public Transport Victoria (PTV)](http://www.bom.gov.au) as a source for public transport departure times for Victoria, Australia.

# Installation (There are two methods, with HACS or manual)

### 1. Easy Mode

Not just yet. ~~We support [HACS](https://hacs.netlify.com/). Go to "STORE", search "Public Transport Victoria" and install.~~
Add this repo as a custom repository within HACS.

### 2. Manual

Install it as you would do with any homeassistant custom component:

1. Download `custom_components` folder.
2. Copy the `public_transport_victoria` directory within the `custom_components` directory of your homeassistant installation.
The `custom_components` directory resides within your homeassistant configuration directory.
**Note**: if the custom_components directory does not exist, you need to create it.
After a correct installation, your configuration directory should look like the following.

    ```
    └── ...
    └── configuration.yaml
    └── custom_components
        └── public_transport_victoria
            └── __init__.py
            └── config_flow.py
            └── const.py
            └── sensor.py
            └── manifest.json
            └── sensor.py
            └── strings.json
            └── PublicTransportVictoria
                └── public_transport_victoria.py
            └── translations
                └── en.json
    ```

## Prerequisites

### 1. Developer ID and API Key
Please follow the instructions on http://ptv.vic.gov.au/ptv-timetable-api/ for obtaining a Developer ID and API Key.

# Configuration
1. Goto the `Configuration` -> `Integrations` page.  
2. On the bottom right of the page, click on the Orange `+` sign to add an integration.
3. Search for `Public Transport Victoria`. (If you don't see it, try refreshing your browser page to reload the cache.)
4. Enter the required information. (Developer ID/Developer)
5. No reboot is required. You can relogin or change the password/settings by deleting and re-adding on this page.

# Notes
1. This integration will refresh data every 10 minutes. If you wish to update the departure information more frequently during interesting periods, you may use an automation like the one below. It will update the sensors every minute between 7:30AM-8:30AM and 4:45PM-5:45PM.
```
automation:

  - alias: 'bom_melbourne_max_temp_c'
    initial_state: true
    trigger:
      trigger:
      - platform: time_pattern
        minutes: "/1"
    condition:
      condition: or
      conditions:
        - condition: time
          after: '07:30:00'
          before: '08:30:00'
        - condition: time
          after: '16:45:00'
          before: '17:45:00'
    action:
      - service: 'homeassistant.update_entity'
        data:
          entity_id:
            - 'sensor.werribee_line_to_city_flinders_street_from_aircraft_station_0'
            - 'sensor.werribee_line_to_city_flinders_street_from_aircraft_station_1'
            - 'sensor.werribee_line_to_city_flinders_street_from_aircraft_station_2'
            - 'sensor.werribee_line_to_city_flinders_street_from_aircraft_station_3'
            - 'sensor.werribee_line_to_city_flinders_street_from_aircraft_station_4'
```
2. All feature requests, issues and questions are welcome.
