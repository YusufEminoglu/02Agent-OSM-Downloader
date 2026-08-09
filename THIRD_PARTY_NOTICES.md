# Third-Party Notices

## OpenStreetMap data

02Agent OSM Downloader requests and displays OpenStreetMap data. OpenStreetMap
data is © OpenStreetMap contributors and is available under the Open Database
License 1.0 (ODbL).

- Attribution: [OpenStreetMap](https://www.openstreetmap.org/copyright)
- License: [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/)

When you retain or publish downloaded OSM layers, maps, screenshots, exports,
or derived databases, keep the attribution and comply with the ODbL as
applicable.

## OpenStreetMap tile service

The optional standard basemap uses the OpenStreetMap tile service. Keep the
visible attribution and follow the [tile usage policy](https://operations.openstreetmap.org/policies/tiles/).

## Overpass API

OSM downloads use public Overpass API mirrors. The endpoints are network
services and are not bundled with this plugin:

- <https://overpass-api.de/api/interpreter>
- <https://overpass.kumi.systems/api/interpreter>
- <https://overpass.private.coffee/api/interpreter>

The plugin sends a descriptive User-Agent, bounds requests, and falls back
between the listed mirrors when a service is unavailable.
