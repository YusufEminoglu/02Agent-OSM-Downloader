"""Regression checks for the OSM result-layer attribute schema."""
from __future__ import annotations

import json
import unittest

from zero2agent_osm_downloader.core.catalog import TagSpec
from zero2agent_osm_downloader.core.map_styling_expressions import (
    STYLING_COLUMNS,
)
from zero2agent_osm_downloader.core.schema import (
    FIELD_NAMES,
    TAG_COLUMNS,
    feature_attributes,
)

ELEMENT = {
    "type": "relation",
    "id": 7,
    "tags": {
        "name": "Metro Line 1",
        "route": "subway",
        "tourism": "attraction",
        "railway": "subway",
        "building": "yes",
        "building:levels": "4",
        "height": "12",
    },
}


def _row() -> dict:
    values = feature_attributes(
        ELEMENT, "urban_transit", "Urban Context", (TagSpec("route", "subway", "line"),)
    )
    return dict(zip(FIELD_NAMES, values))


class SchemaTests(unittest.TestCase):
    def test_every_column_gets_exactly_one_value(self) -> None:
        values = feature_attributes(
            ELEMENT, "p", "t", (TagSpec("route", "subway", "line"),)
        )
        self.assertEqual(len(values), len(FIELD_NAMES))
        self.assertEqual(len(set(FIELD_NAMES)), len(FIELD_NAMES))

    def test_each_column_holds_its_own_tag(self) -> None:
        # This is the regression: "route" and "tourism" once held each other's
        # values, which silently broke transit and tourism map styling.
        row = _row()
        for column, tag in TAG_COLUMNS:
            self.assertEqual(
                row[column], str(ELEMENT["tags"].get(tag, "")), column
            )
        self.assertEqual(row["route"], "subway")
        self.assertEqual(row["tourism"], "attraction")
        self.assertEqual(row["building_levels"], "4")

    def test_identity_and_query_provenance_are_recorded(self) -> None:
        row = _row()
        self.assertEqual(row["osm_id"], "7")
        self.assertEqual(row["osm_type"], "relation")
        self.assertEqual(row["name"], "Metro Line 1")
        self.assertEqual(row["preset_id"], "urban_transit")
        self.assertEqual(row["theme"], "Urban Context")
        self.assertEqual(row["query_key"], "route")
        self.assertEqual(row["query_value"], "subway")
        self.assertEqual(
            json.loads(row["matched_tags"]), {"route": "subway"}
        )
        self.assertEqual(json.loads(row["tags_json"])["route"], "subway")

    def test_a_tagless_element_still_produces_a_full_row(self) -> None:
        values = feature_attributes(
            {"type": "node", "id": 3}, "p", "t",
            (TagSpec("amenity", "cafe", "point"),),
        )
        self.assertEqual(len(values), len(FIELD_NAMES))
        row = dict(zip(FIELD_NAMES, values))
        self.assertEqual(row["query_value"], "cafe")
        self.assertEqual(row["building"], "")

    def test_a_feature_without_a_matched_selector_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            feature_attributes(ELEMENT, "p", "t", ())

    def test_every_column_the_styling_reads_actually_exists(self) -> None:
        # A renderer expression naming a column that no longer exists fails
        # silently: the category simply never matches.
        missing = STYLING_COLUMNS - set(FIELD_NAMES)
        self.assertEqual(missing, set())


if __name__ == "__main__":
    unittest.main()
