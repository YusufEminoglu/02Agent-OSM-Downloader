"""Checks on the renderer expressions that do not need QGIS.

The renderers themselves are built in `qgis_provider_smoke`, which compares
every category against `expression_values`. These tests guard the other half:
that the helper really reads an expression, and that the Places expressions
stay free of urban-morphology categories.
"""
from __future__ import annotations

import unittest

from zero2agent_osm_downloader.core.map_styling_expressions import (
    ADMIN_LEVEL_STYLES,
    BUILDING_EXPRESSION,
    PLACES_LINE_EXPRESSION,
    PLACES_POINT_EXPRESSION,
    PLACES_POLYGON_EXPRESSION,
    POINT_EXPRESSION,
    POLYGON_EXPRESSION,
    RENDERER_EXPRESSIONS,
    expression_values,
)

PLACES_EXPRESSIONS = {
    "places_polygon": PLACES_POLYGON_EXPRESSION,
    "places_line": PLACES_LINE_EXPRESSION,
    "places_point": PLACES_POINT_EXPRESSION,
}

# Categories that describe built form, land use or the road network. A Places
# layer holds administrative boundaries, so offering the reader any of these
# is the styling bug this plugin shipped.
MORPHOLOGY_VALUES = {
    "green", "water", "parking", "pedestrian", "civic",
    "major", "primary", "secondary", "tertiary", "residential", "service",
    "active", "rail", "transit",
    "tree", "transport", "health", "education", "worship", "emergency",
    "tourism", "sport",
}


class ExpressionValueTests(unittest.TestCase):
    def test_reads_then_and_else_literals_and_expands_admin_levels(self) -> None:
        expression = (
            "CASE"
            " WHEN \"boundary\" = 'administrative'"
            " THEN concat('admin_', coalesce(\"admin_level\",''))"
            " WHEN \"x\" <> '' THEN 'kept'"
            " ELSE 'fallback' END"
        )
        self.assertEqual(
            expression_values(expression),
            {"kept", "fallback"}
            | {f"admin_{level}" for level in ADMIN_LEVEL_STYLES},
        )

    def test_ignores_the_empty_sentinel_and_plain_comparisons(self) -> None:
        # BUILDING_EXPRESSION returns '' for a feature that is not a building,
        # and POLYGON_EXPRESSION tests that with `<> ''`. Neither is a value a
        # renderer could ever hold a category for.
        self.assertNotIn("", expression_values(BUILDING_EXPRESSION))
        self.assertNotIn("", expression_values(POLYGON_EXPRESSION))

    def test_an_unstyled_value_is_visible_to_the_check(self) -> None:
        # The whole point of the helper: a new branch shows up as a value the
        # renderer would have to grow a category for.
        grown = POINT_EXPRESSION.replace(
            " ELSE 'other' END", " WHEN \"aeroway\" <> '' THEN 'aeroway'"
            " ELSE 'other' END"
        )
        self.assertEqual(
            expression_values(grown) - expression_values(POINT_EXPRESSION),
            {"aeroway"},
        )


class PlacesExpressionTests(unittest.TestCase):
    def test_places_expressions_offer_no_morphology_categories(self) -> None:
        for name, expression in PLACES_EXPRESSIONS.items():
            with self.subTest(expression=name):
                values = expression_values(expression)
                self.assertEqual(values & MORPHOLOGY_VALUES, set())
                self.assertFalse(
                    {value for value in values if value.startswith("building_")}
                )

    def test_places_expressions_cover_every_admin_level(self) -> None:
        for name, expression in PLACES_EXPRESSIONS.items():
            with self.subTest(expression=name):
                self.assertTrue(
                    {f"admin_{level}" for level in ADMIN_LEVEL_STYLES}
                    <= expression_values(expression)
                )

    def test_place_centres_and_areas_are_categorised_not_dumped_in_other(
        self,
    ) -> None:
        self.assertIn("place", expression_values(PLACES_POINT_EXPRESSION))
        self.assertIn("place_area", expression_values(PLACES_POLYGON_EXPRESSION))

    def test_every_renderer_expression_has_a_fallback(self) -> None:
        # Without one, a feature matching no branch evaluates to NULL and the
        # categorised renderer draws nothing at all.
        for name, expression in RENDERER_EXPRESSIONS.items():
            with self.subTest(expression=name):
                self.assertIn("other", expression_values(expression))


class BuildingExpressionTests(unittest.TestCase):
    def test_an_unlisted_building_is_other_not_civic(self) -> None:
        # The catch-all used to be 'building_civic', which both mislabelled
        # every shed and garage as civic and left 'building_other' as a legend
        # entry nothing could ever match.
        values = expression_values(BUILDING_EXPRESSION)
        self.assertIn("building_other", values)
        self.assertIn("building_civic", values)
        civic_branch = BUILDING_EXPRESSION.split("THEN 'building_civic'")[0]
        self.assertIn("'school'", civic_branch)
        self.assertIn("'government'", civic_branch)


if __name__ == "__main__":
    unittest.main()
