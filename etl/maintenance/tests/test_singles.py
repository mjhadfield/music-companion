"""Singles filed as albums (singles.py) and folding them in (batch action foldSingle): what counts
as a single and what never may, and that a fold is exactly undoable. Throwaway database built from
schema.sql."""
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
import singles  # noqa: E402
from api import batch  # noqa: E402
from api.core import ApiError, Req  # noqa: E402
from common import DB_PATH, connect  # noqa: E402
from migrations import migrate  # noqa: E402
from tests.test_batch import TABLES, checksum, everything  # noqa: E402

ALBUMS = [  # id, title
    (10, "Fortitude"), (11, "The Chant"), (12, "Absolutely"), (13, "TEKKNO"), (14, "Pump It"),
    (15, "Evolve"), (16, "Ready To Fly (Hardcore Mix)"), (17, "Greatest Hits"), (18, "Amazonia"), (19, "Another World"),
    (20, "Hunter's Moon"), (21, "Lonely Day"),
]
SONGS = [  # id, album, title
    (100, 10, "Amazonia"), (101, 10, "The Chant"), (102, 10, "Into the Storm"), (103, 10, "Another World"), (104, 10, "Hold On"),
    (110, 13, "Pump It"), (111, 13, "Fuckboi"), (112, 13, "Mindreader"), (113, 14, "Pump It"),
    (120, 15, "Ready To Fly"), (121, 15, "Calling For A Sign"), (122, 16, "Ready To Fly (Hardcore Mix)"),
    (130, 12, "Absolutely Song"),
    (105, 10, "Lonely Day"), (140, 20, "Hunter’s Moon"), (141, 21, "Snowblind"), (142, 21, "Lonely Day"),
]
PLAYS = [  # song, album it was played from
    (101, 11), (100, 11), (102, 11),           # "The Chant" single: three pre-release tracks
    (101, 10), (100, 10),
    (113, 14), (113, 14), (110, 13),           # "Pump It" single with its own copy of the song
    (122, 16), (120, 15),                      # remix single: a different version
    (100, 17),                                 # Amazonia on a compilation
    (100, 18),                                 # "Amazonia" single -- but owned on vinyl
    (103, 19),                                 # "Another World" single
    (140, 20), (140, 20),                      # "Hunter's Moon" single: its song filed only under it, on the album's tracklist
    (142, 21), (105, 21), (141, 21),           # "Lonely Day" single: its own copy, the album's song, and a B-side
]


