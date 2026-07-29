from __future__ import annotations

import json
import unittest

from zero2agent_osm_downloader.core.catalog import TagSpec, get_preset
from zero2agent_osm_downloader.core.query import (
    MAX_FEATURES,
    OVERPASS_ENDPOINTS,
    QueryError,
    build_query,
    compact_tags,
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

    def test_duplicate_selectors_are_deduplicated(self) -> None:
        spec = TagSpec("building", "", "polygon")
        query = build_query((spec, spec), SMALL_BBOX)
        self.assertEqual(query.count('way["building"]'), 1)
        self.assertEqual(query.count('relation["building"]'), 1)

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

    def test_mirrors_are_fixed_https_hosts(self) -> None:
        self.assertEqual(len(OVERPASS_ENDPOINTS), 3)
        self.assertTrue(all(item.startswith("https://") for item in OVERPASS_ENDPOINTS))


if __name__ == "__main__":
    unittest.main()
