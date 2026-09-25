"""Merge -> undo must leave every data table exactly as it was. Runs against a throwaway
database built from schema.sql (never the real one)."""
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp()
os.environ["MUSIC_DB_PATH"] = str(Path(_TMP) / "test.sqlite")  # must be set before common is imported

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))
import merge  # noqa: E402
from common import DB_PATH, artist_mbid_sets, connect, get_or_create_album, get_or_create_artist, get_or_create_song  # noqa: E402
from migrations import migrate  # noqa: E402

DATA_TABLES = ["artists", "albums", "album_artists", "songs", "vinyl_holdings", "scrobbles",
               "setlists", "setlist_songs", "notes", "alias_overrides", "artist_mb_aliases"]


def checksum(conn) -> dict:
    out = {}
    for t in DATA_TABLES:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        order = "id" if "id" in cols else ", ".join(cols)
        rows = conn.execute(f"SELECT * FROM {t} ORDER BY {order}").fetchall()
        out[t] = hashlib.sha1(repr(rows).encode()).hexdigest()
    return out


class MergeUndoTests(unittest.TestCase):
    def setUp(self):
        if DB_PATH.exists():
            DB_PATH.unlink()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        conn.executescript(
            """
            INSERT INTO artists (id, mbid, name) VALUES (1, 'aaaaaaaa-0000-0000-0000-000000000001', 'Black Sabbath'),
                                                        (2, NULL, 'Black Sabbath.'), (3, NULL, 'Ozzy');
            INSERT INTO albums (id, mbid, artist_id, title, year) VALUES
                (10, 'bbbbbbbb-0000-0000-0000-000000000010', 1, 'Paranoid', 1970),
                (11, 'bbbbbbbb-0000-0000-0000-000000000011', 1, 'Paranoid (Remastered)', 2009),
                (12, NULL, 2, 'Master of Reality', 1971),
                (13, NULL, 1, 'Shared Credit', 1980);
            INSERT INTO album_artists VALUES (10, 1, 0), (11, 1, 0), (12, 2, 0), (13, 1, 0), (13, 2, 1), (12, 3, 1);
            INSERT INTO songs (id, mbid, artist_id, album_id, title) VALUES
                (100, NULL, 1, 10, 'Paranoid'), (101, 'cccccccc-0000-0000-0000-000000000101', 1, 11, 'Paranoid - 2009 Remaster'),
                (102, NULL, 2, 12, 'Into the Void');
            INSERT INTO vinyl_holdings (id, album_id, discogs_release_id, raw_artist_text, raw_title_text) VALUES (1, 11, 555, 'Black Sabbath', 'Paranoid (Remastered)');
            INSERT INTO scrobbles (id, song_id, artist_id, album_id, played_at, raw_artist_text, raw_track_text, raw_album_text) VALUES
                (1, 100, 1, 10, '2020-01-01', 'Black Sabbath', 'Paranoid', 'Paranoid'),
                (2, 101, 1, 11, '2020-01-02', 'Black Sabbath', 'Paranoid - 2009 Remaster', 'Paranoid (Remastered)'),
                (3, 102, 2, 12, '2020-01-03', 'Black Sabbath.', 'Into the Void', 'Master of Reality');
            INSERT INTO venues (id, name) VALUES (1, 'Hammersmith');
            INSERT INTO setlists (id, setlistfm_id, artist_id, venue_id, event_date, raw_artist_text) VALUES (1, 'x1', 2, 1, '2016-01-01', 'Black Sabbath.');
            INSERT INTO setlist_songs (id, setlist_id, song_id, position, raw_song_text) VALUES (1, 1, 101, 1, 'Paranoid');
            INSERT INTO notes (id, entity_type, entity_id, body_markdown) VALUES (1, 'album', 11, 'reissue'), (2, 'artist', 2, 'dup');
            INSERT INTO alias_overrides (id, source, source_key, canonical_type, canonical_id) VALUES
                (1, 'lastfm', '2:master of reality (deluxe)', 'album', 12),
                (2, 'lastfm', '1:master of reality (deluxe)', 'album', 12),
                (3, 'lastfm', 'sabbath', 'artist', 2),
                (4, 'lastfm', '2:into the void - live', 'song', 102);
            """
        )
        conn.commit()
        conn.close()
        self.conn = connect()

    def tearDown(self):
        self.conn.close()

    def _roundtrip(self, do_merge):
        before = checksum(self.conn)
        result = do_merge()
        self.conn.commit()
        self.assertNotEqual(checksum(self.conn), before)
        merge.undo_merge(self.conn, result["logId"])
        self.conn.commit()
        self.assertEqual(checksum(self.conn), before)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        return result

    def test_artist_merge_undo(self):
        r = self._roundtrip(lambda: merge.merge_artists(self.conn, 2, 1))
        self.assertEqual(r["rowsMoved"]["scrobbles"], 1)

    def test_artist_merge_rekeys_album_aliases(self):
        merge.merge_artists(self.conn, 2, 1)
        keys = {r[0] for r in self.conn.execute("SELECT source_key FROM alias_overrides")}
        self.assertIn("1:into the void - live", keys)  # rekeyed from 2:
        self.assertNotIn("2:master of reality (deluxe)", keys)  # clashed with 1:'s -> dropped

    def test_album_merge_undo_with_identity(self):
        self._roundtrip(lambda: merge.merge_albums(
            self.conn, 10, 11, identity={"mbid": "bbbbbbbb-0000-0000-0000-000000000010", "title": "Paranoid", "year": 1970}))

    def test_album_merge_adopts_mbid(self):
        merge.merge_artists(self.conn, 2, 1)
        self.conn.execute("UPDATE albums SET mbid = NULL WHERE id = 10")
        self.conn.commit()
        self._roundtrip(lambda: merge.merge_albums(self.conn, 11, 10))

    def test_split_album_undo(self):
        """An old (unjournaled) merge of 11 into 10, taken back out by its source titles, then undone."""
        self.conn.execute("UPDATE scrobbles SET album_id = 10 WHERE album_id = 11")
        self.conn.execute("UPDATE songs SET album_id = 10 WHERE album_id = 11")
        self.conn.execute("UPDATE vinyl_holdings SET album_id = 10 WHERE album_id = 11")
        self.conn.execute("INSERT INTO alias_overrides (source, source_key, canonical_type, canonical_id) VALUES ('lastfm', '1:paranoid (remastered)', 'album', 10)")
        self.conn.execute("INSERT INTO album_releases (release_mbid, album_id) VALUES ('dddddddd-0000-0000-0000-000000000001', 10)")
        self.conn.execute("DELETE FROM notes WHERE entity_type = 'album' AND entity_id = 11")
        self.conn.execute("DELETE FROM album_artists WHERE album_id = 11")
        self.conn.execute("DELETE FROM albums WHERE id = 11")
        self.conn.commit()
        before = checksum(self.conn)
        r = merge.split_album(self.conn, 10, ["Paranoid (Remastered)"], {"title": "Paranoid (Remastered)", "year": 2009,
                              "mbid": "bbbbbbbb-0000-0000-0000-000000000011"}, releases=["dddddddd-0000-0000-0000-000000000001"])
        new = r["albumId"]
        self.assertEqual((r["scrobbles"], r["vinyl"], r["songs"], r["aliases"], r["editions"]), (1, 1, 1, 1, 1))
        self.assertEqual(self.conn.execute("SELECT album_id FROM scrobbles WHERE id = 2").fetchone()[0], new)
        self.assertEqual(self.conn.execute("SELECT album_id FROM scrobbles WHERE id = 1").fetchone()[0], 10)
        self.assertEqual(self.conn.execute("SELECT album_id FROM songs WHERE id = 101").fetchone()[0], new)
        self.assertEqual(self.conn.execute("SELECT album_id FROM songs WHERE id = 100").fetchone()[0], 10)
        self.assertEqual(self.conn.execute("SELECT album_id FROM vinyl_holdings WHERE id = 1").fetchone()[0], new)
        self.assertEqual(self.conn.execute("SELECT artist_id FROM album_artists WHERE album_id = ?", (new,)).fetchone()[0], 1)
        merge.undo_edit(self.conn, r["editId"])
        self.conn.commit()
        self.assertEqual(checksum(self.conn), before)

    def test_move_album_plays_undo(self):
        """Plays that arrived as "Paranoid (Remastered)" filed under Master of Reality by mistake:
        moved to Paranoid (with their alias), then undone exactly."""
        self.conn.execute("UPDATE scrobbles SET album_id = 12 WHERE id = 2")
        self.conn.execute("UPDATE songs SET album_id = 12 WHERE id = 101")
        self.conn.execute("UPDATE artists SET id = id WHERE 0")
        self.conn.execute("UPDATE albums SET artist_id = 1 WHERE id = 12")
        self.conn.execute("INSERT INTO alias_overrides (source, source_key, canonical_type, canonical_id) VALUES ('lastfm', '1:paranoid (remastered)', 'album', 12)")
        self.conn.commit()
        before = checksum(self.conn)
        r = merge.move_album_plays(self.conn, 12, 11, ["Paranoid (Remastered)"], "test")
        self.assertEqual((r["scrobbles"], r["songs"], r["aliases"]), (1, 1, 1))
        self.assertEqual(self.conn.execute("SELECT album_id FROM scrobbles WHERE id = 2").fetchone()[0], 11)
        self.assertEqual(self.conn.execute("SELECT album_id FROM scrobbles WHERE id = 3").fetchone()[0], 12)  # different name: stays
        self.assertEqual(self.conn.execute("SELECT canonical_id FROM alias_overrides WHERE source_key = '1:paranoid (remastered)'").fetchone()[0], 11)
        merge.undo_edit(self.conn, r["editId"])
        self.conn.commit()
        self.assertEqual(checksum(self.conn), before)

    def test_move_album_plays_refuses_other_artist(self):
        with self.assertRaises(merge.MergeError):
            merge.move_album_plays(self.conn, 12, 10, ["Master of Reality"], "test")  # 12 is artist 2's, 10 artist 1's

    def _tracklists(self):
        return (sorted(self.conn.execute("SELECT * FROM album_tracklists").fetchall()),
                sorted(self.conn.execute("SELECT album_id, source, release_mbid FROM album_tracklist_sources").fetchall()))

    def _give_tracklist(self, album_id, titles):
        self.conn.executemany("INSERT INTO album_tracklists (album_id, position, number, title) VALUES (?, ?, ?, ?)",
                              [(album_id, i + 1, str(i + 1), t) for i, t in enumerate(titles)])
        self.conn.execute("INSERT INTO album_tracklist_sources (album_id, source, release_mbid) VALUES (?, 'musicbrainz', ?)", (album_id, f"rel-{album_id}"))

    def test_album_merge_carries_tracklist(self):
        self._give_tracklist(11, ["Paranoid", "Iron Man"])
        self.conn.commit()
        before = self._tracklists()
        r = merge.merge_albums(self.conn, 11, 10)
        self.assertEqual(self.conn.execute("SELECT album_id FROM album_tracklist_sources").fetchall(), [(10,)])
        merge.undo_merge(self.conn, r["logId"])
        self.assertEqual(self._tracklists(), before)

    def test_album_merge_drops_absorbed_tracklist_and_undoes(self):
        self._give_tracklist(11, ["Paranoid (Remastered)"])
        self._give_tracklist(10, ["War Pigs", "Paranoid"])
        self.conn.commit()
        before = self._tracklists()
        r = merge.merge_albums(self.conn, 11, 10)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM album_tracklists WHERE album_id = 11").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM album_tracklists WHERE album_id = 10").fetchone()[0], 2)
        merge.undo_merge(self.conn, r["logId"])
        self.assertEqual(self._tracklists(), before)

    def test_song_merge_undo(self):
        r = self._roundtrip(lambda: merge.merge_songs(self.conn, 101, 100))
        self.assertEqual(r["rowsMoved"]["setlist_songs"], 1)

    def test_different_artist_refused(self):
        with self.assertRaises(merge.MergeError):
            merge.merge_albums(self.conn, 12, 10)

    def test_chained_undo_requires_lifo(self):
        before = checksum(self.conn)
        first = merge.merge_albums(self.conn, 11, 10)
        second = merge.merge_albums(self.conn, 10, 13)  # the first merge's survivor is now absorbed
        self.conn.commit()
        with self.assertRaises(merge.MergeError) as ctx:
            merge.undo_merge(self.conn, first["logId"])
        self.assertEqual(ctx.exception.code, "canonical_gone")
        self.conn.rollback()
        merge.undo_merge(self.conn, second["logId"])
        merge.undo_merge(self.conn, first["logId"])
        self.conn.commit()
        self.assertEqual(checksum(self.conn), before)

    def test_edit_undo(self):
        before = checksum(self.conn)
        e = merge.edit_entity(self.conn, "album", 12, {"mbid": "bbbbbbbb-0000-0000-0000-000000000099", "year": 1972}, "test")
        self.conn.commit()
        merge.undo_edit(self.conn, e["editId"])
        self.conn.commit()
        self.assertEqual(checksum(self.conn), before)

    def test_vinyl_move_and_link_undo(self):
        before = checksum(self.conn)
        e1 = merge.edit_entity(self.conn, "vinyl", 1, {"album_id": 10}, "move")
        e2 = merge.edit_entity(self.conn, "vinyl", 1, {"mb_release_id": "dddddddd-0000-0000-0000-000000000001"}, "link")
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT album_id, mb_release_id FROM vinyl_holdings WHERE id = 1").fetchone(),
                         (10, "dddddddd-0000-0000-0000-000000000001"))
        for e in (e2, e1):  # newest first, like the bulk undo
            merge.undo_edit(self.conn, e["editId"])
        self.conn.commit()
        self.assertEqual(checksum(self.conn), before)

    def test_song_merge_routes_future_imports(self):
        """After a merge, both importers' next sighting of the absorbed spelling must land on
        the survivor -- not recreate the duplicate (setlist.fm re-links every song on refresh)."""
        merge.merge_songs(self.conn, 101, 100)
        self.conn.commit()
        n_songs = self.conn.execute("SELECT count(*) FROM songs").fetchone()[0]
        self.assertEqual(get_or_create_song(self.conn, {}, 1, "Paranoid - 2009 Remaster", source="lastfm"), 100)
        self.assertEqual(get_or_create_song(self.conn, {}, 1, "Paranoid", source="setlistfm"), 100)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM songs").fetchone()[0], n_songs)

    def test_album_rename_routes_old_spelling(self):
        merge.edit_entity(self.conn, "album", 11, {"title": "Paranoid (1970 Mix)"}, "rename")
        self.conn.commit()
        n = self.conn.execute("SELECT count(*) FROM albums").fetchone()[0]
        # the old title and the raw texts its scrobbles/vinyl carried both still resolve to album 11
        self.assertEqual(get_or_create_album(self.conn, {}, [1], "Paranoid (Remastered)", source="lastfm"), 11)
        self.assertEqual(get_or_create_album(self.conn, {}, [1], "Paranoid (Remastered)", source="discogs"), 11)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM albums").fetchone()[0], n)

    def test_merge_rename_routes_old_title_and_undo_removes_aliases(self):
        before = checksum(self.conn)
        r = merge.merge_albums(self.conn, 10, 11, identity={"title": "Paranoid"})  # survivor 11 loses "(Remastered)"
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT title FROM albums WHERE id = 11").fetchone()[0], "Paranoid")
        self.assertEqual(get_or_create_album(self.conn, {}, [1], "Paranoid (Remastered)", source="lastfm"), 11)
        merge.undo_merge(self.conn, r["logId"])
        self.conn.commit()
        self.assertEqual(checksum(self.conn), before)  # includes alias_overrides

    def test_cover_relink_is_performer_scoped_and_undoable(self):
        """Setlist 1 (performer 2) played song 101 (artist 1's). Re-linking to performer 2's own
        song 102 moves only that performer's plays, adds a performer-scoped alias, and undoes."""
        before = checksum(self.conn)
        edit_id = merge.relink_live(self.conn, 101, 102, 2, "test")
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT song_id FROM setlist_songs WHERE id = 1").fetchone()[0], 102)
        self.assertEqual(self.conn.execute("SELECT canonical_id FROM alias_overrides WHERE source_key = 'cover:2:paranoid'").fetchone()[0], 102)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM scrobbles WHERE song_id = 101").fetchone()[0], 1)  # original untouched
        merge.undo_edit(self.conn, edit_id)
        self.conn.commit()
        self.assertEqual(checksum(self.conn), before)

    def test_relink_to_new_song_undo_removes_it(self):
        before = checksum(self.conn)
        new_id = self.conn.execute("INSERT INTO songs (artist_id, album_id, title) VALUES (2, 12, 'Paranoid')").lastrowid
        edit_id = merge.relink_live(self.conn, 101, new_id, 2, "test", created_song=True)
        self.conn.commit()
        merge.undo_edit(self.conn, edit_id)
        self.conn.commit()
        self.assertIsNone(self.conn.execute("SELECT 1 FROM songs WHERE id = ?", (new_id,)).fetchone())
        self.assertEqual(checksum(self.conn), before)

    def test_also_releases_as_routes_imports_and_follows_merges(self):
        exp = "eeeeeeee-0000-0000-0000-00000000000e"
        self.conn.execute("INSERT INTO artist_mb_aliases (artist_id, mbid, name) VALUES (2, ?, 'The Experience')", (exp,))
        self.conn.commit()
        # an import carrying the alias's mbid lands on the local artist, whatever the spelling
        self.assertEqual(get_or_create_artist(self.conn, {}, "The Jimi Hendrix Experience", mbid=exp, source="lastfm"), 2)
        self.assertEqual(artist_mbid_sets(self.conn, [2])[2], {exp})
        # merging the artist carries its aliases to the survivor, and undo puts them back
        before = checksum(self.conn)
        r = merge.merge_artists(self.conn, 2, 1)
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT artist_id FROM artist_mb_aliases WHERE mbid = ?", (exp,)).fetchone()[0], 1)
        self.assertIn(exp, artist_mbid_sets(self.conn, [1])[1])
        merge.undo_merge(self.conn, r["logId"])
        self.conn.commit()
        self.assertEqual(checksum(self.conn), before)

    def test_edit_mbid_conflict(self):
        with self.assertRaises(merge.MergeError) as ctx:
            merge.edit_entity(self.conn, "album", 12, {"mbid": "bbbbbbbb-0000-0000-0000-000000000010"}, "test")
        self.assertEqual(ctx.exception.code, "mbid_conflict")


if __name__ == "__main__":
    unittest.main()
