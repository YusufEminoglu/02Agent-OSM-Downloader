"""Nominatim place search: URL construction and result ranking.

This module is deliberately network-free so it can be tested without QGIS.
It builds a request for one pinned host and parses the answer; the actual
call is made by the Processing layer, which owns the network stack and the
usage-policy rate limit.

Nominatim is the reference OpenStreetMap geocoder, so it resolves free-form
text ("izmir konak", "Old Town, Prague") that a tag-regex search over
Overpass cannot.  Where Nominatim returns a boundary relation, the download
can be clipped to that real boundary instead of to a bounding box.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple
from urllib.parse import urlencode, urlsplit

from .places import (
    MAX_PLACE_RESULTS,
    PlaceCandidate,
    normalize_place_name,
)

NOMINATIM_HOST = "nominatim.openstreetmap.org"
NOMINATIM_SEARCH_URL = f"https://{NOMINATIM_HOST}/search"
# The Nominatim usage policy asks for at most one request per second from an
# identified client.  The Processing layer enforces this interval.
NOMINATIM_MIN_INTERVAL_SECONDS = 1.0
MAX_GEOCODE_RESULTS = 10

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
    return str(entry.get("class") or "") in _BOUNDARY_CLASSES


def _rank(entry: Dict[str, Any], order: int) -> Tuple[float, int]:
    """Rank a result, preferring boundaries but preserving Nominatim's order."""
    try:
        importance = float(entry.get("importance") or 0.0)
    except (TypeError, ValueError):
        importance = 0.0
    if str(entry.get("class") or "") in _BOUNDARY_CLASSES:
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
