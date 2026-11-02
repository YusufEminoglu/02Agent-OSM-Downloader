"""Nominatim place search: URL construction and result ranking.

This module is deliberately network-free so it can be tested without QGIS.
It builds a request for one pinned host and parses the answer; the actual
call is made by the Processing layer, which owns the network stack and the
usage-policy rate limit.

Nominatim is the reference OpenStreetMap geocoder, so it resolves free-form
text ("izmir konak", "Old Town, Prague") that a tag-regex search over
Overpass cannot.  Where Nominatim returns a boundary relation, the download
can be clipped to that real boundary instead of to a bounding box: its
outline comes from Nominatim's ``lookup`` (:func:`build_lookup_url`,
:func:`parse_boundary`), so the clip depends on no server's area tables.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode, urlsplit

from .places import (
    MAX_PLACE_RESULTS,
    PlaceCandidate,
    normalize_place_name,
)
from .query import QueryError, validate_area_id

NOMINATIM_HOST = "nominatim.openstreetmap.org"
NOMINATIM_SEARCH_URL = f"https://{NOMINATIM_HOST}/search"
NOMINATIM_LOOKUP_URL = f"https://{NOMINATIM_HOST}/lookup"
# The Nominatim usage policy asks for at most one request per second from an
# identified client.  The Processing layer enforces this interval.
NOMINATIM_MIN_INTERVAL_SECONDS = 1.0
MAX_GEOCODE_RESULTS = 10
#: Tolerance, in degrees, Nominatim simplifies a returned outline to (about
#: 11 m), keeping its topology: a district arrives in a few thousand points.
BOUNDARY_TOLERANCE_DEGREES = "0.0001"
#: The largest lookup answer that is read.  A simplified district or city
#: outline is a few hundred kilobytes at most.
MAX_BOUNDARY_BYTES = 8 * 1024 * 1024
# Overpass area ids, as the place search produces them (core.query
# validate_area_id): relation id + 3600000000, way id + 2400000000.
_RELATION_AREA_OFFSET = 3_600_000_000
_WAY_AREA_OFFSET = 2_400_000_000

# Nominatim ranks a settlement boundary above a street of the same name, but
# a download wants the administrative area whenever one exists, so boundary
# results are nudged ahead of equally-important non-boundary ones.
_BOUNDARY_CLASSES = ("boundary", "place")


class GeocodeError(ValueError):
    """A bounded, user-actionable geocoding error."""


def build_search_url(
    place: object,
    limit: int = MAX_GEOCODE_RESULTS,
    language: str = "",
) -> str:
    """Build the pinned Nominatim search URL for a validated place name.

    The host is fixed and every user-supplied value is percent-encoded as a
    query parameter, so the caller can never steer the request at another
    server or inject extra parameters.
    """
    name = normalize_place_name(place)
    try:
        bounded_limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise GeocodeError("The result limit is not valid.") from exc
    bounded_limit = max(1, min(MAX_GEOCODE_RESULTS, bounded_limit))
    parameters = [
        ("q", name),
        ("format", "jsonv2"),
        ("limit", str(bounded_limit)),
        ("addressdetails", "1"),
        ("extratags", "1"),
        ("dedupe", "1"),
    ]
    clean_language = "".join(
        char for char in str(language or "") if char.isalnum() or char in "-,"
    )[:32]
    if clean_language:
        parameters.append(("accept-language", clean_language))
    url = f"{NOMINATIM_SEARCH_URL}?{urlencode(parameters)}"
    if urlsplit(url).hostname != NOMINATIM_HOST:
        raise GeocodeError("The geocoding request host is not valid.")
    return url


def _bbox(entry: Dict[str, Any]) -> Tuple[float, float, float, float] | None:
    """Read Nominatim's boundingbox, which is ordered south, north, west, east."""
    values = entry.get("boundingbox")
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        return None
    try:
        south, north, west, east = (float(item) for item in values)
    except (TypeError, ValueError):
        return None
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        return None
    return south, west, north, east


