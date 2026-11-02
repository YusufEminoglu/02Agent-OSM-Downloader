"""A closed way is an area only when its tags say so."""
from __future__ import annotations

import unittest

from zero2agent_osm_downloader.core.osm_areas import (
    has_ring_members,
    is_closed_ring,
    relation_is_area,
    way_is_area,
)


def _way(*pairs):
    return [{"lat": lat, "lon": lon} for lon, lat in pairs]


class ClosureTests(unittest.TestCase):
    """Whether a way is closed is read from its coordinates, never invented."""

    def test_a_closed_ring_needs_four_positions_and_first_equal_last(self) -> None:
        self.assertTrue(is_closed_ring(_way((0, 0), (1, 0), (1, 1), (0, 0))))
        self.assertTrue(is_closed_ring(_way((0, 0), (1, 0), (1, 1), (0, 1), (0, 0))))

    def test_open_ways_are_not_closed_whatever_their_length(self) -> None:
        # An open wall, pier or platform: the closing edge does not exist.
        for coordinates in (
            _way((0, 0), (1, 0), (2, 1), (3, 1)),
            _way((0, 0), (1, 0), (1, 1)),
            _way((0, 0), (1, 0)),
            _way((0, 0)),
            [],
        ):
            with self.subTest(points=len(coordinates)):
                self.assertFalse(is_closed_ring(coordinates))

    def test_a_way_back_to_its_start_on_one_edge_is_not_a_ring(self) -> None:
        # A, B, A closes but encloses nothing.
        self.assertFalse(is_closed_ring(_way((0, 0), (1, 0), (0, 0))))

    def test_malformed_coordinates_are_not_closed(self) -> None:
        nan = float("nan")
        for coordinates in (
            None,
            "0,0 1,0 1,1 0,0",
            [None, {"lat": 0, "lon": 1}, {"lat": 1, "lon": 1}, None],
            [{"lat": nan, "lon": 0}, {"lat": 0, "lon": 1}, {"lat": 1, "lon": 1},
             {"lat": nan, "lon": 0}],
            [{"lon": 0}, {"lat": 0, "lon": 1}, {"lat": 1, "lon": 1}, {"lon": 0}],
        ):
            with self.subTest(coordinates=coordinates):
                self.assertFalse(is_closed_ring(coordinates))

    def test_closure_compares_positions_not_dictionaries(self) -> None:
        first = {"lat": 38.41, "lon": 27.12}
        last = {"lon": "27.12", "lat": "38.41", "extra": 1}
        coordinates = [first, {"lat": 38.41, "lon": 27.13}, {"lat": 38.42, "lon": 27.13}, last]
        self.assertTrue(is_closed_ring(coordinates))


class WayAreaTests(unittest.TestCase):
    def test_a_closed_path_is_not_a_plaza(self) -> None:
        # The bug this rule exists for: a footway loop came back as a polygon
        # because it happened to end where it started.
        for tags in (
            {"highway": "footway"},
            {"highway": "path"},
            {"highway": "residential"},
            {"highway": "pedestrian"},
            {"barrier": "fence"},
            {"waterway": "stream"},
            {"natural": "coastline"},
            {"natural": "tree_row"},
        ):
            with self.subTest(tags=tags):
                self.assertFalse(way_is_area(tags))

    def test_tags_that_do_enclose_an_area(self) -> None:
        for tags in (
            {"building": "yes"},
            {"landuse": "grass"},
            {"natural": "water"},
            {"leisure": "park"},
            {"amenity": "school"},
            {"boundary": "administrative"},
            {"barrier": "hedge"},
            {"waterway": "riverbank"},
            {"railway": "platform"},
        ):
            with self.subTest(tags=tags):
                self.assertTrue(way_is_area(tags))

    def test_an_explicit_area_tag_wins_both_ways(self) -> None:
        # This is exactly how a pedestrian square is mapped, and how a
        # building outline is marked as a line.
        self.assertTrue(way_is_area({"highway": "pedestrian", "area": "yes"}))
        self.assertTrue(way_is_area({"highway": "footway", "area": "yes"}))
        self.assertFalse(way_is_area({"building": "yes", "area": "no"}))

    def test_untagged_or_missing_tags_are_not_areas(self) -> None:
        self.assertFalse(way_is_area(None))
        self.assertFalse(way_is_area({}))
        self.assertFalse(way_is_area("not a mapping"))


class RelationAreaTests(unittest.TestCase):
    def test_multipolygon_and_boundary_relations_are_areas(self) -> None:
        self.assertTrue(relation_is_area({"type": "multipolygon"}))
        self.assertTrue(relation_is_area({"type": "boundary"}))

    def test_route_relations_stay_lines_however_they_close(self) -> None:
        # A circular bus route closes up, and is still a route.
        self.assertFalse(relation_is_area({"type": "route", "route": "bus"}))
        self.assertFalse(relation_is_area({"type": "route_master"}))

    def test_an_untyped_relation_falls_back_to_its_tags(self) -> None:
        self.assertTrue(relation_is_area({"building": "yes"}))
        self.assertFalse(relation_is_area({"highway": "footway"}))

    def test_an_untyped_relation_with_rings_is_an_area(self) -> None:
        # Postpass strips `type`; its multipolygons arrive as rings.  A
        # pedestrian square relation is an area there as on Overpass.
        rings = [
            {"type": "way", "role": "outer", "geometry": []},
            {"type": "way", "role": "inner", "geometry": []},
        ]
        route = [{"type": "way", "role": "", "geometry": []}]
        self.assertTrue(relation_is_area({"highway": "pedestrian"}, rings))
        self.assertFalse(relation_is_area({"highway": "pedestrian"}, route))
        self.assertFalse(relation_is_area({"route": "ferry"}, route))
        # A type the relation does carry still decides.
        self.assertFalse(relation_is_area({"type": "route", "route": "bus"}, rings))

    def test_ring_members_are_ways_in_the_outer_or_inner_role(self) -> None:
        self.assertTrue(has_ring_members([{"type": "way", "role": "outer"}]))
        self.assertTrue(has_ring_members([{"type": "way", "role": " Inner "}]))
        self.assertFalse(has_ring_members([{"type": "way", "role": ""}]))
        self.assertFalse(has_ring_members([{"type": "node", "role": "outer"}]))
        self.assertFalse(has_ring_members([{"type": "way", "role": "platform"}]))
        self.assertFalse(has_ring_members(None))
        self.assertFalse(has_ring_members("outer"))


if __name__ == "__main__":
    unittest.main()
