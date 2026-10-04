"""Albums › Unmerge (api/albums.py unmerge_view, split): an album laid open -- merges into it, changes to
it, the titles its plays arrived under -- and splitting some of those titles back out, undoably.
Throwaway database from schema.sql."""
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
from api import albums  # noqa: E402
from api.core import ApiError, Req  # noqa: E402
from api.general import undo  # noqa: E402
from common import DB_PATH, connect  # noqa: E402
from migrations import migrate  # noqa: E402
from tests.test_batch import checksum  # noqa: E402

TABLES = ["albums", "album_artists", "scrobbles", "songs", "vinyl_holdings", "alias_overrides"]


class UnmergeTests(unittest.TestCase):
    def setUp(self):
        if DB_PATH.exists():
            DB_PATH.unlink()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        # "Espresso" (a single, with the vinyl) merged into "Short n' Sweet"
        conn.executescript("""
            INSERT INTO artists (id, name) VALUES (1, 'Sabrina Carpenter');
            INSERT INTO albums (id, artist_id, title) VALUES (10, 1, 'Short n'' Sweet'), (11, 1, 'Espresso');
            INSERT INTO album_artists VALUES (10, 1, 0), (11, 1, 0);
            INSERT INTO songs (id, artist_id, album_id, title) VALUES (1, 1, 10, 'Taste'), (2, 1, 11, 'Espresso');
            INSERT INTO scrobbles (song_id, artist_id, album_id, played_at, raw_artist_text, raw_track_text, raw_album_text) VALUES
                (1, 1, 10, '2026-01-01', 'Sabrina Carpenter', 'Taste', 'Short n'' Sweet'),
                (2, 1, 10, '2026-01-02', 'Sabrina Carpenter', 'Espresso', 'Short n'' Sweet'),
                (2, 1, 11, '2024-05-09', 'Sabrina Carpenter', 'Espresso', 'Espresso');
            INSERT INTO vinyl_holdings (id, album_id, discogs_release_id, raw_title_text, raw_artist_text) VALUES (5, 11, 1, 'Espresso', 'Sabrina Carpenter');
        """)
        conn.commit()
        conn.close()
        self.conn = connect()
        self.before = checksum(self.conn, TABLES)
        merge.merge_albums(self.conn, 11, 10)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_the_album_laid_open(self):
        v = albums.unmerge_view(Req({"albumId": "10"}, {}))
        self.assertEqual([m["absorbedName"] for m in v["merges"]], ["Espresso"])
        self.assertTrue(v["merges"][0]["undoable"])
        self.assertEqual({x["raw"]: (x["scrobbles"], x["vinyl"]) for x in v["spellings"]}, {"Short n' Sweet": (2, 0), "Espresso": (1, 1)})

    def test_splitting_a_title_back_out_and_undoing_it(self):
        out = albums.split(Req({}, {"albumId": 10, "rawTitles": ["Espresso"], "title": "Espresso"}))
        self.assertEqual((out["scrobbles"], out["vinyl"]), (1, 1))
        new = out["albumId"]
        self.assertEqual(self.conn.execute("SELECT album_id FROM vinyl_holdings WHERE id = 5").fetchone()[0], new)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM scrobbles WHERE album_id = 10").fetchone()[0], 2)
        with self.assertRaises(ApiError):
            albums.split(Req({}, {"albumId": 10, "rawTitles": [], "title": "x"}))
        undo(Req({}, {"kind": "edit", "id": out["editId"]}))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM scrobbles WHERE album_id = 10").fetchone()[0], 3)
        self.assertEqual(self.conn.execute("SELECT album_id FROM vinyl_holdings WHERE id = 5").fetchone()[0], 10)


if __name__ == "__main__":
    unittest.main()
