from __future__ import annotations

import unittest

from zero2agent_osm_downloader.core.catalog import (
    GROUPS,
    PRESETS,
    PRESETS_BY_ID,
    get_preset,
    interpret_prompt,
    presets_for_group,
)


class CatalogTests(unittest.TestCase):
    def test_catalog_ids_are_unique_and_every_group_is_populated(self) -> None:
        self.assertEqual(len(PRESETS), len(PRESETS_BY_ID))
        self.assertGreaterEqual(len(GROUPS), 14)
        self.assertGreaterEqual(len(PRESETS), 27)
        for group_id, _title in GROUPS:
            self.assertTrue(presets_for_group(group_id))

    def test_every_preset_has_safe_geometry_and_tags(self) -> None:
        for preset in PRESETS:
            self.assertIs(get_preset(preset.preset_id), preset)
            self.assertTrue(preset.tags)
            for tag in preset.tags:
                self.assertIn(tag.geometry, ("point", "line", "polygon"))
                self.assertTrue(tag.key)

    def test_offline_router_matches_turkish_and_english_presets(self) -> None:
        self.assertEqual(
            interpret_prompt("toplu taşıma duraklarını indir").preset_id,
            "public_transport_all",
        )
        self.assertEqual(
            interpret_prompt("green blue infrastructure").preset_id,
            "green_blue_all",
        )
        self.assertEqual(
            interpret_prompt("bina morfolojisi").preset_id,
            "buildings",
        )
        self.assertEqual(
            interpret_prompt(
                "Ekrandaki map extent'e göre tüm yolları, binaları ve ağaçları indir"
            ).preset_id,
            "urban_context",
        )

    def test_urban_context_is_one_compound_cross_geometry_preset(self) -> None:
        preset = get_preset("urban_context")
        self.assertEqual(
            {(tag.key, tag.value, tag.geometry) for tag in preset.tags},
            {
                ("highway", "", "line"),
                ("building", "", "polygon"),
                ("natural", "tree", "point"),
                ("natural", "tree_row", "line"),
            },
        )

    def test_explicit_tag_becomes_custom_intent(self) -> None:
        intent = interpret_prompt(
            "building=* verilerini poligon olarak indir"
        )
        self.assertEqual(intent.mode, "custom")
        self.assertEqual((intent.key, intent.value), ("building", "*"))
        self.assertEqual(intent.geometry, "polygon")

    def test_router_uses_phrase_boundaries_and_normalizes_punctuation(
        self,
    ) -> None:
        self.assertEqual(interpret_prompt("scarcity analysis").mode, "none")
        self.assertEqual(
            interpret_prompt("green-blue infrastructure").preset_id,
            "green_blue_all",
        )

    def test_router_extracts_named_places_with_or_without_a_dataset(self) -> None:
        parks = interpret_prompt("Download parks in Konak")
        self.assertEqual(parks.mode, "place")
        self.assertEqual(parks.preset_id, "green_spaces")
        self.assertEqual(parks.place_name, "Konak")
        self.assertEqual(
            interpret_prompt("Van için public transport").place_name,
            "Van",
        )
        self.assertEqual(interpret_prompt("london").place_name, "london")


if __name__ == "__main__":
    unittest.main()
