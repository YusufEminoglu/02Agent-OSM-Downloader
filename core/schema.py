"""The attribute schema shared by every OSM result layer.

The column list and the value list are generated from one table on purpose.
When they were maintained separately they drifted: "route" and "tourism" ended
up holding each other's values, which silently broke transit-route and tourism
map styling for every download. Deriving both from `TAG_COLUMNS` makes that
class of bug impossible, and keeps the schema testable without QGIS.

Every OSM value arrives as text, so the text columns hold it exactly as
mapped.  Text cannot be summed or compared as a number -- `maximum("height")`
of 9, 12 and 100 is '9' -- so a few quantities planners measure with also get
a typed companion column, appended after the text columns so no existing
column moves or changes.  A companion is filled only when the mapped value
reads unambiguously as a number in a known unit; anything else is NULL, never
a guess and never an empty string.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .catalog import TagSpec
from .query import compact_tags

# Attribute column -> the OSM tag copied into it.
TAG_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("building", "building"),
    ("highway", "highway"),
    ("boundary", "boundary"),
    ("admin_level", "admin_level"),
    ("place", "place"),
    ("amenity", "amenity"),
    ("landuse", "landuse"),
    ("leisure", "leisure"),
    ("natural", "natural"),
    ("railway", "railway"),
    ("public_transport", "public_transport"),
    ("route", "route"),
    ("shop", "shop"),
    ("tourism", "tourism"),
    ("historic", "historic"),
    ("sport", "sport"),
    # `barrier` was already queried by the presets without a column to land
    # in, so barriers arrived unclassifiable. `kerb` marks the sidewalk nodes
    # and `oneway`/`direction` carry which way the traffic runs.
    ("barrier", "barrier"),
    ("kerb", "kerb"),
    ("oneway", "oneway"),
    ("direction", "direction"),
    ("height", "height"),
    ("building_levels", "building:levels"),
    ("waterway", "waterway"),
    ("water", "water"),
    ("cycleway", "cycleway"),
    ("emergency", "emergency"),
    ("healthcare", "healthcare"),
    ("traffic_calming", "traffic_calming"),
)

HEAD_COLUMNS: Tuple[str, ...] = (
    "osm_id", "osm_type", "name", "preset_id", "theme",
    "query_key", "query_value",
)
TAIL_COLUMNS: Tuple[str, ...] = ("tags_json", "matched_tags")

#: The text columns, in the order every release since 1.0 wrote them.
TEXT_FIELD_NAMES: Tuple[str, ...] = (
    HEAD_COLUMNS
    + tuple(column for column, _tag in TAG_COLUMNS)
    + TAIL_COLUMNS
)

# ------------------------------------------------------------ typed values
_NUMBER = r"(\d+(?:\.\d+)?|\.\d+)"
#: Metres per unit.  A value without a unit is in metres (OSM's default).
_LENGTH_UNITS: Dict[str, float] = {
    "": 1.0, "m": 1.0, "km": 1000.0, "cm": 0.01, "mm": 0.001,
    "ft": 0.3048, "'": 0.3048, "in": 0.0254, '"': 0.0254,
    "mi": 1609.344, "nmi": 1852.0,
}
#: km/h per unit.  A value without a unit is in km/h (OSM's default).
_SPEED_UNITS: Dict[str, float] = {
    "": 1.0, "km/h": 1.0, "kmh": 1.0, "kph": 1.0,
    "mph": 1.609344, "knots": 1.852, "kn": 1.852,
}
_LENGTH_RE = re.compile(_NUMBER + r"\s*(m|km|cm|mm|ft|'|in|\"|mi|nmi)?")
_FEET_INCHES_RE = re.compile(_NUMBER + r"\s*'\s*" + _NUMBER + r"\s*\"")
_SPEED_RE = re.compile(_NUMBER + r"\s*(km/h|kmh|kph|mph|knots|kn)?")
_DECIMAL_RE = re.compile(_NUMBER)
_WHOLE_RE = re.compile(r"\d{1,15}")
#: The largest value a 32-bit integer column holds.  Levels and lanes are
#: read only up to it: no real value comes near, and a larger one
#: (`lanes=99999999999`) is a mapping slip that QGIS refuses to write into
#: an integer field, so it reads as NULL like any other unreadable value.
MAX_INT32 = 2_147_483_647
#: The largest value a 64-bit integer column holds (population: a continent
#: holds billions).
MAX_INT64 = 9_223_372_036_854_775_807


def _text_value(value: object) -> str:
    """The mapped value, trimmed and lower-cased; "" for none.

    A `;` list ("12;15") names several values, and no single number is the
    true one, so a list reads as nothing at all rather than as its first item.
    """
    if value is None:
        return ""
    text = str(value).strip().lower()
    return "" if ";" in text else text


def _finite(number: float) -> Optional[float]:
    return number if math.isfinite(number) else None


def parse_length_m(value: object) -> Optional[float]:
    """Metres from an OSM length such as `12`, `12.5`, `12 m`, `40 ft`, `40'`, `10'6"`.

    None for anything else: a qualifier (`~12`, `12+`), a range, a list, a
    comma decimal (`12,5`) or a negative number.
    """
    text = _text_value(value)
    match = _FEET_INCHES_RE.fullmatch(text)
    if match is not None:
        feet, inches = float(match.group(1)), float(match.group(2))
        return _finite(feet * 0.3048 + inches * 0.0254)
    match = _LENGTH_RE.fullmatch(text)
    if match is None:
        return None
    return _finite(float(match.group(1)) * _LENGTH_UNITS[match.group(2) or ""])


def parse_speed_kmh(value: object) -> Optional[float]:
    """km/h from an OSM speed such as `50`, `30 km/h`, `50 mph` or `10 knots`.

    None for a zone code (`TR:urban`, `DE:motorway`), `none`, `signals`,
    `walk` and anything else that is not one number.
    """
    match = _SPEED_RE.fullmatch(_text_value(value))
    if match is None:
        return None
    return _finite(float(match.group(1)) * _SPEED_UNITS[match.group(2) or ""])


def parse_decimal(value: object) -> Optional[float]:
    """A plain non-negative number such as `3` or `2.5` (building levels).

    None above MAX_INT32.
    """
    match = _DECIMAL_RE.fullmatch(_text_value(value))
    if match is None:
        return None
    number = _finite(float(match.group(1)))
    return number if number is not None and number <= MAX_INT32 else None


def parse_whole_number(value: object, maximum: int = MAX_INT32) -> Optional[int]:
    """A plain non-negative whole number such as `2` (lanes) or `4367251`.

    Thousands separators are refused rather than guessed: `1,500` is 1.5 in
    half of the world.  A number above `maximum` -- by default the largest a
    32-bit integer column holds -- is None.
    """
    text = _text_value(value)
    if not _WHOLE_RE.fullmatch(text):
        return None
    number = int(text)
    return number if number <= maximum else None


def parse_large_whole_number(value: object) -> Optional[int]:
    """`parse_whole_number` up to MAX_INT64, for a 64-bit column (population)."""
    return parse_whole_number(value, MAX_INT64)


#: Typed companion columns: (column, OSM tag, parser, field type).  Names stay
#: within the 10 characters a Shapefile export keeps.  Every parser returns
#: only values its field type can hold, so no mapped value can make QGIS
#: refuse a feature.
NUMERIC_COLUMNS: Tuple[Tuple[str, str, Callable[[object], Any], str], ...] = (
    ("height_m", "height", parse_length_m, "double"),
    ("levels_n", "building:levels", parse_decimal, "double"),
    ("lanes_n", "lanes", parse_whole_number, "integer"),
    ("maxspd_kmh", "maxspeed", parse_speed_kmh, "double"),
    ("width_m", "width", parse_length_m, "double"),
    ("pop_n", "population", parse_large_whole_number, "integer64"),
)
#: Measured on the WGS84 ellipsoid by the layer builder: the length of a
#: line in metres and the area of a polygon in square metres (NULL for the
#: other geometry types).
MEASURE_COLUMNS: Tuple[str, ...] = ("length_m", "area_m2")

FIELD_NAMES: Tuple[str, ...] = (
    TEXT_FIELD_NAMES
    + tuple(column for column, _tag, _parse, _type in NUMERIC_COLUMNS)
    + MEASURE_COLUMNS
)
#: Column -> "string", "double", "integer" or "integer64".
FIELD_TYPES: Dict[str, str] = {
    **{name: "string" for name in TEXT_FIELD_NAMES},
    **{column: kind for column, _tag, _parse, kind in NUMERIC_COLUMNS},
    **{name: "double" for name in MEASURE_COLUMNS},
}


def feature_attributes(
    element: Dict[str, Any],
    preset_id: str,
    theme: str,
    matches: Iterable[TagSpec],
    length_m: Optional[float] = None,
    area_m2: Optional[float] = None,
) -> List[Any]:
    """Return one row of attribute values, in `FIELD_NAMES` order.

    Text columns hold "" for an absent tag, as they always have; typed
    columns hold None (NULL) for a value that is absent or unreadable.
    """
    ordered_matches = tuple(matches)
    if not ordered_matches:
        raise ValueError("A feature needs at least one matched selector.")
    tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
    first_match = ordered_matches[0]
    matched_tags = {}
    for match in ordered_matches:
        matched_tags[match.key] = str(tags.get(match.key, match.value))
        for qualifier_key, qualifier_value in match.qualifiers:
            matched_tags[qualifier_key] = str(
                tags.get(qualifier_key, qualifier_value)
            )
    return [
        str(element.get("id", "")),
        str(element.get("type", "")),
        str(tags.get("name", "")),
        preset_id,
        theme,
        first_match.key,
        str(tags.get(first_match.key, first_match.value)),
        *(str(tags.get(tag, "")) for _column, tag in TAG_COLUMNS),
        compact_tags(tags),
        json.dumps(matched_tags, ensure_ascii=False, sort_keys=True),
        *(
            parse(tags[tag]) if tag in tags else None
            for _column, tag, parse, _type in NUMERIC_COLUMNS
        ),
        _finite_or_none(length_m),
        _finite_or_none(area_m2),
    ]


def _finite_or_none(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None
