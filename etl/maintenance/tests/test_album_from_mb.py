"""Songs heard live but never scrobbled: adding the MusicBrainz album they're on and linking them
(api.songs._album_new) -- and every such change undoing back to exactly where it started."""
import unittest

import test_merge_undo as base  # first: sets the test DB path and import paths

import merge  # noqa: E402
from api.core import ApiError  # noqa: E402
from api.songs import _album_new  # noqa: E402

SABBATH = "aaaaaaaa-0000-0000-0000-000000000001"
OZZY = "aaaaaaaa-0000-0000-0000-000000000003"


def rg(mbid, title, artists, date="1975-07-28"):
    return {"mbid": mbid, "title": title, "artistMbids": artists, "artistCredit": "someone", "firstReleaseDate": date,
            "primaryType": "Album", "secondaryTypes": []}


class AlbumFromMusicBrainzTests(unittest.TestCase):
    setUp = base.MergeUndoTests.setUp
    tearDown = base.MergeUndoTests.tearDown

    def _undo_all(self, handles):
        for h in reversed(handles):  # a batch undo runs newest first
            merge.undo_edit(self.conn, h["id"])
        self.conn.commit()

    def test_song_linked_to_new_album_and_undone(self):
        self.conn.execute("UPDATE songs SET album_id = NULL WHERE id = 100")
        self.conn.commit()
        before = base.checksum(self.conn)
        h = _album_new(self.conn, {"_rg": rg("bbbbbbbb-0000-0000-0000-00000000000f", "Sabotage", [SABBATH]), "songId": 100,
                                   "ownerId": 1, "recordingMbid": "cccccccc-0000-0000-0000-00000000000f"})
        self.assertEqual(len(h), 2)  # the album, then the song's link
        album_id, year, mbid = self.conn.execute("SELECT id, year, mbid FROM albums WHERE title = 'Sabotage'").fetchone()
        self.assertEqual((year, mbid), (1975, "bbbbbbbb-0000-0000-0000-00000000000f"))
        self.assertEqual(self.conn.execute("SELECT album_id, mbid FROM songs WHERE id = 100").fetchone(),
                         (album_id, "cccccccc-0000-0000-0000-00000000000f"))
        self.assertEqual(self.conn.execute("SELECT artist_id FROM album_artists WHERE album_id = ?", (album_id,)).fetchall(), [(1,)])
        self.conn.commit()
        self._undo_all(h)
        self.assertEqual(base.checksum(self.conn), before)

    def test_credited_to_someone_else_is_refused(self):
        with self.assertRaises(ApiError) as cm:
            _album_new(self.conn, {"_rg": rg("bbbbbbbb-0000-0000-0000-00000000001f", "Under the Blade", ["tttttttt-0000"]),
                                   "songId": 100, "ownerId": 1})
        self.assertIn("isn't Black Sabbath's album", str(cm.exception))

    def test_second_song_reuses_the_album(self):
        g = rg("bbbbbbbb-0000-0000-0000-00000000000f", "Sabotage", [SABBATH])
        self.conn.execute("INSERT INTO songs (id, artist_id, title) VALUES (103, 1, 'Symptom of the Universe'), (104, 1, 'Hole in the Sky')")
        h1 = _album_new(self.conn, {"_rg": g, "songId": 103, "ownerId": 1})
        h2 = _album_new(self.conn, {"_rg": g, "songId": 104, "ownerId": 1})
        self.assertEqual((len(h1), len(h2)), (2, 1))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM albums WHERE title = 'Sabotage'").fetchone()[0], 1)

    def test_cover_gets_the_bands_own_recording(self):
        """Ozzy (performer 3) played Sabbath's song 101 live: his own recording goes on his album;
        only his live plays move to it, Sabbath's song and shows are untouched."""
        self.conn.execute("UPDATE artists SET mbid = ? WHERE id = 3", (OZZY,))
        self.conn.execute("UPDATE setlists SET artist_id = 3 WHERE id = 1")
        self.conn.commit()
        before = base.checksum(self.conn)
        h = _album_new(self.conn, {"_rg": rg("bbbbbbbb-0000-0000-0000-00000000002f", "Live & Loud", [OZZY], "1993"),
                                   "songId": 101, "ownerId": 3, "performerId": 3, "title": "Paranoid"})
        album_id = self.conn.execute("SELECT id FROM albums WHERE title = 'Live & Loud'").fetchone()[0]
        new_song = self.conn.execute("SELECT id FROM songs WHERE artist_id = 3 AND title = 'Paranoid'").fetchone()[0]
        self.assertEqual(self.conn.execute("SELECT album_id FROM songs WHERE id = ?", (new_song,)).fetchone()[0], album_id)
        self.assertEqual(self.conn.execute("SELECT song_id FROM setlist_songs WHERE id = 1").fetchone()[0], new_song)
        self.assertEqual(self.conn.execute("SELECT album_id FROM songs WHERE id = 101").fetchone()[0], 11)  # Sabbath's own, untouched
        self.conn.commit()
        self._undo_all(h)
        self.assertEqual(base.checksum(self.conn), before)

    def test_same_title_album_is_linked_not_duplicated(self):
        """You have "Paranoid" already, identified by an old edition id: adding MusicBrainz's
        "Paranoid" links to yours instead of creating a second one."""
        self.conn.execute("UPDATE albums SET mbid = 'dddddddd-0000-0000-0000-00000000000e' WHERE id = 10")
        self.conn.execute("UPDATE albums SET title = 'We Sold Our Soul' WHERE id = 11")  # the fixture's other "Paranoid"
        self.conn.execute("UPDATE songs SET album_id = NULL WHERE id = 100")
        h = _album_new(self.conn, {"_rg": rg("bbbbbbbb-0000-0000-0000-0000000000aa", "Paranoid", [SABBATH]), "songId": 100, "ownerId": 1})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM albums WHERE title = 'Paranoid'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT album_id FROM songs WHERE id = 100").fetchone()[0], 10)
        self.assertEqual(len(h), 1)  # just the song's link -- no album created

    def test_several_same_title_albums_refused(self):
        with self.assertRaises(ApiError) as cm:  # Paranoid + Paranoid (Remastered): don't make a third
            _album_new(self.conn, {"_rg": rg("bbbbbbbb-0000-0000-0000-0000000000aa", "Paranoid", [SABBATH]), "songId": 100, "ownerId": 1})
        self.assertIn("2 Black Sabbath albums", str(cm.exception))

    def test_album_with_songs_on_it_wont_undo_first(self):
        self.conn.execute("UPDATE songs SET album_id = NULL WHERE id = 100")
        h = _album_new(self.conn, {"_rg": rg("bbbbbbbb-0000-0000-0000-00000000000f", "Sabotage", [SABBATH]), "songId": 100, "ownerId": 1})
        with self.assertRaises(merge.MergeError):
            merge.undo_edit(self.conn, h[0]["id"])  # the album, while the song still points at it


if __name__ == "__main__":
    unittest.main()
