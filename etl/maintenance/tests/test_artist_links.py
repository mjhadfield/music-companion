"""Linked artists (merge.link_artists): Live sets offers a linked artist's recording for a performer's
live songs -- Ace Frehley's "2000 Man" (a Rolling Stones cover) -> Kiss's Dynasty recording; Myles
Kennedy's "World on Fire" (filed as his own, no album) -> Slash's. Throwaway database from schema.sql."""
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
import merge  # noqa: E402
from api import batch  # noqa: E402
from api.core import ApiError, Req  # noqa: E402
from api.songs import _apply_item, live_pairs  # noqa: E402
from common import DB_PATH, connect  # noqa: E402
from migrations import migrate  # noqa: E402
from tests.test_batch import checksum  # noqa: E402

ACE, KISS, STONES, MYLES, SLASH, OZZY, SABBATH = 1, 2, 3, 4, 5, 6, 7


class ArtistLinkTests(unittest.TestCase):
    def setUp(self):
        if DB_PATH.exists():
            DB_PATH.unlink()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        conn.executescript("""
            INSERT INTO artists (id, name) VALUES (1, 'Ace Frehley'), (2, 'Kiss'), (3, 'The Rolling Stones'), (4, 'Myles Kennedy'),
                (5, 'Slash'), (6, 'Ozzy Osbourne'), (7, 'Black Sabbath');
            INSERT INTO albums (id, artist_id, title) VALUES (20, 2, 'Dynasty'), (50, 5, 'World on Fire'), (51, 5, 'Apocalyptic Love'),
                (70, 7, 'Paranoid'), (10, 1, 'Ace Frehley');
            INSERT INTO album_artists VALUES (20, 2, 0), (50, 5, 0), (51, 5, 0), (70, 7, 0), (10, 1, 0);
            INSERT INTO songs (id, artist_id, album_id, title) VALUES
                (200, 2, 20, '2000 Man'), (201, 2, 20, 'I Was Made for Lovin'' You'),
                (300, 3, NULL, '2000 Man'),
                (400, 4, NULL, 'World on Fire'), (401, 4, NULL, 'Halo'),
                (500, 5, 50, 'World on Fire'), (510, 5, 51, 'Anastasia'),
                (700, 7, 70, 'Paranoid'),
                (100, 1, 10, 'Rip It Out');
            INSERT INTO venues (id, name) VALUES (1, 'Somewhere');
            INSERT INTO setlists (id, setlistfm_id, artist_id, venue_id, event_date, raw_artist_text) VALUES
                (1, 'a1', 1, 1, '2019-01-01', 'Ace Frehley'), (2, 'm1', 4, 1, '2019-02-01', 'Myles Kennedy'), (3, 'o1', 6, 1, '2019-03-01', 'Ozzy Osbourne');
            INSERT INTO setlist_songs (id, setlist_id, song_id, position, raw_song_text, is_cover, cover_of_artist_text) VALUES
                (1, 1, 300, 1, '2000 Man', 1, 'The Rolling Stones'), (2, 1, 100, 2, 'Rip It Out', 0, NULL),
                (3, 2, 400, 1, 'World on Fire', 0, NULL), (4, 2, 401, 2, 'Halo', 0, NULL),
                (5, 3, 700, 1, 'Paranoid', 1, 'Black Sabbath');
        """)
        conn.commit()
        conn.close()
        self.conn = connect()

    def tearDown(self):
        self.conn.close()

    def _status(self, perf, song):
        _rows, _prof, statuses = live_pairs(self.conn, [perf])
        return statuses[(perf, song)]

    def test_without_a_link_nothing_crosses_over(self):
        self.assertNotEqual(self._status(ACE, 300)["status"], "match")
        self.assertNotEqual(self._status(MYLES, 400)["status"], "match")

    def test_a_linked_bands_recording_is_offered_for_a_cover(self):
        merge.link_artists(self.conn, ACE, KISS)
        st = self._status(ACE, 300)
        self.assertEqual(st["status"], "match")
        self.assertEqual((st["target"]["songId"], st["target"]["relink"], st["target"]["performerName"]), (200, True, "Kiss"))
        _apply_item(self.conn, {"type": "relink", "songId": 300, "toSongId": 200, "performerId": ACE})
        self.assertEqual(self.conn.execute("SELECT song_id FROM setlist_songs WHERE id = 1").fetchone()[0], 200)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM scrobbles").fetchone()[0], 0)   # nothing else touched

    def test_a_linked_band_not_yet_looked_up_reopens_the_lookup(self):
        import json, mbcache
        self.conn.execute("UPDATE artists SET mbid = 'aaaaaaaa-0000-0000-0000-000000000003' WHERE id = 3")
        self.conn.execute("UPDATE artists SET mbid = 'aaaaaaaa-0000-0000-0000-000000000002' WHERE id = 2")
        self.conn.execute("DELETE FROM songs WHERE id = 200")                         # Kiss's 2000 Man isn't in the library
        self.conn.execute("INSERT INTO mb_cache (key, payload_json) VALUES (?, ?)",
                          (mbcache.recording_release_groups_key("2000 Man", "aaaaaaaa-0000-0000-0000-000000000003"),
                           json.dumps([{"mbid": "bbbbbbbb-0000-0000-0000-000000000001", "title": "Their Satanic Majesties Request", "primaryType": "Album", "firstDate": "1967"}])))
        self.assertEqual(self._status(ACE, 300)["status"], "newalbum")                # only the Stones' album so far
        merge.link_artists(self.conn, ACE, KISS)
        st = self._status(ACE, 300)
        self.assertEqual((st["status"], st.get("lookable")), ("unchecked", True))      # ask about Kiss's recording first

    def test_a_linked_bands_album_tracklist_finds_an_unplayed_recording(self):
        self.conn.execute("DELETE FROM songs WHERE id = 200")                         # never played Kiss's 2000 Man...
        self.conn.execute("INSERT INTO album_tracklists (album_id, position, number, title) VALUES (20, 1, 'A1', 'I Was Made for Lovin'' You'), (20, 2, 'A2', '2,000 Man')")   # as MusicBrainz spells it
        merge.link_artists(self.conn, ACE, KISS)
        st = self._status(ACE, 300)                                                    # ...but it's on Dynasty's tracklist
        self.assertEqual((st["status"], st["mbAlbum"]["albumId"], st["mbAlbum"]["relinkNew"], st["mbAlbum"]["performerName"]), ("album", 20, True, "Kiss"))
        _apply_item(self.conn, {"type": "relinkNew", "songId": 300, "performerId": ACE, "albumId": 20, "title": "2000 Man"})
        self.assertEqual(self.conn.execute("SELECT s.artist_id, s.album_id FROM setlist_songs ss JOIN songs s ON s.id = ss.song_id WHERE ss.id = 1").fetchone(), (KISS, 20))

    def test_an_own_titled_song_with_no_album_finds_the_linked_bands(self):
        merge.link_artists(self.conn, SLASH, MYLES)                                   # either way round
        st = self._status(MYLES, 400)
        self.assertEqual((st["status"], st["target"]["songId"], st["target"]["relink"]), ("match", 500, True))

    def test_a_linked_bands_own_song_already_filed_there_stays_put(self):
        merge.link_artists(self.conn, OZZY, SABBATH)
        self.assertEqual(self._status(OZZY, 700)["status"], "ok")                    # Paranoid is already Sabbath's recording

    def test_relink_new_may_add_the_song_to_a_linked_artists_album(self):
        with self.assertRaises(ApiError):
            _apply_item(self.conn, {"type": "relinkNew", "songId": 401, "performerId": MYLES, "albumId": 51, "title": "Halo"})
        merge.link_artists(self.conn, MYLES, SLASH)
        _apply_item(self.conn, {"type": "relinkNew", "songId": 401, "performerId": MYLES, "albumId": 51, "title": "Halo"})
        new = self.conn.execute("SELECT artist_id, album_id FROM songs WHERE title = 'Halo' AND id != 401").fetchone()
        self.assertEqual(new, (SLASH, 51))                                           # Slash's recording, on Slash's album

    def test_links_are_undoable_and_guarded(self):
        before = checksum(self.conn, ["artist_links"])
        out = batch.apply(Req({}, {"actions": [{"type": "linkArtists", "artistId": ACE, "linkedArtistId": KISS}]}))
        self.assertEqual(merge.linked_artists(self.conn, KISS), [ACE])
        with self.assertRaises(merge.MergeError):
            merge.link_artists(self.conn, KISS, ACE)                                  # already linked, either way round
        with self.assertRaises(merge.MergeError):
            merge.link_artists(self.conn, ACE, ACE)
        batch.undo_batch(out["batchId"])
        self.assertEqual(checksum(self.conn, ["artist_links"]), before)

    def test_an_artist_merge_carries_its_links_and_undoes(self):
        merge.link_artists(self.conn, ACE, KISS)
        r = merge.merge_artists(self.conn, ACE, MYLES)                               # (contrived) -- links follow the survivor
        self.assertIn(KISS, merge.linked_artists(self.conn, MYLES))
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        merge.undo_merge(self.conn, r["logId"])
        self.assertEqual(merge.linked_artists(self.conn, ACE), [KISS])


if __name__ == "__main__":
    unittest.main()
