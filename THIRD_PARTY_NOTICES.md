# Third-Party Notices

## OpenStreetMap data

02Agent OSM Downloader requests and displays OpenStreetMap data. OpenStreetMap
data is © OpenStreetMap contributors and is available under the Open Database
License 1.0 (ODbL).

- Attribution: [OpenStreetMap](https://www.openstreetmap.org/copyright)
- License: [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/)

When you retain or publish downloaded OSM layers, maps, screenshots, exports,
or derived databases, keep the attribution and comply with the ODbL as
applicable. Every result layer carries this attribution and the date of its
data in its metadata, and a GeoPackage stores both inside the file.

Every service below is a network service: none of them is bundled with this
plugin, and all of them serve OpenStreetMap data, so the attribution and
licence above apply to whatever they return.

## Overpass API

OSM downloads use two public Overpass API servers, run by different
operators:

- <https://overpass.private.coffee/api/interpreter>
- <https://overpass-api.de/api/interpreter>

The plugin identifies itself with a `Referer` header naming its repository
(QGIS replaces a plugin's own User-Agent; on QGIS 4.2 and later the plugin's
name is also appended to the QGIS User-Agent). It sends one request at a time
to each server, at least a second apart, and never more than 64 requests per
download. A server that answers 429 rests for a minute and one that refuses
(403 or 406) for an hour; each part of a download is asked from each server at
most once. Every query declares a 128 MiB memory claim (`[maxsize]`) and a
server time limit below the plugin's own wait.

## Postpass (Geofabrik)

When both Overpass servers fail or are resting for a part of a download, and
the fallback is allowed (the dock option "If the OpenStreetMap servers are
busy, use Geofabrik's Postpass", or the Processing parameter `ALLOW_FALLBACK`;
optional, off by default), that part is requested from Postpass, a public PostGIS
service with OpenStreetMap data that Geofabrik runs:

- <https://postpass.geofabrik.de/api/0.2/interpreter>

Its operator documents the service, its database schema and where to raise
operational questions at <https://github.com/woodpeck/postpass-ops>. The
plugin sends it the same bounded selection as the Overpass request for that
part, as one generated SQL query in which every tag key and value is
hex-encoded, so no typed text reaches SQL syntax; requests carry the same
`Referer` and count against the same 64-request ceiling. The data it returns
is OpenStreetMap data under the ODbL, as above, and the plugin names Postpass
as the source in the status, the layer metadata and the `DATA_SOURCE` output.

## Nominatim

Place names are looked up with the OpenStreetMap Foundation's Nominatim
geocoder, `nominatim.openstreetmap.org`. The plugin sends it only the place
name, or the OSM id of a place you chose, at most one request per second and
with the same `Referer`, as its
[usage policy](https://operations.osmfoundation.org/policies/nominatim/)
asks.

## OpenStreetMap tile service

The optional standard basemap uses the OpenStreetMap tile service at
`tile.openstreetmap.org`; it is contacted only after you press **Add OSM
basemap**. Keep the visible attribution and follow the
[tile usage policy](https://operations.openstreetmap.org/policies/tiles/).
