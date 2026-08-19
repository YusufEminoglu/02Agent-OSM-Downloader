# Changelog

## [1.2.0] - 2026-08-19

### Query

- **Exclude tag.** Every download endpoint (Presets, Place, Custom tag,
  Advanced query) now accepts an optional exclude key/value. A matching
  feature is dropped once the OSM response arrives — a client-side filter,
  not additional Overpass query text — and is surfaced on the Query tab as
  "Exclude tag (optional)".
- **Regex value matching.** The Advanced query panel gained a "Match values
  as regex (~)" checkbox that renders every filled value as an Overpass
  regex selector instead of an exact match. Regex values still cannot carry
  quotes, backslashes or semicolons, capped at 60 characters.
- **Six tag filters**, up from four (`MAX_ADVANCED_FILTERS`).
- **Persistent Overpass cache.** Responses now persist to a small on-disk
  SQLite cache under the QGIS profile directory instead of an in-session
  dictionary, so re-downloading the same region stays instant even after
  restarting QGIS.
- **Mirror retry.** A transient failure (a dropped connection, a 5xx) gets
  one retry against the same mirror, with a short pause, before the request
  moves on to the next pinned mirror.

### Command

- **Typo tolerance.** A misspelled preset word (`buildngs`, `helthcare`)
  now falls back to a bounded offline fuzzy match when no exact phrase is
  found, weighted so a word owned by one preset outweighs one shared by
  several.
- **Negation.** "download parking without charging" now extracts an
  exclude tag and routes the match to the Query tab, pre-filled with both
  the matched tag(s) and the exclusion.
- **Same-theme combination.** A command naming two datasets from the same
  group (e.g. "parks and water") now checks both, generalising the existing
  Urban Context special case.
- **Command history.** The Command tab keeps a clickable list of the last
  ten interpreted commands.

## [1.1.1] - 2026-08-14

### Follow the companion plugin's rename

