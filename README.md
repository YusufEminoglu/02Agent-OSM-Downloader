# 02Agent OSM Downloader

02Agent OSM Downloader is a focused QGIS 4 plugin for reliable, repeatable
OpenStreetMap data acquisition. It works independently through its dock and
Processing provider, and SmartModeler GIS can use the same Processing
algorithms from Agent Workspace.

## What it provides

- One-click curated thematic presets for:
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

The preset algorithm has fixed point, line and polygon outputs. The dock adds
only populated outputs and groups them under a themed layer-tree group.

## SmartModeler GIS integration

When both plugins are enabled, SmartModeler can discover and run the curated
preset algorithm under the same explicit approval model as its own bounded OSM
downloader. SmartModeler remains usable without this plugin, and this plugin
remains usable without SmartModeler.

Example Agent request:

> Download the Green & Blue — Green-blue system preset for the active layer
> extent using 02Agent OSM Downloader and add temporary outputs.

## License

GPL-3.0-or-later.
