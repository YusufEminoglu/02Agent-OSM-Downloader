# 02Agent OSM Downloader

02Agent OSM Downloader is a focused QGIS 4 plugin for reliable, repeatable
OpenStreetMap data acquisition. It works independently through its dock and
Processing provider, and SmartModeler GIS can use the same Processing
algorithms from Agent Workspace.

The plugin publishes a versioned, machine-readable
[Agent Protocol v1](AGENT_PROTOCOL.md). Its stable transport is the live QGIS
Processing registry rather than plugin UI automation.

## What it provides

- A compact three-tab dock:
  - **Download** for curated presets and safe custom tags.
  - **Command** for the offline Turkish/English intent router.
  - **Connections** for the SmartModeler bridge and Agent Protocol status.
- Palette-derived styling that follows QGIS light, dark, high-contrast and
  custom themes without forcing a fixed foreground/background pair.
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

## License

GPL-3.0-or-later.
