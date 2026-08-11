"""Checks for wide-extent tiling, merging and boundary-area queries."""
from __future__ import annotations

import unittest

from zero2agent_osm_downloader.core.catalog import TagSpec, get_preset
from zero2agent_osm_downloader.core.query import (
    MAX_BBOX_AREA_KM2,
    MAX_TILE_AREA_KM2,
    MAX_TILES,
    QueryError,
    bbox_area_km2,
    build_area_query,
    build_queries,
    merge_elements,
    tile_bbox,
    validate_area_id,
    validate_job_bbox,
)

SMALL_BBOX = (38.410, 27.120, 38.416, 27.126)
# Roughly the whole of Izmir's urban core: far beyond one bounded request.
CITY_BBOX = (38.30, 27.00, 38.55, 27.30)


class TileGeometryTests(unittest.TestCase):
    def test_small_extent_stays_a_single_request(self) -> None:
        tiles = tile_bbox(SMALL_BBOX)
        self.assertEqual(len(tiles), 1)
        self.assertEqual(tiles[0], validate_job_bbox(*SMALL_BBOX))

    def test_city_extent_is_split_into_bounded_tiles(self) -> None:
        tiles = tile_bbox(CITY_BBOX)
        self.assertGreater(len(tiles), 1)
        self.assertLessEqual(len(tiles), MAX_TILES)
        for tile in tiles:
            self.assertLessEqual(bbox_area_km2(*tile), MAX_TILE_AREA_KM2 + 1e-6)

    def test_tiles_cover_the_extent_without_gaps(self) -> None:
        south, west, north, east = CITY_BBOX
        tiles = tile_bbox(CITY_BBOX)
        self.assertAlmostEqual(min(item[0] for item in tiles), south)
        self.assertAlmostEqual(min(item[1] for item in tiles), west)
        self.assertAlmostEqual(max(item[2] for item in tiles), north)
        self.assertAlmostEqual(max(item[3] for item in tiles), east)
        # Compared in degrees: the km estimate is a deliberate worst case that
        # varies with latitude, so it is not additive across tile rows.
        covered = sum(
            (item[2] - item[0]) * (item[3] - item[1]) for item in tiles
        )
        self.assertAlmostEqual(covered, (north - south) * (east - west))

    def test_latitude_bands_never_produce_an_oversized_tile(self) -> None:
        # A degree of longitude is widest near the equator, so a box that
        # straddles it is the worst case for an area estimate.
        for south, north in ((-0.2, 0.2), (38.0, 38.4), (59.5, 59.9)):
            with self.subTest(south=south):
                bbox = (south, 10.0, north, 10.2)
                tiles = tile_bbox(bbox)
                self.assertGreater(len(tiles), 1)
                for tile in tiles:
                    self.assertLessEqual(
                        bbox_area_km2(*tile), MAX_TILE_AREA_KM2 + 1e-6
                    )

    def test_extent_beyond_the_ceiling_is_refused_with_the_number(self) -> None:
        with self.assertRaises(QueryError) as raised:
            tile_bbox((30.0, 20.0, 34.0, 24.0))
        self.assertIn(f"{MAX_BBOX_AREA_KM2:,.0f}", str(raised.exception))

    def test_area_estimate_is_the_worst_case_not_the_average(self) -> None:
        # 1 degree square on the equator is about 111.32 km on both sides.
        self.assertAlmostEqual(
            bbox_area_km2(0.0, 0.0, 1.0, 1.0), 111.32 * 111.32, places=2
        )


class TiledQueryTests(unittest.TestCase):
    def test_one_query_is_built_per_tile(self) -> None:
        specs = get_preset("urban_context").tags
        queries = build_queries(specs, CITY_BBOX)
        self.assertEqual(len(queries), len(tile_bbox(CITY_BBOX)))
        self.assertEqual(len(set(queries)), len(queries))
        for query_text in queries:
            self.assertEqual(query_text.count("[out:json]"), 1)
            self.assertIn('way["building"]', query_text)
            self.assertNotIn("http", query_text)

    def test_merging_tiles_keeps_one_copy_of_a_shared_object(self) -> None:
        # A way crossing a tile seam is returned by both tiles.
        shared = {"type": "way", "id": 7, "tags": {"highway": "primary"}}
        merged = merge_elements(
            (
                {"elements": [shared, {"type": "node", "id": 1}]},
                {"elements": [dict(shared), {"type": "node", "id": 2}]},
            )
        )
        identifiers = sorted(
            (item["type"], item["id"]) for item in merged["elements"]
        )
        self.assertEqual(
            identifiers, [("node", 1), ("node", 2), ("way", 7)]
        )

    def test_merging_rejects_an_invalid_tile_response(self) -> None:
        with self.assertRaises(QueryError):
            merge_elements(({"elements": [{"type": "node", "id": 1}]}, {}))


class BoundaryAreaQueryTests(unittest.TestCase):
    def test_relation_area_query_clips_to_the_named_boundary(self) -> None:
        query_text = build_area_query(
            get_preset("road_network").tags, 3_600_223_474, SMALL_BBOX
        )
        self.assertIn("area(3600223474)->.searchArea;", query_text)
        self.assertIn(
            'way["highway"](area.searchArea)(38.4100000,27.1200000,'
            "38.4160000,27.1260000);",
            query_text,
        )
        self.assertEqual(query_text.count("[out:json]"), 1)

    def test_area_query_is_also_bounded_by_the_extent(self) -> None:
        # An area has no size of its own. Without the extent filter and its
        # ceiling, a country-sized area id paired with a tiny extent would
        # issue an unbounded request.
        with self.assertRaises(QueryError) as raised:
            build_area_query(
                get_preset("road_network").tags,
                3_600_223_474,
                (30.0, 20.0, 34.0, 24.0),
            )
        self.assertIn(f"{MAX_BBOX_AREA_KM2:,.0f}", str(raised.exception))
        for bad_bbox in ((38.0, 27.0, 38.0, 27.1), (91.0, 27.0, 92.0, 27.1)):
            with self.subTest(bbox=bad_bbox):
                with self.assertRaises(QueryError):
                    build_area_query(
                        get_preset("road_network").tags,
                        3_600_223_474,
                        bad_bbox,
                    )

    def test_only_real_overpass_area_ranges_are_accepted(self) -> None:
        self.assertEqual(validate_area_id(3_600_000_001), 3_600_000_001)
        self.assertEqual(validate_area_id("2400000005"), 2_400_000_005)
        for bad in (0, -1, 223474, 9_999_999_999, "drop table", None):
            with self.subTest(value=bad):
                with self.assertRaises(QueryError):
                    validate_area_id(bad)

    def test_area_query_still_validates_its_selectors(self) -> None:
        with self.assertRaises(QueryError):
            build_area_query(
                (TagSpec('building"];out;', "", "polygon"),),
                3_600_000_001,
                SMALL_BBOX,
            )


if __name__ == "__main__":
    unittest.main()
