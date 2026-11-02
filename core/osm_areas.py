"""Whether a closed OSM way encloses an area or just returns to its start.

OSM has no polygon primitive.  A way that ends where it began is an *area*
only when its tags say so: a closed `highway=footway` is a loop path, and a
closed `building=yes` is a building outline.  Deciding by closure alone turns
every footpath loop, roundabout and racetrack into a polygon.

The rules follow the `osm-polygon-features` list that osmtogeojson, iD and
QuickOSM share, so the plugin splits geometry the way the rest of the OSM
ecosystem does.

Tags only say what a *closed* way would be.  Whether a way is closed at all is
:func:`is_closed_ring`, decided from its coordinates: an open wall or pier
tagged like an area is still a line, never a polygon closed by a made-up edge.
"""
from __future__ import annotations

import math
from typing import Dict, Mapping, Optional, Tuple

#: Keys where any value encloses an area.
_AREA_KEYS: Tuple[str, ...] = (
    "building",
    "building:part",
    "landuse",
    "amenity",
    "leisure",
    "boundary",
    "place",
    "shop",
    "tourism",
    "historic",
    "public_transport",
    "office",
    "military",
    "ruins",
    "area:highway",
    "craft",
    "golf",
    "indoor",
    "healthcare",
)

#: Keys where only the listed values enclose an area.
_AREA_VALUE_WHITELIST: Dict[str, Tuple[str, ...]] = {
    "highway": ("services", "rest_area", "escape", "elevator", "platform"),
    "railway": ("station", "turntable", "roundhouse", "platform"),
    "waterway": ("riverbank", "dock", "boatyard", "dam"),
    "barrier": (
        "city_wall", "ditch", "hedge", "retaining_wall", "wall", "spikes",
    ),
    "power": ("plant", "substation", "generator", "transformer"),
}

#: Keys where every value encloses an area *except* the listed ones.
_AREA_VALUE_BLACKLIST: Dict[str, Tuple[str, ...]] = {
    "natural": ("coastline", "cliff", "ridge", "arete", "tree_row"),
    "man_made": ("cutline", "embankment", "pipeline"),
    "aeroway": ("taxiway",),
}

_TRUE = ("yes", "true", "1")
_FALSE = ("no", "false", "0")


def _text(tags: Mapping, key: str) -> str:
    value = tags.get(key)
    return str(value).strip().lower() if value is not None else ""


def _position(coordinate: object) -> Optional[Tuple[float, float]]:
    """(lat, lon) of one Overpass `{"lat", "lon"}` coordinate, or None."""
    if not isinstance(coordinate, Mapping):
        return None
    try:
        position = (float(coordinate["lat"]), float(coordinate["lon"]))
    except (KeyError, TypeError, ValueError):
        return None
    return position if all(math.isfinite(item) for item in position) else None


def is_closed_ring(coordinates: object) -> bool:
    """True when a way's coordinates form a ring: first == last, 4 or more.

    Three positions with first == last (A, B, A) enclose nothing, so a ring
    needs at least four.  An open way is never closed here, whatever its
    tags say: inventing the closing edge turned open walls, piers and
    platforms into polygons that exist nowhere on the ground.
    """
    if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 4:
        return False
    first = _position(coordinates[0])
    return first is not None and first == _position(coordinates[-1])


def way_is_area(tags: Optional[Mapping]) -> bool:
    """True when a closed way carrying `tags` should become a polygon."""
    if not isinstance(tags, Mapping) or not tags:
        return False
    # An explicit `area` tag settles it either way, which is the whole point
    # of the tag -- a pedestrian square is `highway=pedestrian, area=yes`.
    area = _text(tags, "area")
    if area in _TRUE:
        return True
    if area in _FALSE:
        return False
    for key in _AREA_KEYS:
        if _text(tags, key) not in ("", "no"):
            return True
    for key, allowed in _AREA_VALUE_WHITELIST.items():
        if _text(tags, key) in allowed:
            return True
    for key, denied in _AREA_VALUE_BLACKLIST.items():
        value = _text(tags, key)
        if value and value != "no" and value not in denied:
            return True
    return False


#: Relation types whose members outline areas rather than a path.
_AREA_RELATIONS = ("multipolygon", "boundary")
#: Relation types that are a path, however their members close up.
_LINE_RELATIONS = ("route", "route_master", "waterway", "superroute")
#: Member roles only an area relation gives its ways.
_RING_ROLES = ("outer", "inner")


def has_ring_members(members: object) -> bool:
    """True when a relation has a way member in the role "outer" or "inner".

    Postpass removes the `type` tag from every relation it imports, so its
    multipolygons and boundaries arrive untyped.  It sends rings only for
    the relations it assembled as areas, which makes the ring roles the
    area signal there; a route keeps its members' own roles.
    """
    if not isinstance(members, (list, tuple)):
        return False
    return any(
        isinstance(member, Mapping)
        and member.get("type") == "way"
        and str(member.get("role") or "").strip().lower() in _RING_ROLES
        for member in members
    )


def relation_is_area(tags: Optional[Mapping], members: object = None) -> bool:
    """True when a relation outlines an area rather than a route.

    `members`, when given, settles an untyped relation (a Postpass answer)
    by its ring roles before the tags are consulted.
    """
    if not isinstance(tags, Mapping):
        tags = {}
    relation_type = _text(tags, "type")
    if relation_type in _AREA_RELATIONS:
        return True
    if relation_type in _LINE_RELATIONS:
        return False
    if not relation_type and has_ring_members(members):
        return True
    return way_is_area(tags)