SmartModeler GIS is now [02Agent Smart Modeler](https://github.com/YusufEminoglu/02Agent-Smart-Modeler)
(package `planx_smartmodeler` → `zero2smartmodeler`). The AI Connections bridge
(`core/smartmodeler_bridge.py`) now looks up the companion plugin under its new
package name and every user-facing message uses the new display name. No
functional change for users who update both plugins together; users who only
update this one will simply see the old lookup fail closed until they also
update 02Agent Smart Modeler, exactly as it already behaved when the companion
plugin was absent.

## [1.1.0] - 2026-08-11

### Added

- **Wide extents.** A request larger than one bounded query is split into a grid
  of tiles, downloaded in sequence with per-tile progress and cancellation, then
  merged by OpenStreetMap identity so an object crossing a tile seam stays one
  feature. A single download now reaches 2,500 km², up from 100 km².
- **Nominatim place search.** Place names are resolved by the OpenStreetMap
  geocoder, which understands free-form text worldwide. Every match is listed
  with its address context and administrative level, and selecting one zooms the
  canvas; the previous Overpass administrative search remains the fallback.
- **Exact boundary downloads.** When a place has a mapped boundary, the request
  is filtered by that area instead of a rectangle, so a district download stops
  at the district edge. `download_place` gains `CLIP` and `AREA_ID` parameters,
  and the dock passes the match you chose rather than geocoding the name twice.
- A live request estimate above the Download button, reporting the area in km²
  and how many bounded requests it needs, updated as the map is panned and
  zoomed.
- Five illustrated task guides on the documentation site, built from real
  screenshots of the dock, plus a documentation landing page.

### Fixed

- **The `route` and `tourism` attribute columns held each other's values.** Every
  downloaded feature was affected, which broke transit-route and tourism map
  styling. The field list and the value list are now generated from one table so
  they cannot drift apart again.
- **Multi-part route relations could be lost.** The line output declared
  `LineString` while joined route relations produce `MultiLineString`; the output
  is now `MultiLineString` and single-part ways are promoted to match.
- The Command tab claimed no data ever leaves QGIS, which stopped being true for
  place lookups; it now states exactly what is sent. The Agent tab said two
  Processing endpoints where there are four.
- A command naming roads and trees without buildings no longer misses the Urban
  Context preset.
- A boundary request is now filtered by the extent as well as the area, so a
  caller cannot pair a country-sized area id with a small extent and escape the
  area ceiling. An area carries no size of its own, so the extent is required.
- A place chosen from the match list is no longer re-resolved when its
  rectangle is used, which could silently download a different place of the
  same name.
- A place with a mapped object that Overpass has no area for — a river or route
  relation, for instance — falls back to its rectangle instead of running a
  boundary query that matches nothing. A request that returns no features now
  explains why rather than adding three empty layers in silence.
- Starting a new place search clears the previous match, so a failed search can
  no longer leave the earlier place armed for download.
- Cancelling a place search no longer leaves the Search button disabled for the
  rest of the session.
- A project CRS that cannot represent the current view no longer raises a
  transform error on every pan while the request estimate refreshes.

### Changed

- Extent area is measured at its worst case rather than at the mean latitude, so
  a tall box can no longer slip an oversized tile past the limit.
- The session response cache holds 24 entries instead of 8, and a tiled job skips
  its politeness pause for tiles already cached.
- `agent_protocol.json` declares every host the plugin contacts and the real
  limits, and the test suite asserts the manifest against the enforced values.

## [1.0.0] - 2026-08-10

This is the first stable public release and the first release prepared for
submission to the QGIS Plugin Hub.

### Added

- A polished four-tab workflow for Presets, Query, Command and Agent use.
- 30 curated OSM presets across 15 thematic groups, including named-place
  acquisition and multi-dataset Urban Context requests.
- Four documented Agent Protocol v1 Processing endpoints with explicit approval,
  bounded extents and temporary-only outputs.
- Complete user and technical reference documentation, first-run guidance and
  third-party attribution notices.

### Changed

- Remove the QGIS experimental flag and replace alpha language with stable-release
  metadata throughout the Hub-facing copy.
- Align the About dialog, README, metadata, protocol documentation and manual on
  the current 30-preset and four-endpoint feature set.
- Harden malformed place-bound handling with an explicit suppression boundary so
  the QGIS Hub security scan does not see a bare exception pass.

### Compatibility

- QGIS 3.28 or later, including QGIS 3.44 LTR and QGIS 4.
- GPL-3.0-or-later; no pip dependency or API key is required.

## [0.5.5] - 2026-08-09

- Add a complete OpenStreetMap, tile-service and Overpass API third-party notice.
- Record OpenStreetMap attribution metadata directly on the optional basemap layer.
- Extend the reference manual with data attribution and service-use guidance.

## [0.5.4] - 2026-08-09

- Support natural-language place commands using the `of` connector, including `Download trees of London`.
- Add a regression test for the exact trees-of-London workflow.

## [0.5.3] - 2026-08-09

- Automatically resolve place-aware Command requests in the background and zoom the QGIS canvas to the matched administrative extent.
- Keep download-time place resolution and mirror failover as a safe fallback when preview resolution is unavailable.
- Document automatic place zoom behavior in the reference manual and README.

## [0.5.2] - 2026-08-09

- Refresh the English reference manual with a practical Getting Started workflow.
- Document the new multi-dataset selection controls and global Command examples.
- Add reliability and recovery guidance for mirror failover, limits and ambiguous places.
- Update the manual landing metadata and version presentation.

## [0.5.1] - 2026-08-09

- Refine the dock hierarchy with a clearer OSM header, status badge and workflow guidance.
- Add dataset selection actions and a live selection counter for multi-dataset downloads.
- Expand global Command examples with more explicit administrative place names.
- Refresh the toolbar icon for stronger small-size readability and fix the About dialog copy.

## [0.5.0] - 2026-08-09

- Add an English **Add OSM basemap** action with duplicate protection and
  OpenStreetMap attribution metadata.
- Expand Urban Context with public transport and public-realm datasets, plus
  theme focus and related-context guidance in the Presets tab.
- Add OSM highway-category road widths with an optional uniform-width mode.
- Replace the Command examples with a broader global set of named-place
  workflows and improve multi-mirror diagnostics and actionable failures.
- Support line geometries from mapped route relations such as bus and tram
  routes, and expose the route tag in result attributes.

## [0.4.6] - 2026-08-09

- Make all shipped interface copy, examples, documentation and metadata
  English-only.
- Restrict offline command interpretation to English keywords and connectors.

## [0.4.5] - 2026-08-09

- Allow multiple datasets to be checked within a selected theme and combine
  them into one bounded Processing request.
- Expand Query examples and add tag-key/value suggestions, filter count and
  clear-filter controls.
- Add place-aware commands and `download_place`, resolving administrative
  names such as Konak, Van and London through the pinned Overpass mirrors.

## [0.4.4] - 2026-08-09

- Declare QGIS 3.28+ and QGIS 4 compatibility in plugin metadata and
  documentation while retaining the shared SmartModeler AI Connections bridge.

## [0.4.3] - 2026-08-07

- Added online user manual link (https://yusufeminoglu.github.io/02Agent-OSM-Downloader/) and GitHub repository star call-to-action.

## [0.4.2] - 2026-08-07

- Add floating Save as PDF button to reference manual

All notable changes to 02Agent OSM Downloader are documented here.

## [0.4.0] - 2026-08-01

### Added

- Add 16 persistent cartographic themes with compact live palette previews:
  twelve palettes adapted from OSM Quick 3D and four new 02Agent palettes.
- Add native categorized renderers for functional building classes, road
  hierarchy, rail, water, green space, trees, transit and common POI classes.
- Add a **Query** panel for up to four validated OSM key/value filters with
  ANY/OR or ALL/AND matching and point, line, polygon or mixed geometry scope.
- Add four ready-to-load advanced examples and a live, read-only Overpass QL
  preview with a non-executable selected-extent placeholder.
- Add the stable `zero2agentosm:download_advanced` Processing endpoint with
  fixed temporary point, line and polygon outputs.
- Add SmartModeler allowlist, live-signature and proposal validation coverage
  for advanced multi-tag requests.

### Changed

- Apply the chosen map style only to newly downloaded result layers, preserving
  the project background and existing layer styles.
- Refresh the dock with lighter palette-derived dark surfaces, brighter cards,
  selected-tab fills, clearer input separation and WCAG-tested text contrast.
- Rename the main preset and integration tabs to **Presets** and **Agent**, add a
  contextual run summary, and hide download controls where they are irrelevant.
- Collapse the generated Overpass preview by default so the complete advanced
  form fits without scrolling; keep it one click away for inspection.
- Mark the release as experimental while the public alpha receives field
  testing.
- Record all tags matched by advanced filters in the additive `matched_tags`
  output attribute.
- Update the network user agent, public Agent Protocol manifest and user-facing
  documentation for the three-endpoint contract.

### Fixed

- Synchronize the shared session cache across concurrent Processing tasks.
- Detach dock and menu actions during unload so plugin reloads do not retain
  orphaned Qt objects.

### Security

- Keep advanced requests bounded to four normalized tag filters, fixed ANY/ALL
  semantics, QGIS-owned extents, pinned HTTPS mirrors and temporary outputs.
- Continue rejecting editable raw Overpass, arbitrary endpoints, paths and
  credentials.

## [0.3.0] - 2026-07-30

### Added

- Add the **Urban Context — Roads, buildings & trees** preset: all highway
  ways, building footprints, individual trees and tree rows in one bounded
  compound Overpass request with temporary point, line and polygon outputs.
- Route multi-theme requests containing roads, buildings
  and trees directly to the combined preset instead of selecting only the first
  requested theme.
- Add catalog, router and exact-query regression tests for the combined preset,
  plus SmartModeler dual-runtime validation of its three-output approval
  contract.

## [0.2.1] - 2026-07-30

### Changed

- Make the missing-SmartModeler warning explicitly direct users to QGIS
  **Plugins > Manage and Install Plugins**, search for SmartModeler GIS, install
  it, enable it, and reopen the Connections tab.

## [0.2.0] - 2026-07-30

### Fixed

- Keep AI Connections actionable when SmartModeler is installed but not fully
  initialized, and show an explicit error instead of a silent no-op.
- Assemble fragmented outer and inner ways in OSM multipolygon relations.
- Prevent substring-only command matches such as `car` inside unrelated words.
- Reset asynchronous task controls even when result-layer handling fails.
- Use the current `QMetaType` string field API instead of the deprecated
  `QVariant` field-constructor path.

### Changed

- Rebuilt the dock as compact Download, Command and Connections tabs.
- Derive surfaces, text, borders and focus states from the active QGIS palette
  for light, dark, high-contrast and custom theme compatibility.
- Disable inactive preset/custom controls and add clearer privacy, extent and
  execution guidance.

## [0.1.2] - 2026-07-29

### Fixed

- Convert QGIS 4 floating-point task progress to the integer value required by
  `QProgressBar`, preventing repeated `setValue(float)` Python errors while a
  successful download continues.

### Added

- Visible SmartModeler AI profile/provider status in the downloader dock.
- Direct buttons for the shared AI Connections dialog and Agent Workspace.
- A secret-safe public bridge that reuses SmartModeler's QGIS vault profiles
  instead of duplicating API keys in the downloader.

## [0.1.1] - 2026-07-29

### Added

- Versioned, machine-readable Agent Protocol v1 contract.
- Visible agent endpoint status in the dock and detailed integration About text.
- Processing search tags and direct protocol help link.
- Regression tests for protocol safety and Hub discovery metadata.

### Changed

- Expanded the QGIS Hub description, About content and discovery tags.

## [0.1.0] - 2026-07-29

### Added

- Curated preset catalog with 13 thematic groups and 26 datasets.
- One compound, bounded Overpass request per preset.
- Custom OSM key/value acquisition for points, lines and polygons.
- Offline English command interpretation.
- Current-map or active-layer extent selection.
- Asynchronous QGIS task execution, cancellation and themed layer grouping.
- Processing provider for standalone, model and SmartModeler Agent use.
- Three-mirror fallback, strict resource limits and in-session caching.
