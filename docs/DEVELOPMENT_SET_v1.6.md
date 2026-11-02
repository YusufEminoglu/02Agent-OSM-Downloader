# 02Agent OSM Downloader — Development Set for v1.6.0

**Goal: a download either works or says exactly why, and it keeps working when
the public Overpass servers do not.**
Written 2026-11-02 from four independent audits (network, data, first-minute
UX and truthfulness, market) that measured the plugin in real QGIS 3.44 and
4.2, against a fake Overpass server, real OSM extracts and the live servers.
Every item names the measured failure it removes. §5 lists what was left out.

## 0. Baseline (measured 2026-11-02)

| Signal | Value | Source |
|---|---|---|
| Hub downloads | 16,334; about 390/day this week, 177/day lifetime | `plugins.xml?qgis=3.44`, `hub_dashboard` |
| Hub "Popular" rank | about #28 of 3,768 (downloads per day) | Hub source, feed |
| Hub rating | **gone**: the Hub deleted all votes on 2026-10-07 (PR #445) | Hub repo |
| `pf verify` | green: 134 pure tests, both QGIS suites on 3.44 and 4.2, scan CLEAN | `pf verify` |
| Public Overpass | 2 answers in 16 probes this morning; the pinned hosts 1 in 12 | probe log |
| Overpass rules | per-IP and per-app CPU quota (~10 min/24 h, hard ban); wait ≥ 30 s after 429/406; apps with many 429s get banned; an immediate 502 means rejected, an accepted query should be awaited | OSM forum 140598 #157/#171/#182, 149449 #9 |
| Feedback channel | `tracker=` is a 404 for anonymous visitors | HTTP check |

## 1. What the audits found (ranked by harm)

1. **Incomplete downloads are reported as "Finished".** 24 of 25 tiles lost →
   `Finished — Points: 1.`, saved to Recent. An İzmir buildings job lost the
   city-centre tile (36.7% of the data) and said "Finished". Processing callers
   get no signal either.
2. **Failures take minutes and say nothing.** One tile with all hosts hanging:
   303.7 s; 25 tiles: about 2 h 7 min. Every failure, including Cancel, ends in
   "Download failed or was canceled. Check the Processing log."
3. **The client breaks the 2026 Overpass rules.** 429, 406 and 400 are retried
   after 1.2 s (the 4xx no-retry branch is unreachable); a hung host is retried;
   no memory of a bad host; no `[maxsize]`, so every request claims 512 MiB and
   is the first to be shed.
4. **Identity and hosts.** QGIS overwrites the plugin's User-Agent, so nothing
   identifies the plugin. `overpass.kumi.systems` and `overpass.private.coffee`
   are the same machine.
5. **"Exact mapped boundary" never worked through Nominatim.** jsonv2 returns
   `category`; the code reads `class`. Every named-place download since v1.1.0
   was a rectangle. Overpass area tables also stopped updating in 2024/2025.
6. **First click on an empty map is a dead end** ("outside WGS84 bounds").
   Download, Cancel and status sit 400–730 px below the fold at every normal
   window size.
7. **Data truth.** Open ways become invented polygons (3,178 in two metros);
   holes come from member roles, not geometry; dense tiles exceed the 64 MB
   ceiling and are never split; the 400k cap fires after every tile is
   downloaded; ODbL attribution is not in the saved GeoPackage; all fields are
   strings; nothing counts dropped features; failed jobs pin memory (+1.4 GB
   each) until restart.
8. **Smaller but visible.** The basemap lands on top of the data; 21 of 50
   plausible commands become "Place centres"; a failed place search promises a
   retry that never happens; the Query tab starts in AND + Polygons; a bad
   GeoPackage path loses the data; the dock is unreadable under QGIS's Blend of
   Gray theme; `QgsField(name, QMetaType.Type.QString)` needs QGIS 3.38 while
   the plugin claims 3.28.

## 2. The set

Acceptance criteria are what a test asserts. A fake server on 127.0.0.1 stands
in for the public servers in every network test.

### R — Reliable, polite network client
- **R1 Truthful outcome.** The fetch returns the merged payload plus the
  missing tiles. Processing outputs `TILES_TOTAL`, `TILES_MISSING`,
  `DATA_SOURCE`. A job with missing tiles never reports a clean finish.
- **R2 Classify every failure** in a pure module: rate (429), blocked
  (403/406), busy (502/503/504, capacity page, duplicate query), client
  timeout, network/TLS, server timeout/out of memory/too large (`remark`, size
  ceilings), bad query (other 4xx). Read the reply status even when the request
  reports an error.
- **R3 Host memory and pacing**, process-wide and thread-safe: per-host
  cooldown 429 → 60 s, 403/406 → 1 h, immediate 5xx → 45 s, timeout → 120 s,
  network → 120 s, TLS → 1 h; doubling on repeated strikes, capped at
  max(base, 15 min). One request at a time per host, ≥ 1 s apart. Order: hosts
  not cooling, most recent success first, then pin order.
- **R4 Budget and early stop.** Same-host retry only after a connection reset
  (once, 5 s later). At most one attempt per host per tile. Stop when the first
  tile fails on every host before anything succeeded; once data flows, stop
  after 2 consecutive lost tiles; wait for a cooling host only if ≤ 30 s; job
  ceiling 64 requests.
- **R5 Wait patiently, claim little.** Transfer timeout
  `T = min(55 s, QGIS network timeout − 2 s)`; query header
  `[timeout:max(10, T − 15)][maxsize:134217728]`. Abort a transfer as soon as
  it passes 64 MB.
- **R6 Adaptive tiles.** On server timeout, out of memory, > 64 MB or
  > 150,000 elements: split the tile into 4, depth ≤ 2 (100 → 25 → 6.25 km²),
  remember the depth for the rest of the job, do not send a heavy tile to other
  hosts. The 400,000-object cap is counted while tiles arrive and stops the job
  before more requests are sent.
- **R7 Cache honesty.** Never store empty, `remark` or stale (data more than
  7 days old) responses; per-row TTL (1 h, 24 h for tiles of an unfinished job
  so Continue can resume); purge expired rows on first use; byte budget;
  `VACUUM` after Clear cache; a cheap `has()`.
- **R8 Hosts and identity.** Pins: `overpass.private.coffee`, then
  `overpass-api.de` (its operators ask to be used last); the kumi alias goes.
  Every request carries a `Referer` naming the plugin, plus the User-Agent
  suffix where QGIS supports it (4.2+). Nominatim gets the same.
- **R9 Memory.** A failed or canceled run releases its memory: the algorithm
  body runs in an inner frame and the exception is re-raised from a thin outer
  frame.

### P — Postpass fallback (Geofabrik)
- **P1** A pure translator renders the same bounded tag selectors as SQL for
  `postpass.geofabrik.de`, with every user value hex-encoded so no user text
  reaches SQL syntax, and an adapter turns its GeoJSON into the existing
  element shape so matching, schema and styling stay unchanged.
- **P2** Used only when every Overpass host has failed or is cooling for a
  tile, and only while the user setting (dock) or `ALLOW_FALLBACK` parameter
  (Processing) allows it, default off (opt-in until Geofabrik answers). The source is shown in the status, the
  layer metadata and `DATA_SOURCE`.
- **Release gate:** the author contacts the Postpass operator before this ships.

### B — Exact boundaries that work
- **B1** Read Nominatim's `category` (keep `class` as a fallback); replace the
  hand-written fixture with a saved real response (TRAPS 5.3).
- **B2** Exact boundary = the place's polygon from Nominatim (`lookup`,
  simplified) + the bounding box requested in tiles + features kept only if
  they intersect the polygon, using a prepared geometry engine. No dependency
  on Overpass area tables, identical on every backend. If the polygon cannot be
  fetched, fall back to the server-side area filter and say so; an empty area
  result says the server may lack the boundary.

