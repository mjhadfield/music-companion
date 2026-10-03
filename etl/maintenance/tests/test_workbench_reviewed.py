"""Workbench › Reviewed (api/workbench.py reviewed, check): artists with nothing outstanding are listed for
a double-check; "Looks right" is a mark on the albums as they were, so a later change shows as
"changed since". Throwaway database from schema.sql."""
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
        return next((r for r in workbench.reviewed(Req({}, {}))["items"] if r["name"] == name), None)

    def test_only_artists_with_nothing_outstanding(self):
        self.assertIsNotNone(self._row("Clean"))
        self.assertIsNone(self._row("Messy"))
        r = self._row("Clean")
        self.assertEqual((r["albums"], r["covers"], r["ids"], r["check"]), (1, 1, 1, "unchecked"))

    def test_looks_right_then_a_change_shows_as_changed_since(self):
        ck = workbench.check(Req({"artistId": "1"}, {}))
        out = batch.apply(Req({}, {"actions": [{"type": "mark", "entityType": "artist", "entityId": 1, "mark": ck["checkMark"]}]}))
        self.assertEqual(self._row("Clean")["check"], "checked")
        self.conn.execute("UPDATE albums SET title = 'Tidy (Remaster)' WHERE id = 10")
        self.conn.commit()
        self.assertEqual(self._row("Clean")["check"], "changed")
        batch.undo_batch(out["batchId"])
        self.assertEqual(self._row("Clean")["check"], "unchecked")


if __name__ == "__main__":
    unittest.main()
