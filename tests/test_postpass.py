"""Postpass fallback: the SQL translator and the GeoJSON adapter.

The adapter tests read real answers from postpass.geofabrik.de, saved on
2026-11-02 through the plugin's own QGIS request class and trimmed to a few
rows each, verbatim (``tests/fixtures/postpass_*.json``):

* ``cafes``: amenity=cafe as points and polygons (two nodes, two closed ways).
* ``buildings``: building=* in Kemeraltı: a closed way and two hans with a
  courtyard (multipolygon relations with one hole each).
* ``routes``: route~^(tram|ferry)$ lines: a ferry way and a three-part ferry
  route relation.
* ``oneway``: highway=* AND oneway=yes lines.
* ``mahalle``: admin_level=8 boundaries and parks clipped to Konak
  (relation 1269255): one mahalle as lines and as rings, and a three-part park.
* ``null_geometry``: two census boundaries Postpass holds with an empty
  geometry (woodpeck/postpass#46).

Every statement :func:`render_sql` produced for those answers ran on the live
server; the tests below pin the rules that statement is built from.
"""
from __future__ import annotations

import copy
import json
import math
import re
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from zero2agent_osm_downloader.core import mirrors, postpass, transport
from zero2agent_osm_downloader.core.catalog import PRESETS, TagSpec
from zero2agent_osm_downloader.core.osm_areas import relation_is_area, way_is_area
from zero2agent_osm_downloader.core.postpass import (
    POSTPASS_ENDPOINT,
    POSTPASS_HOST,
    PostpassError,
    PostpassTooLarge,
    adapt,
    render_sql,
)
from zero2agent_osm_downloader.core.query import (
    MATCH_MODES,
    MAX_FEATURES,
    MAX_RESPONSE_BYTES,
    MAX_TILE_AREA_KM2,
    VALUE_MODES,
    WILDCARD_EXCLUDED_KEYS,
    QueryError,
    advanced_specs,
    bbox_area_km2,
    build_query,
    is_admin_boundary_spec,
    matching_specs,
    merge_elements,
    normalized_specs,
    osm_timestamp,
    tile_bbox,
    validate_payload,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
#: (south, west, north, east): the Konak box the live checks used.
KONAK_BOX = (38.415, 27.125, 38.430, 27.145)
KONAK_AREA_ID = 3_601_269_255
CAFE_POINT = TagSpec("amenity", "cafe", "point")
CAFE_POLYGON = TagSpec("amenity", "cafe", "polygon")

_LITERAL_RE = re.compile(r"convert_from\(decode\('([0-9a-f]*)', 'hex'\), 'UTF8'\)")
_BRANCH_RE = re.compile(r"FROM (postpass_\w+) AS p\n")


def _fixture(name: str) -> dict:
    """A saved Postpass answer, as the server sent it (rows trimmed)."""
    return json.loads(
        (FIXTURES / f"postpass_{name}.json").read_text(encoding="utf-8")
    )


def _features(name: str, osm_type: str, osm_id: int) -> list:
    return [
        feature for feature in _fixture(name)["features"]
        if feature["properties"]["osm_type"] == osm_type
        and feature["properties"]["osm_id"] == osm_id
    ]


def _element(payload: dict, element_type: str, element_id: int) -> dict:
    matches = [
        element for element in payload["elements"]
        if element["type"] == element_type and element["id"] == element_id
    ]
    if len(matches) != 1:
        raise AssertionError(f"{element_type} {element_id}: {len(matches)} copies")
    return matches[0]


def _points(path: list) -> list:
    return [{"lat": lat, "lon": lon} for lon, lat in path]


def _literals(sql: str) -> list:
    return [bytes.fromhex(item).decode("utf-8") for item in _LITERAL_RE.findall(sql)]


def _skeleton(sql: str) -> str:
    """The statement with every text literal blanked: its syntax alone."""
    return _LITERAL_RE.sub("<text>", sql)


def _branches(sql: str) -> dict:
    """Table name -> that table's part of the statement."""
    parts = {}
    for part in sql.split("\nUNION ALL\n"):
        match = _BRANCH_RE.search(part)
        if match is None:
            raise AssertionError(f"no table in {part[:80]!r}")
        parts[match.group(1)] = part
    return parts


# The Overpass side of the selector comparison: core.query's `_tag`, written
# out again so the comparison does not lean on a private helper.
_OVERPASS_WILDCARD = "".join(
    f'["{key}"!="{value}"]' if value else f'[!"{key}"]'
    for key, value in WILDCARD_EXCLUDED_KEYS
)
#: What core.query appends to a wildcard relation selector, and what
#: postpass answers by reading the polygon table only ("A").
_OVERPASS_AREA_RELATIONS = '["type"~"^(multipolygon|boundary)$"]'


def _overpass_filter(spec: TagSpec, value_mode: str) -> str:
    operator = "~" if value_mode == "regex" else "="
    if spec.key == "*":
        primary = '[~"."~"."]' + _OVERPASS_WILDCARD
    elif spec.value:
        primary = f'["{spec.key}"{operator}"{spec.value}"]'
    else:
        primary = f'["{spec.key}"]'
    return primary + "".join(
        f'["{key}"="{value}"]' if value else f'["{key}"]'
        for key, value in spec.qualifiers
    )


def _overpass_selectors(query_text: str) -> set:
    """(element, tag filters) of every selector in a rendered Overpass request."""
    body = query_text.split("(\n", 1)[1].rsplit("\n);", 1)[0]
    found = set()
    for line in body.splitlines():
        match = re.fullmatch(r"\s*(node|way|relation)(.*)\([^()]*\);", line)
        if match is None:
            raise AssertionError(f"unexpected selector line {line!r}")
        found.add((match.group(1), match.group(2)))
    return found


def _postpass_selectors(specs, match_mode: str, value_mode: str) -> set:
    names = {"N": "node", "W": "way", "R": "relation", "A": "relation"}
    safe = normalized_specs(specs, value_mode)
    return {
        (
            names[osm_type],
            "".join(_overpass_filter(spec, value_mode) for spec in group)
            + (_OVERPASS_AREA_RELATIONS if osm_type == "A" else ""),
        )
        for osm_type, group in postpass._selectors(safe, match_mode)
    }


class EndpointTests(unittest.TestCase):
    def test_one_pinned_host_and_the_versioned_interpreter(self) -> None:
        parts = urlsplit(POSTPASS_ENDPOINT)
        self.assertEqual(parts.scheme, "https")
        self.assertEqual(parts.hostname, POSTPASS_HOST)
        self.assertEqual(POSTPASS_HOST, "postpass.geofabrik.de")
        self.assertEqual(parts.path, "/api/0.2/interpreter")
        self.assertEqual((parts.query, parts.fragment), ("", ""))


class SqlSafetyTests(unittest.TestCase):
    #: Accepted by core.query and dangerous inside an SQL string.
    HOSTILE = (
        "O'Brien",
        "x' OR '1'='1",
        "a -- b",
        "$$quoted$$",
        "50%+off & more?",
        "#hash ~ tilde",
        "Kızlarağası Hanı",
        "東京タワー",
        # A right-to-left override, built at run time: written literally it
        # would itself be a Bandit B613 finding.
        chr(0x202E) + "evil",
    )
    #: Refused by core.query before any statement exists.
    REFUSED = ('say "hi"', "semi;colon", "back\\slash", "new\nline", "nul\x00")

    def test_every_key_and_value_decodes_back_from_its_hex_literal(self) -> None:
        specs = (
            TagSpec("name:tr", "Kızlarağası Hanı", "point"),
            TagSpec("addr:street", "Anafartalar Caddesi", "polygon",
                    (("building", "yes"), ("historic", ""))),
        )
        sql = render_sql(specs, KONAK_BOX)
        self.assertEqual(
            set(_literals(sql)),
            {"name:tr", "Kızlarağası Hanı", "addr:street", "Anafartalar Caddesi",
             "building", "yes", "historic"},
        )
        for text in ("name:tr", "Kızlarağası", "addr:street", "Anafartalar"):
            with self.subTest(text=text):
                self.assertNotIn(text, sql)

    def test_hostile_values_cannot_change_the_statement(self) -> None:
        benign = render_sql([TagSpec("name", "cafe", "point")], KONAK_BOX)
        for value in self.HOSTILE:
            with self.subTest(value=value):
                sql = render_sql([TagSpec("name", value, "point")], KONAK_BOX)
                self.assertEqual(_skeleton(sql), _skeleton(benign))
                self.assertEqual(_literals(sql), ["name", value])
                self.assertNotIn(value, sql)

    def test_hostile_regex_patterns_cannot_change_the_statement(self) -> None:
        benign = render_sql([TagSpec("name", "^x$", "line")], KONAK_BOX,
                            value_mode="regex")
        # "(?:x)" is refused by core.query since regex validation follows the
        # server's POSIX engine; a plain group carries the same SQL hazard.
        with self.assertRaises(QueryError):
            render_sql([TagSpec("name", "[0-9]+ (?:x)?", "line")], KONAK_BOX,
                       value_mode="regex")
        for pattern in ("^(O'Brien|x)$", "a -- b", "$$", "[0-9]+ (x)?", "Hanı|東京"):
            with self.subTest(pattern=pattern):
                sql = render_sql([TagSpec("name", pattern, "line")], KONAK_BOX,
                                 value_mode="regex")
                self.assertEqual(_skeleton(sql), _skeleton(benign))
                self.assertEqual(set(_literals(sql)), {"name", pattern})
                self.assertNotIn(pattern, sql)

    def test_values_core_query_refuses_never_reach_a_statement(self) -> None:
        for value in self.REFUSED:
            for mode in ("exact", "regex"):
                with self.subTest(value=value, mode=mode):
                    with self.assertRaises(QueryError):
                        render_sql([TagSpec("name", value, "point")], KONAK_BOX,
                                   value_mode=mode)

    def test_the_literal_builder_emits_only_hex_whatever_it_is_given(self) -> None:
        # The last line of defence: even text core.query would refuse comes
        # out as hex digits inside a fixed call.
        shape = re.compile(r"convert_from\(decode\('[0-9a-f]*', 'hex'\), 'UTF8'\)")
        for text in self.HOSTILE + self.REFUSED + ("", "'); DROP TABLE x; --"):
            with self.subTest(text=text):
                literal = postpass._text(text)
                self.assertRegex(literal, shape)
                self.assertIsNotNone(shape.fullmatch(literal))
                self.assertEqual(_literals(literal), [text])

    def test_statement_survives_form_transport_and_has_no_separator(self) -> None:
        # Go's form parser drops a pair holding a raw ";" and QUrlQuery leaves
        # "+" unencoded, which a form decoder reads as a space; Postpass also
        # asks for no trailing semicolon.
        specs = (
            TagSpec("*", "", "point"), TagSpec("route", "^(a|b)+$", "line"),
            TagSpec("boundary", "administrative", "polygon", (("admin_level", "8"),)),
        )
        for sql in (
            render_sql(specs[:1], KONAK_BOX, area_id=KONAK_AREA_ID),
            render_sql(specs[1:2], KONAK_BOX, value_mode="regex"),
            render_sql(specs[2:], KONAK_BOX, match_mode="all"),
        ):
            with self.subTest(sql=sql[:60]):
                for forbidden in (";", "+", "--", "$$", "/*", "\\"):
                    self.assertNotIn(forbidden, sql)
                self.assertTrue(sql.startswith(("SELECT ", "WITH clip ")))


class SqlSelectionTests(unittest.TestCase):
    def test_point_spec_reads_nodes_from_the_point_table_only(self) -> None:
        parts = _branches(render_sql([CAFE_POINT], KONAK_BOX))
        self.assertEqual(list(parts), ["postpass_point"])
        self.assertNotIn("osm_type =", parts["postpass_point"])

    def test_line_and_polygon_specs_read_ways_and_relations_from_both_tables(self) -> None:
        # Postpass files a closed way under its own polygon keys, not the
        # plugin's: a closed coastline is in postpass_polygon, a closed
        # healthcare outline in postpass_line.  Overpass's way[...] reads
        # every way, so both tables are read, with no type gate.
        for spec in (
            TagSpec("natural", "coastline", "line"),
            TagSpec("healthcare", "", "polygon"),
        ):
            with self.subTest(spec=spec):
                parts = _branches(render_sql([spec], KONAK_BOX))
                self.assertEqual(list(parts), ["postpass_line", "postpass_polygon"])
                for part in parts.values():
                    self.assertNotIn("osm_type =", part)

    def test_mixed_geometries_read_all_three_tables(self) -> None:
        parts = _branches(render_sql([CAFE_POINT, CAFE_POLYGON], KONAK_BOX))
        self.assertEqual(
            list(parts), ["postpass_point", "postpass_line", "postpass_polygon"]
        )

    def test_admin_boundary_polygon_is_read_as_relations_only(self) -> None:
        spec = TagSpec("boundary", "administrative", "polygon", (("admin_level", "6"),))
        parts = _branches(render_sql([spec], KONAK_BOX))
        self.assertEqual(list(parts), ["postpass_line", "postpass_polygon"])
        for part in parts.values():
            self.assertIn("((p.osm_type = 'R') AND ", part)
            self.assertNotIn("'W'", part)

    def test_admin_boundary_point_reads_relations_not_nodes(self) -> None:
        spec = TagSpec("boundary", "administrative", "point", (("admin_level", "8"),))
        parts = _branches(render_sql([spec], KONAK_BOX))
        self.assertNotIn("postpass_point", parts)
        self.assertTrue(all("(p.osm_type = 'R')" in part for part in parts.values()))

    def test_admin_and_plain_specs_share_a_table_with_one_gate(self) -> None:
        admin = TagSpec("boundary", "administrative", "polygon", (("admin_level", "8"),))
        park = TagSpec("leisure", "park", "polygon")
        part = _branches(render_sql([admin, park], KONAK_BOX))["postpass_polygon"]
        self.assertEqual(part.count("(p.osm_type = 'R')"), 1)
        self.assertIn(" OR (p.tags @> ", part)

    def test_match_any_ors_and_match_all_ands_the_specs(self) -> None:
        specs = (TagSpec("amenity", "cafe", "point"),
                 TagSpec("outdoor_seating", "yes", "point"))
        any_part = _branches(render_sql(specs, KONAK_BOX))["postpass_point"]
        all_part = _branches(render_sql(specs, KONAK_BOX, match_mode="all"))["postpass_point"]
        self.assertIn(")) OR (p.tags @> ", any_part)
        self.assertNotIn(" AND (p.tags", any_part)
        self.assertIn(")) AND (p.tags @> ", all_part)
        self.assertNotIn(" OR ", all_part)
        self.assertEqual(_literals(any_part), _literals(all_part))

    def test_match_all_groups_specs_by_geometry(self) -> None:
        specs = (TagSpec("highway", "", "line"), TagSpec("oneway", "yes", "line"),
                 TagSpec("amenity", "cafe", "point"))
        parts = _branches(render_sql(specs, KONAK_BOX, match_mode="all"))
        self.assertEqual(_literals(parts["postpass_point"]), ["amenity", "cafe"])
        self.assertEqual(_literals(parts["postpass_line"]), ["highway", "oneway", "yes"])
        self.assertIn("(p.tags ? ", parts["postpass_line"])

    def test_regex_mode_matches_the_value_and_exact_mode_contains_it(self) -> None:
        regex = render_sql([TagSpec("route", "^(tram|ferry)$", "line")], KONAK_BOX,
                           value_mode="regex")
        exact = render_sql([TagSpec("route", "tram", "line")], KONAK_BOX)
        self.assertIn("((p.tags ->> convert_from(", regex)
        self.assertIn("'UTF8')) ~ convert_from(", regex)
        self.assertNotIn("@>", regex)
        self.assertNotIn(" ~ ", exact)
        self.assertIn("(p.tags @> jsonb_build_object(", exact)

    def test_regex_mode_keeps_qualifiers_exact(self) -> None:
        spec = TagSpec("name", "^Kemer", "point", (("amenity", "cafe"),))
        sql = render_sql([spec], KONAK_BOX, value_mode="regex")
        self.assertEqual(_literals(sql), ["name", "^Kemer", "amenity", "cafe"])
        self.assertEqual(sql.count(" ~ "), 1)
        self.assertEqual(sql.count("@> jsonb_build_object("), 1)

    def test_blank_value_tests_the_key_only(self) -> None:
        for value in ("", "*"):
            with self.subTest(value=value):
                sql = render_sql([TagSpec("building", value, "polygon")], KONAK_BOX)
                self.assertIn("(p.tags ? convert_from(", sql)
                self.assertNotIn("@>", sql)
                self.assertEqual(set(_literals(sql)), {"building"})

    def test_qualifiers_are_required_exactly(self) -> None:
        spec = TagSpec("boundary", "administrative", "line",
                       (("admin_level", "8"), ("name", "")))
        part = _branches(render_sql([spec], KONAK_BOX))["postpass_line"]
        self.assertEqual(
            _literals(part), ["boundary", "administrative", "admin_level", "8", "name"]
        )
        self.assertEqual(part.count(" AND (p.tags"), 2)

    def test_wildcard_keeps_the_overpass_exclusions(self) -> None:
        sql = render_sql([TagSpec("*", "", "polygon")], KONAK_BOX)
        for part in _branches(sql).values():
            self.assertIn("(p.tags <> '{}'::jsonb)", part)
            self.assertIn("(NOT (p.tags @> jsonb_build_object(", part)
            self.assertIn("(NOT (p.tags ? ", part)
            self.assertEqual(_literals(part), ["boundary", "administrative", "place"])

    def test_wildcard_qualifiers_still_apply(self) -> None:
        sql = render_sql([TagSpec("*", "", "point", (("name", ""),))], KONAK_BOX)
        self.assertEqual(_literals(sql), ["boundary", "administrative", "place", "name"])

    def test_wildcard_relations_come_from_the_area_table_only(self) -> None:
        # Overpass is asked for multipolygon and boundary relations only.
        # Postpass strips `type`, but holds exactly those as rings in
        # postpass_polygon; its line table holds the routes.
        wildcard = [TagSpec("*", "", "line"), TagSpec("*", "", "polygon")]
        parts = _branches(render_sql(wildcard, KONAK_BOX))
        self.assertIn("(p.osm_type = 'W')", parts["postpass_line"])
        self.assertNotIn("'R'", parts["postpass_line"])
        self.assertNotIn("(p.osm_type =", parts["postpass_polygon"])
        # A named relation selector still reads both tables, as before.
        parts = _branches(render_sql([TagSpec("route", "bus", "line")], KONAK_BOX))
        self.assertNotIn("(p.osm_type =", parts["postpass_line"])
        self.assertNotIn("(p.osm_type =", parts["postpass_polygon"])

    def test_area_id_clips_every_table_to_the_boundary(self) -> None:
        sql = render_sql([CAFE_POINT, CAFE_POLYGON], KONAK_BOX, area_id=KONAK_AREA_ID)
        self.assertTrue(sql.startswith(
            "WITH clip AS MATERIALIZED (SELECT geom FROM postpass_polygon"
            " WHERE osm_type = 'R' AND osm_id = 1269255)\n"
        ))
        parts = _branches(sql)
        self.assertEqual(len(parts), 3)
        for part in parts.values():
            self.assertIn("p.geom && ST_MakeEnvelope(", part)
            self.assertTrue(part.endswith(
                "\nAND EXISTS (SELECT 1 FROM clip WHERE ST_Intersects(p.geom, clip.geom))"
            ))

    def test_way_area_ids_clip_to_the_way(self) -> None:
        sql = render_sql([CAFE_POINT], KONAK_BOX, area_id="2400012345")
        self.assertIn("WHERE osm_type = 'W' AND osm_id = 12345)", sql)

    def test_no_area_means_no_clip(self) -> None:
        plain = render_sql([CAFE_POINT], KONAK_BOX)
        for none in (None, 0, ""):
            with self.subTest(area_id=none):
                self.assertEqual(render_sql([CAFE_POINT], KONAK_BOX, area_id=none), plain)
        self.assertNotIn("clip", plain)

    def test_area_ids_outside_the_overpass_ranges_are_refused(self) -> None:
        for bad in (1269255, "3601269255; DROP", 4_000_000_000, -3_601_269_255):
            with self.subTest(area_id=bad):
                with self.assertRaises(QueryError):
                    render_sql([CAFE_POINT], KONAK_BOX, area_id=bad)

    def test_bbox_is_an_envelope_in_lon_lat_order(self) -> None:
        sql = render_sql([CAFE_POINT], KONAK_BOX)
        self.assertIn(
            "p.geom && ST_MakeEnvelope(27.1250000, 38.4150000, 27.1450000, "
            "38.4300000, 4326)",
            sql,
        )
        southern = render_sql([CAFE_POINT], (-33.9, 18.4, -33.85, 18.45))
        self.assertIn("ST_MakeEnvelope(18.4000000, -33.9000000, 18.4500000, -33.8500000, 4326)",
                      southern)

    def test_bad_extents_are_refused(self) -> None:
        for bbox in (
            (38.43, 27.125, 38.415, 27.145),            # south above north
            (38.415, 27.145, 38.43, 27.125),            # west east of east
            (math.nan, 27.125, 38.43, 27.145),
            (38.415, -math.inf, 38.43, 27.145),
            (-91.0, 27.125, 38.43, 27.145),
            (38.415, 27.125, 38.43, 181.0),
            (38.0, 27.0, 39.0, 28.0),                   # ~9,700 km², one request
            (38.415, 27.125, 38.43),
            ("38.415", "x", "38.43", "27.145"),
            None,
        ):
            with self.subTest(bbox=bbox):
                with self.assertRaises(QueryError):
                    render_sql([CAFE_POINT], bbox)

    def test_planner_tiles_at_the_ceiling_are_accepted(self) -> None:
        # A 20 x 20 km job on the equator splits into four 10 x 10 km tiles,
        # two of which measure 100.00000000000058 km²: rounding puts a planner
        # tile just past MAX_TILE_AREA_KM2, and a strict ceiling would lose it.
        step = 10.0 / 111.32
        tiles = tile_bbox((-step, 10.0, step, 10.0 + 2 * step))
        self.assertEqual(len(tiles), 4)
        self.assertTrue(any(bbox_area_km2(*tile) > MAX_TILE_AREA_KM2 for tile in tiles))
        for tile in tiles:
            with self.subTest(tile=tile):
                self.assertAlmostEqual(bbox_area_km2(*tile), MAX_TILE_AREA_KM2, places=6)
                self.assertTrue(render_sql([CAFE_POINT], tile).startswith("SELECT "))
        over = (-step / 2, 10.0, step / 2 * 1.002, 10.0 + step)   # 100.1 km²
        self.assertAlmostEqual(bbox_area_km2(*over), 100.1, places=6)
        with self.assertRaises(QueryError):
            render_sql([CAFE_POINT], over)

    def test_bad_modes_are_refused(self) -> None:
        with self.assertRaises(QueryError):
            render_sql([CAFE_POINT], KONAK_BOX, match_mode="some")
        with self.assertRaises(QueryError):
            render_sql([CAFE_POINT], KONAK_BOX, value_mode="glob")
        with self.assertRaises(QueryError):
            render_sql([], KONAK_BOX)
        with self.assertRaises(QueryError):
            render_sql([TagSpec("amenity", "cafe", "area")], KONAK_BOX)

    def test_selectors_match_the_overpass_request_for_every_preset(self) -> None:
        for preset in PRESETS:
            with self.subTest(preset=preset.preset_id):
                self.assertEqual(
                    _postpass_selectors(preset.tags, "any", "exact"),
                    _overpass_selectors(build_query(preset.tags, KONAK_BOX)),
                )

    def test_selectors_match_the_overpass_request_in_all_and_regex_modes(self) -> None:
        cases = (
            ([("highway", ""), ("oneway", "yes")], ("line",), "all", "exact"),
            ([("amenity", "cafe"), ("cuisine", "coffee_shop")],
             ("point", "line", "polygon"), "all", "exact"),
            ([("route", "^(tram|ferry)$")], ("line",), "any", "regex"),
            ([("name", "^Kemer"), ("amenity", "^(cafe|bar)$")], ("point", "polygon"),
             "all", "regex"),
        )
        for filters, geometries, match_mode, value_mode in cases:
            specs = advanced_specs(filters, geometries, match_mode, value_mode)
            with self.subTest(filters=filters, mode=match_mode, values=value_mode):
                self.assertEqual(
                    _postpass_selectors(specs, match_mode, value_mode),
                    _overpass_selectors(build_query(
                        specs, KONAK_BOX, match_mode, value_mode=value_mode
                    )),
                )
        admin = TagSpec("boundary", "administrative", "polygon", (("admin_level", "4"),))
        mixed = (admin, TagSpec("leisure", "park", "polygon"),
                 TagSpec("boundary", "administrative", "point", (("admin_level", "4"),)))
        self.assertEqual(
            _postpass_selectors(mixed, "all", "exact"),
            _overpass_selectors(build_query(mixed, KONAK_BOX, "all")),
        )


class MirrorParityTests(unittest.TestCase):
    """Every rule copied from core.query still says what core.query says."""

    def test_constants_match_core_query(self) -> None:
        self.assertEqual(postpass._WILDCARD_EXCLUDED_KEYS, WILDCARD_EXCLUDED_KEYS)
        self.assertEqual(postpass._MATCH_MODES, MATCH_MODES)
        self.assertEqual(postpass._VALUE_MODES, VALUE_MODES)
        self.assertEqual(postpass._MAX_FEATURES, MAX_FEATURES)
        self.assertEqual(postpass._MAX_RESPONSE_BYTES, MAX_RESPONSE_BYTES)
        self.assertEqual(postpass._MAX_TILE_AREA_KM2, MAX_TILE_AREA_KM2)

    def test_admin_boundary_rule_matches_core_query(self) -> None:
        specs = {spec for preset in PRESETS for spec in preset.tags}
        specs |= {
            TagSpec("boundary", "administrative", "polygon", (("admin_level", ""),)),
            TagSpec("boundary", "administrative", "line"),
            TagSpec("boundary", "protected_area", "polygon", (("admin_level", "4"),)),
        }
        for spec in specs:
            with self.subTest(spec=spec):
                self.assertEqual(postpass._is_admin_boundary(spec),
                                 is_admin_boundary_spec(spec))

    def test_area_rule_matches_core_query(self) -> None:
        for bbox in (
            KONAK_BOX, (-33.9, 18.4, -33.85, 18.45), (-0.05, 36.8, 0.05, 36.9),
            (64.1, -21.95, 64.2, -21.8), (38.30, 27.00, 38.48, 27.23),
        ):
            with self.subTest(bbox=bbox):
                self.assertAlmostEqual(postpass._box_area_km2(*bbox),
                                       bbox_area_km2(*bbox), places=9)


class AdaptTests(unittest.TestCase):
    def test_points_become_overpass_nodes(self) -> None:
        payload = adapt(_fixture("cafes"))
        for feature in _fixture("cafes")["features"]:
            props = feature["properties"]
            if props["osm_type"] != "N":
                continue
            lon, lat = feature["geometry"]["coordinates"]
            with self.subTest(node=props["osm_id"]):
                self.assertEqual(
                    _element(payload, "node", props["osm_id"]),
                    {"type": "node", "id": props["osm_id"], "lat": lat, "lon": lon,
                     "tags": props["tags"]},
                )
        marache = _element(payload, "node", 11054359805)
        self.assertEqual((marache["lat"], marache["lon"]), (38.4231711, 27.1432771))
        self.assertEqual(marache["tags"]["addr:city"], "İzmir")

    def test_a_polygon_way_carries_its_closed_outline(self) -> None:
        payload = adapt(_fixture("cafes"))
        (feature,) = _features("cafes", "W", 1224700905)
        ring = feature["geometry"]["coordinates"][0][0]
        way = _element(payload, "way", 1224700905)
        self.assertEqual(
            way,
            {"type": "way", "id": 1224700905, "geometry": _points(ring),
             "tags": feature["properties"]["tags"]},
        )
        self.assertEqual(way["geometry"][0], way["geometry"][-1])
        self.assertTrue(way_is_area(way["tags"]))
        self.assertEqual(
            matching_specs(way["tags"], (CAFE_POINT, CAFE_POLYGON), "polygon"),
            (CAFE_POLYGON,),
        )

    def test_a_line_way_keeps_its_vertices_in_order(self) -> None:
        payload = adapt(_fixture("oneway"))
        for feature in _fixture("oneway")["features"]:
            (path,) = feature["geometry"]["coordinates"]
            way = _element(payload, "way", feature["properties"]["osm_id"])
            with self.subTest(way=way["id"]):
                # Order is direction: oneway=yes depends on it.
                self.assertEqual(way["geometry"], _points(path))
                self.assertEqual(way["tags"]["oneway"], "yes")
                self.assertIn("highway", way["tags"])

    def test_a_courtyard_becomes_an_inner_member(self) -> None:
        payload = adapt(_fixture("buildings"))
        (feature,) = _features("buildings", "R", 15607096)
        ((outer, inner),) = feature["geometry"]["coordinates"]
        han = _element(payload, "relation", 15607096)
        self.assertEqual(han["members"], [
            {"type": "way", "role": "outer", "geometry": _points(outer)},
            {"type": "way", "role": "inner", "geometry": _points(inner)},
        ])
        self.assertEqual(han["tags"], feature["properties"]["tags"])
        self.assertEqual(han["tags"]["name"], "Kızlarağası Hanı")
        for member in han["members"]:
            self.assertEqual(member["geometry"][0], member["geometry"][-1])
        self.assertEqual(len(han["members"][1]["geometry"]), 5)  # a closed quad

    def test_relation_tags_pass_through_without_an_invented_type(self) -> None:
        # Postpass's import consumes the `type` tag (postpass-ops#42); the
        # adapter must not guess it back.  Without it, relation_is_area falls
        # back to the other tags, which still say "building".
        for name, osm_id in (("buildings", 15607096), ("routes", 451605),
                             ("mahalle", 10344960)):
            (feature, *_rest) = _features(name, "R", osm_id)
            relation = _element(adapt(_fixture(name)), "relation", osm_id)
            with self.subTest(relation=osm_id):
                self.assertEqual(relation["tags"], feature["properties"]["tags"])
                self.assertNotIn("type", relation["tags"])
        building = _element(adapt(_fixture("buildings")), "relation", 15607096)
        route = _element(adapt(_fixture("routes")), "relation", 451605)
        self.assertTrue(relation_is_area(building["tags"]))
        self.assertFalse(relation_is_area(route["tags"]))

    def test_route_parts_become_way_members_without_a_role(self) -> None:
        payload = adapt(_fixture("routes"))
        (feature,) = _features("routes", "R", 451605)
        route = _element(payload, "relation", 451605)
        self.assertEqual(route["members"], [
            {"type": "way", "role": "", "geometry": _points(part)}
            for part in feature["geometry"]["coordinates"]
        ])
        self.assertEqual(len(route["members"]), 3)
        ferry = _element(payload, "way", 52023019)
        self.assertEqual(len(ferry["geometry"]), 5)
        self.assertEqual(ferry["tags"]["route"], "ferry")

    def test_a_boundary_stored_twice_becomes_one_relation_of_rings(self) -> None:
        source = _fixture("mahalle")
        rows = _features("mahalle", "R", 10344960)
        self.assertEqual(sorted(row["geometry"]["type"] for row in rows),
                         ["MultiLineString", "MultiPolygon"])
        (rings,) = [row for row in rows if row["geometry"]["type"] == "MultiPolygon"]
        payload = adapt(source)
        self.assertEqual(len(payload["elements"]), 2)
        mahalle = _element(payload, "relation", 10344960)
        self.assertEqual(mahalle["members"], [
            {"type": "way", "role": "outer", "geometry": _points(ring)}
            for ring in rings["geometry"]["coordinates"][0]
        ])
        self.assertEqual(mahalle["tags"]["name"], "Fevzi Paşa Mahallesi")
        # The same answer with the rows the other way round folds the same.
        source["features"].reverse()
        self.assertEqual(_element(adapt(source), "relation", 10344960)["members"],
                         mahalle["members"])

    def test_a_park_in_three_parts_has_three_outer_rings(self) -> None:
        park = _element(adapt(_fixture("mahalle")), "relation", 6040265)
        self.assertEqual([member["role"] for member in park["members"]],
                         ["outer", "outer", "outer"])
        self.assertTrue(all(m["geometry"][0] == m["geometry"][-1] for m in park["members"]))

    def test_a_relation_with_only_lines_keeps_them_when_rings_are_empty(self) -> None:
        source = _fixture("mahalle")
        for feature in source["features"]:
            if feature["geometry"]["type"] == "MultiPolygon":
                feature["geometry"] = {"type": None}
        mahalle = _element(adapt(source), "relation", 10344960)
        self.assertEqual({member["role"] for member in mahalle["members"]}, {""})
        self.assertEqual(len(mahalle["members"]), 5)

    def test_an_empty_geometry_is_kept_without_coordinates(self) -> None:
        payload = adapt(_fixture("null_geometry"))
        for feature in _fixture("null_geometry")["features"]:
            self.assertEqual(feature["geometry"], {"type": None})
            relation = _element(payload, "relation", feature["properties"]["osm_id"])
            with self.subTest(relation=relation["id"]):
                self.assertEqual(relation["members"], [])
                self.assertEqual(relation["tags"], feature["properties"]["tags"])

    def test_null_and_empty_geometries_of_nodes_and_ways(self) -> None:
        node = copy.deepcopy(_features("cafes", "N", 9981858582)[0])
        way = copy.deepcopy(_features("cafes", "W", 872239165)[0])
        for empty in (None, {"type": None}, {"type": "Point", "coordinates": []},
                      {"type": "GeometryCollection", "geometries": []}):
            node["geometry"] = way["geometry"] = empty
            payload = adapt({"type": "FeatureCollection", "features": [node, way]})
            with self.subTest(geometry=empty):
                self.assertNotIn("lat", _element(payload, "node", 9981858582))
                self.assertEqual(_element(payload, "way", 872239165)["geometry"], [])

    def test_data_date_becomes_osm3s_timestamp(self) -> None:
        payload = adapt(_fixture("buildings"))
        self.assertEqual(payload["osm3s"], {"timestamp_osm_base": "2026-11-02T07:47:26Z"})
        self.assertEqual(osm_timestamp([payload]), "2026-11-02T07:47:26Z")
        self.assertEqual(payload["generator"], "Postpass API 0.2")

    def test_data_date_formats(self) -> None:
        source = _fixture("routes")
        for stamp, expected in (
            ("2026-11-02T07:48:28Z", "2026-11-02T07:48:28Z"),
            ("2026-11-02T07:48:28+00:00", "2026-11-02T07:48:28Z"),
            ("2026-11-02T10:48:28+03:00", "2026-11-02T07:48:28Z"),
            (" 2026-11-02T07:48:28Z ", "2026-11-02T07:48:28Z"),
            ("2026-11-02T07:48:28", ""),        # no zone: not knowably UTC
            ("2026-13-02T07:48:28Z", ""),
            ("", ""),                           # a database without replication
            (None, ""),
        ):
            source["postpass_properties"]["timestamp"] = stamp
            with self.subTest(stamp=stamp):
                payload = adapt(source)
                self.assertEqual(
                    payload.get("osm3s", {}).get("timestamp_osm_base", ""), expected
                )
        del source["postpass_properties"]
        self.assertNotIn("osm3s", adapt(source))
        self.assertNotIn("generator", adapt(source))

    def test_every_element_has_only_the_overpass_keys(self) -> None:
        allowed = {
            "node": {"type", "id", "lat", "lon", "tags"},
            "way": {"type", "id", "geometry", "tags"},
            "relation": {"type", "id", "members", "tags"},
        }
        for name in ("cafes", "buildings", "routes", "oneway", "mahalle", "null_geometry"):
            payload = adapt(_fixture(name))
            self.assertEqual(set(payload) - {"generator", "osm3s"}, {"elements"})
            for element in payload["elements"]:
                with self.subTest(fixture=name, element=element["id"]):
                    self.assertLessEqual(set(element), allowed[element["type"]])
                    self.assertIsInstance(element["id"], int)
                    for member in element.get("members", []):
                        self.assertEqual(set(member), {"type", "role", "geometry"})
                        self.assertIn(member["role"], ("outer", "inner", ""))

    def test_adapted_answers_pass_the_overpass_payload_checks(self) -> None:
        cafes, buildings = adapt(_fixture("cafes")), adapt(_fixture("buildings"))
        self.assertIs(validate_payload(cafes), cafes)
        merged = merge_elements([cafes, buildings, adapt(_fixture("cafes"))])
        self.assertEqual(len(merged["elements"]),
                         len(cafes["elements"]) + len(buildings["elements"]))
        self.assertEqual(merged["osm3s"]["timestamp_osm_base"], "2026-11-02T07:47:26Z")

    def test_bytes_text_and_parsed_answers_adapt_alike(self) -> None:
        raw = (FIXTURES / "postpass_mahalle.json").read_bytes()
        parsed = adapt(_fixture("mahalle"))
        self.assertEqual(adapt(raw), parsed)
        self.assertEqual(adapt(bytearray(raw)), parsed)
        self.assertEqual(adapt(raw.decode("utf-8")), parsed)

    def test_untagged_rows_leave_tags_out_as_overpass_does(self) -> None:
        source = _fixture("routes")
        source["features"][0]["properties"]["tags"] = {}
        source["features"][1]["properties"]["tags"] = None
        payload = adapt(source)
        self.assertNotIn("tags", payload["elements"][0])
        self.assertNotIn("tags", payload["elements"][1])

    def test_a_repeated_row_keeps_the_first_copy(self) -> None:
        source = _fixture("oneway")
        second = copy.deepcopy(source["features"][0])
        second["properties"]["tags"] = {"highway": "service"}
        source["features"].append(second)
        payload = adapt(source)
        self.assertEqual(len(payload["elements"]), 2)
        self.assertEqual(payload["elements"][0]["tags"]["name"], "Mucibur Rahman Caddesi")


class AdaptRejectionTests(unittest.TestCase):
    def _feature(self) -> dict:
        return copy.deepcopy(_fixture("cafes")["features"][0])

    def _refused(self, payload, error=PostpassError) -> None:
        with self.assertRaises(error):
            adapt(payload)

    def test_errors_are_query_errors(self) -> None:
        self.assertTrue(issubclass(PostpassError, QueryError))
        self.assertTrue(issubclass(PostpassTooLarge, PostpassError))

    def test_a_body_that_is_not_json_names_what_came(self) -> None:
        body = b"pq: syntax error at or near \"x\"\npq: syntax error at or near \"x\"\n"
        with self.assertRaises(PostpassError) as caught:
            adapt(body)
        self.assertIn("pq: syntax error", str(caught.exception))
        for bad in (b"", b"\xff\xfe", b"<html>504 Gateway Time-out</html>"):
            with self.subTest(body=bad):
                self._refused(bad)

    def test_a_body_past_64_mb_is_refused_before_parsing(self) -> None:
        self._refused(b" " * (MAX_RESPONSE_BYTES + 1), PostpassTooLarge)

    def test_too_many_features_ask_for_a_split(self) -> None:
        source = _fixture("cafes")
        with mock.patch.object(postpass, "_MAX_FEATURES", 3):
            self._refused(source, PostpassTooLarge)
            source["features"] = source["features"][:3]
            self.assertEqual(len(adapt(source)["elements"]), 3)

    def test_shapes_that_are_not_a_feature_collection(self) -> None:
        for payload in (
            None, [], "[]", 7,
            {"elements": [], "osm3s": {}},
            {"type": "Feature", "features": []},
            {"type": "FeatureCollection"},
            {"type": "FeatureCollection", "features": None},
            {"type": "FeatureCollection", "features": {}},
        ):
            with self.subTest(payload=payload):
                self._refused(payload)

    def test_invalid_features(self) -> None:
        def variant(**changes) -> dict:
            feature = self._feature()
            for path, value in changes.items():
                target = feature
                *parents, leaf = path.split("__")
                for parent in parents:
                    target = target[parent]
                target[leaf] = value
            return {"type": "FeatureCollection", "features": [feature]}

        cases = {
            "not a dict": {"type": "FeatureCollection", "features": ["x"]},
            "not a Feature": variant(type="Point"),
            "no properties": variant(properties=None),
            "unknown osm_type": variant(properties__osm_type="X"),
            "list osm_type": variant(properties__osm_type=["N"]),
            "missing osm_type": variant(properties__osm_type=None),
            "bool id": variant(properties__osm_id=True),
            "zero id": variant(properties__osm_id=0),
            "negative id": variant(properties__osm_id=-5),
            "text id": variant(properties__osm_id="9981858582"),
            "float id": variant(properties__osm_id=1.5),
            "tags list": variant(properties__tags=["amenity"]),
            "number tag": variant(properties__tags={"amenity": 1}),
            "null tag": variant(properties__tags={"amenity": None}),
            "nested tag": variant(properties__tags={"amenity": {"x": "y"}}),
            "geometry text": variant(geometry="POINT(27 38)"),
            "node as line": variant(geometry={"type": "LineString",
                                              "coordinates": [[27.1, 38.4], [27.2, 38.5]]}),
            "unknown shape": variant(geometry={"type": "CircularString",
                                               "coordinates": [[27.1, 38.4]]}),
            "collection": variant(geometry={"type": "GeometryCollection", "geometries": [
                {"type": "Point", "coordinates": [27.1, 38.4]}]}),
            "text coordinate": variant(geometry__coordinates=["27.1", "38.4"]),
            "short coordinate": variant(geometry__coordinates=[27.1]),
            "bool coordinate": variant(geometry__coordinates=[True, False]),
            "nan coordinate": variant(geometry__coordinates=[math.nan, 38.4]),
            "lon out of range": variant(geometry__coordinates=[200.0, 38.4]),
            "lat out of range": variant(geometry__coordinates=[27.1, -91.0]),
            "not a list": variant(geometry__coordinates="27.1 38.4"),
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                self._refused(payload)

    def test_way_and_relation_geometries_that_cannot_be_overpass_shapes(self) -> None:
        way = copy.deepcopy(_features("cafes", "W", 872239165)[0])
        ring = way["geometry"]["coordinates"][0][0]
        relation = copy.deepcopy(_features("routes", "R", 451605)[0])
        cases = (
            (way, {"type": "MultiPolygon", "coordinates": [[ring, ring]]}),
            (way, {"type": "MultiPolygon", "coordinates": [[ring], [ring]]}),
            (way, {"type": "MultiLineString", "coordinates": [ring, ring]}),
            (way, {"type": "Point", "coordinates": ring[0]}),
            (relation, {"type": "Point", "coordinates": ring[0]}),
            (relation, {"type": "MultiLineString", "coordinates": [ring[0]]}),
            (relation, {"type": "MultiPolygon", "coordinates": "ring"}),
        )
        for feature, geometry in cases:
            changed = copy.deepcopy(feature)
            changed["geometry"] = geometry
            with self.subTest(osm_type=feature["properties"]["osm_type"],
                              geometry=json.dumps(geometry)[:50]):
                self._refused({"type": "FeatureCollection", "features": [changed]})


def _sql_error(message: str) -> bytes:
    """A Postpass SQL error as handlers.go writes it: http.Error sends the
    message and a newline, then HandleInterpreter writes the message again."""
    return f"{message}\n{message}".encode("utf-8")


#: What QgsBlockingNetworkRequest reports for any HTTP 400 reply.
QT_BAD_REQUEST = (302, "Error transferring https://postpass.geofabrik.de/api/0.2/"
                       "interpreter - server replied: Bad Request")
STATEMENT_TIMEOUT = "pq: canceling statement due to statement timeout"


class ClassifyTests(unittest.TestCase):
    """One Postpass attempt, in the classes core.transport uses for Overpass."""

    def _raw(self, name: str) -> bytes:
        return (FIXTURES / f"postpass_{name}.json").read_bytes()

    def test_a_real_answer_is_adapted_into_the_verdict(self) -> None:
        for name in ("cafes", "buildings", "routes", "mahalle"):
            with self.subTest(fixture=name):
                body = self._raw(name)
                verdict = postpass.classify(200, body)
                self.assertEqual((verdict.kind, verdict.detail), (transport.OK, "delivered the data"))
                self.assertEqual(verdict.payload, adapt(body))
                self.assertIs(validate_payload(verdict.payload), verdict.payload)
        cafes = postpass.classify(200, self._raw("cafes")).payload
        self.assertEqual(
            sorted((element["type"], element["id"]) for element in cafes["elements"]),
            [("node", 9981858582), ("node", 11054359805),
             ("way", 872239165), ("way", 1224700905)],
        )
        self.assertEqual(osm_timestamp([cafes]), "2026-11-02T07:48:28Z")

    def test_an_empty_answer_is_an_answer(self) -> None:
        body = json.dumps({
            "postpass_properties": {"generator": "Postpass API 0.2",
                                    "timestamp": "2026-11-02T07:48:28Z"},
            "type": "FeatureCollection", "features": [],
        }).encode("utf-8")
        verdict = postpass.classify(200, body)
        self.assertEqual(verdict.kind, transport.OK)
        self.assertEqual(verdict.payload["elements"], [])

    def test_a_statement_timeout_splits_the_part(self) -> None:
        verdict = postpass.classify(400, _sql_error(STATEMENT_TIMEOUT), *QT_BAD_REQUEST)
        self.assertEqual((verdict.kind, verdict.detail),
                         (transport.SERVER_TIMEOUT, "ran out of time on this query"))
        self.assertIn(verdict.kind, transport.HEAVY)
        self.assertIsNone(verdict.payload)
        self.assertFalse(verdict.retry_same_host)

    def test_running_out_of_memory_splits_the_part(self) -> None:
        for message in ("pq: out of memory",
                        "pq: out of memory DETAIL: Failed on request of size 8192."):
            with self.subTest(message=message):
                verdict = postpass.classify(400, _sql_error(message), *QT_BAD_REQUEST)
                self.assertEqual((verdict.kind, verdict.detail),
                                 (transport.SERVER_MEMORY, "ran out of memory on this query"))
                self.assertIn(verdict.kind, transport.HEAVY)

    def test_any_other_sql_error_is_a_request_the_server_refuses(self) -> None:
        for message in (
            'pq: syntax error at or near "convert_from"',
            'pq: column "geom" does not exist',
            "pq: invalid regular expression: parentheses () not balanced",
            "no query field given",
        ):
            with self.subTest(message=message):
                verdict = postpass.classify(400, _sql_error(message), *QT_BAD_REQUEST)
                self.assertEqual(
                    (verdict.kind, verdict.detail),
                    (transport.BAD_QUERY, "rejected the request as invalid (HTTP 400)"),
                )
                self.assertFalse(verdict.retry_same_host)

    def test_too_large_answers_split_the_part(self) -> None:
        with mock.patch.object(postpass, "_MAX_FEATURES", 3):
            crowded = postpass.classify(200, self._raw("cafes"))
        self.assertEqual((crowded.kind, crowded.detail),
                         (transport.TOO_LARGE, "sent more than 3 objects"))
        stopped = postpass.classify(200, b"", oversize=True)
        self.assertEqual((stopped.kind, stopped.detail),
                         (transport.TOO_LARGE, "sent more than 64 MB"))
        too_long = postpass.classify(200, b" " * (MAX_RESPONSE_BYTES + 1))
        self.assertEqual(too_long.kind, transport.TOO_LARGE)
        self.assertTrue({crowded.kind, stopped.kind, too_long.kind} <= transport.HEAVY)

    def test_an_unusable_answer_counts_like_a_busy_one(self) -> None:
        body = self._raw("cafes")
        for label, unusable in (
            ("error page", b"<html>504 Gateway Time-out</html>"),
            ("cut off", body[: len(body) // 2]),
            ("not GeoJSON", b'{"elements": []}'),
            ("bad feature", b'{"type": "FeatureCollection", "features": [7]}'),
            ("not UTF-8", b"\xff\xfe"),
        ):
            with self.subTest(answer=label):
                verdict = postpass.classify(200, unusable)
                self.assertEqual((verdict.kind, verdict.detail),
                                 (transport.BUSY, "sent an answer that is not OSM data"))
                self.assertIsNone(verdict.payload)

    def test_everything_else_reads_as_it_does_for_overpass(self) -> None:
        half = self._raw("cafes")[:500]
        for status, body, code, text in (
            (429, b"<html>Too Many Requests</html>", 299, "server replied: Too Many Requests"),
            (403, b"<html>Forbidden</html>", 201, "server replied: Forbidden"),
            (406, b"<html>Not Acceptable</html>", 299, "server replied: Not Acceptable"),
            (502, b"<html>Bad Gateway</html>", 401, "server replied: Bad Gateway"),
            (503, b"<html>Service Unavailable</html>", 401, "server replied: Service Unavailable"),
            (504, b"<html>Gateway Time-out</html>", 401, "server replied: Gateway Time-out"),
            (404, b"404 page not found", 203, "server replied: Not Found"),
            (0, b"", 5, ""),
            (0, b"", 4, "Operation timed out"),
            (0, b"", 1, "Connection refused"),
            (0, b"", 2, "Connection closed"),
            (200, half, 2, "Connection closed"),
            (200, b"", 0, ""),
        ):
            with self.subTest(status=status, text=text):
                self.assertEqual(
                    postpass.classify(status, body, code, text, transfer_s=55),
                    transport.classify(status, body, code, text, transfer_s=55),
                )

    def test_details_are_plain_and_name_no_machinery(self) -> None:
        verdicts = [
            postpass.classify(400, _sql_error(STATEMENT_TIMEOUT)),
            postpass.classify(400, _sql_error("pq: out of memory")),
            postpass.classify(400, _sql_error("pq: syntax error")),
            postpass.classify(200, b"<html></html>"),
            postpass.classify(200, b"", oversize=True),
            postpass.classify(200, self._raw("cafes")),
        ]
        for verdict in verdicts:
            with self.subTest(kind=verdict.kind):
                # A verb phrase that completes "<host> ...", in plain words.
                self.assertTrue(verdict.detail[0].islower())
                self.assertFalse(any(word in verdict.detail for word in transport.JARGON))
                self.assertNotIn("pq:", verdict.detail)

    def test_the_host_board_treats_postpass_like_any_host(self) -> None:
        now = [1000.0]
        board = mirrors.HostBoard(clock=lambda: now[0], spacing_s=0.0)
        host = POSTPASS_HOST
        sent = []
        waited = []

        def pause(seconds, _host, why):
            waited.append((round(seconds, 3), why))
            now[0] += seconds

        def ask(verdict):
            def attempt(_host):
                sent.append(verdict.kind)
                return verdict

            return mirrors.ask_hosts(
                (host,), attempt, board, mirrors.RequestBudget(), pause
            )

        heavy = ask(postpass.classify(400, _sql_error(STATEMENT_TIMEOUT)))
        self.assertEqual(heavy.kind, transport.SERVER_TIMEOUT)
        # A statement timeout rests Postpass briefly, as a server timeout
        # rests an Overpass host; a short rest is waited out.
        self.assertEqual(board.remaining(host), 30.0)
        refused = ask(postpass.classify(400, _sql_error("pq: syntax error")))
        self.assertEqual(refused.kind, transport.BAD_QUERY)
        self.assertEqual(waited, [(30.0, mirrors.RESTING)])
        self.assertEqual(board.remaining(host), 0.0)  # a refused request is no fault
        unusable = ask(postpass.classify(200, b"<html>maintenance</html>"))
        self.assertEqual(unusable.kind, mirrors.LOST)
        # The timeout was no strike, so the busy rest does not double.
        self.assertEqual(board.strikes(host), 1)
        self.assertEqual(board.remaining(host), mirrors.COOLDOWN_SECONDS[transport.BUSY])
        # Resting longer than a part may wait, it is not asked at all, even
        # for an answer it would give.
        later = ask(postpass.classify(200, self._raw("cafes")))
        self.assertEqual(later.kind, mirrors.LOST)
        self.assertEqual(sent, [transport.SERVER_TIMEOUT, transport.BAD_QUERY, transport.BUSY])


if __name__ == "__main__":
    unittest.main()
