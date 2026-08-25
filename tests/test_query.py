from __future__ import annotations

import json
import unittest

from zero2agent_osm_downloader.core.catalog import (
    TagSpec,
    get_preset,
    presets_for_group,
)
from zero2agent_osm_downloader.core.query import (
    MAX_ADVANCED_FILTERS,
    MAX_FEATURES,
    MAX_REGEX_VALUE_LENGTH,
    OVERPASS_ENDPOINTS,
    QueryError,
    advanced_specs,
    build_query,
    build_queries,
    compact_tags,
    excludes_tag,
    matching_specs,
    normalize_tag,
    validate_bbox,
    validate_payload,
    wildcard_excluded,
)

SMALL_BBOX = (38.410, 27.120, 38.416, 27.126)


class QueryTests(unittest.TestCase):
    def test_compound_preset_query_is_one_bounded_request(self) -> None:
        preset = get_preset("green_blue_all")
        query = build_query(preset.tags, SMALL_BBOX)
        self.assertEqual(query.count("[out:json]"), 1)
        self.assertIn('way["leisure"="park"]', query)
        self.assertIn('relation["natural"="water"]', query)
        self.assertIn('way["waterway"]', query)
        self.assertNotIn("http", query)

    def test_urban_context_query_contains_every_requested_theme(self) -> None:
        query = build_query(get_preset("urban_context").tags, SMALL_BBOX)
        self.assertIn('way["highway"]', query)
        self.assertIn('way["building"]', query)
        self.assertIn('node["natural"="tree"]', query)
        self.assertIn('way["natural"="tree_row"]', query)
        self.assertEqual(query.count("[out:json]"), 1)
        self.assertNotIn("http", query)

    def test_osm_as_is_full_visible_stack_is_bounded_and_multi_geometry(self) -> None:
        preset = get_preset("osm_as_is_full_visible_stack")
        self.assertEqual(len(preset.tags), 3)
        query = build_query(preset.tags, SMALL_BBOX)
        self.assertIn('node[~"."~"."]', query)
        self.assertIn('way[~"."~"."]', query)
        self.assertIn('relation[~"."~"."]', query)
        self.assertEqual(query.count("[out:json]"), 1)
        self.assertNotIn("http", query)

    def test_wildcard_leaves_boundaries_and_places_to_the_places_group(
        self,
    ) -> None:
        query = build_query(
            get_preset("osm_as_is_full_visible_stack").tags, SMALL_BBOX
        )
        # Asked for at Overpass, so they never travel...
        self.assertEqual(query.count('["boundary"!="administrative"]'), 3)
        self.assertEqual(query.count('[!"place"]'), 3)
        # ...and refused again on the way in, so a cached or mirrored payload
        # cannot slip them past the query filter.
        self.assertTrue(wildcard_excluded({"boundary": "administrative"}))
        self.assertTrue(wildcard_excluded({"place": "town"}))
        self.assertFalse(wildcard_excluded({"highway": "footway"}))
        self.assertFalse(wildcard_excluded({"boundary": "national_park"}))

    def test_no_osm_as_is_preset_asks_for_boundaries_or_places(self) -> None:
        for preset in presets_for_group("osm_as_is"):
            with self.subTest(preset=preset.preset_id):
                for tag in preset.tags:
                    self.assertNotEqual(tag.key, "place")
                    self.assertNotEqual(
                        (tag.key, tag.value), ("boundary", "administrative")
                    )

    def test_admin_level_preset_combines_boundary_and_exact_level(self) -> None:
        query = build_query(get_preset("admin_level_8").tags, SMALL_BBOX)
        expected = (
            'way["boundary"="administrative"]["admin_level"="8"]'
            "(38.4100000,27.1200000,38.4160000,27.1260000);"
        )
        self.assertIn(expected, query)
        self.assertIn(
            'relation["boundary"="administrative"]["admin_level"="8"]',
            query,
        )
        self.assertNotIn('node["admin_level"="8"]', query)

    def test_admin_level_requests_are_not_tiled(self) -> None:
        queries = build_queries(
            get_preset("admin_level_4").tags,
            (38.30, 27.00, 38.55, 27.30),
        )
        self.assertEqual(len(queries), 1)
        self.assertIn(
            'relation["boundary"="administrative"]["admin_level"="4"]',
            queries[0],
        )

    def test_qualified_matching_requires_every_tag(self) -> None:
        spec = next(
            tag for tag in get_preset("admin_level_4").tags
            if tag.geometry == "line"
        )
        self.assertEqual(
            matching_specs(
                {"boundary": "administrative", "admin_level": "4"},
                (spec,),
                "line",
            ),
            (spec,),
        )
        self.assertEqual(
            matching_specs({"boundary": "administrative"}, (spec,), "line"),
            (),
        )

    def test_duplicate_selectors_are_deduplicated(self) -> None:
        spec = TagSpec("building", "", "polygon")
        query = build_query((spec, spec), SMALL_BBOX)
        self.assertEqual(query.count('way["building"]'), 1)
        self.assertEqual(query.count('relation["building"]'), 1)

    def test_advanced_any_query_builds_separate_safe_selectors(self) -> None:
        specs = advanced_specs(
            (("amenity", "school"), ("amenity", "kindergarten")),
            ("point", "polygon"),
            "any",
        )
        query = build_query(specs, SMALL_BBOX, "any")
        self.assertIn('node["amenity"="school"]', query)
        self.assertIn('relation["amenity"="kindergarten"]', query)
        self.assertNotIn("<selected extent>", query)

    def test_advanced_all_query_combines_tags_per_geometry(self) -> None:
        specs = advanced_specs(
            (("amenity", "school"), ("wheelchair", "yes")),
            ("polygon",),
            "all",
        )
        query = build_query(specs, SMALL_BBOX, "all")
        combined = '["amenity"="school"]["wheelchair"="yes"]'
        self.assertIn(f"way{combined}", query)
        self.assertIn(f"relation{combined}", query)
        self.assertEqual(query.count("(38.4100000,27.1200000"), 2)

    def test_advanced_filters_are_bounded_and_reject_conflicting_all_keys(self) -> None:
        with self.assertRaises(QueryError):
            advanced_specs(
                tuple((f"key_{index}", "") for index in range(MAX_ADVANCED_FILTERS + 1)),
                ("point",),
            )
        with self.assertRaises(QueryError):
            advanced_specs(
                (("amenity", "school"), ("amenity", "hospital")),
                ("polygon",),
                "all",
            )

    def test_tag_and_extent_injection_are_blocked(self) -> None:
        self.assertEqual(normalize_tag("building", "*"), ("building", ""))
        with self.assertRaises(QueryError):
            normalize_tag('building"];out;', "")
        with self.assertRaises(QueryError):
            validate_bbox(38, 27, 40, 29)

    def test_payload_and_tag_compaction_are_bounded(self) -> None:
        payload = {"elements": [{"type": "node", "id": 1}]}
        self.assertIs(validate_payload(payload), payload)
        with self.assertRaises(QueryError):
            validate_payload({"elements": [{}] * (MAX_FEATURES + 1)})
        text = compact_tags({"name": "Izmir", "x": "y" * 20_000})
        self.assertLessEqual(len(text), 16_000)
        decoded = json.loads(text)
        self.assertTrue(decoded["_truncated"])
        self.assertEqual(decoded["name"], "Izmir")

    def test_matching_specs_uses_key_value_and_geometry(self) -> None:
        specs = (
            TagSpec("amenity", "school", "point"),
            TagSpec("amenity", "school", "polygon"),
        )
        matches = matching_specs(
            {"amenity": "school"}, specs, "point"
        )
        self.assertEqual(matches, (specs[0],))

    def test_all_matching_requires_every_filter(self) -> None:
        specs = advanced_specs(
            (("amenity", "school"), ("wheelchair", "yes")),
            ("point",),
            "all",
        )
        self.assertEqual(
            matching_specs(
                {"amenity": "school"}, specs, "point", "all"
            ),
            (),
        )
        self.assertEqual(
            len(
                matching_specs(
                    {"amenity": "school", "wheelchair": "yes"},
                    specs,
                    "point",
                    "all",
                )
            ),
            2,
        )

    def test_mirrors_are_fixed_https_hosts(self) -> None:
        self.assertEqual(len(OVERPASS_ENDPOINTS), 3)
        self.assertTrue(all(item.startswith("https://") for item in OVERPASS_ENDPOINTS))

    def test_advanced_filter_cap_is_six(self) -> None:
        self.assertEqual(MAX_ADVANCED_FILTERS, 6)
        specs = advanced_specs(
            tuple((f"key_{index}", "") for index in range(MAX_ADVANCED_FILTERS)),
            ("point",),
        )
        self.assertEqual(len({spec.key for spec in specs}), MAX_ADVANCED_FILTERS)

    def test_regex_value_mode_renders_a_tilde_selector(self) -> None:
        specs = advanced_specs(
            (("building", "garage|shed"),),
            ("polygon",),
            value_mode="regex",
        )
        query = build_query(specs, SMALL_BBOX, value_mode="regex")
        self.assertIn('way["building"~"garage|shed"]', query)
        self.assertNotIn('="garage|shed"', query)

    def test_regex_value_mode_rejects_quotes_and_backslashes(self) -> None:
        with self.assertRaises(QueryError):
            normalize_tag("building", 'x"y', value_mode="regex")
        with self.assertRaises(QueryError):
            normalize_tag("building", "x\\y", value_mode="regex")
        with self.assertRaises(QueryError):
            normalize_tag("building", "", value_mode="regex")
        with self.assertRaises(QueryError):
            normalize_tag(
                "building", "x" * (MAX_REGEX_VALUE_LENGTH + 1), value_mode="regex"
            )

    def test_regex_value_mode_allows_regex_metacharacters(self) -> None:
        key, value = normalize_tag(
            "building", "^(garage|shed)$", value_mode="regex"
        )
        self.assertEqual(value, "^(garage|shed)$")

    def test_regex_matching_specs_uses_search_not_equality(self) -> None:
        specs = advanced_specs(
            (("building", "garage|shed"),), ("polygon",), value_mode="regex"
        )
        matches = matching_specs(
            {"building": "detached_garage"},
            specs,
            "polygon",
            value_mode="regex",
        )
        self.assertEqual(matches, specs)

    def test_excludes_tag_matches_any_or_exact_value(self) -> None:
        self.assertTrue(excludes_tag({"amenity": "parking"}, "amenity"))
        self.assertTrue(
            excludes_tag({"amenity": "parking"}, "amenity", "parking")
        )
        self.assertFalse(
            excludes_tag({"amenity": "parking"}, "amenity", "fuel")
        )
        self.assertFalse(excludes_tag({"amenity": "parking"}, "building"))
        self.assertFalse(excludes_tag({}, "", ""))


if __name__ == "__main__":
    unittest.main()
