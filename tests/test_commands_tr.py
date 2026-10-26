from __future__ import annotations

import unittest

from zero2agent_osm_downloader.core.catalog import interpret_prompt
from zero2agent_osm_downloader.core.command_tr import translate


class TurkishCommandTests(unittest.TestCase):
    """A Turkish command must reach exactly what its English twin reaches."""

    PAIRS = (
        ("Konak'taki parkları indir", "download parks in Konak"),
        ("İzmir'de okulları getir", "download schools in İzmir"),
        ("Kadıköy'de otobüs duraklarını göster", "download bus stops in Kadıköy"),
        ("Bornova içinde binalar", "download buildings in Bornova"),
        ("Parkları Konak'ta indir", "download parks in Konak"),
        ("Çankaya ilçesinde hastaneler", "download hospitals in Çankaya"),
        ("otoparkları indir", "download parking"),
        ("yeşil alanları indir", "download green space"),
        ("Konak, İzmir'deki camileri indir", "download mosque in Konak, İzmir"),
        ("tren istasyonları Ankara'da", "download rail transit in Ankara"),
        ("Konak'ta yollar ve binalar", "download roads and buildings in Konak"),
        ("mahalle sınırları İzmir'de", "download admin level 8 in İzmir"),
        ("Beşiktaş'ta bisiklet yollarını indir", "download cycle network in Beşiktaş"),
        ("Üsküdar'daki eczaneleri getir", "download pharmacy in Üsküdar"),
        ("Muğla'da müzeleri göster", "download museums in Muğla"),
        ("İstanbul'un metro istasyonları", "download rail transit in İstanbul"),
        ("ağaçları indir", "download trees"),
        ("Bodrum'da spor tesisleri", "download sport in Bodrum"),
        ("Bayraklı'daki kiliseler", "download church in Bayraklı"),
        ("Karşıyaka'da arazi kullanımı", "download land use in Karşıyaka"),
    )

    def test_turkish_and_english_route_to_the_same_request(self) -> None:
        for turkish, english in self.PAIRS:
            with self.subTest(turkish=turkish):
                tr = interpret_prompt(turkish)
                en = interpret_prompt(english)
                self.assertEqual(tr.mode, en.mode)
                self.assertEqual(tr.preset_id, en.preset_id)
                self.assertEqual(tr.place_name, en.place_name)

    def test_the_place_keeps_its_own_spelling(self) -> None:
        # Nominatim matches "Kadıköy", not the ASCII fold "kadikoy".
        self.assertEqual(interpret_prompt("Kadıköy'de parklar").place_name, "Kadıköy")
        self.assertEqual(interpret_prompt("Beyoğlu'nda okullar").place_name, "Beyoğlu")

    def test_negation_becomes_an_exclusion(self) -> None:
        intent = interpret_prompt("Konak'taki binalar, otoparklar hariç")
        self.assertEqual(intent.preset_id, "buildings")
        self.assertEqual(intent.place_name, "Konak")
        self.assertEqual((intent.exclude_key, intent.exclude_value), ("amenity", "parking"))

    def test_a_tag_with_a_turkish_place(self) -> None:
        intent = interpret_prompt("amenity=cafe Beyoğlu'nda")
        self.assertEqual((intent.mode, intent.key, intent.value), ("custom", "amenity", "cafe"))
        self.assertEqual(intent.place_name, "Beyoğlu")

    def test_english_commands_are_never_rewritten(self) -> None:
        for english in (
            "download parks in London",
            "parks in Kadıköy",
            "bus stops near Beşiktaş",
            "get buildings",
            "download amenity=cafe in Beyoğlu",
        ):
            with self.subTest(english=english):
                self.assertIsNone(translate(english))

    def test_a_turkish_verb_is_not_mistaken_for_an_english_one(self) -> None:
        # "getir" starts with "get"; it is still Turkish.
        self.assertEqual(translate("getir okulları"), "download school")


if __name__ == "__main__":
    unittest.main()
