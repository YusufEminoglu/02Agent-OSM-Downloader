# Changelog

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
- Route Turkish and English multi-theme requests containing roads, buildings
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
- Offline Turkish/English command interpretation.
- Current-map or active-layer extent selection.
- Asynchronous QGIS task execution, cancellation and themed layer grouping.
- Processing provider for standalone, model and SmartModeler Agent use.
- Three-mirror fallback, strict resource limits and in-session caching.
