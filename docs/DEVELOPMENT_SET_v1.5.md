# 02Agent OSM Downloader — Development Set for v1.5.0

**Goal: more people use it, and the people who try it keep using it.**
Written 2026-10-26 from measured evidence, not from a feature wish-list. Every
item names the friction it removes; anything that did not clear that bar was
left out (§4).

## 0. Baseline (measured 2026-10-26)

| Signal | Value | Source |
|---|---|---|
| Hub downloads | **13,597** in 77 days (since 2026-08-10) | `plugins.xml?qgis=3.44` |
| Nearest competitors | QuickOSM 3.45 M (since 2014), OSMDownloader 1.04 M (since 2015) | same |
| Hub version | 1.4.0 = local | `pf status --hub` |
| Open issues | none on GitHub | `gh issue list` |
| Code | 9.4 k lines; 4 Processing algorithms; 43 presets / 16 groups | `git ls-files` |

The trend is good for a 2.5-month-old plugin; the competition is a decade old.
Growth has to come from (a) what a first-time user meets in the first minute
and (b) the moments where a returning user currently has to do something by
hand or reach for another plugin.

## 1. Frictions found by reading the code

1. **Every download is a temporary memory layer.** `_add_result_layers` adds
   `context.takeResultLayer(...)` straight to the project. Close QGIS and the
   data is gone; QGIS warns about temporary layers on save. A user who wanted
   data — the whole point — has to export three layers by hand and loses the
   styling doing it.
2. **The area can only be the map view, the active layer's extent or a
   geocoded place.** No "the features I selected", no "a box I draw", no
   "within 500 m of this place" — the last being QuickOSM's most used mode
   ("cafés around the station").
3. **Nothing remembers a download.** The Command tab keeps a command history;
   a Presets or Query download cannot be repeated without re-entering it, and
   updating last month's data means rebuilding the request.
4. **A wildcard ("All tagged objects") pull on a large extent gets no
   warning** (open backlog since v1.3.0); it is many times heavier than a
   filtered preset over the same area.
5. **The data carries no date and no attribution.** Overpass returns
   `osm3s.timestamp_osm_base`; it is discarded. ODbL requires attribution, and
   a planner citing the data needs "OSM as of …".
6. **The command router only understands English.** The author's own market
   (Türkiye) types "Konak'ta parkları indir".
7. **Hub copy talks about the plugin, not the user's task.** The description
   leads with "acquisition studio", "structured multi-tag queries", "offline
   command router"; the words people search the Hub for — *download*, *roads*,
   *buildings*, *POI*, *GeoPackage* — are thin or missing from tags.

## 2. The set

Each item lists acceptance criteria that a test asserts; "it works when I click
it" is not an acceptance criterion.

### O1 — Keep what you download (GeoPackage output) — *highest value*
- Dock: **Save to** combo — *Temporary layers* (default, unchanged behaviour)
  / *GeoPackage file…*. With a file, every result layer is written as its own
  table into one `.gpkg`, its QML style stored in the GeoPackage
  (`layer_styles`, default style), and the layers loaded from the file.
  Re-downloading into the same file replaces same-named tables only.
- Acceptance: smoke writes a fixture download to a temp `.gpkg`; the file has
  the point/line/polygon tables and a `layer_styles` row per table; the loaded
  layers are `ogr` provider layers whose renderer equals the in-memory one.

### O2 — More ways to say *where*
- New area modes: **Selected features (active layer)** — union bbox of the
  selection, refused when nothing is selected — and **Draw a rectangle on the
  map** — a map tool that captures one rectangle and returns to the dock.
- Acceptance: unit-level `_extent()` returns the selection bbox in the layer
  CRS; the map tool emits a `QgsRectangle` in canvas CRS (smoke drives it
  with synthetic press/move/release events).

### O3 — "Around" searches
- Area mode **Around a point**: centre = geocoded place, selected feature's
  centroid or canvas centre; radius 50 m – 5 km. The request is the circle's
  bounding box (so every existing ceiling, tiling and cache path applies
  unchanged) and features are then kept only if they intersect the circle —
  exact, client-side, no new Overpass syntax.
- Processing: `AROUND_X`, `AROUND_Y` (EPSG:4326) and `AROUND_METERS` on the
  four endpoints, all optional; radius capped in `core/query.py`.
- Acceptance: pure test of the circle filter (inside/outside/straddling
  geometries); smoke run on fixture data keeps only intersecting features.

### O4 — Download history with one-click re-run
- The last 15 dock downloads (label, algorithm id, parameters, area, time,
  feature counts) persisted in `QgsSettings`; a **Recent** list re-runs one
  exactly, with today's data. A pure `core/history.py` owns the format so it
  is testable without QGIS.
- Acceptance: pure round-trip/size-cap/corrupt-entry tests; smoke records a
  download and re-runs it with identical parameters.

### O5 — Warn before a heavy wildcard pull
- When the chosen datasets include a wildcard preset and the request is
  larger than 2 km², a confirmation explains the cost (features per km² of an
  unfiltered pull) before anything is sent. Filtered presets are untouched.
- Acceptance: pure predicate test (`heavy_request(presets, area)`); dock
  smoke asserts the prompt fires only in the heavy case.

### O6 — Data date and attribution on every layer
- Capture `osm3s.timestamp_osm_base` from the Overpass response (oldest across
  tiles), write it and "© OpenStreetMap contributors (ODbL)" into each result
  layer's metadata (rights, abstract, `history`) and a custom property, and
  show "OSM data as of …" in the finished status line.
- Acceptance: pure extraction test (single and tiled payloads, missing
  field); smoke asserts the metadata on a result layer.

### O7 — Turkish commands
- The router accepts Turkish: dataset words (bina, yol, park, okul, hastane,
  durak, cami, …), verbs (indir, getir, göster), Turkish place suffixes
  (`Konak'ta`, `İzmir'deki`, `Kadıköy'de`) and negation (`… hariç`,
  `… olmadan`). English behaviour is unchanged; UI copy stays English.
- Acceptance: ≥ 20 Turkish command tests routed to the same presets/places as
  their English equivalents, plus the existing English suite unchanged.

### O8 — Hub copy that matches what users search for
- Rewrite `description` (task-first, ≤ 300 chars), `about` (what you can do
  in the first minute, then depth), `tags` (download, roads, buildings, poi,
  amenities, geopackage, nominatim, boundary…), README's first screen with a
  four-step quick start. No self-praise wording.
- Acceptance: catalog numbers in the copy come from the catalogue (existing
  test pattern); `%`-free metadata; gate green.

## 3. Release discipline
One release, **v1.5.0**, after all items pass `pf verify` on QGIS 3.44 LTR and
4.2: version bump, CHANGELOG, tag, push to both remotes, GitHub + GitLab
releases with the zip, Hub upload via `hub_upload.py`.

## 4. Considered and left out — and why
- **Raw Overpass QL editor** — the plugin's safety model is bounded,
  reviewable requests; a free-form QL box defeats the ceilings and is what
  QuickOSM already does. Power users keep the six-filter Query tab.
- **Translated UI** — repository rule: shipped UI copy is English-only. O7
  only widens what the command box *accepts*.
- **.osm/.pbf file import** — QGIS opens both natively.
- **History/diff of OSM edits over time** — needs the attic API, slow and
  rate-limited; low demand relative to cost.