def _admin_level(entry: Dict[str, Any]) -> str:
    extratags = entry.get("extratags")
    if isinstance(extratags, dict):
        level = str(extratags.get("admin_level") or "").strip()
        if level.isdigit():
            return level
    return ""


def _label(entry: Dict[str, Any], name: str) -> str:
    display = " ".join(str(entry.get("display_name") or "").split())
    return display or name


def _short_name(entry: Dict[str, Any], fallback: str) -> str:
    """Return the place's own name without its full administrative address."""
    address = entry.get("address")
    if isinstance(address, dict):
        address_type = str(entry.get("addresstype") or "").strip()
        direct = str(address.get(address_type) or "").strip()
        if direct:
            return direct
    display = str(entry.get("display_name") or "").strip()
    if display:
        return display.split(",")[0].strip() or fallback
    return fallback


def _category(entry: Dict[str, Any]) -> str:
    """Return the result's main OSM tag key, such as boundary or highway.

    The ``jsonv2`` format this module requests calls it ``category``; only the
    older ``json`` format calls it ``class``.  Reading ``class`` alone made
    every jsonv2 boundary look like a plain rectangle, so ``category`` is read
    first and ``class`` stays as the fallback.
    """
    return str(entry.get("category") or entry.get("class") or "").strip()


def _has_area(entry: Dict[str, Any], osm_type: str) -> bool:
    """Report whether Overpass will have an area for this result.

    Overpass builds areas only from boundary and place polygons.  A Nominatim
    hit can just as easily be a river relation, a bus route or a building, and
    those produce an area id that matches nothing — an empty download with no
    error attached.  Restricting the claim to the two classes that really have
    areas keeps the boundary path honest and lets the caller fall back to the
    rectangle instead.
    """
    if osm_type not in ("relation", "way"):
        return False
    return _category(entry) in _BOUNDARY_CLASSES


def _rank(entry: Dict[str, Any], order: int) -> Tuple[float, int]:
    """Rank a result, preferring boundaries but preserving Nominatim's order."""
    try:
        importance = float(entry.get("importance") or 0.0)
    except (TypeError, ValueError):
        importance = 0.0
    if _category(entry) in _BOUNDARY_CLASSES:
        importance += 0.25
    return -importance, order


def parse_geocode_results(
    payload: object,
    requested: object,
) -> Tuple[PlaceCandidate, ...]:
    """Turn a Nominatim jsonv2 response into ranked place candidates."""
    name = normalize_place_name(requested)
    entries: Iterable[Any] = payload if isinstance(payload, list) else ()
    scored = []
    seen = set()
    for order, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        bbox = _bbox(entry)
        if bbox is None:
            continue
        osm_type = str(entry.get("osm_type") or "").strip()
        try:
            osm_id = int(entry.get("osm_id") or 0)
        except (TypeError, ValueError):
            osm_id = 0
        marker = (osm_type, osm_id) if osm_id else (_label(entry, name),)
        if marker in seen:
            continue
        seen.add(marker)
        scored.append(
            (
                _rank(entry, order),
                PlaceCandidate(
                    _short_name(entry, name),
                    _label(entry, name),
                    str(entry.get("addresstype") or entry.get("type") or "place"),
                    _admin_level(entry),
                    bbox,
                    osm_type,
                    osm_id,
                    "nominatim",
                    _has_area(entry, osm_type),
                ),
            )
        )
        if len(scored) >= MAX_PLACE_RESULTS:
            break
    scored.sort(key=lambda item: item[0])
    return tuple(item[1] for item in scored)


def _lookup_object(area_id: object) -> Tuple[str, int]:
    """(Nominatim osm_type, OSM id) of the object an Overpass area id names."""
    try:
        checked = validate_area_id(area_id)
    except QueryError as exc:
        raise GeocodeError(str(exc)) from exc
    if checked >= _RELATION_AREA_OFFSET:
        return "relation", checked - _RELATION_AREA_OFFSET
    return "way", checked - _WAY_AREA_OFFSET


