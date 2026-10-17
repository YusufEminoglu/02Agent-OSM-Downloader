"""The attribute schema shared by every OSM result layer.

The column list and the value list are generated from one table on purpose.
When they were maintained separately they drifted: "route" and "tourism" ended
up holding each other's values, which silently broke transit-route and tourism
map styling for every download. Deriving both from `TAG_COLUMNS` makes that
class of bug impossible, and keeps the schema testable without QGIS.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Tuple

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

FIELD_NAMES: Tuple[str, ...] = (
    HEAD_COLUMNS
    + tuple(column for column, _tag in TAG_COLUMNS)
    + TAIL_COLUMNS
)


def feature_attributes(
    element: Dict[str, Any],
    preset_id: str,
    theme: str,
    matches: Iterable[TagSpec],
) -> List[str]:
    """Return one row of attribute values, in `FIELD_NAMES` order."""
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
    ]
