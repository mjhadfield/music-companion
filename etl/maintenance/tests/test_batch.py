"""Reviewed batches (api/batch.py), split_song and the integrity checks: preview must leave the
database untouched, apply + undo must restore every table exactly, a failing action or an
integrity regression must change nothing at all. Runs against a throwaway database built from
schema.sql (never the real one)."""
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
import integrity  # noqa: E402
import merge  # noqa: E402
from api import batch  # noqa: E402
from api.core import ApiError, Req  # noqa: E402
from common import DB_PATH, connect  # noqa: E402
from migrations import migrate  # noqa: E402

TABLES = ["artists", "albums", "album_artists", "songs", "scrobbles", "setlists", "setlist_songs", "notes", "alias_overrides",
          "track_links", "suggestions", "review_marks", "duplicate_dismissals", "vinyl_holdings", "album_releases"]
RG = "dddddddd-0000-0000-0000-0000000000{:02d}"


def checksum(conn, tables=TABLES) -> dict:
    out = {}
    for t in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        order = "id" if "id" in cols else ", ".join(cols)
        out[t] = hashlib.sha1(repr(conn.execute(f"SELECT * FROM {t} ORDER BY {order}").fetchall()).encode()).hexdigest()
    return out


def everything(conn) -> dict:
    """Every table, logs included -- for 'preview changes nothing at all'."""
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    return checksum(conn, names)