### D — Data that is true and complete
- **D1** Open ways never become polygons: a closure test (first == last,
  ≥ 4 points). Wildcard → line; explicit polygon-only request → dropped and
  counted.
- **D2** Ring nesting decides holes, not member roles; repair invalid output
  with `makeValid`. Real-data target: 3,490 of 3,491 relations correct.
- **D3** Count dropped features by reason, report them, output
  `FEATURES_DROPPED`.
- **D4** Typed companion columns, raw strings kept: `height_m`, `levels_n`,
  `lanes_n`, `maxspd_kmh`, `width_m`, `pop_n` (strict unit parser, NULL when
  unparseable), plus ellipsoidal `length_m` (lines) and `area_m2` (polygons).
  All names ≤ 10 characters for Shapefile export.
- **D5** Field creation works on QGIS 3.28–3.36: probe the `QMetaType`
  overload, fall back to `QVariant` (TRAPS 3.2 / 3.2b).
- **D6** Wildcard ("All tagged objects") relations limited to
  `type=multipolygon|boundary`, in the query and in client matching alike. The
  As-Is exclusion applies to wildcard matches only. Relation points only for
  presets that ask for them.
- **D7** Regex mode: refuse constructs Overpass's POSIX engine rejects
  (`(?…)`, lookarounds, backreferences) and nested quantifiers; translate POSIX
  classes (`[[:digit:]]` …) for client-side matching; `*` means any value.
- **D8** ODbL attribution and data date persist inside the GeoPackage
  (`saveDefaultMetadata`), asserted on a layer reopened from the file; the
  stored style embeds the crossing icon instead of an absolute local path;
  folder paths with `#` work.

### U — First five minutes
- **U1** Live status in the dock: elapsed time, part N of M, which server, and
  plain final messages (servers busy, rate limited, refused, offline, too
  dense, canceled, finished with gaps). Message bar when the dock is hidden.
