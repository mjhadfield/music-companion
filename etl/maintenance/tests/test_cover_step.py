"""The Last.fm import's cover step (etl/cover_step.py) and its Inbox › Covers review (api/imports.py): albums
just played get a cover fetched into .pending/ -- small for one-offs, full size from FULL_SIZE_PLAYS plays or
on vinyl -- and an Inbox item; accepting puts it in place, "Wrong cover" sets it aside and stops it being
fetched again, and both undo exactly. Crossing the play line swaps a small cover for the big one. The
archive is faked. Throwaway database and covers dir."""
import json
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
import cover_step  # noqa: E402
import covers  # noqa: E402
import merge  # noqa: E402
from api import general, imports  # noqa: E402
from api.core import Req  # noqa: E402
from common import DB_PATH, connect  # noqa: E402
from migrations import migrate  # noqa: E402

MB = "eeeeeeee-0000-0000-0000-0000000000{:02d}"


def fake_jpeg(width: int) -> bytes:
    """Just enough JPEG for covers.jpeg_width: SOI, a padding segment sized by width (bigger = more bytes), SOF0."""
    pad = b"\xff\xe0" + (2 + width).to_bytes(2, "big") + b"\0" * width
    return b"\xff\xd8" + pad + b"\xff\xc0\x00\x11\x08" + width.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\0" * 12


