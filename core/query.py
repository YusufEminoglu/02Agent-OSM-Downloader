"""Bounded Overpass query construction shared by UI and Processing."""
from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterable, Sequence, Tuple

from .catalog import GEOMETRY_KINDS, TagSpec

OVERPASS_ENDPOINTS: Tuple[str, ...] = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
OVERPASS_TIMEOUT_SECONDS = 45
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_FEATURES = 150_000
# One Overpass request stays small enough to be a polite, reliable request.
# Anything larger is split into a grid of these tiles rather than refused, so a
# whole district or city can be acquired without ever sending a heavy query.
MAX_TILE_AREA_KM2 = 100.0
# Ceiling for a complete tiled job.  Past this the honest answer is that the
# user wants a regional extract, not an Overpass download.
MAX_BBOX_AREA_KM2 = 2_500.0
MAX_TILES = 40
# Merged across every tile.  A single response is still capped at MAX_FEATURES.
MAX_TOTAL_FEATURES = 400_000
MAX_SELECTORS = 32
MAX_ADVANCED_FILTERS = 4
MATCH_MODES = ("any", "all")

_KM_PER_DEGREE = 111.32

_KEY_RE = re.compile(r"^[A-Za-z0-9_:.~-]{1,80}$")
_UNSAFE_VALUE_RE = re.compile(r"[\x00-\x1f\x7f\"\\;\[\]\(\){}]")


class QueryError(ValueError):
    """A bounded, user-actionable query or response error."""


def normalize_tag(key: object, value: object = "") -> Tuple[str, str]:
    key_text = str(key or "").strip()
    value_text = str(value or "").strip()
    if not _KEY_RE.fullmatch(key_text):
        raise QueryError("The OSM key contains unsupported characters.")
    if value_text == "*":
        value_text = ""
    if len(value_text) > 120 or _UNSAFE_VALUE_RE.search(value_text):
        raise QueryError("The OSM value contains unsupported query characters.")
    return key_text, value_text


def _widest_longitude_scale(south: float, north: float) -> float:
    """Return the largest km-per-degree-of-longitude factor inside the box.

    Longitude degrees are widest nearest the equator, so the honest worst case
    for an area estimate is the latitude edge closest to it.  Using the mean
    latitude instead under-reports a tall box and lets an oversized tile
    through.
    """
    if south <= 0 <= north:
        return 1.0
    closest_to_equator = min(abs(south), abs(north))
    return max(0.01, math.cos(math.radians(closest_to_equator)))


def bbox_area_km2(
    south: object, west: object, north: object, east: object
) -> float:
    """Return the worst-case planar area of a WGS84 box in km²."""
    south_f, west_f, north_f, east_f = (
        float(item) for item in (south, west, north, east)
    )
    return (
        (north_f - south_f)
        * _KM_PER_DEGREE
        * (east_f - west_f)
        * _KM_PER_DEGREE
        * _widest_longitude_scale(south_f, north_f)
    )


def _checked_bbox(
    south: object, west: object, north: object, east: object
) -> Tuple[float, float, float, float]:
    """Validate WGS84 sanity only, without applying any area ceiling."""
    try:
        values = tuple(float(item) for item in (south, west, north, east))
    except (TypeError, ValueError) as exc:
        raise QueryError("The selected extent is not valid.") from exc
    south_f, west_f, north_f, east_f = values
    if not all(math.isfinite(item) for item in values):
        raise QueryError("The selected extent is not finite.")
    if not (
        -90 <= south_f < north_f <= 90
        and -180 <= west_f < east_f <= 180
    ):
        raise QueryError("The selected extent is outside WGS84 bounds.")
    return values


def validate_bbox(
    south: object, west: object, north: object, east: object
) -> Tuple[float, float, float, float]:
    """Validate an extent that must fit in a single Overpass request."""
    values = _checked_bbox(south, west, north, east)
    area = bbox_area_km2(*values)
    if area <= 0 or area > MAX_TILE_AREA_KM2:
        raise QueryError(
            f"The selected extent is {area:,.1f} km²; a single OSM request is "
            f"limited to {MAX_TILE_AREA_KM2:,.0f} km²."
        )
    return values


