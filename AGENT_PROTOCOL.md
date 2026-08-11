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
- `zero2agentosm:download_place`
- `zero2agentosm:download_custom_tag`
- `zero2agentosm:download_advanced`

`download_place` resolves a validated place name through the OpenStreetMap
Nominatim geocoder, falling back to a pinned-Overpass administrative search,
then applies the same bounded extent and temporary output contract as
`download_preset`. Two extra parameters control how the place is used:

- `CLIP` — `0` filters the request by the place's exact mapped boundary using
  an Overpass area, so results stop at the administrative edge. `1` uses the
  place's rectangular extent instead. A place with no mapped area falls back to
  its rectangle and reports doing so.
- `AREA_ID` — an already resolved Overpass area id, optional. When supplied the
  name is not geocoded a second time, so a caller that has shown the user a
  specific match cannot end up downloading a different place. Only the relation
  range (3600000000–3699999999) and the way range (2400000000–2499999999) are
  accepted.

## Output contract

All endpoints expose fixed point, line and polygon sinks. Agent execution binds
all three to temporary QGIS outputs. Empty geometry outputs may be discarded by
the dock, while Processing callers receive the declared sinks.

## Safety contract

- Extents come only from the current map, an existing project layer, or a
  geocoded place name.
- One request covers at most 100 square kilometres. A wider extent is split
  into tiled requests, bounded at 2,500 square kilometres, 40 requests and
  400,000 merged features per download.
- Network requests reach four fixed hosts and no others: the three pinned
  Overpass mirrors and the Nominatim geocoder. Geocoding sends only the place
  name, percent-encoded, no more than once per second.
- Raw query text, arbitrary URLs, file paths and API keys are not parameters.
- Advanced requests expose at most four validated key/value rows, a fixed
  ANY/ALL enum and a fixed geometry-scope enum.
- Live algorithm signatures are checked against SmartModeler's reviewed policy.
- A proposal never executes without explicit user approval.

## Compatibility

Protocol `1.x` keeps the provider ID, algorithm IDs, approval boundary and
parameter meanings stable. Additive presets may extend the live `PRESET` enum.
A breaking algorithm or binding change requires a new protocol major version.
