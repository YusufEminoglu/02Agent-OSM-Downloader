from __future__ import annotations

import unittest

from zero2agent_osm_downloader.core.places import (
    PlaceCandidate,
    build_place_query,
    parse_place_candidates,
)

_BOX = (38.39, 27.06, 38.45, 27.19)


class AreaIdTests(unittest.TestCase):
    """Overpass area ids: relation + 3,600,000,000 and way + 2,400,000,000."""

    def test_relation_and_way_offsets(self) -> None:
        relation = PlaceCandidate("Konak", "Konak", "town", "6", _BOX,
                                  "relation", 1269255, "nominatim", True)
        self.assertEqual(relation.area_id, 3_601_269_255)
        way = PlaceCandidate("Park", "Park", "park", "", _BOX, "way", 42,
                             "nominatim", True)
        self.assertEqual(way.area_id, 2_400_000_042)

    def test_no_area_without_an_area_flag_or_for_a_node(self) -> None:
        for osm_type, has_area in (("relation", False), ("way", False), ("node", True)):
            with self.subTest(osm_type=osm_type, has_area=has_area):
                candidate = PlaceCandidate("X", "X", "town", "", _BOX,
                                           osm_type, 99, "nominatim", has_area)
                self.assertEqual(candidate.area_id, 0)


class PlaceTests(unittest.TestCase):
    def test_place_query_escapes_names_and_targets_admin_and_place_features(self) -> None:
        query = build_place_query("Izmir Konak")
        self.assertIn('["boundary"="administrative"]', query)
        self.assertIn('["place"]', query)
        self.assertNotIn('";out:', query)

    def test_candidates_prefer_named_administrative_relations(self) -> None:
        payload = {
            "elements": [
                {
                    "type": "node",
                    "lat": 38.4,
                    "lon": 27.1,
                    "tags": {"name": "Konak", "place": "town"},
                },
                {
                    "type": "relation",
                    "bounds": {
                        "minlat": 38.3,
                        "minlon": 27.0,
                        "maxlat": 38.5,
                        "maxlon": 27.2,
                    },
                    "tags": {
                        "name": "Konak",
                        "boundary": "administrative",
                        "admin_level": "6",
                    },
                },
            ]
        }
        candidates = parse_place_candidates(payload, "Konak")
        self.assertEqual(candidates[0].kind, "administrative")
        self.assertEqual(candidates[0].admin_level, "6")


if __name__ == "__main__":
    unittest.main()
