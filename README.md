# 02Agent OSM Downloader
[![Documentation](https://img.shields.io/badge/📖_Reference_Manual-13a0a0)](https://yusufeminoglu.github.io/02Agent-OSM-Downloader/)

02Agent OSM Downloader is a public-alpha QGIS 3.28+ and QGIS 4 plugin for reliable, repeatable
OpenStreetMap data acquisition. It works independently through its dock and
Processing provider, and SmartModeler GIS can use the same Processing
algorithms from Agent Workspace.

The plugin publishes a versioned, machine-readable
[Agent Protocol v1](AGENT_PROTOCOL.md). Its stable transport is the live QGIS
Processing registry rather than plugin UI automation.

## 📖 Documentation

**[Comprehensive Academic Reference Manual](https://yusufeminoglu.github.io/02Agent-OSM-Downloader/)** — complete documentation of every feature, parameter, and workflow. Hosted on GitHub Pages.


## What it provides

- A compact four-tab dock:
  - **Presets** for curated datasets and safe custom tags.
  - **Query** for structured multi-tag ANY/ALL requests and a read-only preview.
  - **Command** for the offline Turkish/English intent router.
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
- Context-aware controls: the run area appears only for Presets and Query, and
  the generated query preview stays collapsed until requested.
- Palette-derived styling that follows QGIS light, dark, high-contrast and
  custom themes without forcing a fixed foreground/background pair.
- One-click curated thematic presets for:
  - Urban Context (roads, building footprints, trees and tree rows in one run)
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
  geometry-specific requests, loadable examples and an exact query preview.
- Current-map and active-layer extent modes.
- Separate temporary point, line and polygon result layers.
- A single compound Overpass request per preset, rather than one request per
  tag.
- Three pinned HTTPS Overpass mirrors, QGIS proxy support, cancellation, a
  15-minute session cache, a 100 km² area ceiling, a 64 MB response ceiling and
  a 150,000-element ceiling.

No raw Overpass QL, arbitrary URL, user path, API key, pip package or QuickOSM
installation is accepted or required.

## Processing algorithms

- `zero2agentosm:download_preset`
- `zero2agentosm:download_custom_tag`
- `zero2agentosm:download_advanced`

All algorithms have fixed point, line and polygon outputs. The dock adds only
populated outputs, groups them under a themed layer-tree group, and applies the
selected persistent map style without changing the project background or any
existing layer.

The advanced endpoint exposes four fixed key/value rows instead of accepting a
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

## License

GPL-3.0-or-later.
