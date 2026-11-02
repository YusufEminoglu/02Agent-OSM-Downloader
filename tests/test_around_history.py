from __future__ import annotations

import json
import math
import unittest

from zero2agent_osm_downloader.core import around, history
from zero2agent_osm_downloader.core.catalog import PRESETS, PRESETS_BY_ID, get_preset
from zero2agent_osm_downloader.core.query import (
    HEAVY_WILDCARD_KM2,
    QueryError,
    is_heavy_wildcard,
    merge_elements,
    osm_timestamp,
)

#: The PRESET options of 1.5.1, index by index: its enum was built from the
#: catalogue's PRESETS order, computed from `git show v1.5.1:core/catalog.py`
#: (the same file as the 1.6 branch point).  Written out, never derived from
#: the code under test: models and Recent entries store these indexes.
V151_PRESET_IDS = (
    "urban_context", "urban_transit", "urban_amenities", "buildings", "land_use",
    "urban_form", "multimodal_network", "rail_network", "road_network",
    "admin_level_2", "admin_level_3", "admin_level_4", "admin_level_5",
    "admin_level_6", "admin_level_7", "admin_level_8", "admin_level_9",
    "admin_level_10", "place_centres", "osm_as_is_full_visible_stack",
    "osm_as_is_mobility", "osm_as_is_overview", "osm_as_is_urban_detail",
    "bike_facilities", "cycle_network", "car_services", "parking", "education",
    "emergency", "blue_network", "green_spaces", "green_blue_all", "healthcare",
    "public_transport_all", "bus_transit", "rail_transit", "worship",
    "religious_buildings", "sport", "heritage", "tourism", "traffic_calming",
    "traffic_controls",
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
    ORDER = tuple(item.preset_id for item in PRESETS)

    def entry(self, preset=0, label="Parks", where=""):
        return history.make_entry(
            label, "zero2agentosm:download_preset",
            {"PRESET": [preset], "EXTENT": object(), "OUTPUT_POINTS": "TEMPORARY_OUTPUT"},
            self.EXTENT, "2026-10-26T10:00:00",
            presets=[self.ORDER[preset]], where=where,
        )

    def test_outputs_extent_object_and_indexes_are_not_stored(self) -> None:
        entry = self.entry(4)
        self.assertEqual(entry["parameters"], {})
        self.assertEqual(entry["presets"], [self.ORDER[4]])
        self.assertEqual(entry["extent"]["crs"], "EPSG:4326")

    def test_round_trip_dedupe_and_cap(self) -> None:
        entries = []
        for index in range(history.MAX_ENTRIES + 5):
            entries = history.add(entries, self.entry(index, f"P{index}"))
        self.assertEqual(len(entries), history.MAX_ENTRIES)
        self.assertEqual(entries[0]["label"], f"P{history.MAX_ENTRIES + 4}")
        again = history.add(entries, self.entry(5, "P5 again"))
        self.assertEqual(again[0]["label"], "P5 again")
        self.assertEqual(sum(1 for e in again if e["presets"] == [self.ORDER[5]]), 1)
        self.assertEqual(history.loads(history.dumps(again)), again)

    def test_corrupt_storage_costs_only_the_bad_entries(self) -> None:
        good = self.entry()
        text = json.dumps([good, {"label": "x"}, 5, {**good, "algorithm": "native:buffer"},
                           {**good, "presets": "buildings"}])
        self.assertEqual(history.loads(text), [good])
        self.assertEqual(history.loads("not json"), [])
        self.assertEqual(history.loads(None), [])

    def test_foreign_algorithms_and_empty_extents_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            history.make_entry("x", "native:buffer", {}, self.EXTENT, "now")
        with self.assertRaises(ValueError):
            history.make_entry("x", "zero2agentosm:download_custom_tag", {},
                               {**self.EXTENT, "xmax": 27.1}, "now")
        # A preset download without its ids could only be re-run by index.
        with self.assertRaises(ValueError):
            history.make_entry("x", "zero2agentosm:download_preset",
                               {"PRESET": [3]}, self.EXTENT, "now")

    def test_run_again_resolves_ids_against_todays_catalogue(self) -> None:
        entry = self.entry(PRESETS.index(PRESETS_BY_ID["buildings"]), "Buildings")
        parameters = history.request_parameters(entry, self.ORDER)
        self.assertEqual([PRESETS[index].preset_id for index in parameters["PRESET"]],
                         ["buildings"])
        # The same entry after the catalogue gained a preset in front of it.
        shifted = ("new_preset",) + self.ORDER
        parameters = history.request_parameters(entry, shifted)
        self.assertEqual([shifted[index] for index in parameters["PRESET"]], ["buildings"])
        with self.assertRaises(ValueError):
            history.request_parameters({**entry, "presets": ["gone"]}, self.ORDER)

    def test_entries_from_1_5_are_read_by_the_old_catalogue_order(self) -> None:
        # 1.5.x wrote PRESET indexes. Index 3 was Buildings and 30 Green
        # spaces then; the catalogue has since gained a preset before both.
        old = {"label": "Buildings, Green spaces", "algorithm": "zero2agentosm:download_place",
               "parameters": {"PRESET": [3, 30], "PLACE": "Konak", "CLIP": 0, "AREA_ID": "3601269255"},
               "extent": self.EXTENT, "when": "2026-10-28T09:12:00", "summary": "Polygons: 110",
               "style_scope": ""}
        loaded = history.loads(json.dumps([old]))
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["presets"], ["buildings", "green_spaces"])
        self.assertNotIn("PRESET", loaded[0]["parameters"])
        parameters = history.request_parameters(loaded[0], self.ORDER)
        self.assertEqual([PRESETS[index].preset_id for index in parameters["PRESET"]],
                         ["buildings", "green_spaces"])
        self.assertEqual(parameters["PLACE"], "Konak")
        self.assertNotEqual(parameters["PRESET"], [3, 30])
        # An index the old catalogue never had costs that entry only.
        broken = {**old, "parameters": {"PRESET": [99]}}
        self.assertEqual(history.loads(json.dumps([broken, old]))[0]["presets"],
                         ["buildings", "green_spaces"])
        self.assertEqual(len(history.loads(json.dumps([broken]))), 0)

    def test_the_old_order_names_only_presets_that_still_exist(self) -> None:
        self.assertEqual(len(history.LEGACY_PRESET_ORDER), 43)
        self.assertEqual(len(set(history.LEGACY_PRESET_ORDER)), 43)
        for preset_id in history.LEGACY_PRESET_ORDER:
            self.assertIn(preset_id, PRESETS_BY_ID)

    def test_the_old_order_is_exactly_the_one_1_5_1_shipped(self) -> None:
        self.assertEqual(len(V151_PRESET_IDS), 43)
        for index, preset_id in enumerate(V151_PRESET_IDS):
            with self.subTest(index=index):
                self.assertEqual(history.LEGACY_PRESET_ORDER[index], preset_id)
        self.assertEqual(history.LEGACY_PRESET_ORDER, V151_PRESET_IDS)

    def test_where_tells_two_areas_apart_and_is_kept(self) -> None:
        izmir = self.entry(3, "Buildings", where="38.42° N, 27.13° E")
        paris = history.make_entry(
            "Buildings", "zero2agentosm:download_preset", {"PRESET": [3]},
            {"xmin": 2.3, "ymin": 48.8, "xmax": 2.4, "ymax": 48.9, "crs": "EPSG:4326"},
            "2026-10-26T10:00:00", presets=[self.ORDER[3]], where="48.85° N, 2.35° E")
        self.assertNotEqual(izmir["where"], paris["where"])
        self.assertEqual(history.loads(history.dumps([izmir]))[0]["where"], "38.42° N, 27.13° E")
        self.assertEqual(len(self.entry(where="x" * 200)["where"]), 80)


if __name__ == "__main__":
    unittest.main()
