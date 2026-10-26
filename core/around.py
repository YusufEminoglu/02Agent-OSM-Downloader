# -*- coding: utf-8 -*-
"""Geometry of an "around" request: a circle on the ground, in WGS84.

An around request ("cafés within 500 m of the station") is served without
any new Overpass syntax: the request is the circle's bounding box - so every
size ceiling, tiling rule and cache path applies unchanged - and the features
that come back are then kept only if they intersect the circle itself. The
circle is built on a sphere of the mean Earth radius; over a few kilometres
the difference from the ellipsoid is a few metres, far inside OSM's own
positional accuracy.

Pure Python, no QGIS import, so it is testable on its own.
"""
from __future__ import annotations

import math
from typing import List, Tuple

from .query import QueryError

EARTH_RADIUS_M = 6_371_008.8
MIN_RADIUS_M = 50.0
MAX_RADIUS_M = 5_000.0
RING_SEGMENTS = 72


def validate_radius(meters: object) -> float:
    """The radius in metres, or QueryError outside 50 m - 5 km."""
    try:
        value = float(meters)
    except (TypeError, ValueError) as exc:
        raise QueryError("The search radius must be a number of metres.") from exc
    if not math.isfinite(value) or not MIN_RADIUS_M <= value <= MAX_RADIUS_M:
        raise QueryError(
            f"The search radius must be between {MIN_RADIUS_M:,.0f} m and "
            f"{MAX_RADIUS_M:,.0f} m."
        )
    return value


def validate_centre(lon: object, lat: object) -> Tuple[float, float]:
    try:
        x, y = float(lon), float(lat)
    except (TypeError, ValueError) as exc:
        raise QueryError("The search centre must be a longitude and latitude.") from exc
    if not (-180.0 <= x <= 180.0 and -85.0 <= y <= 85.0):
        raise QueryError("The search centre is not a valid WGS84 position.")
    return x, y


def destination(lon: float, lat: float, bearing_deg: float, meters: float) -> Tuple[float, float]:
    """The point ``meters`` from (lon, lat) along a great circle at ``bearing``."""
    delta = meters / EARTH_RADIUS_M
    theta = math.radians(bearing_deg)
    phi1 = math.radians(lat)
    lambda1 = math.radians(lon)
    phi2 = math.asin(math.sin(phi1) * math.cos(delta)
                     + math.cos(phi1) * math.sin(delta) * math.cos(theta))
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    lon2 = (math.degrees(lambda2) + 540.0) % 360.0 - 180.0
    return lon2, math.degrees(phi2)


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def circle_ring(lon: float, lat: float, meters: float,
                segments: int = RING_SEGMENTS) -> List[Tuple[float, float]]:
    """A closed ring of (lon, lat) approximating the circle, first == last.

    The vertices lie exactly on the circle, so the polygon is inscribed and
    a feature that touches only the sliver between a chord and the arc is
    left out - at 72 segments that sliver is 0.1 percent of the radius.
    """
    ring = [destination(lon, lat, 360.0 * i / segments, meters) for i in range(segments)]
    ring.append(ring[0])
    return ring


def circle_bbox(lon: float, lat: float, meters: float) -> Tuple[float, float, float, float]:
    """(south, west, north, east) of the circle - the request rectangle."""
    north = destination(lon, lat, 0.0, meters)[1]
    south = destination(lon, lat, 180.0, meters)[1]
    # The widest east-west reach is not at the centre latitude but close to
    # it; the ring's own extremes are exact to the ring's resolution, and the
    # cardinal points add the true east/west tangent.
    ring = circle_ring(lon, lat, meters)
    east = max(max(x for x, _ in ring), destination(lon, lat, 90.0, meters)[0])
    west = min(min(x for x, _ in ring), destination(lon, lat, 270.0, meters)[0])
    return south, west, north, east
