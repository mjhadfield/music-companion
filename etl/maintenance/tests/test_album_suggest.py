"""The album-suggest sweep (suggest.py): tiers from cached-style fixtures, and the sweep itself
against a stubbed MusicBrainz -- it may only ever write suggestions, never touch an album.
Throwaway database built from schema.sql."""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("MUSIC_DB_PATH", str(Path(tempfile.mkdtemp()) / "test.sqlite"))  # before common is imported

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))
import mbcache  # noqa: E402
import suggest  # noqa: E402
from common import DB_PATH, connect  # noqa: E402
from migrations import migrate  # noqa: E402

RG = "eeeeeeee-0000-0000-0000-0000000000{:02d}"
HEROES_TRACKS = ["Night Witches", "No Bullets Fly", "Smoking Snakes", "Inmate 4859", "To Hell and Back", "The Ballad of Bull"]


def rg(n, title, primary="Album", secondary=(), date="2014"):
    return {"mbid": RG.format(n), "title": title, "primaryType": primary, "secondaryTypes": list(secondary), "firstReleaseDate": date}


class ScoringTests(unittest.TestCase):
    def test_exact_title_backed_by_tracks_is_high(self):
        out = suggest.score_album_candidates("Heroes (Deluxe Edition)", ["night witches", "to hell and back"], [rg(1, "Heroes")], {RG.format(1): HEROES_TRACKS})
        self.assertEqual(out[0]["tier"], "high")
        self.assertEqual(out[0]["evidence"]["trackOverlap"], 100)

    def test_two_strong_candidates_means_neither_is_high(self):
        # the album and its title-track single both carry the one song you've played
        out = suggest.score_album_candidates("Heroes", ["heroes"], [rg(1, "Heroes"), rg(2, "Heroes", "Single")],
                                             {RG.format(1): ["Heroes", "Night Witches"], RG.format(2): ["Heroes"]})
        self.assertEqual({c["tier"] for c in out}, {"medium"})

    def test_exact_title_without_tracks_is_medium(self):
        out = suggest.score_album_candidates("1916", [], [rg(3, "1916")], {})
        self.assertEqual(out[0]["tier"], "medium")

    def test_live_vs_studio_is_low(self):
        out = suggest.score_album_candidates("Swedish Empire (Live)", ["night witches"], [rg(4, "Swedish Empire")], {RG.format(4): ["Night Witches"]})
        self.assertEqual(out[0]["tier"], "low")
        self.assertIn("live", out[0]["evidence"]["typeConflict"])
        out = suggest.score_album_candidates("Swedish Empire (Live)", ["night witches"], [rg(5, "Swedish Empire Live", secondary=["Live"])], {RG.format(5): ["Night Witches"]})
        self.assertNotEqual(out[0]["tier"], "low")

    def test_remix_vs_original_is_low(self):
        out = suggest.score_album_candidates("Everytime We Touch (Hardwell Remix)", ["everytime we touch"], [rg(8, "Everytime We Touch")], {RG.format(8): ["Everytime We Touch"]})
        self.assertEqual(out[0]["tier"], "low")
        out = suggest.score_album_candidates("All Around the World", ["all around the world"], [rg(9, "All Around the World (Some Remix)", "Single", ["Remix"])], {RG.format(9): ["All Around the World"]})
        self.assertTrue(all(c["tier"] == "low" for c in out))

    def test_near_title_with_none_of_your_songs_is_dropped(self):
        out = suggest.score_album_candidates("Heroes", ["night witches"], [rg(6, "Heroez")], {RG.format(6): ["Something Else"]})
        self.assertEqual(out, [])


class SweepTests(unittest.TestCase):
    def setUp(self):
        if DB_PATH.exists():
            DB_PATH.unlink()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        conn.executescript(f"""
            INSERT INTO artists (id, mbid, name) VALUES (1, 'aaaaaaaa-0000-0000-0000-000000000001', 'Sabaton'), (2, NULL, 'Nobody');
            INSERT INTO albums (id, mbid, artist_id, title) VALUES
                (10, NULL, 1, 'Heroes (Deluxe Edition)'), (11, NULL, 1, 'Bootleg Live 2003'), (12, NULL, 1, 'Carolus Rex'),
                (13, '{RG.format(9)}', 1, 'The Art of War'), (20, NULL, 2, 'Unknown');
            INSERT INTO album_artists VALUES (10, 1, 0), (11, 1, 0), (12, 1, 0), (13, 1, 0), (20, 2, 0);
            INSERT INTO songs (id, artist_id, album_id, title) VALUES (100, 1, 10, 'Night Witches'), (101, 1, 10, 'To Hell and Back'), (102, 1, 12, 'Carolus Rex');
            INSERT INTO scrobbles (song_id, artist_id, album_id, played_at, raw_artist_text, raw_track_text) VALUES
                (100, 1, 10, '2020-01-01', 'Sabaton', 'Night Witches'), (101, 1, 10, '2020-01-02', 'Sabaton', 'To Hell and Back'),
                (102, 1, 12, '2020-01-03', 'Sabaton', 'Carolus Rex');
            INSERT INTO review_marks (entity_type, entity_id, mark) VALUES ('album', 11, 'no-mbid');
            INSERT INTO suggestions (entity_type, entity_id, mbid, label, confidence, tier, source, status) VALUES ('album', 12, '{RG.format(3)}', 'Carolus Rex', 90, 'high', 'album-suggest', 'rejected');
        """)
        conn.commit()
        conn.close()
        self.real = (mbcache.artist_release_groups, mbcache.release_group_tracklist)
        mbcache.artist_release_groups = lambda mbid: [rg(1, "Heroes"), rg(2, "Heroes", "Single"), rg(3, "Carolus Rex"), rg(9, "The Art of War"), rg(7, "Bootleg Live 2003")]
        mbcache.release_group_tracklist = lambda mbid: {"tracks": [{"title": t} for t in (HEROES_TRACKS if mbid == RG.format(1) else ["Heroes"])]}

    def tearDown(self):
        mbcache.artist_release_groups, mbcache.release_group_tracklist = self.real

    def test_sweep_writes_suggestions_only(self):
        conn = connect()
        sums = {t: hashlib.sha1(repr(conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()).encode()).hexdigest() for t in ("albums", "songs", "scrobbles", "album_artists")}
        conn.close()
        lines = []
        suggest._album_suggest(50, lines.append, lambda *_: None, lambda: False)
        conn = connect()
        try:
            for t, h in sums.items():
                self.assertEqual(hashlib.sha1(repr(conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()).encode()).hexdigest(), h, t)
            rows = {(r[0], r[1]): (r[2], json.loads(r[3])) for r in conn.execute(
                "SELECT entity_id, mbid, tier, evidence_json FROM suggestions WHERE status = 'pending' AND source = 'album-suggest'")}
        finally:
            conn.close()
        self.assertEqual(rows[(10, RG.format(1))][0], "high")                 # the album, both played songs on it
        self.assertEqual(rows[(10, RG.format(1))][1]["trackOverlap"], 100)
        self.assertNotEqual(rows.get((10, RG.format(2)), ("low",))[0], "high")   # the single never outranks it
        self.assertFalse(any(k[0] == 11 for k in rows))                       # marked "not on MusicBrainz"
        self.assertNotIn((12, RG.format(3)), rows)                            # rejected before: never again
        self.assertFalse(any(k[0] == 20 for k in rows))                       # artist has no MusicBrainz id
        self.assertFalse(any(k[0] == 13 for k in rows))                       # already identified


if __name__ == "__main__":
    unittest.main()