def build_lookup_url(area_id: object) -> str:
    """Build the pinned Nominatim lookup URL for the outline behind an area id.

    `area_id` is an Overpass area id, as the place search produces it: a
    relation is ``osm_ids=R<id>``, a way ``W<id>``.  Only a validated number
    reaches the URL and the host is fixed; the outline comes back as GeoJSON,
    simplified to BOUNDARY_TOLERANCE_DEGREES.
    """
    osm_type, osm_id = _lookup_object(area_id)
    parameters = [
        ("osm_ids", f"{'R' if osm_type == 'relation' else 'W'}{osm_id}"),
        ("format", "jsonv2"),
        ("polygon_geojson", "1"),
        ("polygon_threshold", BOUNDARY_TOLERANCE_DEGREES),
    ]
    url = f"{NOMINATIM_LOOKUP_URL}?{urlencode(parameters)}"
    if urlsplit(url).hostname != NOMINATIM_HOST:
        raise GeocodeError("The geocoding request host is not valid.")
    return url


def _position(value: object) -> Optional[List[float]]:
    """A GeoJSON [lon, lat] that is a finite point in WGS84, or None."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    lon, lat = value[0], value[1]
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float))
        for item in (lon, lat)
    ):
        return None
    lon_f, lat_f = float(lon), float(lat)
    if not (
        math.isfinite(lon_f) and math.isfinite(lat_f)
        and -180.0 <= lon_f <= 180.0 and -90.0 <= lat_f <= 90.0
    ):
        return None
    return [lon_f, lat_f]


def _rings(polygon: object) -> Optional[List[List[List[float]]]]:
    """A polygon's rings, each closed and at least four positions long."""
    if not isinstance(polygon, list) or not polygon:
        return None
    rings = []
    for ring in polygon:
        if not isinstance(ring, list) or len(ring) < 4:
            return None
        points = [_position(item) for item in ring]
        if any(point is None for point in points) or points[0] != points[-1]:
            return None
        rings.append(points)
    return rings


def boundary_geojson(value: object) -> Optional[Dict[str, Any]]:
    """`value` as a GeoJSON Polygon or MultiPolygon in WGS84, or None.

    Every ring must close and hold at least four finite positions.  Anything
    else -- a point, a line, a damaged coordinate -- is not an outline a
    download can be clipped to.
    """
    if not isinstance(value, dict):
        return None
    shape = value.get("type")
    coordinates = value.get("coordinates")
    if shape == "Polygon":
        rings = _rings(coordinates)
        return None if rings is None else {"type": "Polygon", "coordinates": rings}
    if shape != "MultiPolygon" or not isinstance(coordinates, list) or not coordinates:
        return None
    polygons = [_rings(polygon) for polygon in coordinates]
    if any(rings is None for rings in polygons):
        return None
    return {"type": "MultiPolygon", "coordinates": polygons}


def parse_boundary(payload: object, area_id: object) -> Optional[Dict[str, Any]]:
    """The outline of the place `area_id` names in a lookup answer, or None.

    `payload` is the reply body (bytes or text) or its parsed JSON: a list
    of places with ``osm_type``, ``osm_id`` and the ``geojson`` asked for by
    :func:`build_lookup_url`.  Only the place the area id names counts, and
    only an area outline (:func:`boundary_geojson`).  A body larger than
    MAX_BOUNDARY_BYTES is refused unread.
    """
    osm_type, osm_id = _lookup_object(area_id)
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if isinstance(payload, (bytes, bytearray, memoryview)):
        body = bytes(payload)
        if len(body) > MAX_BOUNDARY_BYTES:
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
    if not isinstance(payload, list):
        return None
    for entry in payload:
        if (
            isinstance(entry, dict)
            and entry.get("osm_type") == osm_type
            and _whole_number(entry.get("osm_id")) == osm_id
        ):
            return boundary_geojson(entry.get("geojson"))
    return None


def _whole_number(value: object) -> Optional[int]:
    """An OSM id as Nominatim writes it (a number, or digits), or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None
