from __future__ import annotations

import json
import unittest

from zero2agent_osm_downloader.core.catalog import TagSpec, get_preset
from zero2agent_osm_downloader.core.query import (
    MAX_ADVANCED_FILTERS,
    MAX_FEATURES,
    MAX_REGEX_VALUE_LENGTH,
    OVERPASS_ENDPOINTS,
    QueryError,
    advanced_specs,
    build_query,
    compact_tags,
    excludes_tag,
    matching_specs,
    normalize_tag,
    validate_bbox,
    validate_payload,
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
        text = compact_tags({"name": "İzmir", "x": "y" * 20_000})
        self.assertLessEqual(len(text), 16_000)
        decoded = json.loads(text)
        self.assertTrue(decoded["_truncated"])
        self.assertEqual(decoded["name"], "İzmir")

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