class SinglesTests(unittest.TestCase):
    def setUp(self):
        if DB_PATH.exists():
            DB_PATH.unlink()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        conn.execute("INSERT INTO artists (id, name) VALUES (1, 'Band')")
        conn.executemany("INSERT INTO albums (id, artist_id, title) VALUES (?, 1, ?)", ALBUMS)
        conn.executemany("INSERT INTO album_artists VALUES (?, 1, 0)", [(a,) for a, _ in ALBUMS])
        conn.executemany("INSERT INTO songs (id, artist_id, album_id, title) VALUES (?, 1, ?, ?)", SONGS)
        titles = {s: t for s, _, t in SONGS}
        conn.executemany("INSERT INTO scrobbles (song_id, artist_id, album_id, played_at, raw_artist_text, raw_track_text, raw_album_text) VALUES (?, 1, ?, ?, 'Band', ?, ?)",
                         [(s, a, f"2020-01-{i + 1:02d}", titles[s], dict(ALBUMS)[a]) for i, (s, a) in enumerate(PLAYS)])
        conn.execute("INSERT INTO vinyl_holdings (album_id, discogs_release_id, raw_title_text, raw_artist_text) VALUES (18, 1, 'Amazonia', 'Band')")
        conn.execute("INSERT INTO album_tracklists (album_id, position, number, title) VALUES (10, 6, '6', 'Hunters Moon')")
        conn.commit()
        conn.close()
        self.conn = connect()

    def tearDown(self):
        self.conn.close()

    def _found(self):
        return {x["singleId"]: x for x in singles.find_singles(self.conn)}

    def test_what_counts_as_a_single(self):
        f = self._found()
        self.assertEqual(f[11]["target"]["albumId"], 10)                   # The Chant -> Fortitude, all three tracks
        self.assertEqual(sorted(t["title"] for t in f[11]["tracks"]), ["Amazonia", "Into the Storm", "The Chant"])
        self.assertEqual(f[11]["tier"], "high")
        self.assertEqual(f[14]["target"]["albumId"], 13)                   # Pump It -> TEKKNO, with its own song copy
        self.assertEqual(f[14]["songsToMerge"], 1)
        self.assertEqual(f[19]["tier"], "high")
        self.assertEqual(f[18]["tier"], "review")                         # a single you own on vinyl: never pre-ticked
        self.assertNotIn(16, f)                                           # remix single: a different version of the song
        self.assertNotIn(12, f)                                           # title isn't one of its tracks
        self.assertNotIn(10, f)                                           # the album itself
        self.assertFalse(any(x["target"]["albumId"] == 17 for x in f.values()))   # never into a compilation

    def test_not_a_single_is_remembered(self):
        self.conn.execute("INSERT INTO duplicate_dismissals (entity_type, entity_id_a, entity_id_b) VALUES ('album', 10, 19)")
        self.assertNotIn(19, self._found())

    def test_fold_previews_applies_and_undoes_exactly(self):
        before_all = everything(self.conn)
        prev = batch.preview(Req({}, {"actions": [{"type": "foldSingle", "singleId": 14, "albumId": 13}, {"type": "foldSingle", "singleId": 11, "albumId": 10}]}))
        self.assertTrue(prev["ok"], prev)
        self.assertEqual(everything(self.conn), before_all)                # a dry run keeps nothing
        before = checksum(self.conn)
        out = batch.apply(Req({}, {"actions": [{"type": "foldSingle", "singleId": 14, "albumId": 13}, {"type": "foldSingle", "singleId": 11, "albumId": 10}]}))
        q = self.conn.execute
        self.assertIsNone(q("SELECT 1 FROM albums WHERE id IN (11, 14)").fetchone())
        self.assertIsNone(q("SELECT 1 FROM songs WHERE id = 113").fetchone())                    # the single's copy merged
        self.assertEqual(q("SELECT count(*) FROM scrobbles WHERE song_id = 110 AND album_id = 13").fetchone()[0], 3)
        self.assertEqual(q("SELECT count(*) FROM scrobbles WHERE album_id = 10").fetchone()[0], 5)
        # future imports of the single's title land on the album
        self.assertEqual(q("SELECT canonical_id FROM alias_overrides WHERE canonical_type = 'album' AND source_key = '1:the chant'").fetchone()[0], 10)
        batch.undo_batch(out["batchId"])
        self.assertEqual(checksum(self.conn), before)

    def test_a_stale_fold_is_refused(self):
        with self.assertRaises(ApiError):
            batch.apply(Req({}, {"actions": [{"type": "foldSingle", "singleId": 11, "albumId": 13}]}))      # wrong album
        with self.assertRaises(ApiError):
            batch.apply(Req({}, {"actions": [{"type": "foldSingle", "singleId": 12, "albumId": 10}]}))      # not a single

    def test_on_the_albums_tracklist_counts(self):
        x = self._found()[20]                                              # Hunter's Moon: no song filed on Fortitude, but it's listed
        self.assertEqual((x["kind"], x["target"]["albumId"], x["tier"]), ("fold", 10, "review"))
        self.assertEqual(x["tracks"][0]["via"], "tracklist")
        before = checksum(self.conn)
        out = batch.apply(Req({}, {"actions": [{"type": "foldSingle", "singleId": 20, "albumId": 10}]}))
        q = self.conn.execute
        self.assertEqual(q("SELECT album_id FROM songs WHERE id = 140").fetchone()[0], 10)       # its song moves to the album
        self.assertEqual(q("SELECT count(*) FROM scrobbles WHERE song_id = 140 AND album_id = 10").fetchone()[0], 2)
        batch.undo_batch(out["batchId"])
        self.assertEqual(checksum(self.conn), before)

    def test_a_single_with_b_sides_moves_only_the_albums_tracks(self):
        x = self._found()[21]
        self.assertEqual((x["kind"], x["target"]["albumId"], x["tier"]), ("tracks", 10, "review"))
        self.assertEqual({t["title"]: t["onTarget"] for t in x["tracks"]}, {"Lonely Day": True, "Snowblind": False})
        with self.assertRaises(ApiError):                                  # a fold would take the B-side too
            batch.apply(Req({}, {"actions": [{"type": "foldSingle", "singleId": 21, "albumId": 10}]}))
        before = checksum(self.conn)
        out = batch.apply(Req({}, {"actions": [{"type": "moveSingleTracks", "singleId": 21, "albumId": 10}]}))
        q = self.conn.execute
        self.assertIsNone(q("SELECT 1 FROM songs WHERE id = 142").fetchone())                    # its own copy merged into the album's
        self.assertEqual(q("SELECT count(*) FROM scrobbles WHERE song_id = 105 AND album_id = 10").fetchone()[0], 2)
        self.assertEqual(q("SELECT album_id FROM scrobbles WHERE song_id = 141").fetchone()[0], 21)  # the B-side stays
        self.assertEqual(q("SELECT album_id FROM songs WHERE id = 141").fetchone()[0], 21)
        self.assertIsNotNone(q("SELECT 1 FROM albums WHERE id = 21").fetchone())                 # still a single of its own
        self.assertNotIn(21, self._found())                                                      # and nothing left to move
        batch.undo_batch(out["batchId"])
        self.assertEqual(checksum(self.conn), before)



