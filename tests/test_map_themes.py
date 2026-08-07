from __future__ import annotations

import unittest

from zero2agent_osm_downloader.core.map_themes import (
    DEFAULT_MAP_THEME,
    MAP_THEMES,
    map_theme,
    map_theme_items,
    map_theme_swatches,
    validate_map_themes,
)


class MapThemeCatalogTests(unittest.TestCase):
    def test_catalog_contains_quick3d_and_original_palettes(self) -> None:
        self.assertEqual(len(MAP_THEMES), 16)
        self.assertTrue(
            {
                "default", "cyber", "paper", "frost", "noir", "atlas",
                "mediterranean", "nightprint", "anime", "desert", "candy",
                "vapor", "aegean", "blueprint", "olive", "signal",
            } <= set(MAP_THEMES)
        )
        self.assertEqual(MAP_THEMES["cyber"]["water"], "#00ffcc")
        self.assertEqual(MAP_THEMES["vapor"]["roads_major"], "#ff71ce")

    def test_every_palette_is_complete_and_valid(self) -> None:
        self.assertEqual(validate_map_themes(), ())
        self.assertEqual(len(map_theme_items()), len(MAP_THEMES))
        for theme_id in MAP_THEMES:
            self.assertEqual(len(map_theme_swatches(theme_id)), 6)

    def test_unknown_theme_falls_back_to_civic_atlas(self) -> None:
        self.assertIs(map_theme("not-a-theme"), MAP_THEMES[DEFAULT_MAP_THEME])

    def test_original_palettes_are_visually_distinct(self) -> None:
        signatures = {
            tuple(map_theme_swatches(theme_id))
            for theme_id in ("aegean", "blueprint", "olive", "signal")
        }
        self.assertEqual(len(signatures), 4)


if __name__ == "__main__":
    unittest.main()
