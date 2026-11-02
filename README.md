# 02Agent OSM Downloader
[![Documentation](https://img.shields.io/badge/📖_Reference_Manual-13a0a0)](https://geophilo.com/zero2agent_osm_downloader/)

02Agent OSM Downloader downloads OpenStreetMap (OSM) data into QGIS 3.28 to
4.x: roads and the street network, buildings, schools, hospitals, parks,
transit and the rest of its 44 ready-made datasets, or any OSM tag, for the
area you choose, styled on arrival. It works on its own through its dock and
its Processing provider, and 02SmartModeler can run three of its four
Processing endpoints from Agent Workspace.

The plugin publishes a versioned, machine-readable
[Agent Protocol v1](AGENT_PROTOCOL.md). Its stable transport is the live QGIS
Processing registry rather than plugin UI automation.

Results are temporary QGIS layers, or go straight into a GeoPackage with their
style when you choose *Save to → GeoPackage*.

## Quick start

1. Open the dock from the **02Agent** toolbar icon. On an empty map it opens
   at **Named place**: type a town and press Enter.
2. Pick a topic and tick datasets — *Buildings*, *Road network*, *Green
   spaces*, *Bus transit*, … — or type a request on the **Command** tab in
   English or Turkish: `download parks in Konak` or `Konak'taki parkları indir`.
3. Choose the **Extent**: the map view, the active layer, a named place (inside
   its mapped boundary when it has one), the selected features, a rectangle you
   draw, or a radius around a point.
4. Press **Download**. While it runs, the status at the bottom of the dock
   shows the elapsed time, the part of the area and the server being asked.
   Layers arrive styled, with the OpenStreetMap attribution and the data date
   in their metadata. **Recent → Run again** repeats an earlier download;
   within an hour the answer can come from the local cache.

If some parts of the area could not be downloaded, the status says *Finished
with gaps: N of M parts are missing*, the Recent entry is marked incomplete,
and **Continue** fetches only the missing parts. While the servers rest after
the errors that left the gaps, Continue waits and a countdown in its place
says when it becomes available. A download that fails says
why in one plain sentence, for example that the servers are busy, asked this
computer to slow down, refused it or could not be reached, or that the area
holds too much data; Cancel ends with *Download canceled. Nothing was added.*
When the dock is hidden, the result also appears in the QGIS message bar.

## 📖 Documentation

**[Documentation site](https://geophilo.com/zero2agent_osm_downloader/)** —
five illustrated task guides and the complete user and technical reference
manual. **Manual** and **Report a problem** links sit at the top of the dock;
Report a problem opens a GitHub issue form with the plugin, QGIS and system
versions and the class of the last failure filled in, never an area or a place
name.

| Guide | What it covers |
| --- | --- |
| [Your first download](https://geophilo.com/zero2agent_osm_downloader/tasks/first-download.html) | Topic, datasets, extent, results |
| [Download a named place](https://geophilo.com/zero2agent_osm_downloader/tasks/place-boundary.html) | Geocoder matches and exact boundaries |
| [Cover a whole district or city](https://geophilo.com/zero2agent_osm_downloader/tasks/wide-area.html) | Downloads in parts, merging and limits |
| [Build a structured query](https://geophilo.com/zero2agent_osm_downloader/tasks/advanced-query.html) | ANY/ALL matching and the query preview |
| [Commands, styling and agents](https://geophilo.com/zero2agent_osm_downloader/tasks/command-and-agent.html) | Commands, map styles, Processing endpoints |
| [Reference manual](https://geophilo.com/zero2agent_osm_downloader/manual.html) | Query grammar, tag semantics, cache and security model |

## What it provides

- A compact four-tab dock with Download, Cancel, progress and status in a
  footer that stays in view while the rest of the dock scrolls:
  - **Presets** for 44 ready-made datasets in 16 topics and safe custom tags.
    Datasets within a topic can be ticked together and downloaded in one
    request.
  - **Query** for structured multi-tag ANY/ALL requests and a read-only
    preview. It starts on *Match any* and *All geometries*, also after
    *Clear filters*.
  - **Command** for short requests in English and Turkish, interpreted on your
    computer, such as `Download parks in London`, `restaurants in Paris` or
    `Üsküdar'daki eczaneleri göster`. Road and street words, and everyday
    places such as restaurants, cafes, pharmacies, banks, shops,
    supermarkets, universities, libraries, playgrounds, trees and beaches,
    route to what they name; a place plus something unknown says so instead
    of guessing, and a place name alone downloads its place centres. Place
    commands resolve in the background and zoom the map to the match before
    anything is downloaded.
  - **Agent** for the 02SmartModeler bridge and Agent Protocol status.
- Six ways to choose the area: the current map view, the active layer, a named
  place, the selected features of the active layer (their bounding box), a
  rectangle drawn on the map, and a radius of 50 m to 5 km around the map
  centre, the selected features or a searched place.
- 17 map styles with a live six-colour preview: twelve palettes adapted from
  OSM Quick 3D, the OSM As-Is standard-map style, and four of this plugin's
  own — Aegean Blueprint, Urban Blueprint, Olive & Terracotta and Signal
  Contrast. Changing the style or the road width restyles this plugin's
  layers in the project and nothing else.
- Semantic native QGIS renderers applied immediately after download: buildings
  are colored by function, roads by class and width, while waterways, green
  space, trees, transit and common POIs receive geometry-aware symbols. In the
  OSM As-Is style, point and polygon labels appear only from 1:10,000 in.
- An **Add OSM basemap** action for the standard OpenStreetMap XYZ layer, with
  duplicate protection and attribution metadata. The basemap goes to the
  bottom of the layer tree, under the data.
- A road-width choice between **By OSM highway category** and **Uniform road
  width**. The category mode gives motorways, primary roads, residential roads,
  service roads and active travel routes distinct visual hierarchy.
- A dock that takes its colours from the QGIS palette and switches to a
  high-contrast text colour wherever a theme would give text less than 4.5:1
  contrast: every enabled label, checkbox, group title, link and button reads
  at 4.5:1 or more under each built-in QGIS theme (default, Blend of Gray and
  Night Mapping), measured on the dock's pixels in QGIS 3.44 and 4.2; a
  disabled Download button looks disabled.
- Ready-made datasets in these topics:
  - OSM As-Is (overview, urban detail, mobility and all tagged objects)
  - Urban Context (street network, built form and nature, transit, public
    realm, and everyday amenities for 15-minute city studies)
  - Network
  - Morphology
  - Green & Blue
  - Public Transport
  - Religious
  - Tourism
  - Sport
  - Bike
  - Car
  - Traffic
  - Health
  - Education
  - Emergency
  - Places (`admin_level=2` through `admin_level=10` and place centres)
- A custom `key=value` downloader for point, line or polygon data.
- A six-filter advanced builder with ANY/OR and ALL/AND matching, mixed or
  geometry-specific requests, loadable examples, key/value suggestions, an
  exclude tag, regular-expression values, filter clearing, a live filter count
  and an exact query preview. A regular expression the OSM servers would
  refuse (a `(?…)` group, a lookaround, a backreference) or one that could
  hang (a repeated group that repeats inside, such as `(a+)+`) is refused
  before anything is sent; POSIX classes such as `[[:digit:]]` work, and `*`
  means any value.
- **Nominatim place search.** Type any place name the OpenStreetMap geocoder
  understands. Every match is listed with its address context and
  administrative level, selecting one zooms the canvas, and the match you
  confirm is the one that downloads. If the geocoder cannot be reached, a
  search of the OpenStreetMap administrative names takes over; a plain "no
  match" is final and suggests a fuller name. A search can be canceled.
- **Exact boundaries.** When the chosen place has a mapped boundary, only the
  features that touch it are kept: the plugin fetches the place's outline from
  Nominatim, requests the outline's bounding box in parts, and leaves out what
  lies outside. If the outline cannot be fetched, the Overpass server's own
  area filter is used instead and a line under the result says so; when that
  filter then returns nothing, the line says the server may not hold the
  boundary and suggests the rectangular extent. A place with no mapped
  boundary is downloaded as its rectangle: the note above Download says so and
  shows the rectangle's size, and the boundary choice shows Rectangular extent.
- **Large areas in parts.** One request covers at most 100 km². A larger area
  is split into a grid of up to 40 parts, up to 2,500 km² in all, downloaded in
  sequence and merged by OSM identity, so a road crossing a seam stays one
  feature. A part a server proves too dense (out of memory, more than 64 MB or
  more than 150,000 objects) is split into four, at most twice
  (100 → 25 → 6.25 km²), and the parts after it start at that size. A query
  that runs out of time is asked from the other server first and split only
  when every server asked ran out of time; when all four smaller parts then
  arrive, the parts after it start at that size too. A part still too dense
  at the smallest size is skipped for 24 hours and the rest of the area
  still arrives; timeouts everywhere mean busy servers, so the download
  stops instead, and it sends no more requests after 6 timeouts. A download
  stops at 400,000 merged OSM objects, counted as the parts arrive.
- A live request estimate above the Download button — the area in km² and the
  number of requests — updated as you pan and zoom, so an oversized extent is
  visible before you start rather than after a failure.
- Separate point, line and polygon result layers, saved as temporary layers
  or into a GeoPackage. A GeoPackage stores each layer's style, the
  OpenStreetMap attribution and the date of the data inside the file; a path
  that cannot be written keeps the data as temporary layers and says why.

## The data you get

- Every layer has the same columns: the OSM id and type, the name, the
  dataset, a column for each common OSM key holding the value exactly as
  mapped, `tags_json` with every tag, and `matched_tags`.
- Typed columns sit beside them: `height_m`, `levels_n`, `lanes_n`,
  `maxspd_kmh`, `width_m` and `pop_n`, filled only when the mapped value is a
  plain number in a known unit (NULL otherwise, never a guess), plus
  `length_m` for lines and `area_m2` for polygons, measured on the WGS84
  ellipsoid. The typed column names fit the 10 characters a Shapefile export
  keeps.
- A way becomes a polygon only when it closes; an open way that was asked for
  as an area only is dropped and counted. Which ring of a multipolygon is a
  hole is decided by nesting, not by member roles, and invalid outlines are
  repaired. *All tagged objects* reads only multipolygon and boundary
  relations.
- Features that could not be built are counted by reason (no coordinates, an
  outline that does not close, an invalid outline that could not be repaired,
  a failed projection), reported in the log and returned as
  `FEATURES_DROPPED`. Matched features left out because they lie outside the
  chosen area (a place's boundary or an around circle) are not failures: they
  are counted apart, returned as `FEATURES_OUTSIDE`, and the finished status
  says how many were left out.

## Network use

| Host | Used for | When |
| --- | --- | --- |
| `overpass.private.coffee` | OSM data (Overpass API) | Every download |
| `overpass-api.de` | OSM data (Overpass API) | Every download |
| `postpass.geofabrik.de` | OSM data (Geofabrik's Postpass) | Only for a part both Overpass servers failed or are resting for, and only while the dock option *If the OpenStreetMap servers are busy, use Geofabrik's Postpass* or the Processing parameter `ALLOW_FALLBACK` allows it (optional, off by default; turn it on in the dock or with `ALLOW_FALLBACK`) |
| `nominatim.openstreetmap.org` | Place names and the outline of a chosen place | Named place searches, place commands, `download_place` and exact boundaries |
| `tile.openstreetmap.org` | Basemap tiles | Only after you press **Add OSM basemap** |

Downloads follow the Overpass operators' rules. Each server gets one request
at a time, at least a second apart. A server that answers 429 rests for a
minute, one that refuses (403 or 406) or fails the secure connection for an
hour, a busy one (5xx) for 45 seconds, and one that does not answer or cannot
be reached for two minutes; the rest doubles while a server keeps failing.
Each part of the area is asked from each server at most once (once more only
after a dropped connection), and a resting server is waited for only when it
is back within 30 seconds. A download stops at once when a part fails on every
server before any data arrived, stops after two lost parts in a row once data
is arriving, and never sends more than 64 requests. A request waits up to 55
seconds for its answer (less when the QGIS network timeout is shorter), gives
the server 15 seconds less to run the query, claims 128 MiB of server memory
(`[maxsize]`) instead of the 512 MiB default, and is abandoned as soon as the
answer passes 64 MB.

Every request names the plugin in a `Referer` header, because QGIS replaces a
plugin's own User-Agent; on QGIS 4.2 and later the plugin's name is also
appended to the QGIS User-Agent. Nominatim gets the same header and at most
one request per second.

Answers are kept in a small SQLite cache in your QGIS profile for one hour;
the parts of a download that finished with gaps are kept for 24 hours, so
Continue fetches only the missing ones. Empty answers, answers the server
marked as incomplete and data more than seven days old are never stored.
**Clear cache** deletes every entry and gives the disk space back.

No raw Overpass QL, arbitrary URL, API key, pip package or QuickOSM
installation is accepted or required; the only file path the plugin takes is
the GeoPackage you choose under *Save to*.

## Processing algorithms

- `zero2agentosm:download_preset`
- `zero2agentosm:download_place`
- `zero2agentosm:download_custom_tag`
- `zero2agentosm:download_advanced`

All algorithms have fixed point, line and polygon outputs, plus
`OSM_TIMESTAMP` (the date of the data), `TILES_TOTAL` and `TILES_MISSING`
(`0` means complete), `DATA_SOURCE` (the servers that delivered),
`FEATURES_DROPPED` (features that could not be built or written), `FEATURES_OUTSIDE`
(features left out because they lie outside the chosen area) and `NOTICE`
(one sentence about the result, blank when there is nothing to say; the dock
shows it under the status). Every algorithm also accepts an optional resolved
`AREA_ID`, an exclude key/value, an around search (`AROUND_X`, `AROUND_Y`,
`AROUND_METERS`) and `ALLOW_FALLBACK` (the Postpass fallback: optional, off by
default; turn it on in the dock or with `ALLOW_FALLBACK`). The dock adds only
populated outputs and groups them under a themed layer-tree group.

The preset endpoint accepts multiple datasets from its live enum. The `PRESET`
options keep their numbers from release to release: new datasets are added at
the end, so a saved model or script keeps its datasets. The advanced endpoint
exposes six fixed key/value rows instead of accepting a JSON document or raw
query. `MATCH_MODE` chooses ANY/OR or ALL/AND semantics and `GEOMETRY` limits
the request to all geometries, points, lines or polygons. Unused optional rows
are omitted.

## 02SmartModeler integration

When both plugins are enabled, 02SmartModeler can discover and run the
curated, custom-tag and structured advanced algorithms under the same explicit
approval model as its own bounded OSM downloader; `download_place` is not on
its reviewed allowlist. 02SmartModeler remains usable without this plugin, and
this plugin remains usable without 02SmartModeler.

The connection handshake is:

1. `plugin.capabilities` confirms package and provider ownership.
2. `processing.search` discovers the stable algorithm ID.
3. `processing.describe` returns the live signature and a fresh context token.
4. 02SmartModeler submits a validated `processing_run` proposal.
5. The download starts only after the user clicks **Run**.

The formal contract and safety boundary are documented in
[`AGENT_PROTOCOL.md`](AGENT_PROTOCOL.md) and
[`agent_protocol.json`](agent_protocol.json).

The downloader dock also shows the active 02SmartModeler provider/profile and
provides **AI Connections** and **Agent Workspace** buttons. Connection
profiles and API secrets remain owned by 02SmartModeler: keys are kept in
process memory or the encrypted QGIS authentication vault and are never copied
to this plugin. The local command router remains usable when 02SmartModeler is
disabled or its active profile is Offline. If the 02SmartModeler package is
installed but its plugin dock has not initialized, **AI Connections** opens
the shared settings dialog directly instead of becoming a disabled or silent
action. If it is not installed, search for **02SmartModeler** in **Plugins >
Manage and Install Plugins**, install it and enable it.

Example Agent request:

> Download the Green & Blue — Green-blue system preset for the active layer
> extent using 02Agent OSM Downloader and add temporary outputs.

Advanced example:

> Download polygons where `amenity=school` AND `wheelchair=yes` for the active
> layer extent using 02Agent OSM Downloader.

## Verification

The release gate (`pf verify`) runs the pure-Python tests and the QGIS suites
on QGIS 3.44 LTR and QGIS 4.2. The plugin metadata targets QGIS 3.28+. The
QGIS suites run against a local fake server and saved real responses; none of
them needs a public service. Maintainers can also exercise the real servers
with a small bounded extent:

```powershell
C:\OSGeo4W\bin\python-qgis.bat -m zero2agent_osm_downloader.tests.qgis_live_overpass --live
```

The live test creates temporary outputs only and is intentionally excluded from
the offline release gate.

## First run

1. Open **02Agent OSM Downloader** from the QGIS toolbar or Plugins menu.
2. Choose a dataset and an area of up to 2,500 km²; the note above Download
   shows the area and how many requests it needs.
3. Review the dataset summary, map style and extent, then click **Download**.
4. Check the point, line and polygon layers before saving or exporting the
   data you intend to keep.

The plugin does not require QuickOSM, a Python package, an API key or a
separate account. The hosts it contacts are listed under
[Network use](#network-use). See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for attribution and
service-use requirements.

## License

GPL-3.0-or-later.

## Data attribution

This plugin uses OpenStreetMap data under the [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
Keep [OpenStreetMap attribution](https://www.openstreetmap.org/copyright) with
downloaded layers and published exports. The optional tile basemap and the
public Overpass, Postpass and Nominatim services have their own usage
conditions, documented in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
