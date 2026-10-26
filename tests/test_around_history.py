from __future__ import annotations

import json
import math
import unittest

from zero2agent_osm_downloader.core import around, history
from zero2agent_osm_downloader.core.catalog import get_preset
from zero2agent_osm_downloader.core.query import (
    HEAVY_WILDCARD_KM2,
    QueryError,
    is_heavy_wildcard,
    merge_elements,
    osm_timestamp,
)


class AroundTests(unittest.TestCase):
    def test_every_ring_vertex_lies_on_the_circle(self) -> None:
        for lat in (0.0, 38.42, 60.0):
            ring = around.circle_ring(27.14, lat, 500.0)
            self.assertEqual(ring[0], ring[-1])
            for lon2, lat2 in ring:
                self.assertAlmostEqual(around.haversine_m(27.14, lat, lon2, lat2), 500.0, delta=0.01)

    def test_bbox_is_the_circle_tangent_rectangle(self) -> None:
        south, west, north, east = around.circle_bbox(27.14, 38.42, 1000.0)
        self.assertAlmostEqual(around.haversine_m(27.14, 38.42, 27.14, north), 1000.0, delta=0.01)
        self.assertAlmostEqual(around.haversine_m(27.14, 38.42, 27.14, south), 1000.0, delta=0.01)
        # One degree of longitude at 38.42 N is cos(38.42) of one at the equator.
        half_width_deg = (east - west) / 2.0
        expected = math.degrees(1000.0 / around.EARTH_RADIUS_M) / math.cos(math.radians(38.42))
        self.assertAlmostEqual(half_width_deg, expected, delta=expected * 0.002)

    def test_radius_and_centre_are_bounded(self) -> None:
        self.assertEqual(around.validate_radius("750"), 750.0)
        for bad in (0, 10, 5001, "x", float("nan")):
            with self.subTest(bad=bad), self.assertRaises(QueryError):
                around.validate_radius(bad)
        with self.assertRaises(QueryError):
            around.validate_centre(200, 10)


class TimestampAndHeavyTests(unittest.TestCase):
    def test_oldest_valid_timestamp_wins(self) -> None:
        payloads = [
            {"osm3s": {"timestamp_osm_base": "2026-10-26T08:15:02Z"}, "elements": []},
            {"osm3s": {"timestamp_osm_base": "2026-10-26T08:14:00Z"}, "elements": []},
            {"osm3s": {"timestamp_osm_base": "yesterday"}, "elements": []},
            {"elements": []},
        ]
        self.assertEqual(osm_timestamp(payloads), "2026-10-26T08:14:00Z")
        self.assertEqual(osm_timestamp([{"elements": []}]), "")

    def test_merging_tiles_keeps_the_oldest_timestamp(self) -> None:
        merged = merge_elements([
            {"osm3s": {"timestamp_osm_base": "2026-10-26T08:15:02Z"},
             "elements": [{"type": "node", "id": 1}]},
            {"osm3s": {"timestamp_osm_base": "2026-10-26T08:10:00Z"},
             "elements": [{"type": "node", "id": 1}, {"type": "node", "id": 2}]},
        ])
        self.assertEqual(len(merged["elements"]), 2)
        self.assertEqual(merged["osm3s"]["timestamp_osm_base"], "2026-10-26T08:10:00Z")

    def test_only_a_large_wildcard_request_is_heavy(self) -> None:
        wildcard = get_preset("osm_as_is_full_visible_stack").tags
        filtered = get_preset("buildings").tags
        self.assertTrue(is_heavy_wildcard(wildcard, HEAVY_WILDCARD_KM2 + 0.1))
        self.assertFalse(is_heavy_wildcard(wildcard, HEAVY_WILDCARD_KM2))
        self.assertFalse(is_heavy_wildcard(filtered, 500.0))


class HistoryTests(unittest.TestCase):
    EXTENT = {"xmin": 27.1, "ymin": 38.4, "xmax": 27.2, "ymax": 38.5, "crs": "EPSG:4326"}

    def entry(self, preset=0, label="Parks"):
        return history.make_entry(
            label, "zero2agentosm:download_preset",
            {"PRESET": [preset], "EXTENT": object(), "OUTPUT_POINTS": "TEMPORARY_OUTPUT"},
            self.EXTENT, "2026-10-26T10:00:00",
        )

    def test_outputs_and_extent_object_are_not_stored(self) -> None:
        entry = self.entry()
        self.assertEqual(entry["parameters"], {"PRESET": [0]})
        self.assertEqual(entry["extent"]["crs"], "EPSG:4326")

    def test_round_trip_dedupe_and_cap(self) -> None:
        entries = []
        for index in range(history.MAX_ENTRIES + 5):
            entries = history.add(entries, self.entry(index, f"P{index}"))
        self.assertEqual(len(entries), history.MAX_ENTRIES)
        self.assertEqual(entries[0]["label"], f"P{history.MAX_ENTRIES + 4}")
        again = history.add(entries, self.entry(5, "P5 again"))
        self.assertEqual(again[0]["label"], "P5 again")
        self.assertEqual(sum(1 for e in again if e["parameters"] == {"PRESET": [5]}), 1)
        self.assertEqual(history.loads(history.dumps(again)), again)

    def test_corrupt_storage_costs_only_the_bad_entries(self) -> None:
        good = self.entry()
        text = json.dumps([good, {"label": "x"}, 5, {**good, "algorithm": "native:buffer"}])
        self.assertEqual(history.loads(text), [good])
        self.assertEqual(history.loads("not json"), [])
        self.assertEqual(history.loads(None), [])

    def test_foreign_algorithms_and_empty_extents_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            history.make_entry("x", "native:buffer", {}, self.EXTENT, "now")
        with self.assertRaises(ValueError):
            history.make_entry("x", "zero2agentosm:download_preset", {},
                               {**self.EXTENT, "xmax": 27.1}, "now")


if __name__ == "__main__":
    unittest.main()