class MusicBrainzEvidenceTests(unittest.TestCase):
    """A single whose song is filed nowhere else, but MusicBrainz's recording search (cached) has it
    on the studio album -- a bonus track of one edition (Sabaton's In the Army Now on Carolus Rex)."""
    ARTIST_MBID = "39a31de6-0000-0000-0000-000000000001"

    def setUp(self):
        if DB_PATH.exists():
            DB_PATH.unlink()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        conn.execute("INSERT INTO artists (id, name, mbid) VALUES (1, 'Band', ?)", (self.ARTIST_MBID,))
        conn.executemany("INSERT INTO albums (id, artist_id, title, mbid) VALUES (?, 1, ?, ?)", [
            (30, "Carolus Rex", "rg-carolus"), (31, "In the Army Now", "rg-army"), (32, "Steel Commanders", "rg-steel"), (33, "Defence of Moscow", "rg-moscow")])
        conn.executemany("INSERT INTO album_artists VALUES (?, 1, 0)", [(30,), (31,), (32,), (33,)])
        conn.executemany("INSERT INTO songs (id, artist_id, album_id, title) VALUES (?, 1, ?, ?)", [
            (300, 30, "Carolus Rex"), (301, 30, "Gott Mit Uns"), (302, 30, "The Lion from the North"), (303, 30, "A Lifetime of War"),
            (304, 30, "1648"), (305, 30, "The Carolean's Prayer"), (306, 30, "Poltava"), (310, 31, "In the Army Now"),
            (320, 32, "Steel Commanders"), (321, 32, "Panzerkampf"), (322, 32, "Night Witches"), (330, 33, "Defence of Moscow")])
        conn.executemany("INSERT INTO scrobbles (song_id, artist_id, album_id, played_at, raw_artist_text, raw_track_text) VALUES (?, 1, ?, '2020-01-01', 'Band', 'x')",
                         [(310, 31), (310, 31), (330, 33), (300, 30)])
        def rgs(*groups):
            return json.dumps([{"mbid": m, "title": t, "primaryType": "Album", "secondaryTypes": sec, "firstDate": None} for m, t, sec in groups])
        conn.executemany("INSERT INTO mb_cache (key, payload_json, fetched_at) VALUES (?, ?, datetime('now'))", [
            (f"search:recording-rgs:{self.ARTIST_MBID}:in the army now", rgs(("rg-carolus", "Carolus Rex", []), ("rg-army", "In the Army Now", []))),
            (f"search:recording-rgs:{self.ARTIST_MBID}:defence of moscow", rgs(("rg-steel", "Steel Commanders", ["Compilation"]))),
            ("lookup:release-group:rg-steel", json.dumps({"mbid": "rg-steel", "title": "Steel Commanders", "primaryType": "Album", "secondaryTypes": ["Compilation"]})),
        ])
        conn.commit()
        conn.close()
        self.conn = connect()

    def tearDown(self):
        self.conn.close()

    def test_musicbrainz_puts_the_single_on_the_album(self):
        f = {x["singleId"]: x for x in singles.find_singles(self.conn)}
        self.assertEqual(f[31]["target"]["albumId"], 30)
        self.assertEqual(f[31]["kind"], "fold")
        self.assertEqual(f[31]["tier"], "review")                         # MusicBrainz-only evidence is never pre-ticked
        self.assertEqual(f[31]["tracks"][0]["via"], "musicbrainz")
        self.assertTrue(any("MusicBrainz lists it on Carolus Rex" in r for r in f[31]["reasons"]))
        self.assertNotIn(33, f)                                           # only on a compilation: stays a single

    def test_lookup_candidates_skip_what_is_cached(self):
        self.assertEqual([x["albumId"] for x in singles.lookup_candidates(self.conn)], [])   # both singles already asked
        self.conn.execute("DELETE FROM mb_cache WHERE key LIKE 'search:recording-rgs:%in the army now'")
        self.assertEqual([(x["albumId"], x["title"]) for x in singles.lookup_candidates(self.conn, [1])], [(31, "In the Army Now")])

    def test_fold_by_musicbrainz_applies_and_undoes(self):
        before = checksum(self.conn)
        out = batch.apply(Req({}, {"actions": [{"type": "foldSingle", "singleId": 31, "albumId": 30}]}))
        q = self.conn.execute
        self.assertIsNone(q("SELECT 1 FROM albums WHERE id = 31").fetchone())
        self.assertEqual(q("SELECT album_id FROM songs WHERE id = 310").fetchone()[0], 30)   # no twin: the song moves with it
        self.assertEqual(q("SELECT count(*) FROM scrobbles WHERE song_id = 310 AND album_id = 30").fetchone()[0], 2)
        batch.undo_batch(out["batchId"])
        self.assertEqual(checksum(self.conn), before)

if __name__ == "__main__":
    unittest.main()
