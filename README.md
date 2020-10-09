[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)

The `public transport victoria` sensor platform uses the [Public Transport Victoria (PTV)](http://www.bom.gov.au) as a source for public transport departure times for Victoria, Australia.

# Installation (There are two methods, with HACS or manual)

### 1. Easy Mode

We support [HACS](https://hacs.netlify.com/). Go to "STORE", search "Public Transport Victoria" and install.

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
