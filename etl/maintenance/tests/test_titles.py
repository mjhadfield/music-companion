import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from titles import base_key, normalize_artist_name, sequel_marker, split_title, variant_tags  # noqa: E402


class SplitTitleTests(unittest.TestCase):
    def test_remaster_suffixes_fold_together(self):
        for t in ["Into the Void - 2009 Remaster", "Into the Void (Remastered)", "Into The Void [2009 Remastered Version]",
                  "Into the Void - Remastered 2012", "Into the Void"]:
            self.assertEqual(base_key(t), "into the void", t)
            self.assertEqual(variant_tags(t), set(), t)

    def test_live_is_a_variant_not_an_edition(self):
        base, tags = split_title("Capricorn - Live in England 1981")
        self.assertEqual(base, "Capricorn")
        self.assertIn("live", tags)
        self.assertIn("live", variant_tags("The Heaviest Matter of the Universe - Live at Brixton Academy, London, UK 3/29/2013"))

    def test_edits_and_remixes(self):
        self.assertEqual(variant_tags("Million Voices - Radio Edit"), {"edit"})
        self.assertIn("remix", variant_tags("Tell Me Why - MEDUZA Remix"))

    def test_single_version_is_edition(self):
        self.assertEqual(variant_tags("Paranoid - Single Version"), set())
        self.assertEqual(base_key("Paranoid - Single Version"), "paranoid")

    def test_unknown_suffix_is_kept(self):
        self.assertEqual(base_key("Song - Part Two"), "song part two")
        self.assertEqual(base_key("War Pigs / Luke's Wall"), "war pigs luke s wall")

    def test_album_editions(self):
        self.assertEqual(base_key("Heaven in Hiding (Deluxe Edition)"), base_key("Heaven in Hiding"))
        self.assertEqual(base_key("Master of Reality (2009 Remastered Version)"), "master of reality")
        self.assertEqual(base_key("Rumours (Super Deluxe)"), "rumours")

    def test_sequels_differ(self):
        self.assertEqual(sequel_marker("Van Halen II (Remastered)"), "2")
        self.assertIsNone(sequel_marker("Van Halen (Remastered)"))
        self.assertEqual(sequel_marker("Vol. 4"), "4")
        self.assertEqual(sequel_marker("Led Zeppelin IV"), "4")
        self.assertIsNone(sequel_marker("I"))


class ArtistNameTests(unittest.TestCase):
    def test_the_and_ampersand(self):
        self.assertEqual(normalize_artist_name("The Beatles"), normalize_artist_name("Beatles, The"))
        self.assertEqual(normalize_artist_name("Toots & The Maytals"), normalize_artist_name("Toots and the Maytals"))
        self.assertEqual(normalize_artist_name("Motörhead"), "motorhead")


if __name__ == "__main__":
    unittest.main()