def validate_job_bbox(
    south: object, west: object, north: object, east: object
) -> Tuple[float, float, float, float]:
    """Validate an extent that may be split across several tiled requests."""
    values = _checked_bbox(south, west, north, east)
    area = bbox_area_km2(*values)
    if area <= 0:
        raise QueryError("The selected extent has no area.")
    if area > MAX_BBOX_AREA_KM2:
        raise QueryError(
            f"The selected extent is {area:,.0f} km²; this downloader covers "
            f"up to {MAX_BBOX_AREA_KM2:,.0f} km². Zoom in, or download the "
            "area in parts."
        )
    return values


def tile_bbox(
    bbox: Tuple[object, object, object, object],
    max_tile_km2: float = MAX_TILE_AREA_KM2,
) -> Tuple[Tuple[float, float, float, float], ...]:
    """Split an extent into a grid of boxes that each fit one OSM request.

    Tiles are kept close to square so no single request degenerates into a
    long thin strip, which Overpass answers far more slowly than a compact
    box of the same area.
    """
    south, west, north, east = validate_job_bbox(*bbox)
    limit = max(1.0, float(max_tile_km2))
    area = bbox_area_km2(south, west, north, east)
    if area <= limit:
        return ((south, west, north, east),)

    scale = _widest_longitude_scale(south, north)
    height_km = (north - south) * _KM_PER_DEGREE
    width_km = (east - west) * _KM_PER_DEGREE * scale
    side_km = math.sqrt(limit)
    rows = max(1, math.ceil(height_km / side_km))
    columns = max(1, math.ceil(width_km / side_km))
    if rows * columns > MAX_TILES:
        raise QueryError(
            f"The selected extent needs {rows * columns} OSM requests; the "
            f"limit is {MAX_TILES}. Zoom in, or download the area in parts."
        )

    latitude_step = (north - south) / rows
    longitude_step = (east - west) / columns
    tiles = []
    for row in range(rows):
        tile_south = south + row * latitude_step
        tile_north = north if row == rows - 1 else tile_south + latitude_step
        for column in range(columns):
            tile_west = west + column * longitude_step
            tile_east = (
                east if column == columns - 1 else tile_west + longitude_step
            )
            tiles.append((tile_south, tile_west, tile_north, tile_east))
    return tuple(tiles)


def normalized_specs(specs: Iterable[TagSpec]) -> Tuple[TagSpec, ...]:
    unique = []
    seen = set()
    for spec in specs:
        if spec.geometry not in GEOMETRY_KINDS:
            raise QueryError("An unsupported geometry type was requested.")
        key, value = normalize_tag(spec.key, spec.value)
        row = TagSpec(key, value, spec.geometry)
        marker = (row.key, row.value, row.geometry)
        if marker not in seen:
            seen.add(marker)
            unique.append(row)
    if not unique:
        raise QueryError("At least one OSM tag selector is required.")
    if len(unique) > MAX_SELECTORS:
        raise QueryError("The preset contains too many OSM selectors.")
    return tuple(unique)


def advanced_specs(
    filters: Iterable[Tuple[object, object]],
    geometries: Sequence[str],
    match_mode: str = "any",
) -> Tuple[TagSpec, ...]:
    """Create bounded selectors for the structured advanced-query endpoint.

    Advanced requests deliberately accept tag fields rather than raw Overpass
    text.  Keeping this normalization in the shared core makes the dock,
    Processing provider and agent manifest use the same authority boundary.
    """
    mode = str(match_mode or "").strip().casefold()
    if mode not in MATCH_MODES:
        raise QueryError("The advanced match mode is not valid.")

    safe_filters = []
    seen_filters = set()
    for key, value in filters:
        normalized = normalize_tag(key, value)
        if normalized not in seen_filters:
            seen_filters.add(normalized)
            safe_filters.append(normalized)
    if not safe_filters:
        raise QueryError("At least one advanced OSM tag is required.")
    if len(safe_filters) > MAX_ADVANCED_FILTERS:
        raise QueryError(
            f"Advanced queries accept at most {MAX_ADVANCED_FILTERS} tag filters."
        )
    if mode == "all":
        keys = [key for key, _value in safe_filters]
        if len(keys) != len(set(keys)):
            raise QueryError(
                "ALL matching cannot use the same OSM key more than once."
            )

    safe_geometries = tuple(dict.fromkeys(str(item) for item in geometries))
    if not safe_geometries or any(
        item not in GEOMETRY_KINDS for item in safe_geometries
    ):
        raise QueryError("Choose at least one supported geometry type.")
    return normalized_specs(
        TagSpec(key, value, geometry)
        for geometry in safe_geometries
        for key, value in safe_filters
    )


