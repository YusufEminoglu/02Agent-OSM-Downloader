# 02Agent OSM Downloader
[![Documentation](https://img.shields.io/badge/📖_Reference_Manual-13a0a0)](https://yusufeminoglu.github.io/02Agent-OSM-Downloader/)

02Agent OSM Downloader is a stable QGIS 3.28+ and QGIS 4 plugin for reliable, repeatable
OpenStreetMap data acquisition. It works independently through its dock and
Processing provider, and SmartModeler GIS can use the same Processing
algorithms from Agent Workspace.

The plugin publishes a versioned, machine-readable
[Agent Protocol v1](AGENT_PROTOCOL.md). Its stable transport is the live QGIS
Processing registry rather than plugin UI automation.

The plugin is designed for bounded, reviewable downloads: results are temporary
QGIS layers until you explicitly save or export them.

## 📖 Documentation

**[Documentation site](https://yusufeminoglu.github.io/02Agent-OSM-Downloader/)** —
five illustrated task guides built from real screenshots of the dock, plus the
complete user and technical reference manual.

| Guide | What it covers |
| --- | --- |
| [Your first download](https://yusufeminoglu.github.io/02Agent-OSM-Downloader/guide-first-download.html) | Theme, datasets, extent, results |
| [Download a named place](https://yusufeminoglu.github.io/02Agent-OSM-Downloader/guide-place-boundary.html) | Geocoder matches and exact boundaries |
| [Cover a whole district or city](https://yusufeminoglu.github.io/02Agent-OSM-Downloader/guide-wide-area.html) | Tiled requests, merging and limits |
| [Build a structured query](https://yusufeminoglu.github.io/02Agent-OSM-Downloader/guide-advanced-query.html) | ANY/ALL matching and the query preview |
| [Commands, styling and agents](https://yusufeminoglu.github.io/02Agent-OSM-Downloader/guide-command-and-agent.html) | English commands, 16 map styles, Processing endpoints |
| [Reference manual](https://yusufeminoglu.github.io/02Agent-OSM-Downloader/REFERENCE_MANUAL.html) | Query grammar, tag semantics, cache and security model |


## What it provides

- A compact four-tab dock:
  - **Presets** for curated datasets and safe custom tags. Datasets within a
    context-aware theme can be checked together and downloaded in one bounded
    request. Urban Context links streets, built form, trees, transit and public
    realm datasets.
  - **Query** for structured multi-tag ANY/ALL requests and a read-only preview.
  - **Command** for the English intent router, including named-place commands
    such as `Download parks in London` and `Download public transport in Van`.
    Place-aware commands resolve in the background and automatically zoom the
    map canvas to the matched administrative extent before download.
  - **Agent** for the SmartModeler bridge and Agent Protocol status.
- A lighter palette-derived visual system with separated cards, selected-tab
  fills, contextual run summaries and tested text contrast in light and dark
  QGIS themes.
- Sixteen built-in cartographic styles with a live six-color preview. The
  twelve OSM Quick 3D palettes are joined by Aegean Blueprint, Urban Blueprint,
  Olive & Terracotta and Signal Contrast.
- Semantic native QGIS renderers applied immediately after download: buildings
  are colored by function, roads by class and width, while waterways, green
  space, trees, transit and common POIs receive geometry-aware symbols.
- An **Add OSM basemap** action for the standard OpenStreetMap XYZ layer, with
  duplicate protection and attribution metadata.
- A road-width choice between **By OSM highway category** and **Uniform road
  width**. The category mode gives motorways, primary roads, residential roads,
  service roads and active travel routes distinct visual hierarchy.
- Context-aware controls: the run area appears only for Presets and Query, and
  the generated query preview stays collapsed until requested.
- Palette-derived styling that follows QGIS light, dark, high-contrast and
  custom themes without forcing a fixed foreground/background pair.
- One-click curated thematic presets for:
  - Urban Context (street network, built form, trees, transit and public realm)
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
- An offline natural-language intent router in the dock.
- A custom `key=value` downloader for point, line or polygon data.
- A four-filter advanced builder with ANY/OR and ALL/AND matching, mixed or
  geometry-specific requests, loadable examples, key/value suggestions,
  filter clearing, a live filter count and an exact query preview.
- **Nominatim place search.** Type any place name the OpenStreetMap geocoder
  understands. Every match is listed with its address context and administrative
  level, selecting one zooms the canvas, and the match you confirm is the one
  that downloads. If the geocoder is unreachable the original Overpass
  administrative search takes over.
- **Exact administrative-boundary downloads.** When the chosen place has a mapped
  boundary, the request is filtered by that OSM area rather than a rectangle, so
  a district download stops at the district edge instead of pulling in the
  corners of its bounding box. Places with no mapped area fall back to their
  rectangle and say so.
- **Tiled wide extents.** Anything larger than one bounded request is split into
  a grid of roughly square tiles, downloaded in sequence with per-tile progress
  and cancellation, then merged by OSM identity so a road crossing a tile seam
  stays one feature. One job covers up to 2,500 km² in at most 40 requests.
- A live request estimate above the Download button — the area in km² and the
  number of bounded requests — updated as you pan and zoom, so an oversized
  extent is visible before you start rather than after a failure.
- Current-map, active-layer and geocoded-place extent modes.
- Separate temporary point, line and polygon result layers.
- A single compound Overpass request per preset, rather than one request per
  tag.
- Three pinned HTTPS Overpass mirrors with per-mirror diagnostics and automatic
  failover, plus the Nominatim geocoder — four fixed hosts and no way to point
  the plugin anywhere else. QGIS proxy support, cancellation, a 15-minute
  session cache, a 100 km² ceiling per request and 2,500 km² per job, a 64 MB
  response ceiling, a 150,000-element response ceiling and a 400,000-feature
  merge ceiling.

No raw Overpass QL, arbitrary URL, user path, API key, pip package or QuickOSM
installation is accepted or required.

## Processing algorithms

- `zero2agentosm:download_preset`
- `zero2agentosm:download_place`
- `zero2agentosm:download_custom_tag`
- `zero2agentosm:download_advanced`

All algorithms have fixed point, line and polygon outputs. The dock adds only
populated outputs, groups them under a themed layer-tree group, and applies the
selected persistent map style without changing the project background or any
existing layer.

The preset endpoint accepts multiple datasets from its live enum. The advanced
endpoint exposes four fixed key/value rows instead of accepting a
JSON document or raw query. `MATCH_MODE` chooses ANY/OR or ALL/AND semantics and
`GEOMETRY` limits the request to all geometries, points, lines or polygons.
Unused optional rows are omitted.

## SmartModeler GIS integration

When both plugins are enabled, SmartModeler can discover and run the curated,
custom-tag and structured advanced algorithms under the same explicit approval
model as its own bounded OSM downloader. SmartModeler remains usable without
this plugin, and this plugin remains usable without SmartModeler.

The connection handshake is:

1. `plugin.capabilities` confirms package and provider ownership.
2. `processing.search` discovers the stable algorithm ID.
3. `processing.describe` returns the live signature and a fresh context token.
4. SmartModeler submits a validated `processing_run` proposal.
5. The download starts only after the user clicks **Run**.

The formal contract and safety boundary are documented in
[`AGENT_PROTOCOL.md`](AGENT_PROTOCOL.md) and
[`agent_protocol.json`](agent_protocol.json).

The downloader dock also shows the active SmartModeler provider/profile and
provides **AI Connections** and **Agent Workspace** buttons. Connection profiles
and API secrets remain owned by SmartModeler: keys are kept in process memory
or the encrypted QGIS authentication vault and are never copied to this plugin.
The local command router remains usable when SmartModeler is disabled or its
active profile is Offline. If the SmartModeler package is installed but its
plugin dock has not initialized, **AI Connections** opens the shared settings
dialog directly instead of becoming a disabled or silent action. If it is not
installed, the Connections tab directs the user to **Plugins > Manage and
Install Plugins**, where they can search for, install and enable SmartModeler
GIS.

Example Agent request:

> Download the Green & Blue — Green-blue system preset for the active layer
> extent using 02Agent OSM Downloader and add temporary outputs.

Advanced example:

> Download polygons where `amenity=school` AND `wheelchair=yes` for the active
> layer extent using 02Agent OSM Downloader.

## Verification

The release gate runs pure-Python tests plus the provider smoke test on QGIS
3.44 LTR and QGIS 4.2. The plugin metadata targets QGIS 3.28+, while the
current deterministic smoke covers preset and advanced
geometry conversion without depending on a public service. Maintainers can
also exercise the real pinned-mirror transport with a small bounded extent:

```powershell
C:\OSGeo4W\bin\python-qgis.bat -m zero2agent_osm_downloader.tests.qgis_live_overpass --live
```

The live test creates temporary outputs only and is intentionally excluded from
the offline release gate.

## First run

1. Open **02Agent OSM Downloader** from the QGIS toolbar or Plugins menu.
2. Choose a preset and keep the map extent within the 100 km² request limit.
3. Review the dataset summary, map style and extent, then click **Download**.
4. Check the temporary point, line and polygon layers before saving or exporting
   the data you intend to retain.

The plugin does not require QuickOSM, a Python package, an API key or a separate
account. Network access is limited to the pinned HTTPS Overpass mirrors and the
optional OpenStreetMap tile basemap. See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for attribution and service-use
requirements.

## License

GPL-3.0-or-later.

## Data attribution

This plugin uses OpenStreetMap data under the [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
Keep [OpenStreetMap attribution](https://www.openstreetmap.org/copyright) with
downloaded layers and published exports. The optional tile basemap and public
Overpass services have additional usage conditions documented in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
