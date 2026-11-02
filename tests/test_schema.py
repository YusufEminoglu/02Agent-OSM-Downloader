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
    FIELD_TYPES,
    MEASURE_COLUMNS,
    NUMERIC_COLUMNS,
    TAG_COLUMNS,
    TEXT_FIELD_NAMES,
    feature_attributes,
    parse_decimal,
    parse_length_m,
    parse_speed_kmh,
    parse_whole_number,
)

#: The columns 1.5.1 wrote, in its order.  Saved projects, styles and
#: expressions name these; none may move or change.
COLUMNS_1_5_1 = (
    "osm_id", "osm_type", "name", "preset_id", "theme", "query_key", "query_value",
    "building", "highway", "boundary", "admin_level", "place", "amenity", "landuse",
    "leisure", "natural", "railway", "public_transport", "route", "shop", "tourism",
    "historic", "sport", "barrier", "kerb", "oneway", "direction", "height",
    "building_levels", "waterway", "water", "cycleway", "emergency", "healthcare",
    "traffic_calming", "tags_json", "matched_tags",
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

    def test_new_thematic_tag_columns_are_extracted(self) -> None:
        element = {
            "type": "way",
            "id": 99,
            "tags": {
                "waterway": "river",
                "water": "river",
                "cycleway": "track",
                "emergency": "ambulance_station",
                "healthcare": "clinic",
                "traffic_calming": "bump",
            },
        }
        values = feature_attributes(
            element,
            "osm_as_is_full_visible_stack",
            "OSM As-Is",
            (TagSpec("*", "", "line"),),
        )
        row = dict(zip(FIELD_NAMES, values))
        self.assertEqual(row["waterway"], "river")
        self.assertEqual(row["water"], "river")
        self.assertEqual(row["cycleway"], "track")
        self.assertEqual(row["emergency"], "ambulance_station")
        self.assertEqual(row["healthcare"], "clinic")
        self.assertEqual(row["traffic_calming"], "bump")


class TypedColumnTests(unittest.TestCase):
    def test_text_columns_keep_their_names_and_order(self) -> None:
        self.assertEqual(TEXT_FIELD_NAMES, COLUMNS_1_5_1)
        self.assertEqual(FIELD_NAMES[: len(COLUMNS_1_5_1)], COLUMNS_1_5_1)

    def test_typed_columns_are_appended_in_order_and_fit_a_shapefile(self) -> None:
        typed = FIELD_NAMES[len(COLUMNS_1_5_1):]
        self.assertEqual(
            typed,
            ("height_m", "levels_n", "lanes_n", "maxspd_kmh", "width_m", "pop_n",
             "length_m", "area_m2"),
        )
        self.assertTrue(all(len(name) <= 10 for name in typed))
        self.assertEqual(
            {name: FIELD_TYPES[name] for name in typed},
            {"height_m": "double", "levels_n": "double", "lanes_n": "integer",
             "maxspd_kmh": "double", "width_m": "double", "pop_n": "integer64",
             "length_m": "double", "area_m2": "double"},
        )
        self.assertTrue(all(FIELD_TYPES[name] == "string" for name in COLUMNS_1_5_1))
        self.assertEqual(MEASURE_COLUMNS, ("length_m", "area_m2"))

    def test_styling_never_reads_a_typed_column(self) -> None:
        # docs/TRAPS.md 5.5: the renderers find their columns by name, and
        # none of them may start reading a new numeric one.
        self.assertLessEqual(STYLING_COLUMNS, set(TEXT_FIELD_NAMES))
        typed = {column for column, *_rest in NUMERIC_COLUMNS} | set(MEASURE_COLUMNS)
        self.assertEqual(STYLING_COLUMNS & typed, set())

    def test_lengths_read_units_strictly(self) -> None:
        for text, metres in (
            ("12", 12.0), ("12.5", 12.5), ("12 m", 12.0), ("12m", 12.0), ("12 M", 12.0),
            ("40 ft", 12.192), ("40'", 12.192), ("10'6\"", 3.2004), (".5", 0.5),
            ("1.5 km", 1500.0), ("250 cm", 2.5),
            # Real spellings from Izmir and Istanbul (OSM 2026-10-24, ODbL).
            ("62.59 m", 62.59), ("26", 26.0), ("70m", 70.0), ("1.25 m", 1.25),
            ("18.50", 18.5), ("0.5 m", 0.5), ("3.5 m", 3.5), ("1.50 m", 1.5), ("2.40", 2.4),
        ):
            with self.subTest(text=text):
                self.assertAlmostEqual(parse_length_m(text), metres, places=9)
        for text in (
            "", None, "TR:urban", "12;15", "12,5", "-1", "-0.3", "12-16", "almost 30",
            "Bilinmiyor", "´-", ">6", "~12", "12+", "building", "e", "2.5-4",
            "7.75 - 8.24 m", "12 metres", "inf", "nan", "1" * 400,
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_length_m(text))

    def test_speeds_read_units_strictly(self) -> None:
        for text, kmh in (
            ("50", 50.0), ("30 km/h", 30.0), ("30km/h", 30.0), ("50 mph", 80.4672),
            ("10 knots", 18.52), ("130", 130.0), ("0", 0.0),
        ):
            with self.subTest(text=text):
                self.assertAlmostEqual(parse_speed_kmh(text), kmh, places=9)
        for text in ("TR:urban", "DE:motorway", "none", "signals", "walk", "30; 50",
                     "30;20", "30,40,50", "unknwon", "-30", ""):
            with self.subTest(text=text):
                self.assertIsNone(parse_speed_kmh(text))

    def test_counts_read_plain_numbers_only(self) -> None:
        self.assertEqual(parse_decimal("2"), 2.0)
        self.assertEqual(parse_decimal("2.5"), 2.5)
        self.assertEqual(parse_decimal("0"), 0.0)
        for text in ("-1", "15,17", "4-5", "3 katlı", "2m", "*", "Zemin", "17O", ""):
            with self.subTest(levels=text):
                self.assertIsNone(parse_decimal(text))
        self.assertEqual(parse_whole_number("3"), 3)
        self.assertEqual(parse_whole_number("766609"), 766609)
        for text in ("1,500", "1.5", "2;3", "-2", "15 462 452", "1" * 16, ""):
            with self.subTest(whole=text):
                self.assertIsNone(parse_whole_number(text))

    def test_numbers_beyond_a_32_bit_column_read_as_null(self) -> None:
        # One `lanes=99999999999` made QGIS refuse the feature and the whole
        # download failed; levels and lanes stop at 2,147,483,647.
        for parse in (parse_whole_number, parse_decimal):
            with self.subTest(parse=parse.__name__):
                self.assertEqual(parse("2147483647"), 2147483647)
                for text in ("2147483648", "99999999999", "999999999999999"):
                    self.assertIsNone(parse(text), text)
        self.assertIsNone(parse_decimal("2147483647.5"))
        self.assertEqual(parse_decimal("2147483646.5"), 2147483646.5)
        element = {
            "type": "way", "id": 2,
            "tags": {"highway": "primary", "lanes": "99999999999",
                     "building:levels": "99999999999", "population": "4600000000"},
        }
        row = dict(zip(FIELD_NAMES, feature_attributes(
            element, "p", "t", (TagSpec("highway", "", "line"),)
        )))
        # The mapped text is kept as it is; only the typed companions are NULL.
        self.assertEqual(json.loads(row["tags_json"])["lanes"], "99999999999")
        self.assertEqual(
            (row["lanes_n"], row["building_levels"], row["levels_n"]),
            (None, "99999999999", None),
        )
        # Population is a 64-bit column: a continent's count is kept.
        self.assertEqual(row["pop_n"], 4600000000)

    def test_every_typed_value_fits_its_field_type(self) -> None:
        # The ranges of QGIS's Int and LongLong fields, written out.
        limits = {"integer": 2**31 - 1, "integer64": 2**63 - 1}
        samples = ("0", "7", "2147483647", "2147483648", "4600000000",
                   "9223372036854775807", "9223372036854775808", "999999999999999",
                   "1" * 15, "3.5", "12 m")
        # Levels (a double column) are bounded like the 32-bit lanes.
        own_limit = {"levels_n": 2**31 - 1, "lanes_n": 2**31 - 1, "pop_n": 2**63 - 1}
        for column, _tag, parse, kind in NUMERIC_COLUMNS:
            for text in samples:
                with self.subTest(column=column, text=text):
                    value = parse(text)
                    if value is None:
                        continue
                    if kind in limits:
                        self.assertIsInstance(value, int)
                        self.assertLessEqual(value, limits[kind])
                    if column in own_limit:
                        self.assertLessEqual(value, own_limit[column])
        self.assertEqual(set(own_limit), {column for column, *_rest in NUMERIC_COLUMNS
                                          if column.endswith("_n")})
        pop = next(parse for column, _tag, parse, _kind in NUMERIC_COLUMNS if column == "pop_n")
        self.assertEqual((pop("4600000000"), pop("999999999999999")),
                         (4600000000, 999999999999999))
        self.assertIsNone(pop("1" * 16))

    def test_typed_values_are_null_never_empty_text(self) -> None:
        element = {
            "type": "way", "id": 5,
            "tags": {"building": "yes", "height": "12 m", "building:levels": "3",
                     "maxspeed": "TR:urban", "lanes": "2", "width": "3.5 m",
                     "population": "4367251"},
        }
        row = dict(zip(FIELD_NAMES, feature_attributes(
            element, "p", "t", (TagSpec("building", "", "polygon"),), area_m2=812.25
        )))
        self.assertEqual(row["height"], "12 m")
        self.assertEqual(row["height_m"], 12.0)
        self.assertEqual(row["levels_n"], 3.0)
        self.assertIsNone(row["maxspd_kmh"])
        self.assertEqual(row["lanes_n"], 2)
        self.assertEqual(row["width_m"], 3.5)
        self.assertEqual(row["pop_n"], 4367251)
        self.assertIsNone(row["length_m"])
        self.assertEqual(row["area_m2"], 812.25)
        bare = dict(zip(FIELD_NAMES, feature_attributes(
            {"type": "node", "id": 1}, "p", "t", (TagSpec("amenity", "", "point"),),
            length_m=float("nan"),
        )))
        for column in FIELD_NAMES[len(COLUMNS_1_5_1):]:
            with self.subTest(column=column):
                self.assertIsNone(bare[column])
        self.assertEqual(bare["height"], "")


if __name__ == "__main__":
    unittest.main()
