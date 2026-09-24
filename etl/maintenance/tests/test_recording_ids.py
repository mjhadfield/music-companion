"""Sorting Last.fm song ids (recording vs track vs stale) -- the rules only, no database."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("MUSIC_DB_PATH", str(Path(tempfile.mkdtemp()) / "test.sqlite"))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))
from api.recordings import classify  # noqa: E402

REL, OTHER = "rel-1", "rel-2"
TRACKS = {REL: {"trk-a": "rec-a", "trk-b": "rec-b"}, OTHER: {"trk-c": "rec-c"}}
TITLES = {REL: {"rec-a": "Ausländer", "rec-b": "Sonne"}, OTHER: {"rec-c": "Stranded"}}


class ClassifyTests(unittest.TestCase):
    def test_track_id_on_edition(self):
        r = classify("trk-a", "x", [REL], TRACKS, TITLES, {})
        self.assertEqual((r["state"], r["recording"], r["kind"], r["release"]), ("convert", "rec-a", "track", REL))

    def test_recording_id_confirmed(self):
        self.assertEqual(classify("rec-b", "x", [REL], TRACKS, TITLES, {})["state"], "confirmed")

    def test_not_fetched_is_pending(self):
        self.assertEqual(classify("trk-z", "x", ["rel-unfetched"], TRACKS, TITLES, {})["state"], "pending")

    def test_track_from_another_edition_via_direct(self):
        r = classify("trk-q", "The Chant", [REL], TRACKS, TITLES, {"trk-q": {"recording": "rec-q", "kind": "track"}})
        self.assertEqual((r["state"], r["recording"], r["kind"]), ("convert", "rec-q", "track"))

    def test_merged_recording_via_direct(self):
        r = classify("rec-old", "x", [], TRACKS, TITLES, {"rec-old": {"recording": "rec-new", "kind": "recording"}})
        self.assertEqual((r["state"], r["recording"]), ("convert", "rec-new"))

    def test_stale_id_exact_title_on_edition(self):
        r = classify("stale", "AUSLÄNDER", [REL], TRACKS, TITLES, {})
        self.assertEqual((r["state"], r["recording"], r["kind"], r["matchedTitle"]), ("convert", "rec-a", "title", "Ausländer"))

    def test_stale_id_title_needs_every_edition_fetched(self):
        self.assertEqual(classify("stale", "AUSLÄNDER", [REL, "rel-unfetched"], TRACKS, TITLES, {})["state"], "pending")

    def test_stale_id_no_title_match_is_invalid_once_asked(self):
        self.assertEqual(classify("stale", "Mein Herz", [REL], TRACKS, TITLES, {})["state"], "pending")
        self.assertEqual(classify("stale", "Mein Herz", [REL], TRACKS, TITLES, {"stale": {"recording": None, "kind": None}})["state"], "invalid")

    def test_ambiguous_title_not_matched(self):
        titles = {REL: {"rec-a": "Intro", "rec-b": "Intro"}}
        self.assertEqual(classify("stale", "Intro", [REL], TRACKS, titles, {})["state"], "pending")


if __name__ == "__main__":
    unittest.main()