def _render_query(
    safe_specs: Tuple[TagSpec, ...],
    scope: str,
    match_mode: str,
    preamble: str = "",
) -> str:
    """Render the bounded request.

    `scope` is the complete Overpass filter suffix appended to every selector,
    already parenthesised — one bounding box, or an area filter followed by a
    bounding box.
    """
    mode = str(match_mode or "").strip().casefold()
    if mode not in MATCH_MODES:
        raise QueryError("The query match mode is not valid.")

    selectors = []
    if mode == "any":
        for spec in safe_specs:
            tag = (
                f'["{spec.key}"="{spec.value}"]'
                if spec.value
                else f'["{spec.key}"]'
            )
            if spec.geometry == "point":
                selectors.append(f"  node{tag}{scope};")
            elif spec.geometry == "line":
                selectors.append(f"  way{tag}{scope};")
                selectors.append(f"  relation{tag}{scope};")
            else:
                selectors.append(f"  way{tag}{scope};")
                selectors.append(f"  relation{tag}{scope};")
    else:
        for geometry in GEOMETRY_KINDS:
            geometry_specs = tuple(
                spec for spec in safe_specs if spec.geometry == geometry
            )
            if not geometry_specs:
                continue
            tags = "".join(
                f'["{spec.key}"="{spec.value}"]'
                if spec.value
                else f'["{spec.key}"]'
                for spec in geometry_specs
            )
            if geometry == "point":
                selectors.append(f"  node{tags}{scope};")
            elif geometry == "line":
                selectors.append(f"  way{tags}{scope};")
                selectors.append(f"  relation{tags}{scope};")
            else:
                selectors.append(f"  way{tags}{scope};")
                selectors.append(f"  relation{tags}{scope};")

    return (
        f"[out:json][timeout:{OVERPASS_TIMEOUT_SECONDS}];\n"
        + (f"{preamble}\n" if preamble else "")
        + "(\n"
        + "\n".join(dict.fromkeys(selectors))
        + "\n);\nout body geom;"
    )


def _box_scope(bbox: Tuple[float, float, float, float]) -> str:
    """Return a bounding-box filter, parenthesised and ready to append."""
    south, west, north, east = bbox
    return f"({south:.7f},{west:.7f},{north:.7f},{east:.7f})"


def build_query(
    specs: Iterable[TagSpec],
    bbox: Tuple[object, object, object, object],
    match_mode: str = "any",
) -> str:
    """Build the single bounded request for an extent that fits one tile."""
    safe_specs = normalized_specs(specs)
    return _render_query(
        safe_specs, _box_scope(validate_bbox(*bbox)), match_mode
    )


def build_queries(
    specs: Iterable[TagSpec],
    bbox: Tuple[object, object, object, object],
    match_mode: str = "any",
    max_tile_km2: float = MAX_TILE_AREA_KM2,
) -> Tuple[str, ...]:
    """Build one bounded request per tile covering the whole extent."""
    safe_specs = normalized_specs(specs)
    return tuple(
        _render_query(safe_specs, _box_scope(tile), match_mode)
        for tile in tile_bbox(bbox, max_tile_km2)
    )


