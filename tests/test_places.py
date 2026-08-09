from __future__ import annotations

import unittest

from zero2agent_osm_downloader.core.places import (
    build_place_query,
    parse_place_candidates,
)


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
