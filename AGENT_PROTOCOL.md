# 02Agent OSM Downloader — Agent Protocol v1

02Agent OSM Downloader uses QGIS Processing as its stable agent transport. It
does not expose arbitrary Python objects, UI automation, raw Overpass queries or
unreviewed network actions to an agent.

The machine-readable contract is [`agent_protocol.json`](agent_protocol.json).

## Discovery handshake

SmartModeler GIS performs the following bounded handshake:

1. Inspect package `zero2agent_osm_downloader` with `plugin.capabilities`.
2. Find provider `zero2agentosm` through the live QGIS Processing registry.
3. Inspect the selected algorithm with `processing.describe`.
4. Bind only parameter forms advertised by that live description.
5. Submit a `processing_run` proposal with a fresh `context_token` and a
   non-empty explanation.
6. Run only after the user clicks the Run approval card.

This makes the integration independent of plugin UI labels and language. The
stable public endpoints are:

- `zero2agentosm:download_preset`
- `zero2agentosm:download_custom_tag`

## Output contract

Both endpoints expose fixed point, line and polygon sinks. Agent execution binds
all three to temporary QGIS outputs. Empty geometry outputs may be discarded by
the dock, while Processing callers receive the declared sinks.

## Safety contract

- Extents come only from the current map or an existing project layer.
- Download area is limited to 100 square kilometres.
- Network requests use pinned Overpass mirrors.
- Raw query text, arbitrary URLs, file paths and API keys are not parameters.
- Live algorithm signatures are checked against SmartModeler's reviewed policy.
- A proposal never executes without explicit user approval.

## Compatibility

Protocol `1.x` keeps the provider ID, algorithm IDs, approval boundary and
parameter meanings stable. Additive presets may extend the live `PRESET` enum.
A breaking algorithm or binding change requires a new protocol major version.