- **U2** "Finished with gaps: N of M parts missing" + Recent entry marked
  incomplete + **Continue**, which re-runs the stored request so finished parts
  come from the cache.
- **U3** Empty map: plain message and Named place preselected. Download,
  Cancel and status in a fixed footer that is always visible.
- **U4** Basemap goes under the data. Opening the OSM As-Is theme restyles
  nothing. Query tab defaults to Match any + All geometries, also after Clear.
- **U5** Commands never silently become "Place centres": road/street words and
  a small EN/TR POI word table; "found the place, not the data" message.
- **U6** Place search: no Overpass fallback after a plain "no match"; search
  can be canceled; no false "will retry" promises.
- **U7** GeoPackage: refuse early without a file; "Will save to"; an
  unwritable path keeps temporary layers (OSError caught).
- **U8** Report a problem (prefilled GitHub issue with plugin, QGIS and OS
  versions and the last failure class, no coordinates), Manual link, version in
  About; `tracker=` points at GitHub issues.
- **U9** Readable under every built-in QGIS theme (contrast ≥ 4.5 fallback);
  disabled Download looks disabled; Recent rows distinguishable; Run again
  stores preset ids, not indexes.
- **U10** Rendering: As-Is building expression evaluated once; labels on point
  and polygon layers scale-gated.
- **U11** A "Everyday amenities (15-minute city)" preset.

### T — Truthful copy
- **T1** metadata, README, AGENT_PROTOCOL, `agent_protocol.json` (new
  parameters and outputs), CITATION, About and Processing help match the code:
  three runnable SmartModeler endpoints, host list including the optional
  basemap tiles, 1 h persistent cache, six extent modes, 17 styles, English and
  Turkish commands.
- **T2** Hub `description` and tags carry the words people search for
  (download OSM data, extract, street network, schools, hospitals); `about` is
  not indexed by the Hub or Plugin Manager.

## 3. Owner decisions (not taken by the developer)
- Contact Geofabrik (Postpass) before P ships; notify private.coffee, whose
  policy asks large projects to tell them in advance.
- VK's `maps.mail.ru` is the only other keyless global Overpass. Not added:
  users' extents would go to a Russian-hosted service and it was offline for
  three months in 2026. One line adds it if the owner prefers.
- Pushing, tagging and the Hub upload.

## 4. Release discipline
One release, **v1.6.0**, after every item passes `pf verify` on QGIS 3.44 LTR
and 4.2, an independent review, and a live smoke when the servers allow:
CHANGELOG heading written first (TRAPS 2.2), `release.ps1`, then the Hub upload
via `hub_upload.py` with the owner's approval.

## 4b. Status (2026-11-02, before release)
- Every item in §2 is implemented. `pf verify` is green on QGIS 3.44 and
  4.2: 348 pure tests and 8 QGIS suites (provider, v1.5, persistence and
  style, dock UX, fetch resilience, dock feedback, data truth, fallback and
  boundary). Each new suite was shown to fail on the code before its fix.
- Two independent reviews (code; UX and copy) found 1 blocker, 6 high and
  smaller items; all were fixed and their tests fail on the unfixed code.
  A mutation run of 43 one-line defects is caught in full.
- Test suites cannot reach a public host: `tests/_offline.py` redirects every
  plugin host and guards QGIS's network access; an operating-system sample of
  the QGIS processes during a full verify saw 0 non-local connections. The
  same module pins the data-age clock, so cache checks do not expire a week
  after their fixtures were recorded.
- Live smoke (one 0.3 km² Konak buildings download through the plugin):
  overpass.private.coffee did not answer in 55 s, overpass-api.de delivered
  605 buildings in 43 s, 2 requests in total.
- Not yet done: the owner decisions in §3 and the release itself.

## 5. Considered and left out — and why
- **Racing mirrors in parallel / many more mirrors** — explicitly forbidden by
  the operators, and the public pool is short of capacity, not of hostnames.
- **`/api/status` preflight** — hung for more than 20 s this morning while the
  interpreter refused in 0.2 s; it would add a wait exactly when it hurts.
- **Writing GeoPackage on the worker thread** — 1.7 s per 100k features on the
  GUI thread is real but bounded; the change touches every sink. Next release.
- **Country-scale admin boundaries** — depends on Postpass permission. Next.
- **GeoJSON/CSV/Shapefile output** — QGIS's own *Export > Save Features As*
  covers it; the GeoPackage keeps the style.
- **Raw QL, UI translation, .osm import, history/`[date:]`** — v1.5 reasons
  still hold, and attic queries now cost quota on an overloaded server.
- **Scheduled refresh, batch-city UI** — against the per-app quota.
- **ohsome, OSM editing API, Overture, PMTiles, QLever** — key required, policy
  forbids, no OSM tags, or weeks stale.
