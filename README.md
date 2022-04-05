[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)

The `public transport victoria` sensor platform uses the [Public Transport Victoria (PTV)](https://www.ptv.vic.gov.au/) as a source for public transport departure times for Victoria, Australia.

## Installation (There are two methods, with HACS or manual)

[![hacs][hacsbadge]][hacs]

Install via HACS (default store) or install manually by copying the files in a new 'custom_components/public_transport_victoria' directory.

## Prerequisites

### Developer ID and API Key
Please follow the instructions on http://ptv.vic.gov.au/ptv-timetable-api/ for obtaining a Developer ID and API Key.

## Configuration
After you have installed the custom component (see above):
1. Goto the `Configuration` -> `Integrations` page.  
2. On the bottom right of the page, click on the `+ Add Integration` sign to add an integration.
3. Search for `Public Transport Victoria`. (If you don't see it, try refreshing your browser page to reload the cache.)
4. Click `Submit` to add the integration.

## Notes
This integration will refresh data every 10 minutes. If you wish to update the departure information more frequently during interesting periods, you may use an automation like the one below. It will update the sensors every minute between 7:30AM-8:30AM and 4:45PM-5:45PM.
```
automation:

  - alias: 'update_trains'
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

<a href="https://www.buymeacoffee.com/bremor" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height=40px width=144px></a>

[hacs]: https://hacs.xyz
[hacsbadge]: https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge
