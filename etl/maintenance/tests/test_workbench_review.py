"""Workbench review status (api/workbench.py artists, workbench): every artist is listed with a status;
Mark reviewed is a mark on the albums as they were (plus, when anything was outstanding, a mark on
that), so a later change shows as "changed since". Throwaway database from schema.sql."""
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
from api import batch, workbench  # noqa: E402
from api.core import Req  # noqa: E402
from common import DB_PATH, connect  # noqa: E402
from migrations import migrate  # noqa: E402


class ReviewedTests(unittest.TestCase):
    def setUp(self):
        if DB_PATH.exists():
            DB_PATH.unlink()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        # Clean has one tidy album; Messy has the same album twice (a version group: outstanding).
        conn.executescript("""
            INSERT INTO artists (id, name) VALUES (1, 'Clean'), (2, 'Messy');
            INSERT INTO albums (id, artist_id, title, mbid, cover_status, year) VALUES
                (10, 1, 'Tidy', 'eeeeeeee-0000-0000-0000-000000000010', 'ok', 2001),
                (20, 2, 'Record', NULL, NULL, NULL), (21, 2, 'Record (Deluxe Edition)', NULL, NULL, NULL);
            INSERT INTO album_artists VALUES (10, 1, 0), (20, 2, 0), (21, 2, 0);
            INSERT INTO songs (id, artist_id, album_id, title) VALUES (1, 1, 10, 'a'), (2, 2, 20, 'b');
            INSERT INTO scrobbles (song_id, artist_id, album_id, played_at, raw_artist_text, raw_track_text) VALUES
                (1, 1, 10, '2026-01-01', 'Clean', 'a'), (2, 2, 20, '2026-01-01', 'Messy', 'b');
        """)
        conn.commit()
        conn.close()
        self.conn = connect()

    def tearDown(self):
        self.conn.close()

    def _row(self, name):
        return next((r for r in workbench.artists(Req({}, {}))["items"] if r["name"] == name), None)

    def _review(self, artist_id):
        wb = workbench.workbench(Req({"artistId": str(artist_id)}, {}))
        actions = [{"type": "mark", "entityType": "artist", "entityId": artist_id, "mark": m} for m in wb["reviewMarks"]]
        return batch.apply(Req({}, {"actions": actions, "artistId": artist_id}))

    def test_every_artist_listed_with_a_status(self):
        clean, messy = self._row("Clean"), self._row("Messy")
        self.assertEqual((clean["status"], clean["albums"], clean["covers"], clean["ids"]), ("unreviewed", 1, 1, 1))
        self.assertEqual(messy["status"], "needs-work")
        self.assertEqual(messy["counts"].get("albumGroups"), 1)
        self.assertEqual((messy["gaps"]["noCover"], messy["gaps"]["noYear"], messy["gaps"]["noArtistMbid"]), (2, 2, 1))

    def test_reviewed_then_a_change_shows_as_changed_since(self):
        out = self._review(1)
        self.assertEqual(self._row("Clean")["status"], "reviewed")
        self.conn.execute("UPDATE albums SET title = 'Tidy (Remaster)' WHERE id = 10")
        self.conn.commit()
        self.assertEqual(self._row("Clean")["status"], "changed")
        batch.undo_batch(out["batchId"])
        self.assertEqual(self._row("Clean")["status"], "unreviewed")

    def test_a_type_change_counts_as_a_change(self):
        self._review(1)
        self.conn.execute("UPDATE albums SET release_type = 'album', release_type_source = 'manual' WHERE id = 10")
        self.conn.commit()
        self.assertEqual(self._row("Clean")["status"], "changed")

    def test_reviewing_an_artist_with_work_left_leaves_it_as_is(self):
        self.assertEqual(len(workbench.workbench(Req({"artistId": "2"}, {}))["reviewMarks"]), 2)
        out = self._review(2)
        r = self._row("Messy")
        self.assertEqual((r["status"], r["leftAsIs"]), ("reviewed", r["outstanding"]))
        self.assertGreater(r["leftAsIs"], 0)
        batch.undo_batch(out["batchId"])   # one undo takes back both marks
        self.assertEqual(self._row("Messy")["status"], "needs-work")


if __name__ == "__main__":
    unittest.main()
