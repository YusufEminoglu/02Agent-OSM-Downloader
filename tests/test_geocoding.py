"""Checks for Nominatim URL construction and result ranking.

The parsing tests read real answers saved from nominatim.openstreetmap.org
(``format=jsonv2&addressdetails=1&extratags=1``, captured on 2026-11-02) in
``tests/fixtures``.  The test fixture used to be written by hand and carried a
``class`` key; jsonv2 calls it ``category``, so the boundary path had never
worked through Nominatim while every test stayed green.
"""
from __future__ import annotations

import copy
import json
import math
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from zero2agent_osm_downloader.core.geocoding import (
    MAX_BOUNDARY_BYTES,
    MAX_GEOCODE_RESULTS,
    NOMINATIM_HOST,
    GeocodeError,
    boundary_geojson,
    build_lookup_url,
    build_search_url,
    parse_boundary,
    parse_geocode_results,
)
from zero2agent_osm_downloader.core.places import MAX_PLACE_RESULTS

FIXTURES = Path(__file__).resolve().parent / "fixtures"
#: Konak, İzmir: the district's boundary relation 1269255.
KONAK_AREA_ID = 3_601_269_255


def _real(name: str) -> list:
    """A saved Nominatim jsonv2 answer, as the geocoder sent it."""
    return json.loads(
        (FIXTURES / f"nominatim_{name}.json").read_text(encoding="utf-8")
    )


def _variant(entry: dict, **changes) -> dict:
    """A copy of a real entry with only the named fields changed."""
    result = copy.deepcopy(entry)
    result.update(changes)
    return result


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


class SavedFixtureTests(unittest.TestCase):
    def test_saved_answers_have_the_real_jsonv2_shape(self) -> None:
        # The hand-written fixture said "class"; jsonv2 never does. If a
        # fabricated entry creeps back in, this fails before any parsing test
        # can pass on it.
        for name in ("konak", "province", "noarea", "amsterdam"):
            for entry in _real(name):
                with self.subTest(fixture=name, osm_id=entry["osm_id"]):
                    self.assertIn("category", entry)
                    self.assertNotIn("class", entry)
                    self.assertIn(entry["osm_type"], ("node", "way", "relation"))
                    self.assertEqual(len(entry["boundingbox"]), 4)


class ResultParsingTests(unittest.TestCase):
    def test_konak_boundary_relation_has_its_overpass_area(self) -> None:
        candidate = parse_geocode_results(_real("konak"), "Konak, Izmir")[0]
        self.assertEqual(candidate.name, "Konak")
        self.assertEqual(candidate.label, "Konak, İzmir, Ege Bölgesi, Türkiye")
        self.assertEqual(candidate.admin_level, "6")
        self.assertEqual(candidate.kind, "town")
        self.assertEqual(candidate.source, "nominatim")
        self.assertEqual((candidate.osm_type, candidate.osm_id), ("relation", 1269255))
        # Nominatim orders its box south, north, west, east.
        self.assertEqual(
            candidate.bbox, (38.3930028, 27.0688039, 38.4508181, 27.1896253)
        )
        self.assertTrue(candidate.has_area)
        self.assertEqual(candidate.area_id, KONAK_AREA_ID)

    def test_every_boundary_relation_in_a_real_answer_gets_an_area(self) -> None:
        results = parse_geocode_results(_real("konak"), "Konak")
        self.assertEqual(
            [(item.name, item.admin_level, item.area_id) for item in results],
            [("Konak", "6", KONAK_AREA_ID), ("Konak Mahallesi", "8", 3_610_340_921)],
        )
        amsterdam = parse_geocode_results(_real("amsterdam"), "Amsterdam")
        self.assertEqual(
            [item.area_id for item in amsterdam], [3_600_271_110, 3_600_047_811]
        )

    def test_a_place_node_and_a_province_relation(self) -> None:
        results = parse_geocode_results(_real("province"), "Izmir")
        by_type = {item.osm_type: item for item in results}
        # A place node is a point: there is no area to follow.
        self.assertFalse(by_type["node"].has_area)
        self.assertEqual(by_type["node"].area_id, 0)
        self.assertEqual(by_type["relation"].admin_level, "4")
        self.assertEqual(by_type["relation"].area_id, 3_600_223_167)

    def test_non_boundary_results_do_not_claim_an_area(self) -> None:
        # A military area, two streets, a hospital, a restaurant and a hotel:
        # Overpass builds areas only from boundary and place polygons, so an
        # id for any of these would match nothing and download nothing.
        results = parse_geocode_results(_real("noarea"), "Kordon")
        self.assertEqual(len(results), 10)
        for item in results:
            with self.subTest(label=item.label):
                self.assertFalse(item.has_area)
                self.assertEqual(item.area_id, 0)

    def test_the_older_class_key_is_still_read(self) -> None:
        # format=json (not jsonv2) names the same field "class".
        entry = _real("konak")[0]
        legacy = {
            ("class" if key == "category" else key): value
            for key, value in entry.items()
        }
        candidate = parse_geocode_results([legacy], "Konak")[0]
        self.assertEqual(candidate.area_id, KONAK_AREA_ID)

    def test_boundaries_outrank_equally_important_non_boundaries(self) -> None:
        # A real military area, given a little more importance than the real
        # Konak Mahallesi boundary: the boundary still comes first.
        military = _variant(_real("noarea")[0], importance=0.40)
        mahalle = _real("konak")[1]
        self.assertLess(float(mahalle["importance"]), 0.40)
        results = parse_geocode_results([military, mahalle], "Konak")
        self.assertEqual(results[0].name, "Konak Mahallesi")
        self.assertEqual(results[0].area_id, 3_610_340_921)

    def test_entries_without_a_usable_box_are_dropped(self) -> None:
        entry = _real("konak")[0]
        self.assertEqual(
            parse_geocode_results(
                [
                    _variant(entry, boundingbox=None),
                    _variant(entry, boundingbox=["a", "b", "c", "d"]),
                    _variant(entry, boundingbox=["38.5", "38.4", "27.0", "27.2"]),
                    "not a dict",
                ],
                "Konak",
            ),
            (),
        )

    def test_duplicate_objects_appear_once_and_output_is_bounded(self) -> None:
        entry = _real("konak")[0]
        entries = [entry, copy.deepcopy(entry)] + [
            _variant(entry, osm_id=1000 + index)
            for index in range(MAX_PLACE_RESULTS + 5)
        ]
        results = parse_geocode_results(entries, "Konak")
        self.assertLessEqual(len(results), MAX_PLACE_RESULTS)
        identifiers = [item.osm_id for item in results]
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_non_list_payloads_are_handled(self) -> None:
        for payload in ({"error": "unavailable"}, None, "", []):
            with self.subTest(payload=payload):
                self.assertEqual(parse_geocode_results(payload, "Konak"), ())


