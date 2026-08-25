"""A closed way is an area only when its tags say so."""
from __future__ import annotations

import unittest

from zero2agent_osm_downloader.core.osm_areas import (
    relation_is_area,
    way_is_area,
)


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


if __name__ == "__main__":
    unittest.main()
