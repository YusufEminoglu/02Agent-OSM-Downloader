from __future__ import annotations

import unittest

from zero2agent_osm_downloader.core.catalog import (
    COMMAND_EXAMPLES,
    GROUPS,
    PRESETS,
    PRESETS_BY_ID,
    group_context,
    get_preset,
    interpret_prompt,
    presets_for_group,
)
from zero2agent_osm_downloader.core.query import MAX_SELECTORS, normalized_specs


class CatalogTests(unittest.TestCase):
    def test_catalog_ids_are_unique_and_every_group_is_populated(self) -> None:
        self.assertEqual(len(PRESETS), len(PRESETS_BY_ID))
        # 43 through 1.5.x, plus Everyday amenities (15-minute city) in 1.6.
        self.assertEqual(len(PRESETS), 44)
        self.assertEqual(len(GROUPS), 16)
        for group_id, _title in GROUPS:
            self.assertTrue(presets_for_group(group_id))

    def test_everyday_amenities_covers_the_daily_needs_it_names(self) -> None:
        preset = get_preset("everyday_amenities")
        self.assertEqual(preset.title, "Everyday amenities (15-minute city)")
        self.assertEqual(preset.group_id, "urban_context")
        pairs = {(tag.key, tag.value) for tag in preset.tags}
        for need in (
            ("shop", "supermarket"), ("shop", "bakery"), ("amenity", "pharmacy"),
            ("amenity", "doctors"), ("amenity", "clinic"), ("amenity", "school"),
            ("amenity", "kindergarten"), ("leisure", "park"),
            ("leisure", "playground"), ("highway", "bus_stop"),
            ("amenity", "library"), ("amenity", "post_office"),
        ):
            with self.subTest(need=need):
                self.assertIn(need, pairs)
        # The same selectors the query builder accepts, within its limit.
        self.assertEqual(len(normalized_specs(preset.tags)), len(preset.tags))
        self.assertLessEqual(len(preset.tags), MAX_SELECTORS)
        self.assertEqual(
            interpret_prompt("15-minute city amenities in Bornova").preset_id,
            "everyday_amenities",
        )

    def test_groups_use_planning_first_order_then_alphabetical(self) -> None:
        self.assertEqual(
            [group_id for group_id, _title in GROUPS[:5]],
            ["urban_context", "morphology", "network", "places", "osm_as_is"],
        )
        remaining_titles = [title.casefold() for _group_id, title in GROUPS[5:]]
        self.assertEqual(remaining_titles, sorted(remaining_titles))
        for group_id, _title in GROUPS:
            if group_id == "urban_context":
                self.assertEqual(
                    presets_for_group(group_id)[0].preset_id,
                    "urban_context",
                )
                titles = [
                    preset.title.casefold()
                    for preset in presets_for_group(group_id)[1:]
                ]
                self.assertEqual(titles, sorted(titles))
                continue
            if group_id == "places":
                # An admin level is a number, so ordering these by title reads
                # 10, 2, 3 ... and looks broken in the list.
                continue
            titles = [
                preset.title.casefold()
                for preset in presets_for_group(group_id)
            ]
            self.assertEqual(titles, sorted(titles))

    def test_presets_follow_the_group_order_the_dock_shows(self) -> None:
        # The Processing PRESET enum is built from PRESETS, so it has to list
        # the groups in the same order the dock does.
        group_order = [group_id for group_id, _title in GROUPS]
        seen: list = []
        for preset in PRESETS:
            if preset.group_id not in seen:
                seen.append(preset.group_id)
        self.assertEqual(seen, group_order)

    def test_admin_levels_are_listed_by_level_not_as_text(self) -> None:
        places = presets_for_group("places")
        levels = [
            int(preset.preset_id.rsplit("_", 1)[1])
            for preset in places
            if preset.preset_id.startswith("admin_level_")
        ]
        self.assertEqual(levels, sorted(levels))
        self.assertEqual(levels[0], 2)
        self.assertEqual(levels[-1], 10)
        # The one entry that is not a boundary comes after them.
        self.assertEqual(places[-1].preset_id, "place_centres")

    def test_every_preset_has_safe_geometry_and_tags(self) -> None:
        for preset in PRESETS:
            self.assertIs(get_preset(preset.preset_id), preset)
            self.assertTrue(preset.tags)
            for tag in preset.tags:
                self.assertIn(tag.geometry, ("point", "line", "polygon"))
                self.assertTrue(tag.key)
                for qualifier_key, qualifier_value in tag.qualifiers:
                    self.assertTrue(qualifier_key)
                    self.assertTrue(qualifier_value)

    def test_osm_as_is_presets_are_scale_aware_and_bounded(self) -> None:
        as_is = presets_for_group("osm_as_is")
        self.assertEqual(
            {preset.preset_id for preset in as_is},
            {
                "osm_as_is_full_visible_stack",
                "osm_as_is_mobility",
                "osm_as_is_overview",
                "osm_as_is_urban_detail",
            },
        )
        self.assertEqual(
            {
                (tag.key, tag.value, tag.geometry)
                for tag in get_preset("osm_as_is_full_visible_stack").tags
            },
            {("*", "", "point"), ("*", "", "line"), ("*", "", "polygon")},
        )
        urban_tags = {
            (tag.key, tag.value, tag.geometry)
            for tag in get_preset("osm_as_is_urban_detail").tags
        }
        self.assertIn(("highway", "", "line"), urban_tags)
        self.assertIn(("building", "", "polygon"), urban_tags)
        self.assertIn(("amenity", "", "point"), urban_tags)
        # Sports pitches, stadiums and sports centres were never queried, so
        # a real download of this preset came back with no sport features at
        # all -- not a styling gap, a missing selector.
        self.assertIn(("leisure", "pitch", "polygon"), urban_tags)
        self.assertIn(("leisure", "stadium", "polygon"), urban_tags)
        self.assertIn(("leisure", "sports_centre", "polygon"), urban_tags)
        self.assertIn(("sport", "", "polygon"), urban_tags)

    def test_places_exposes_real_osm_admin_levels_and_place_centres(self) -> None:
        for level in range(2, 11):
            preset = get_preset(f"admin_level_{level}")
            self.assertIn(f"admin_level={level}", preset.title)
            self.assertIn("vary by country", preset.description)
            self.assertEqual(
                {
                    (tag.key, tag.value, tag.geometry, tag.qualifiers)
                    for tag in preset.tags
                },
                {
                    (
                        "boundary",
                        "administrative",
                        "point",
                        (("admin_level", str(level)),),
                    ),
                    (
                        "boundary",
                        "administrative",
                        "line",
                        (("admin_level", str(level)),),
                    ),
                    (
                        "boundary",
                        "administrative",
                        "polygon",
                        (("admin_level", str(level)),),
                    ),
                },
            )
        self.assertEqual(
            {
                (tag.key, tag.value, tag.geometry)
                for tag in get_preset("place_centres").tags
            },
            {("place", "", "point")},
        )

    def test_offline_router_matches_english_presets(self) -> None:
        self.assertEqual(
            interpret_prompt("download public transport stops").preset_id,
            "public_transport_all",
        )
        self.assertEqual(
            interpret_prompt("green blue infrastructure").preset_id,
            "green_blue_all",
        )
        self.assertEqual(
            interpret_prompt("building morphology").preset_id,
            "buildings",
        )
        self.assertEqual(
            interpret_prompt(
                "download all roads, buildings and trees for the map extent"
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

    def test_urban_context_includes_related_transit_and_amenity_datasets(self) -> None:
        urban_ids = {
            preset.preset_id
            for preset in presets_for_group("urban_context")
        }
        self.assertGreaterEqual(
            urban_ids,
            {"urban_context", "urban_transit", "urban_amenities"},
        )
        transit = get_preset("urban_transit")
        self.assertIn(("highway", "bus_stop", "point"), {
            (tag.key, tag.value, tag.geometry) for tag in transit.tags
        })
        self.assertIn(("route", "bus", "line"), {
            (tag.key, tag.value, tag.geometry) for tag in transit.tags
        })
        focus, related = group_context("urban_context")
        self.assertIn("transit", focus.casefold())
        self.assertIn("Public Transport", related)

    def test_explicit_tag_becomes_custom_intent(self) -> None:
        intent = interpret_prompt(
            "download building=* data as polygon"
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
        # Trees are trees: this used to pass as a "place" command that
        # quietly downloaded place centres in London.
        trees = interpret_prompt("Download trees of London")
        self.assertEqual(trees.mode, "custom")
        self.assertEqual((trees.key, trees.value, trees.geometry), ("natural", "tree", "point"))
        self.assertEqual(trees.place_name, "London")
        self.assertEqual(
            interpret_prompt("Download public transport in Van").place_name,
            "Van",
        )
        self.assertEqual(interpret_prompt("london").place_name, "london")

    def test_router_maps_global_road_and_building_command_to_context(self) -> None:
        intent = interpret_prompt("Download roads and buildings in Paris")
        self.assertEqual(intent.mode, "place")
        self.assertEqual(intent.preset_id, "urban_context")
        self.assertEqual(intent.place_name, "Paris")

    def test_typo_tolerant_fallback_matches_close_misspellings(self) -> None:
        intent = interpret_prompt("download buildngs in izmir")
        self.assertEqual(intent.mode, "place")
        self.assertEqual(intent.preset_id, "buildings")
        self.assertLessEqual(intent.confidence, 0.72)
        healthcare = interpret_prompt("download helthcare in ankara")
        self.assertEqual(healthcare.preset_id, "healthcare")

    def test_typo_fallback_does_not_fire_on_unrelated_text(self) -> None:
        self.assertEqual(interpret_prompt("scarcity analysis").mode, "none")
        self.assertEqual(interpret_prompt("xyzqwerty placeholder").mode, "none")

    def test_same_group_command_adds_a_sibling_preset(self) -> None:
        intent = interpret_prompt("download parks and water")
        self.assertEqual(intent.mode, "preset")
        self.assertIn(intent.preset_id, ("green_spaces", "blue_network"))
        other = (
            "blue_network" if intent.preset_id == "green_spaces"
            else "green_spaces"
        )
        self.assertEqual(intent.extra_preset_ids, (other,))

    def test_negation_extracts_an_exclude_tag_and_strips_the_clause(self) -> None:
        intent = interpret_prompt("download parking without charging")
        self.assertEqual(intent.mode, "preset")
        self.assertEqual(intent.preset_id, "parking")
        self.assertEqual(intent.exclude_key, "amenity")
        self.assertEqual(intent.exclude_value, "fuel")

    def test_negation_preserves_the_place_after_the_clause(self) -> None:
        intent = interpret_prompt(
            "download roads without service roads in Berlin"
        )
        self.assertEqual(intent.mode, "place")
        self.assertEqual(intent.place_name, "Berlin")

    def test_no_negation_means_no_exclude_tag(self) -> None:
        intent = interpret_prompt("Download parks in Konak")
        self.assertEqual(intent.exclude_key, "")
        self.assertEqual(intent.exclude_value, "")

    def test_cross_group_compound_command_keeps_second_preset(self) -> None:
        intent = interpret_prompt("download buildings and parks in Konak")
        self.assertEqual(intent.mode, "place")
        self.assertEqual(intent.place_name, "Konak")
        combined = {intent.preset_id, *intent.extra_preset_ids}
        self.assertEqual(combined, {"buildings", "green_spaces"})

    def test_custom_tag_command_extracts_place_and_exclusion(self) -> None:
        intent = interpret_prompt(
            "download building=apartments polygon without charging in Konak"
        )
        self.assertEqual(intent.mode, "custom")
        self.assertEqual((intent.key, intent.value), ("building", "apartments"))
        self.assertEqual(intent.geometry, "polygon")
        self.assertEqual(intent.place_name, "Konak")
        self.assertEqual((intent.exclude_key, intent.exclude_value), ("amenity", "fuel"))


def _route(intent) -> tuple:
    """(mode, every preset chosen, custom tag, extra tags, place) of an intent."""
    presets = frozenset(
        item for item in (intent.preset_id, *intent.extra_preset_ids) if item
    )
    tag = f"{intent.key}={intent.value}" if intent.mode == "custom" else ""
    extra = frozenset(f"{key}={value}" for key, value, _geometry in intent.extra_tags)
    return intent.mode, presets, tag, extra, intent.place_name


class CommandWordsTests(unittest.TestCase):
    """Commands download what their words name, or say they cannot."""

    #: Every example the Command tab ships, with the route its words ask for.
    SHIPPED = {
        "Download admin level 8 boundaries in Konak, Izmir, Turkey":
            ("place", {"admin_level_8"}, "", set(), "Konak, Izmir, Turkey"),
        "Download admin level 6 boundaries in Van, Turkey":
            ("place", {"admin_level_6"}, "", set(), "Van, Turkey"),
        "Download buildings in London, United Kingdom":
            ("place", {"buildings"}, "", set(), "London, United Kingdom"),
        "Download roads and buildings in Paris, France":
            ("place", {"urban_context", "buildings"}, "", set(), "Paris, France"),
        "Download public transport in Tokyo, Japan":
            ("place", {"public_transport_all", "bus_transit", "rail_transit"}, "",
             set(), "Tokyo, Japan"),
        "Download parks in Nairobi, Kenya":
            ("place", {"green_spaces"}, "", set(), "Nairobi, Kenya"),
        "Download healthcare in Toronto, Canada":
            ("place", {"healthcare"}, "", set(), "Toronto, Canada"),
        "Download schools in Melbourne, Australia":
            ("place", {"education"}, "", set(), "Melbourne, Australia"),
        "Download cycle network in Copenhagen, Denmark":
            ("place", {"cycle_network"}, "", set(), "Copenhagen, Denmark"),
        "Download green and blue infrastructure in Amsterdam, Netherlands":
            ("place", {"green_spaces", "blue_network"}, "", set(), "Amsterdam, Netherlands"),
        "Download buildings in New York City, United States":
            ("place", {"buildings"}, "", set(), "New York City, United States"),
        "Download place centres in São Paulo, Brazil":
            ("place", {"place_centres"}, "", set(), "São Paulo, Brazil"),
        "Download public transport in Singapore":
            ("place", {"public_transport_all", "bus_transit", "rail_transit"}, "",
             set(), "Singapore"),
        "Download roads in Cape Town, South Africa":
            ("place", {"road_network"}, "", set(), "Cape Town, South Africa"),
        "Download restaurants and cafes in Rome, Italy":
            ("custom", set(), "amenity=restaurant", {"amenity=cafe"}, "Rome, Italy"),
        "Download everyday amenities in Barcelona, Spain":
            ("place", {"everyday_amenities"}, "", set(), "Barcelona, Spain"),
        "building=* polygon": ("custom", set(), "building=*", set(), ""),
        "Download wheelchair=yes points": ("custom", set(), "wheelchair=yes", set(), ""),
        "Download parks and playgrounds in Seoul, South Korea":
            ("place", {"green_spaces"}, "", {"leisure=playground"}, "Seoul, South Korea"),
        "Konak'taki parkları indir": ("place", {"green_spaces"}, "", set(), "Konak"),
        "Kadıköy'de otobüs duraklarını göster":
            ("place", {"urban_transit"}, "", set(), "Kadıköy"),
        "Üsküdar'daki eczaneleri göster":
            ("custom", set(), "amenity=pharmacy", set(), "Üsküdar"),
    }

    def test_every_shipped_example_routes_as_its_words_say(self) -> None:
        self.assertEqual(len(COMMAND_EXAMPLES), len(set(COMMAND_EXAMPLES)))
        # A new example needs an expected route here before it can ship.
        self.assertEqual(set(COMMAND_EXAMPLES), set(self.SHIPPED))
        for text in COMMAND_EXAMPLES:
            mode, presets, tag, extra, place = self.SHIPPED[text]
            with self.subTest(example=text):
                self.assertEqual(
                    _route(interpret_prompt(text)),
                    (mode, frozenset(presets), tag, frozenset(extra), place),
                )

    def test_everyday_requests_download_what_they_name(self) -> None:
        # The A3 audit list: each of these used to become "Place centres".
        cases = {
            "download roads in Berlin": ("place", {"road_network"}, ""),
            "streets in Lyon": ("place", {"road_network"}, ""),
            "trees in Barcelona": ("custom", set(), "natural=tree"),
            "restaurants in Paris": ("custom", set(), "amenity=restaurant"),
            "cafes near Konak": ("custom", set(), "amenity=cafe"),
            "coffee shops in Seattle": ("custom", set(), "amenity=cafe"),
            "shops in London": ("custom", set(), "shop=*"),
            "supermarkets in Madrid": ("custom", set(), "shop=supermarket"),
            "banks in Rome": ("custom", set(), "amenity=bank"),
            "pharmacies in Ankara": ("custom", set(), "amenity=pharmacy"),
            "pharmacy in Ankara": ("custom", set(), "amenity=pharmacy"),
            "universities in Boston": ("custom", set(), "amenity=university"),
            "libraries in Dublin": ("custom", set(), "amenity=library"),
            "playgrounds in Vienna": ("custom", set(), "leisure=playground"),
            "beaches in Antalya": ("custom", set(), "natural=beach"),
            "Konak'ta restoranları indir": ("custom", set(), "amenity=restaurant"),
            "Bornova'daki kafeleri göster": ("custom", set(), "amenity=cafe"),
            "Buca'daki marketleri indir": ("custom", set(), "shop=supermarket"),
        }
        for text, (mode, presets, tag) in cases.items():
            with self.subTest(command=text):
                route = _route(interpret_prompt(text))
                self.assertEqual(route[:3], (mode, frozenset(presets), tag))
                self.assertTrue(route[4])

    def test_a_place_name_holding_a_dataset_word_does_not_choose_the_data(self) -> None:
        # "City" is a Place centres keyword and "Tree" starts "Treeton".
        intent = interpret_prompt("restaurants in Mexico City")
        self.assertEqual((intent.mode, intent.key, intent.value),
                         ("custom", "amenity", "restaurant"))
        self.assertEqual(intent.place_name, "Mexico City")
        self.assertEqual(interpret_prompt("roads in Treeton").preset_id, "road_network")

    def test_found_the_place_but_not_the_data(self) -> None:
        for text, place in (
            ("sidewalks in Seattle", "Seattle"),
            ("power lines in Oslo", "Oslo"),
            ("get everything in Konak", "Konak"),
            ("all data for Karsiyaka", "Karsiyaka"),
            ("Konak'ta yemek yerleri indir", "Konak"),
        ):
            with self.subTest(command=text):
                intent = interpret_prompt(text)
                self.assertEqual(intent.mode, "place_only")
                self.assertEqual(intent.place_name, place)
                self.assertEqual(intent.preset_id, "")

    def test_place_centres_only_for_a_place_on_its_own(self) -> None:
        for text in ("Bornova", "london", "download Bornova"):
            with self.subTest(command=text):
                intent = interpret_prompt(text)
                self.assertEqual((intent.mode, intent.preset_id), ("place", "place_centres"))
        # Over the audit's 50 commands, place centres never stands in for a
        # request that named something else.
        for text in (
            "download roads in Berlin", "trees in Barcelona", "restaurants in Paris",
            "sidewalks in Seattle", "farmland in Iowa", "get everything in Konak",
            "universities in Boston", "Konak'ta restoranları indir",
        ):
            with self.subTest(command=text):
                self.assertNotEqual(interpret_prompt(text).preset_id, "place_centres")

    def test_a_preset_and_a_named_tag_travel_together(self) -> None:
        intent = interpret_prompt("Download parks and playgrounds in Seoul")
        self.assertEqual(intent.preset_id, "green_spaces")
        self.assertEqual(intent.extra_tags, (("leisure", "playground", "polygon"),))
        # A tag the preset already downloads is not added twice.
        roads_trees = interpret_prompt("download roads and trees in Izmir")
        self.assertEqual(roads_trees.preset_id, "urban_context")
        self.assertEqual(roads_trees.extra_tags, ())
        self.assertNotIn("road_network", roads_trees.extra_preset_ids)
        # A phrase a preset owns is not read again as a named thing.
        amenities = interpret_prompt("street amenities in Paris")
        self.assertEqual(amenities.preset_id, "urban_amenities")
        self.assertEqual((amenities.extra_preset_ids, amenities.extra_tags), ((), ()))

    def test_named_things_can_be_excluded(self) -> None:
        intent = interpret_prompt("download parks without playgrounds in Seoul")
        self.assertEqual(intent.preset_id, "green_spaces")
        self.assertEqual((intent.exclude_key, intent.exclude_value), ("leisure", "playground"))
        self.assertEqual(intent.place_name, "Seoul")


if __name__ == "__main__":
    unittest.main()
