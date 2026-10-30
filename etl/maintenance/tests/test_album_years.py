"""Album years from MusicBrainz (album_years.py, batch action yearFill): only a missing year is filled,
from the cached first-release date (an edition id followed to its group), and the fill undoes as a
whole. Throwaway database from schema.sql."""
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
import album_years  # noqa: E402
from api import batch  # noqa: E402
from api.core import Req  # noqa: E402
from common import DB_PATH, connect  # noqa: E402
from migrations import migrate  # noqa: E402
from tests.test_batch import checksum  # noqa: E402

ARTIST_MBID = "39a31de6-0000-0000-0000-000000000001"


class AlbumYearTests(unittest.TestCase):
    def setUp(self):
        if DB_PATH.exists():
            DB_PATH.unlink()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        conn.execute("INSERT INTO artists (id, name, mbid) VALUES (1, 'Band', ?)", (ARTIST_MBID,))
        conn.executemany("INSERT INTO albums (id, artist_id, title, mbid, year) VALUES (?, 1, ?, ?, ?)", [
            (1, "Primo Victoria", "rg-primo", None),        # browse has it: 2005
            (2, "Coat of Arms", "rg-coat", 2011),           # has a year: never touched
            (3, "Magma", "rel-magma", None),                # an edition id: its group is rg-magma (2016)
            (4, "Unknown", "rg-unknown", None),             # MusicBrainz has no date: nothing
            (5, "No id", None, None)])
        conn.executemany("INSERT INTO album_artists VALUES (?, 1, 0)", [(i,) for i in range(1, 6)])
        browse = [{"mbid": "rg-primo", "title": "Primo Victoria", "primaryType": "Album", "secondaryTypes": [], "firstReleaseDate": "2005-03-04"},
                  {"mbid": "rg-coat", "title": "Coat of Arms", "primaryType": "Album", "secondaryTypes": [], "firstReleaseDate": "2010-05-21"},
                  {"mbid": "rg-unknown", "title": "Unknown", "primaryType": "Album", "secondaryTypes": [], "firstReleaseDate": None}]
        conn.executemany("INSERT INTO mb_cache (key, payload_json, fetched_at) VALUES (?, ?, datetime('now'))", [
            (f"browse:release-groups:{ARTIST_MBID}", json.dumps(browse)),
            ("resolve:release-group:rel-magma", json.dumps("rg-magma")),
            ("lookup:release-group:rg-magma", json.dumps({"mbid": "rg-magma", "title": "Magma", "primaryType": "Album", "secondaryTypes": [], "firstReleaseDate": "2016-06-17"}))])
        conn.commit()
        conn.close()
        self.conn = connect()

    def tearDown(self):
        self.conn.close()

    def test_plan_fills_only_missing_years(self):
        self.assertEqual({r["albumId"]: r["year"] for r in album_years.plan(self.conn)}, {1: 2005, 3: 2016})
        p = album_years.preview(self.conn)
        self.assertEqual((p["yearless"], p["count"]), (3, 2))

    def test_fill_applies_and_undoes(self):
        before = checksum(self.conn)
        out = batch.apply(Req({}, {"actions": [{"type": "yearFill"}]}))
        self.assertTrue(out["snapshot"])                                    # a fill takes a named snapshot first --
        Path(out["snapshot"]).unlink()                                      # not left in the shared temp folder (Housekeeping lists it)
        years = dict(self.conn.execute("SELECT id, year FROM albums").fetchall())
        self.assertEqual((years[1], years[2], years[3], years[4]), (2005, 2011, 2016, None))
        batch.undo_batch(out["batchId"])
        self.assertEqual(checksum(self.conn), before)

    def test_lookup_artists_skips_browsed(self):
        self.assertEqual(album_years.lookup_artists(self.conn), [])          # already browsed
        self.conn.execute("DELETE FROM mb_cache WHERE key LIKE 'browse:%'")
        self.conn.commit()
        self.assertEqual(album_years.lookup_artists(self.conn), [(ARTIST_MBID, "Band")])


if __name__ == "__main__":
    unittest.main()