#: The real lookup answer for Konak, saved from nominatim.openstreetmap.org on
#: 2026-11-02 through the plugin's own request (build_lookup_url).
LOOKUP_FIXTURE = FIXTURES / "nominatim_lookup_konak.json"


class LookupUrlTests(unittest.TestCase):
    def test_a_relation_area_looks_up_the_relation_outline(self) -> None:
        url = build_lookup_url(KONAK_AREA_ID)
        self.assertEqual(
            url,
            "https://nominatim.openstreetmap.org/lookup?osm_ids=R1269255"
            "&format=jsonv2&polygon_geojson=1&polygon_threshold=0.0001",
        )
        parts = urlsplit(url)
        self.assertEqual((parts.scheme, parts.hostname, parts.path),
                         ("https", NOMINATIM_HOST, "/lookup"))
        self.assertEqual(build_lookup_url(str(KONAK_AREA_ID)), url)

    def test_a_way_area_looks_up_the_way(self) -> None:
        query = parse_qs(urlsplit(build_lookup_url(2_400_000_005)).query)
        self.assertEqual(query["osm_ids"], ["W5"])
        self.assertEqual(query["polygon_geojson"], ["1"])

    def test_only_overpass_area_ids_reach_the_url(self) -> None:
        for bad in (0, 1269255, "R1269255", "3601269255&format=xml", 3_700_000_000,
                    2_500_000_000, None, "", "abc"):
            with self.subTest(value=bad):
                with self.assertRaises(GeocodeError):
                    build_lookup_url(bad)

    def test_the_saved_answer_is_what_the_url_asks_for(self) -> None:
        answer = json.loads(LOOKUP_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(answer), 1)
        entry = answer[0]
        self.assertEqual((entry["osm_type"], entry["osm_id"]), ("relation", 1269255))
        self.assertEqual(entry["category"], "boundary")
        self.assertNotIn("class", entry)
        self.assertEqual(entry["geojson"]["type"], "Polygon")


class BoundaryParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = LOOKUP_FIXTURE.read_bytes()
        self.answer = json.loads(self.raw.decode("utf-8"))

    def _with_geojson(self, geojson) -> list:
        answer = copy.deepcopy(self.answer)
        answer[0]["geojson"] = geojson
        return answer

    def test_the_real_konak_outline_is_read(self) -> None:
        outline = parse_boundary(self.raw, KONAK_AREA_ID)
        rings = outline["coordinates"]
        self.assertEqual(outline["type"], "Polygon")
        self.assertEqual([len(ring) for ring in rings], [257])
        self.assertEqual(rings, self.answer[0]["geojson"]["coordinates"])
        self.assertEqual(rings[0][0], [27.0688039, 38.4012772])
        self.assertEqual(rings[0][0], rings[0][-1])
        # The simplified outline stays inside the box Nominatim reports.
        south, north, west, east = (float(item) for item in self.answer[0]["boundingbox"])
        self.assertTrue(all(west <= lon <= east and south <= lat <= north
                            for lon, lat in rings[0]))
        self.assertEqual(min(lon for lon, _lat in rings[0]), west)
        self.assertEqual(max(lat for _lon, lat in rings[0]), north)

    def test_bytes_text_and_parsed_answers_read_alike(self) -> None:
        expected = parse_boundary(self.raw, KONAK_AREA_ID)
        self.assertEqual(parse_boundary(self.raw.decode("utf-8"), KONAK_AREA_ID), expected)
        self.assertEqual(parse_boundary(self.answer, KONAK_AREA_ID), expected)

    def test_only_the_place_the_area_id_names_counts(self) -> None:
        # Konak Mahallesi's area id, and the same number as a way.
        self.assertIsNone(parse_boundary(self.raw, 3_610_340_921))
        self.assertIsNone(parse_boundary(self.raw, 2_401_269_255))
        as_text = copy.deepcopy(self.answer)
        as_text[0]["osm_id"] = "1269255"
        self.assertIsNotNone(parse_boundary(as_text, KONAK_AREA_ID))
        other_first = [dict(self.answer[0], osm_id=10340921)] + self.answer
        self.assertEqual(parse_boundary(other_first, KONAK_AREA_ID),
                         parse_boundary(self.raw, KONAK_AREA_ID))

    def test_answers_without_an_area_outline(self) -> None:
        ring = self.answer[0]["geojson"]["coordinates"][0]
        for label, payload in (
            ("label point", self._with_geojson({"type": "Point", "coordinates": ring[0]})),
            ("line", self._with_geojson({"type": "LineString", "coordinates": ring})),
            ("no geojson", self._with_geojson(None)),
            ("nothing found", []),
            ("an error", {"error": "Unable to geocode"}),
            ("not JSON", b"<html>busy</html>"),
            ("not UTF-8", b"\xff\xfe"),
            ("nothing", None),
        ):
            with self.subTest(answer=label):
                self.assertIsNone(parse_boundary(payload, KONAK_AREA_ID))

    def test_damaged_outlines_are_refused(self) -> None:
        ring = self.answer[0]["geojson"]["coordinates"][0]
        for label, rings in (
            ("not closed", [ring[:-1]]),
            ("three positions", [[ring[0], ring[1], ring[0]]]),
            ("no rings", []),
            ("text coordinate", [[["27.1", "38.4"]] + ring[1:]]),
            ("bool coordinate", [[[True, False]] + ring[1:]]),
            ("NaN", [[[math.nan, 38.4]] + ring[1:]]),
            ("off the globe", [[[200.0, 38.4]] + ring[1:-1] + [[200.0, 38.4]]]),
            ("short position", [[[27.1]] + ring[1:]]),
        ):
            with self.subTest(outline=label):
                geojson = {"type": "Polygon", "coordinates": rings}
                self.assertIsNone(boundary_geojson(geojson))
                self.assertIsNone(parse_boundary(self._with_geojson(geojson), KONAK_AREA_ID))
        broken_part = {"type": "MultiPolygon", "coordinates": [[ring], [ring[:-1]]]}
        self.assertIsNone(boundary_geojson(broken_part))

    def test_a_multipolygon_keeps_every_part_and_positions_lose_their_height(self) -> None:
        ring = self.answer[0]["geojson"]["coordinates"][0]
        raised = [[lon, lat, 12.5] for lon, lat in ring]
        outline = boundary_geojson({"type": "MultiPolygon", "coordinates": [[ring], [raised]]})
        self.assertEqual(outline["type"], "MultiPolygon")
        self.assertEqual(outline["coordinates"], [[ring], [ring]])

    def test_an_answer_over_8_mb_is_refused_unread(self) -> None:
        padded = self.raw + b" " * 1000
        self.assertIsNotNone(parse_boundary(padded, KONAK_AREA_ID))
        too_big = self.raw + b" " * (MAX_BOUNDARY_BYTES - len(self.raw) + 1)
        self.assertEqual(len(too_big), MAX_BOUNDARY_BYTES + 1)
        self.assertIsNone(parse_boundary(too_big, KONAK_AREA_ID))
        self.assertEqual(MAX_BOUNDARY_BYTES, 8 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
