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
    ("amenity", "amenity"),
    ("landuse", "landuse"),
    ("leisure", "leisure"),
    ("natural", "natural"),
    ("railway", "railway"),
    ("public_transport", "public_transport"),
    ("route", "route"),
    ("tourism", "tourism"),
    ("sport", "sport"),
    ("height", "height"),
    ("building_levels", "building:levels"),
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
    matched_tags = {
        match.key: str(tags.get(match.key, match.value))
        for match in ordered_matches
    }
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
