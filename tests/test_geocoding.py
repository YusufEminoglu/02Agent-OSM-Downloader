"""Checks for Nominatim URL construction and result ranking."""
from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlsplit

from zero2agent_osm_downloader.core.geocoding import (
    MAX_GEOCODE_RESULTS,
    NOMINATIM_HOST,
    GeocodeError,
    build_search_url,
    parse_geocode_results,
)
from zero2agent_osm_downloader.core.places import MAX_PLACE_RESULTS


def _entry(**overrides) -> dict:
    entry = {
        "osm_type": "relation",
        "osm_id": 223474,
        "display_name": "Konak, İzmir, Türkiye",
        "addresstype": "municipality",
        "class": "boundary",
        "type": "administrative",
        "importance": 0.55,
        "boundingbox": ["38.3500", "38.4600", "27.0500", "27.2000"],
        "extratags": {"admin_level": "6"},
        "address": {"municipality": "Konak"},
    }
    entry.update(overrides)
    return entry


class SearchUrlTests(unittest.TestCase):
    def test_url_targets_only_the_pinned_geocoder(self) -> None:
        url = build_search_url("Konak, Izmir")
        self.assertEqual(urlsplit(url).scheme, "https")
        self.assertEqual(urlsplit(url).hostname, NOMINATIM_HOST)
        self.assertEqual(urlsplit(url).path, "/search")

    def test_place_name_is_encoded_not_interpolated(self) -> None:
        url = build_search_url("Saint-Étienne & Co, France")
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["q"], ["Saint-Étienne & Co, France"])
        self.assertEqual(query["format"], ["jsonv2"])
        self.assertEqual(query["extratags"], ["1"])
        # A raw ampersand would otherwise split into a second parameter.
        self.assertNotIn("Co", query)

    def test_result_limit_is_clamped_and_validated(self) -> None:
        self.assertEqual(
            parse_qs(urlsplit(build_search_url("Izmir", 999)).query)["limit"],
            [str(MAX_GEOCODE_RESULTS)],
        )
        self.assertEqual(
            parse_qs(urlsplit(build_search_url("Izmir", 0)).query)["limit"],
            ["1"],
        )
        with self.assertRaises(GeocodeError):
            build_search_url("Izmir", "many")

    def test_unusable_place_names_are_refused(self) -> None:
        for bad in ("", "   ", 'Izmir" or 1=1', "x" * 200):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    build_search_url(bad)


class ResultParsingTests(unittest.TestCase):
    def test_relation_result_yields_a_usable_boundary(self) -> None:
        candidate = parse_geocode_results([_entry()], "Konak")[0]
        self.assertEqual(candidate.name, "Konak")
        self.assertEqual(candidate.label, "Konak, İzmir, Türkiye")
        self.assertEqual(candidate.admin_level, "6")
        self.assertEqual(candidate.source, "nominatim")
        # Nominatim orders its box south, north, west, east.
        self.assertEqual(candidate.bbox, (38.35, 27.05, 38.46, 27.20))
        self.assertEqual(candidate.area_id, 3_600_223_474)

    def test_way_and_node_results_map_to_the_right_area_kind(self) -> None:
        way = parse_geocode_results(
            [_entry(osm_type="way", osm_id=42)], "Konak"
        )[0]
        self.assertEqual(way.area_id, 2_400_000_042)
        node = parse_geocode_results(
            [_entry(osm_type="node", osm_id=42)], "Konak"
        )[0]
        # A place node has no area, so a download must fall back to its box.
        self.assertEqual(node.area_id, 0)

    def test_non_area_relations_do_not_claim_an_area(self) -> None:
        # Overpass builds areas only from boundary and place polygons. A river
        # or route relation would otherwise yield an in-range id that matches
        # nothing, producing an empty download with no error to explain it.
        for entry_class in ("waterway", "route", "building", "highway"):
            with self.subTest(cls=entry_class):
                candidate = parse_geocode_results(
                    [_entry(**{"class": entry_class})], "Konak"
                )[0]
                self.assertFalse(candidate.has_area)
                self.assertEqual(candidate.area_id, 0)
        for entry_class in ("boundary", "place"):
            with self.subTest(cls=entry_class):
                candidate = parse_geocode_results(
                    [_entry(**{"class": entry_class})], "Konak"
                )[0]
                self.assertTrue(candidate.has_area)
                self.assertEqual(candidate.area_id, 3_600_223_474)

    def test_boundaries_outrank_equally_important_non_boundaries(self) -> None:
        street = _entry(
            osm_type="way", osm_id=9, display_name="Konak Street, Ankara",
            **{"class": "highway"},
        )
        boundary = _entry(importance=0.4)
        results = parse_geocode_results([street, boundary], "Konak")
        self.assertEqual(results[0].area_id, 3_600_223_474)

    def test_entries_without_a_usable_box_are_dropped(self) -> None:
        self.assertEqual(
            parse_geocode_results(
                [
                    _entry(boundingbox=None),
                    _entry(boundingbox=["a", "b", "c", "d"]),
                    _entry(boundingbox=["38.5", "38.4", "27.0", "27.2"]),
                    "not a dict",
                ],
                "Konak",
            ),
            (),
        )

    def test_duplicate_objects_appear_once_and_output_is_bounded(self) -> None:
        entries = [_entry(), _entry()] + [
            _entry(osm_id=1000 + index) for index in range(MAX_PLACE_RESULTS + 5)
        ]
        results = parse_geocode_results(entries, "Konak")
        self.assertLessEqual(len(results), MAX_PLACE_RESULTS)
        identifiers = [item.osm_id for item in results]
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_non_list_payloads_are_handled(self) -> None:
        for payload in ({"error": "unavailable"}, None, "", []):
            with self.subTest(payload=payload):
                self.assertEqual(parse_geocode_results(payload, "Konak"), ())


if __name__ == "__main__":
    unittest.main()
