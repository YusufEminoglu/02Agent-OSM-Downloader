"""Whether a closed OSM way encloses an area or just returns to its start.

OSM has no polygon primitive.  A way that ends where it began is an *area*
only when its tags say so: a closed `highway=footway` is a loop path, and a
closed `building=yes` is a building outline.  Deciding by closure alone turns
every footpath loop, roundabout and racetrack into a polygon.

The rules follow the `osm-polygon-features` list that osmtogeojson, iD and
QuickOSM share, so the plugin splits geometry the way the rest of the OSM
ecosystem does.
"""
from __future__ import annotations

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


def relation_is_area(tags: Optional[Mapping]) -> bool:
    """True when a relation outlines an area rather than a route."""
    if not isinstance(tags, Mapping) or not tags:
        return False
    relation_type = _text(tags, "type")
    if relation_type in _AREA_RELATIONS:
        return True
    if relation_type in _LINE_RELATIONS:
        return False
    return way_is_area(tags)
