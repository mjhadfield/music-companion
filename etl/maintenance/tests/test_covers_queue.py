"""Albums > Covers (api/albums.py covers_queue_rows, clear_cover): artists with no covered album, most
played first, each proposed their most played album that could have a cover; turned-down albums and
skipped artists stay out; Undo moves a saved cover aside. Throwaway database and covers dir."""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("MUSIC_DB_PATH", str(Path(tempfile.mkdtemp()) / "test.sqlite"))  # before common is imported
os.environ.setdefault("MUSIC_COVERS_DIR", str(Path(tempfile.mkdtemp()) / "covers"))

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))
import covers  # noqa: E402
from api import albums, batch  # noqa: E402
from api.core import Req  # noqa: E402
from common import DB_PATH, connect  # noqa: E402
from migrations import migrate  # noqa: E402

MB = "dddddddd-0000-0000-0000-0000000000{:02d}"


class CoversQueueTests(unittest.TestCase):
    def setUp(self):
        if DB_PATH.exists():
            DB_PATH.unlink()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        # Band A: three albums, the most played has no id; Band B already has a cover; Band C has no albums at all.
        conn.executescript(f"""
            INSERT INTO artists (id, name) VALUES (1, 'Band A'), (2, 'Band B'), (3, 'Band C'), (4, 'Band D');
            INSERT INTO albums (id, artist_id, title, mbid, cover_status) VALUES
                (10, 1, 'No Id', NULL, NULL), (11, 1, 'Second', '{MB.format(11)}', NULL), (12, 1, 'Third', '{MB.format(12)}', NULL),
                (20, 2, 'Has Art', '{MB.format(20)}', 'ok'), (40, 4, 'Nothing On CAA', '{MB.format(40)}', 'none'), (41, 4, 'Other', '{MB.format(41)}', NULL);
            INSERT INTO songs (id, artist_id, album_id, title) VALUES (1, 1, 10, 'a'), (2, 1, 11, 'b'), (3, 1, 12, 'c'), (4, 2, 20, 'd'), (5, 3, NULL, 'e'),
                (6, 4, 40, 'f'), (7, 4, 41, 'g');
        """)
        plays = [(1, 1, 10)] * 5 + [(2, 1, 11)] * 3 + [(3, 1, 12)] * 2 + [(4, 2, 20)] * 20 + [(5, 3, None)] * 30 + [(6, 4, 40)] * 2 + [(7, 4, 41)] * 1
        conn.executemany("INSERT INTO scrobbles (song_id, artist_id, album_id, played_at, raw_artist_text, raw_track_text) VALUES (?, ?, ?, '2026-01-01', 'x', 'y')", plays)
        conn.commit()
        conn.close()
        self.conn = connect()

    def tearDown(self):
        self.conn.close()

    def _queue(self):
        total, items = albums.covers_queue_rows(self.conn)
        return total, {i["artistName"]: i for i in items}

    def test_who_is_listed_and_what_is_proposed(self):
        total, q = self._queue()
        self.assertEqual(total, 2)
        self.assertEqual(list(q), ["Band A", "Band D"])                    # most played first; B has art, C has no album
        self.assertEqual(q["Band A"]["candidateId"], 11)                    # the most played album with an id
        self.assertEqual([a["albumId"] for a in q["Band A"]["albums"]], [10, 11, 12])
        self.assertEqual(q["Band D"]["candidateId"], 41)                    # the archive already came up empty for 40

    def test_turned_down_and_skipped_stay_out(self):
        batch.apply(Req({}, {"actions": [{"type": "mark", "entityType": "album", "entityId": 11, "mark": albums.COVER_REJECTED},
                                          {"type": "mark", "entityType": "artist", "entityId": 4, "mark": albums.COVER_SKIP}]}))
        total, q = self._queue()
        self.assertEqual((total, list(q)), (1, ["Band A"]))
        self.assertEqual(q["Band A"]["candidateId"], 12)                    # the next album
        self.assertTrue(next(a for a in q["Band A"]["albums"] if a["albumId"] == 11)["rejected"])

    def test_a_saved_cover_takes_the_artist_off_and_undo_puts_them_back(self):
        covers._save(11, b"\xff\xd8\xff fake jpeg")
        covers._mark(self.conn, 11, "ok")
        self.conn.commit()
        self.assertNotIn("Band A", self._queue()[1])
        out = albums.clear_cover(Req({}, {"albumId": 11, "status": None}))
        self.assertTrue(out["removed"])
        self.assertFalse((covers.COVERS_DIR / "11.jpg").exists())
        self.assertEqual(len(list((covers.COVERS_DIR / ".removed").glob("11-*.jpg"))), 1)   # moved aside, not deleted
        self.assertIsNone(self.conn.execute("SELECT cover_status FROM albums WHERE id = 11").fetchone()[0])
        self.assertIn("Band A", self._queue()[1])

    def test_most_played_albums_of_artists_that_have_a_picture(self):
        self.assertEqual(albums.album_covers_rows(self.conn), (0, []))       # only B has a picture, and its album has it
        covers._save(11, b"\xff\xd8\xff fake jpeg")
        covers._mark(self.conn, 11, "ok")
        self.conn.commit()
        total, items = albums.album_covers_rows(self.conn)
        self.assertEqual([i["albumId"] for i in items], [12])                # A's other album with an id (10 has none)
        batch.apply(Req({}, {"actions": [{"type": "mark", "entityType": "album", "entityId": 12, "mark": albums.COVER_REJECTED}]}))
        self.assertEqual(albums.album_covers_rows(self.conn), (0, []))       # turned down: off the list

    def test_by_artist_lists_every_album_without_a_cover(self):
        self.conn.executescript("INSERT INTO album_artists VALUES (10, 1, 0), (11, 1, 0), (12, 1, 0), (20, 2, 0), (40, 4, 0), (41, 4, 0)")
        one = albums.artist_covers_rows(self.conn, 4)
        self.assertEqual([a["albumId"] for a in one["items"]], [40, 41])        # the archive's "none" too: a link can still be pasted
        self.assertEqual(albums.artist_covers_rows(self.conn, 2)["covered"], 1)
        names = [a["name"] for a in albums.cover_artists(self.conn)]
        self.assertEqual(names, ["Band A", "Band D"])                         # B has nothing left to cover; most played first


if __name__ == "__main__":
    unittest.main()
