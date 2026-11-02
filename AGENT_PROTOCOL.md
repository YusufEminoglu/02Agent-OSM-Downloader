# 02Agent OSM Downloader — Agent Protocol v1

02Agent OSM Downloader uses QGIS Processing as its stable agent transport. It
does not expose arbitrary Python objects, UI automation, raw Overpass queries or
unreviewed network actions to an agent.

The machine-readable contract is [`agent_protocol.json`](agent_protocol.json).

## Discovery handshake

02SmartModeler performs the following bounded handshake:

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

02SmartModeler runs three of them; `download_place` is not on its reviewed
allowlist. It binds only the parameters below, and every other parameter keeps
its default in an agent run (no area id, no exclusion, no around search, exact
values, no Postpass fallback):

| Endpoint | Parameters 02SmartModeler binds |
| --- | --- |
| `download_preset` | `PRESET`, `EXTENT` |
| `download_custom_tag` | `KEY`, `VALUE`, `GEOMETRY`, `EXTENT` |
| `download_advanced` | `MATCH_MODE`, `GEOMETRY`, `KEY_1`–`KEY_4`, `VALUE_1`–`VALUE_4`, `EXTENT` |

## Parameters every endpoint shares

- `EXTENT` — the area to download, in any CRS QGIS can transform to WGS84.
- `AREA_ID` — an already resolved area id, optional. It keeps the result
  inside that place's mapped boundary. Only the relation range
  (3600000000–3699999999) and the way range (2400000000–2499999999) are
  accepted.
- `EXCLUDE_KEY` / `EXCLUDE_VALUE` — optional. When set, a fetched feature
  carrying that tag (any value, or the given value only) is dropped after the
  OSM response arrives — a client-side filter, not additional query text.
- `AROUND_X`, `AROUND_Y`, `AROUND_METERS` — an optional around search. With
  `AROUND_METERS` between 50 and 5,000, the bounding box of the circle around
  (`AROUND_X`, `AROUND_Y`), in WGS84, replaces `EXTENT`, so every size limit
  applies to what is actually requested, and only features that touch the
  circle are kept. `0` turns it off.
- `ALLOW_FALLBACK` — boolean, optional, off by default (`false`); turn it on
  in the dock or with `ALLOW_FALLBACK`. While it is `true`, a part of the
  download that every Overpass server has failed or is resting for is
  requested from Geofabrik's Postpass instead; otherwise the part stays
  missing.

`download_place` resolves a validated place name through the OpenStreetMap
Nominatim geocoder. Only when Nominatim cannot answer does an
administrative-name search on the Overpass servers run; a plain "no match" is
final. It then applies the same bounded extent and output contract as
`download_preset`. Two extra parameters control how the place is used:

- `CLIP` — `0` keeps the result inside the place's mapped boundary: the
  place's outline is fetched from Nominatim, the boundary's bounding box is
  requested in parts, and only features that touch the outline are kept. If
  the outline cannot be fetched, the Overpass server's own area filter is used
  instead and the log and `NOTICE` say so. `1` uses the place's rectangular
  extent. A place with no mapped boundary falls back to its rectangle and the
  log says so.
- `AREA_ID` — when supplied, the name is not geocoded a second time, so a
  caller that has shown the user a specific match cannot end up downloading a
  different place.

## Output contract

All endpoints expose fixed point, line and polygon sinks. Agent execution binds
all three to temporary QGIS outputs. Empty geometry outputs may be discarded by
the dock, while Processing callers receive the declared sinks.

Every endpoint also returns these values:

| Output | Meaning |
| --- | --- |
| `OSM_TIMESTAMP` | The date of the OSM data the servers reported (UTC), the oldest across parts; blank when none was reported. |
| `TILES_TOTAL` | The parts the download was made of; a part split into four counts as four. |
| `TILES_MISSING` | The parts that could not be downloaded; `0` means complete. Running the same parameters again within 24 hours fetches only the missing parts. |
| `DATA_SOURCE` | The servers that delivered the data, comma-separated, or `local cache`. |
| `FEATURES_DROPPED` | Matched OSM features that could not be built or written (no coordinates, an outline that does not close, an invalid outline that could not be repaired, a failed projection, or a feature the output refused); `0` means none. |
| `FEATURES_OUTSIDE` | Matched OSM features left out because they lie outside the chosen area (the extent box, the place's mapped boundary or the around circle); not a failure. `0` means none. |
| `NOTICE` | One plain sentence about the result, for example that the server may not hold a place's boundary; blank when there is nothing to say. |

A download that ends without any data raises an error whose text names each
server and what it answered.

## Safety contract

- Extents come only from the current map, an existing project layer or the
  selected features of one, a rectangle drawn on the map, a geocoded place
  name, or the circle of an around search.
- One request covers at most 100 square kilometres. A wider extent is split
  into at most 40 parts and 2,500 square kilometres; a part a server finds too
  dense is split into four, at most twice. A download sends at most 64
  requests and stops at 400,000 merged features.
- Network requests reach these hosts and no others:
  - `overpass.private.coffee` and `overpass-api.de`, the Overpass API;
  - `postpass.geofabrik.de`, only for a part no Overpass server could deliver,
    and only while `ALLOW_FALLBACK` is true;
  - `nominatim.openstreetmap.org`, for place names and the outline of a chosen
    place, at most once per second. Geocoding sends only the place name,
    percent-encoded.

  The dock's optional basemap loads tiles from `tile.openstreetmap.org`; no
  endpoint does.
- Requests follow the server operators' rules: one request at a time per
  server, a rest for a server that is busy, refuses or asks to slow down, at
  most one attempt per server for each part, and a `Referer` header that
  identifies the plugin.
- Raw query text, arbitrary URLs, file paths and API keys are not parameters.
- Advanced requests expose at most six validated key/value rows, a fixed
  ANY/ALL enum, a fixed geometry-scope enum, and an optional `VALUE_MODE`
  boolean that renders every filled value as a regular expression (`~`)
  instead of an exact match. Regex values still cannot carry quotes,
  backslashes or semicolons, and a pattern is refused before any request when
  the servers' POSIX engine rejects it (`(?…)` groups, lookarounds,
  backreferences) or when it could hang or be read two ways (two repeat
  operators in a row, a repeated group that repeats inside, such as `(a+)+`).
- Live algorithm signatures are checked against 02SmartModeler's reviewed
  policy.
- A proposal never executes without explicit user approval.

## Compatibility

Protocol `1.x` keeps the provider ID, algorithm IDs, approval boundary and
parameter meanings stable. New parameters are optional, so an earlier
proposal still runs. New datasets are appended to the live `PRESET` enum, so
an existing index always names the same dataset. A breaking algorithm or
binding change requires a new protocol major version.