class BatchTests(unittest.TestCase):
    def setUp(self):
        if DB_PATH.exists():
            DB_PATH.unlink()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        conn.executescript(f"""
            INSERT INTO artists (id, mbid, name) VALUES (1, 'aaaaaaaa-0000-0000-0000-000000000001', 'Black Sabbath'), (2, NULL, 'Ozzy');
            INSERT INTO albums (id, mbid, artist_id, title, year) VALUES
                (10, '{RG.format(10)}', 1, 'Paranoid', 1970), (11, NULL, 1, 'Paranoid (Remastered)', 2009), (12, NULL, 1, 'Master of Reality', 1971),
                (20, NULL, 2, 'Blizzard of Ozz', 1980);
            INSERT INTO album_artists VALUES (10, 1, 0), (11, 1, 0), (12, 1, 0), (20, 2, 0);
            INSERT INTO songs (id, artist_id, album_id, title) VALUES
                (100, 1, 10, 'Paranoid'), (101, 1, 11, 'Paranoid - 2009 Remaster'), (102, 1, 10, 'War Pigs'),
                (103, 1, 10, 'Paranoid - Live'), (104, 1, 12, 'Into the Void'), (200, 2, 20, 'Crazy Train');
            INSERT INTO scrobbles (id, song_id, artist_id, album_id, played_at, raw_artist_text, raw_track_text, raw_album_text) VALUES
                (1, 100, 1, 10, '2020-01-01', 'Black Sabbath', 'Paranoid', 'Paranoid'),
                (2, 101, 1, 11, '2020-01-02', 'Black Sabbath', 'Paranoid - 2009 Remaster', 'Paranoid (Remastered)'),
                (3, 103, 1, 10, '2020-01-03', 'Black Sabbath', 'Paranoid - Live', 'Paranoid'),
                (4, 103, 1, 10, '2020-01-04', 'Black Sabbath', 'Paranoid - Live', 'Paranoid'),
                (5, 102, 1, 10, '2020-01-05', 'Black Sabbath', 'War Pigs', 'Paranoid'),
                (6, 200, 2, 20, '2020-01-06', 'Ozzy', 'Crazy Train', 'Blizzard of Ozz');
            INSERT INTO venues (id, name) VALUES (1, 'Hammersmith');
            INSERT INTO setlists (id, setlistfm_id, artist_id, venue_id, event_date, raw_artist_text) VALUES (1, 'x1', 1, 1, '2016-01-01', 'Black Sabbath');
            INSERT INTO setlist_songs (id, setlist_id, song_id, position, raw_song_text) VALUES (1, 1, 100, 1, 'Paranoid'), (2, 1, 102, 2, 'War Pigs');
            INSERT INTO alias_overrides (id, source, source_key, canonical_type, canonical_id, note) VALUES
                (1, 'lastfm', '1:paranoid - live', 'song', 103, NULL),
                (2, 'lastfm', 'zzz gone', 'artist', 999, 'left behind by a test');
            INSERT INTO suggestions (id, entity_type, entity_id, mbid, label, confidence, tier, source) VALUES
                (1, 'album', 12, '{RG.format(12)}', 'Master of Reality', 95, 'high', 'album-suggest'),
                (2, 'album', 12, '{RG.format(13)}', 'Master of Reality (Deluxe)', 60, 'low', 'album-suggest');
        """)
        conn.commit()
        conn.close()
        self.conn = connect()

    def tearDown(self):
        self.conn.close()

    def _req(self, **body):
        return Req({}, body)

    # -- preview -----------------------------------------------------------------------------
    def test_preview_changes_nothing_and_reports_every_problem(self):
        before = everything(self.conn)
        out = batch.preview(self._req(actions=[
            {"type": "mergeSongs", "canonicalId": 100, "absorbedIds": [101]},
            {"type": "mergeAlbums", "canonicalId": 10, "absorbedIds": [20]},          # another artist: refused
            {"type": "renameSong", "songId": 104, "title": "Into The Void"},
        ]))
        self.assertEqual(everything(self.conn), before)
        self.assertEqual([r["ok"] for r in out["results"]], [True, False, True])
        self.assertIn("different artists", out["results"][1]["error"])
        self.assertEqual(out["results"][0]["rowsMoved"]["scrobbles"], 1)
        self.assertFalse(out["ok"])

    # -- apply + undo ------------------------------------------------------------------------
    def test_apply_then_undo_restores_every_table(self):
        before = checksum(self.conn)
        out = batch.apply(self._req(label="test", actions=[
            {"type": "mergeSongs", "canonicalId": 100, "absorbedIds": [101], "title": "Paranoid"},
            {"type": "mergeAlbums", "canonicalId": 10, "absorbedIds": [11]},
            {"type": "assignAlbumMbid", "albumId": 12, "mbid": RG.format(12), "suggestionId": 1},
            {"type": "setSongAlbum", "songId": 104, "albumId": 10},
            {"type": "dismiss", "entityType": "song", "ids": [100, 103]},
            {"type": "mark", "entityType": "artist", "entityId": 1, "mark": "workbench-reviewed:x"},
        ]))
        self.assertNotEqual(checksum(self.conn), before)
        self.assertEqual(self.conn.execute("SELECT status FROM suggestions ORDER BY id").fetchall(), [("accepted",), ("rejected",)])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM merge_log WHERE batch_id = ?", (out["batchId"],)).fetchone()[0], 2)
        batch.undo_batch(out["batchId"])
        self.assertEqual(checksum(self.conn), before)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        with self.assertRaises(ApiError):
            batch.undo_batch(out["batchId"])                       # once only

    def test_a_failing_action_changes_nothing(self):
        before = everything(self.conn)
        with self.assertRaises(ApiError) as cm:
            batch.apply(self._req(actions=[
                {"type": "mergeSongs", "canonicalId": 100, "absorbedIds": [101]},
                {"type": "setSongAlbum", "songId": 104, "albumId": 20},               # Ozzy's album: refused
            ]))
        self.assertEqual(cm.exception.code, "batch_failed")
        self.assertEqual(everything(self.conn), before)

    def test_an_integrity_regression_is_rolled_back(self):
        real = batch.run_action

        def sneaky(c, it):
            c.execute("INSERT INTO alias_overrides (source, source_key, canonical_type, canonical_id) VALUES ('lastfm', 'x', 'song', 4242)")
            return real(c, it)
        batch.run_action = sneaky
        try:
            before = everything(self.conn)
            with self.assertRaises(ApiError) as cm:
                batch.apply(self._req(actions=[{"type": "renameSong", "songId": 104, "title": "Into The Void"}]))
            self.assertEqual(cm.exception.code, "integrity")
            self.assertEqual(everything(self.conn), before)
        finally:
            batch.run_action = real

    def test_undo_is_refused_while_a_later_change_depends_on_it(self):
        first = batch.apply(self._req(actions=[{"type": "mergeSongs", "canonicalId": 100, "absorbedIds": [101]}]))
        # a later batch merges the first one's survivor away -- the first can't be undone before it
        batch.apply(self._req(actions=[{"type": "mergeSongs", "canonicalId": 102, "absorbedIds": [100]}]))
        before = everything(self.conn)
        with self.assertRaises(ApiError) as cm:
            batch.undo_batch(first["batchId"])
        self.assertEqual(cm.exception.code, "undo_blocked")
        self.assertEqual(everything(self.conn), before)

    def test_batch_undo_after_one_item_was_undone_on_its_own(self):
        before = checksum(self.conn)
        out = batch.apply(self._req(actions=[
            {"type": "mergeSongs", "canonicalId": 100, "absorbedIds": [101]},
            {"type": "renameSong", "songId": 104, "title": "Into The Void"},
        ]))
        log_id = self.conn.execute("SELECT id FROM merge_log WHERE batch_id = ?", (out["batchId"],)).fetchone()[0]
        merge.undo_merge(self.conn, log_id)                                       # Activity's per-item Undo
        self.conn.commit()
        batch.undo_batch(out["batchId"])                                          # the rest still undoes
        self.assertEqual(checksum(self.conn), before)

    def test_move_album_to_a_new_artist_and_back(self):
        before = checksum(self.conn)
        out = batch.apply(self._req(actions=[{"type": "moveAlbumToArtist", "albumId": 12, "newArtist": {"name": "Other Band", "mbid": "bbbbbbbb-0000-0000-0000-000000000009"}}]))
        new_id = self.conn.execute("SELECT id FROM artists WHERE name = 'Other Band'").fetchone()[0]
        q = self.conn.execute
        self.assertEqual(q("SELECT artist_id FROM albums WHERE id = 12").fetchone()[0], new_id)
        self.assertEqual(q("SELECT artist_id FROM album_artists WHERE album_id = 12").fetchall(), [(new_id,)])
        self.assertEqual(q("SELECT artist_id FROM songs WHERE id = 104").fetchone()[0], new_id)
        self.assertEqual(integrity.counts(self.conn, hard_only=True)["play_song_artist_mismatch"], 0)
        batch.undo_batch(out["batchId"])
        self.assertEqual(checksum(self.conn), before)

    def test_move_album_keeping_the_old_artist_credited_second(self):
        before = checksum(self.conn)
        out = batch.apply(self._req(actions=[{"type": "moveAlbumToArtist", "albumId": 12, "toArtistId": 2, "keepCredit": True}]))
        self.assertEqual(self.conn.execute("SELECT artist_id FROM album_artists WHERE album_id = 12 ORDER BY position").fetchall(), [(2,), (1,)])
        self.assertEqual(self.conn.execute("SELECT artist_id FROM albums WHERE id = 12").fetchone()[0], 2)
        batch.undo_batch(out["batchId"])
        self.assertEqual(checksum(self.conn), before)

    def test_move_album_refuses_what_isnt_self_contained(self):
        with self.assertRaises(ApiError):                                      # songs played from another album too
            batch.apply(self._req(actions=[{"type": "moveAlbumToArtist", "albumId": 10, "toArtistId": 2}]))
        self.conn.execute("INSERT INTO alias_overrides (source, source_key, canonical_type, canonical_id) VALUES ('lastfm', '1:into the void', 'song', 104)")
        self.conn.commit()
        with self.assertRaises(ApiError):                                      # an alias would keep routing to the old artist
            batch.apply(self._req(actions=[{"type": "moveAlbumToArtist", "albumId": 12, "toArtistId": 2}]))

    def test_undo_merge_as_a_batch_and_redo(self):
        first = batch.apply(self._req(actions=[{"type": "mergeSongs", "canonicalId": 100, "absorbedIds": [103]}]))
        log_id = self.conn.execute("SELECT id FROM merge_log WHERE batch_id = ?", (first["batchId"],)).fetchone()[0]
        merged = checksum(self.conn)
        out = batch.apply(self._req(actions=[{"type": "undoMerge", "logId": log_id}]))
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM songs WHERE id = 103").fetchone())     # the live version is back
        batch.undo_batch(out["batchId"])                                                              # and merged again
        self.assertEqual(checksum(self.conn, [t for t in TABLES if t != "alias_overrides"]),
                         {k: v for k, v in merged.items() if k != "alias_overrides"})

    # -- split_song ----------------------------------------------------------------------------
    def test_split_song_takes_a_version_back_out_and_undoes(self):
        batch.apply(self._req(actions=[{"type": "mergeSongs", "canonicalId": 100, "absorbedIds": [103]}]))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM scrobbles WHERE song_id = 100").fetchone()[0], 3)
        before = checksum(self.conn)
        out = batch.apply(self._req(actions=[{"type": "splitSong", "songId": 100, "rawTitles": ["Paranoid - Live"], "title": "Paranoid - Live (split)"}]))
        new_id = self.conn.execute("SELECT id FROM songs WHERE title = 'Paranoid - Live (split)'").fetchone()[0]
        self.assertEqual(sorted(r[0] for r in self.conn.execute("SELECT id FROM scrobbles WHERE song_id = ?", (new_id,))), [3, 4])
        self.assertEqual(self.conn.execute("SELECT canonical_id FROM alias_overrides WHERE id = 1").fetchone()[0], new_id)
        batch.undo_batch(out["batchId"])
        self.assertEqual(checksum(self.conn), before)

    def test_split_song_guards(self):
        with self.assertRaises(merge.MergeError):
            merge.split_song(self.conn, 100, ["Paranoid"], "War Pigs")          # a title the artist already has
        with self.assertRaises(merge.MergeError):
            merge.split_song(self.conn, 103, ["Paranoid - Live"], "Paranoid (Live)")   # every play: that's a rename
        with self.assertRaises(merge.MergeError):
            merge.split_song(self.conn, 100, ["Nothing like it"], "Other")

    # -- integrity -------------------------------------------------------------------------------
    def test_integrity_counts_and_the_dangling_alias_fix(self):
        n = integrity.counts(self.conn)
        self.assertEqual(n["alias_missing_target"], 1)
        self.assertEqual(n["fk_violations"], 0)
        with self.assertRaises(ApiError):                                          # only dangling aliases can be removed
            batch.apply(self._req(actions=[{"type": "removeAlias", "aliasId": 1}]))
        out = batch.apply(self._req(actions=[{"type": "removeAlias", "aliasId": 2}]))
        self.assertEqual(integrity.counts(self.conn)["alias_missing_target"], 0)
        batch.undo_batch(out["batchId"])
        self.assertEqual(integrity.counts(self.conn)["alias_missing_target"], 1)  # undo brings it back exactly
        rep = integrity.report(self.conn)
        self.assertEqual(rep["hard"], 1)
        self.assertTrue(next(i for i in rep["items"] if i["check"] == "alias_missing_target")["fixable"])


if __name__ == "__main__":
    unittest.main()