def validate_area_id(value: object) -> int:
    """Validate an Overpass area identifier derived from an OSM object id.

    Overpass derives area ids from OSM ids: a relation becomes
    3600000000 + id and a way becomes 2400000000 + id.  Only those two ranges
    are accepted, so no other numeric text can reach the query.
    """
    try:
        area_id = int(value)
    except (TypeError, ValueError) as exc:
        raise QueryError("The place area identifier is not valid.") from exc
    relation_area = 3_600_000_000 <= area_id < 3_700_000_000
    way_area = 2_400_000_000 <= area_id < 2_500_000_000
    if not (relation_area or way_area):
        raise QueryError("The place area identifier is out of range.")
    return area_id


def build_area_query(
    specs: Iterable[TagSpec],
    area_id: object,
    bbox: Tuple[object, object, object, object],
    match_mode: str = "any",
) -> str:
    """Build a request clipped to a real administrative boundary.

    An area filter follows the mapped boundary itself, so a district download
    stops at the district edge instead of at the corners of its bounding box.

    The extent is applied as well, and is not optional.  An area carries no
    size of its own, so without a second bounding filter a caller could hand in
    the area id of an entire country and issue an unbounded request; requiring
    the box means every area request is still covered by the job ceiling.
    """
    safe_specs = normalized_specs(specs)
    checked_id = validate_area_id(area_id)
    box = _box_scope(validate_job_bbox(*bbox))
    return _render_query(
        safe_specs,
        f"(area.searchArea){box}",
        match_mode,
        preamble=f"area({checked_id})->.searchArea;",
    )


def preview_query(
    specs: Iterable[TagSpec],
    match_mode: str = "any",
) -> str:
    """Return a display-only query with a non-executable extent placeholder."""
    return _render_query(
        normalized_specs(specs), "(<selected extent>)", match_mode
    )


def validate_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise QueryError("The OSM server returned an invalid response.")
    if str(payload.get("remark") or "").strip():
        raise QueryError("The OSM server timed out; zoom in and retry.")
    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise QueryError("The OSM response has no elements list.")
    if len(elements) > MAX_FEATURES:
        raise QueryError("The OSM response is too large; zoom in and retry.")
    if any(not isinstance(item, dict) for item in elements):
        raise QueryError("The OSM response contains an invalid element.")
    return payload


def merge_elements(payloads: Iterable[Any]) -> Dict[str, Any]:
    """Merge tiled Overpass responses into one de-duplicated payload.

    Overpass returns any object whose geometry touches the requested box, so
    a road or building crossing a tile seam comes back from both tiles.
    Keeping the first copy of each (type, id) leaves exactly one feature.
    """
    merged: Dict[Tuple[str, str], Any] = {}
    for payload in payloads:
        data = validate_payload(payload)
        elements = data.get("elements", [])
        for element in elements:
            key = (str(element.get("type", "")), str(element.get("id", "")))
            if key not in merged:
                merged[key] = element
        if len(merged) > MAX_TOTAL_FEATURES:
            raise QueryError(
                f"The request returned more than {MAX_TOTAL_FEATURES:,} OSM "
                "objects. Reduce the extent or select fewer datasets."
            )
    return {"elements": list(merged.values())}


def compact_tags(tags: object) -> str:
    clean = tags if isinstance(tags, dict) else {}
    text = json.dumps(
        clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(text) <= 16_000:
        return text
    bounded = {"_truncated": True}
    for key in sorted(clean):
        bounded[str(key)] = clean[key]
        candidate = json.dumps(
            bounded,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(candidate) > 15_900:
            bounded.pop(str(key), None)
            break
    return json.dumps(
        bounded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def matching_specs(
    tags: object,
    specs: Iterable[TagSpec],
    geometry: str,
    match_mode: str = "any",
) -> Tuple[TagSpec, ...]:
    data = tags if isinstance(tags, dict) else {}
    candidates = tuple(spec for spec in specs if spec.geometry == geometry)
    matches = tuple(
        spec
        for spec in candidates
        if spec.key in data
        and (not spec.value or str(data.get(spec.key)) == spec.value)
    )
    mode = str(match_mode or "").strip().casefold()
    if mode not in MATCH_MODES:
        raise QueryError("The query match mode is not valid.")
    if mode == "all" and len(matches) != len(candidates):
        return ()
    return matches