class CoverStepTests(unittest.TestCase):
    def setUp(self):
        if DB_PATH.exists():
            DB_PATH.unlink()
        for d in (covers.COVERS_DIR, covers.PENDING_DIR, covers.REMOVED_DIR):
            if d.is_dir():
                for f in d.glob("*.jpg"):
                    f.unlink()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        # 10 one-off · 11 played lots · 12 on vinyl · 13 no id · 14 turned down before · 15 has art · 16 not on the archive · 17 not played this run
        conn.executescript(f"""
            INSERT INTO artists (id, name) VALUES (1, 'Band');
            INSERT INTO albums (id, artist_id, title, mbid, cover_status) VALUES
                (10, 1, 'One Off', '{MB.format(10)}', NULL), (11, 1, 'Favourite', '{MB.format(11)}', NULL),
                (12, 1, 'On Vinyl', '{MB.format(12)}', NULL), (13, 1, 'No Id', NULL, NULL),
                (14, 1, 'Turned Down', '{MB.format(14)}', NULL), (15, 1, 'Has Art', '{MB.format(15)}', 'ok'),
                (16, 1, 'Nothing There', '{MB.format(16)}', NULL), (17, 1, 'Not Played', '{MB.format(17)}', NULL);
            INSERT INTO songs (id, artist_id, album_id, title) VALUES (1, 1, 10, 'a'), (2, 1, 11, 'b'), (3, 1, 12, 'c'),
                (4, 1, 13, 'd'), (5, 1, 14, 'e'), (6, 1, 15, 'f'), (7, 1, 16, 'g'), (8, 1, 17, 'h');
            INSERT INTO vinyl_holdings (id, album_id, discogs_release_id, raw_artist_text, raw_title_text) VALUES (1, 12, 999, 'Band', 'On Vinyl');
            INSERT INTO review_marks (entity_type, entity_id, mark) VALUES ('album', 14, 'cover-rejected');
            INSERT INTO import_runs (id, source, summary_json) VALUES (1, 'lastfm', '{{"imported": 9}}');
        """)
        old = [(8, 17)] + [(2, 11)] * 3               # before this import: ids 1-4
        new = [(1, 10), (2, 11), (2, 11), (3, 12), (4, 13), (5, 14), (6, 15), (7, 16)]
        conn.executemany("INSERT INTO scrobbles (song_id, artist_id, album_id, played_at, raw_artist_text, raw_track_text) VALUES (?, 1, ?, '2026-10-01', 'x', 'y')",
                         old + new)
        conn.commit()
        conn.close()
        self.conn = connect()
        self.asked = []

        def fake_front(mbid, size=covers.FULL):
            self.asked.append((mbid, size))
            return None if mbid == MB.format(16) else fake_jpeg(size)
        self._real = covers.caa_front
        covers.caa_front = fake_front
        cover_step.PAUSE = 0

    def tearDown(self):
        covers.caa_front = self._real
        self.conn.close()

    def _status(self, album_id):
        return self.conn.execute("SELECT cover_status FROM albums WHERE id = ?", (album_id,)).fetchone()[0]

    def _inbox(self, reviewed=False):
        return imports.inbox(Req({"tab": "covers", **({"reviewed": "1"} if reviewed else {})}, {}))

    def test_fetches_what_was_just_played_sized_by_plays(self):
        r = cover_step.run(self.conn, since_scrobble=4, after_run=0)
        self.assertEqual((r["found"], r["notOnArchive"]), (3, 1))
        sizes = {m[-2:]: s for m, s in self.asked}
        self.assertEqual(sizes, {"10": 250, "11": 500, "12": 500, "16": 250})   # 11 has 5 plays, 12 is on vinyl
        self.assertEqual(sorted(int(p.stem) for p in covers.PENDING_DIR.glob("*.jpg")), [10, 11, 12])
        self.assertEqual(self._status(16), "none")
        self.assertIsNone(self._status(10))                                      # nothing is in place until it's reviewed
        self.assertFalse((covers.COVERS_DIR / "10.jpg").exists())
        summary = json.loads(self.conn.execute("SELECT summary_json FROM import_runs WHERE id = 1").fetchone()[0])
        self.assertEqual((summary["imported"], summary["events"]["new_cover"], summary["covers"]["found"]), (9, 3, 3))
        # a second import with the same plays fetches nothing again (waiting / none / rejected / has art)
        self.asked.clear()
        self.assertEqual(cover_step.run(self.conn, since_scrobble=4, after_run=0)["found"], 0)
        self.assertEqual(self.asked, [])

    def test_inbox_lists_them_ticked_with_storage(self):
        cover_step.run(self.conn, since_scrobble=4, after_run=0)
        out = self._inbox()
        self.assertEqual(sorted(it["entityId"] for it in out["items"]), [10, 11, 12])
        self.assertTrue(all(it["clean"] and it["hasFile"] for it in out["items"]))
        self.assertEqual(out["pending"]["covers"], 3)
        self.assertEqual(out["pending"]["other"], 0)
        self.assertEqual(out["storage"]["pendingFiles"], 3)
        # the album's id changed since: no longer pre-ticked
        self.conn.execute("UPDATE albums SET mbid = ? WHERE id = 10", (MB.format(99),))
        self.conn.commit()
        self.assertFalse(next(it for it in self._inbox()["items"] if it["entityId"] == 10)["clean"])

    def test_accept_puts_it_in_place_and_undo_puts_it_back(self):
        cover_step.run(self.conn, since_scrobble=4, after_run=0)
        ids = [it["id"] for it in self._inbox()["items"]]
        out = imports.accept(Req({}, {"ids": ids}))
        self.assertEqual((out["reviewed"], out["covers"], out["applied"]), (3, 3, 0))
        self.assertTrue((covers.COVERS_DIR / "10.jpg").is_file())
        self.assertFalse(covers.pending_path(10).exists())
        self.assertEqual(self._status(10), "ok")
        self.assertEqual(self._inbox()["items"], [])
        general.undo(Req({}, {"kind": "batch", "items": out["undo"]["id"]}))
        self.assertTrue(covers.pending_path(10).is_file())
        self.assertFalse((covers.COVERS_DIR / "10.jpg").exists())
        self.assertIsNone(self._status(10))
        self.assertEqual(len(self._inbox()["items"]), 3)

    def test_wrong_cover_is_set_aside_and_never_fetched_again(self):
        cover_step.run(self.conn, since_scrobble=4, after_run=0)
        it = next(it for it in self._inbox()["items"] if it["entityId"] == 10)
        out = imports.reject_cover(Req({}, {"eventId": it["id"]}))
        self.assertFalse(covers.pending_path(10).exists())
        self.assertEqual(len(list(covers.REMOVED_DIR.glob("10-*.jpg"))), 1)
        self.assertTrue(self.conn.execute("SELECT 1 FROM review_marks WHERE entity_type = 'album' AND entity_id = 10 AND mark = 'cover-rejected'").fetchone())
        self.conn.execute("INSERT INTO scrobbles (song_id, artist_id, album_id, played_at, raw_artist_text, raw_track_text) VALUES (1, 1, 10, '2026-10-02', 'x', 'y')")
        self.conn.commit()
        self.asked.clear()
        cover_step.run(self.conn, since_scrobble=12, after_run=0)
        self.assertEqual(self.asked, [])                                         # played again: still not fetched
        general.undo(Req({}, {"kind": "batch", "items": out["undo"]["id"]}))
        self.assertTrue(covers.pending_path(10).is_file())
        self.assertFalse(self.conn.execute("SELECT 1 FROM review_marks WHERE entity_id = 10 AND mark = 'cover-rejected'").fetchone())
        self.assertIn(10, [i["entityId"] for i in self._inbox()["items"]])

    def test_merged_away_or_covered_elsewhere_settles_and_drops_the_file(self):
        cover_step.run(self.conn, since_scrobble=4, after_run=0)
        merge.merge_albums(self.conn, 10, 15)
        self.conn.execute("UPDATE albums SET cover_status = 'ok' WHERE id = 11")   # e.g. fetched from Albums > Covers meanwhile
        self.conn.commit()
        self.assertEqual([it["entityId"] for it in self._inbox()["items"]], [12])
        self.assertFalse(covers.pending_path(10).exists())
        self.assertFalse(covers.pending_path(11).exists())

    def test_crossing_the_play_line_swaps_in_the_big_cover(self):
        cover_step.run(self.conn, since_scrobble=4, after_run=0)
        imports.accept(Req({}, {"ids": [it["id"] for it in self._inbox()["items"]]}))
        self.assertEqual(covers.jpeg_width(covers.COVERS_DIR / "10.jpg"), 250)
        last = self.conn.execute("SELECT max(id) FROM scrobbles").fetchone()[0]
        self.conn.executemany("INSERT INTO scrobbles (song_id, artist_id, album_id, played_at, raw_artist_text, raw_track_text) VALUES (1, 1, 10, '2026-10-03', 'x', 'y')", [()] * 4)
        self.conn.commit()
        r = cover_step.run(self.conn, since_scrobble=last, after_run=None)
        self.assertEqual((r["found"], r["upgraded"]), (0, 1))
        self.assertEqual(covers.jpeg_width(covers.COVERS_DIR / "10.jpg"), 500)
        self.assertEqual(self._status(10), "ok")
        # past the line already: playing it again asks the archive nothing
        self.asked.clear()
        self.conn.execute("INSERT INTO scrobbles (song_id, artist_id, album_id, played_at, raw_artist_text, raw_track_text) VALUES (1, 1, 10, '2026-10-04', 'x', 'y')")
        self.conn.commit()
        cover_step.run(self.conn, since_scrobble=last + 4, after_run=None)
        self.assertEqual(self.asked, [])

    def test_limit_defers_the_least_played(self):
        r = cover_step.run(self.conn, since_scrobble=4, after_run=0, limit=1)
        self.assertEqual((r["found"], r["deferred"]), (1, 3))
        self.assertEqual([p.stem for p in covers.PENDING_DIR.glob("*.jpg")], ["11"])

    def test_jpeg_width_reads_real_frame_headers(self):
        p = covers.COVERS_DIR / "t.jpg"
        covers.COVERS_DIR.mkdir(parents=True, exist_ok=True)
        p.write_bytes(fake_jpeg(250))
        self.assertEqual(covers.jpeg_width(p), 250)
        p.write_bytes(b"\x89PNG\r\n\x1a\n...")
        self.assertIsNone(covers.jpeg_width(p))
        p.unlink()


if __name__ == "__main__":
    unittest.main()
