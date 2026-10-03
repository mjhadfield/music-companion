"""A Discogs collection import against a library that already has records (etl/discogs_import.py):
new records go in and say which album they were filed under; changes to records you have are raised
in the Inbox -- never written -- once; records missing from the export are flagged, never deleted;
applying a change is journaled and undoes exactly. Throwaway database and CSVs."""
import csv
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
import discogs_import  # noqa: E402
from api import imports  # noqa: E402
from api.core import Req  # noqa: E402
from common import DB_PATH, connect  # noqa: E402
from migrations import migrate  # noqa: E402
from tests.test_batch import checksum  # noqa: E402

HEADER = ["Catalog#", "Artist", "Title", "Label", "Format", "Rating", "Released", "release_id", "CollectionFolder", "Date Added",
          "Collection Media Condition", "Collection Sleeve Condition", "Collection Notes"]
STAR_WARS = ["BTD 541", "John Williams (4), London Symphony Orchestra", "Star Wars", "20th Century Records", "2xLP, Album, Gat", "5", "1977",
             "1010548", "Uncategorized", "2026-09-05 07:56:29", "Near Mint (NM or M-)", "Very Good Plus (VG+)", "No poster."]
DESTROYER = ["NBLP 7025", "Kiss", "Destroyer", "Casablanca", "LP, Album", "", "1976", "2000", "Uncategorized", "2026-09-05 08:00:00",
             "Very Good Plus (VG+)", "Very Good (VG)", ""]
DETONATOR = ["7 91342-1", "Ratt", "Detonator", "Atlantic", "LP, Album", "0", "1990", "6645593", "Uncategorized", "2026-10-03 20:00:00",
             "Near Mint (NM or M-)", "Near Mint (NM or M-)", ""]


class DiscogsImportTests(unittest.TestCase):
    def setUp(self):
        if DB_PATH.exists():
            DB_PATH.unlink()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        conn.commit()
        conn.close()
        self.dir = Path(tempfile.mkdtemp())
        discogs_import.import_csv(self._csv("first", [STAR_WARS, DESTROYER]))
        self.conn = connect()
        self.conn.execute("UPDATE import_events SET reviewed_at = datetime('now')")   # the first import is settled
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _csv(self, name, rows):
        path = self.dir / f"{name}.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            w.writerows(rows)
        return path

    def _open(self, kind=None):
        sql = "SELECT kind, entity_id, json_extract(detail_json, '$.title') FROM import_events WHERE reviewed_at IS NULL"
        return [r for r in self.conn.execute(sql) if kind is None or r[0] == kind]

    def test_a_new_record_goes_in_and_says_where_it_was_filed(self):
        discogs_import.import_csv(self._csv("second", [STAR_WARS, DESTROYER, DETONATOR]))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vinyl_holdings").fetchone()[0], 3)
        self.assertIsNone(self.conn.execute("SELECT rating FROM vinyl_holdings WHERE discogs_release_id = 6645593").fetchone()[0])  # Discogs' 0 = not rated
        self.assertEqual({k for k, *_ in self._open()}, {"new_holding", "new_album", "new_artist"})

    def test_a_change_on_discogs_is_raised_not_written_and_only_once(self):
        regraded = list(STAR_WARS)
        regraded[11], regraded[5] = "Very Good (VG)", "4"
        before = checksum(self.conn, ["vinyl_holdings"])
        discogs_import.import_csv(self._csv("second", [regraded, DESTROYER]))
        self.assertEqual(checksum(self.conn, ["vinyl_holdings"]), before)                       # nothing overwritten
        [(kind, hid, title)] = self._open()
        self.assertEqual((kind, title), ("holding_changed", "Star Wars"))
        discogs_import.import_csv(self._csv("third", [regraded, DESTROYER]))
        self.assertEqual(len(self._open()), 1)                                                   # not raised again
        # applied: the record takes the export's values; undo puts it back exactly
        event_id = self.conn.execute("SELECT id FROM import_events WHERE reviewed_at IS NULL").fetchone()[0]
        out = imports.apply_holding(Req({}, {"eventId": event_id}))
        self.assertEqual(set(out["applied"]), {"sleeve_condition", "rating"})
        row = self.conn.execute("SELECT sleeve_condition, rating FROM vinyl_holdings WHERE id = ?", (hid,)).fetchone()
        self.assertEqual(row, ("Very Good (VG)", 4))
        from api.general import undo
        undo(Req({}, {"kind": "batch", "items": out["undo"]["id"]}))
        self.assertEqual(checksum(self.conn, ["vinyl_holdings"]), before)
        self.assertEqual(len(self._open()), 1)                                                   # and the item is open again

    def test_a_kept_change_is_not_raised_again_and_one_that_reverts_settles(self):
        regraded = list(DESTROYER)
        regraded[12] = "Bought new."
        discogs_import.import_csv(self._csv("second", [STAR_WARS, regraded]))
        self.conn.execute("UPDATE import_events SET reviewed_at = datetime('now') WHERE kind = 'holding_changed'")  # "Keep mine"
        self.conn.commit()
        discogs_import.import_csv(self._csv("third", [STAR_WARS, regraded]))
        self.assertEqual(self._open(), [])
        other = list(DESTROYER)
        other[12] = "Something else."
        discogs_import.import_csv(self._csv("fourth", [STAR_WARS, other]))
        self.assertEqual(len(self._open("holding_changed")), 1)                                  # a different change is new
        discogs_import.import_csv(self._csv("fifth", [STAR_WARS, DESTROYER]))
        self.assertEqual(self._open("holding_changed"), [])                                      # back as it was: settled

    def test_a_record_missing_from_the_export_is_flagged_never_deleted(self):
        discogs_import.import_csv(self._csv("second", [STAR_WARS]))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vinyl_holdings").fetchone()[0], 2)
        self.assertEqual([(k, t) for k, _, t in self._open()], [("holding_missing", "Destroyer")])
        discogs_import.import_csv(self._csv("third", [STAR_WARS, DESTROYER]))
        self.assertEqual(self._open(), [])                                                       # back in the export: settled

    def test_a_pressing_set_by_hand_is_journaled_and_undoes(self):
        from api import vinyl
        from api.general import undo
        hid = self.conn.execute("SELECT id FROM vinyl_holdings WHERE discogs_release_id = 2000").fetchone()[0]
        before = checksum(self.conn, ["vinyl_holdings"])
        out = vinyl.set_pressing(Req({}, {"vinylId": hid, "kind": "repress", "year": ""}))
        self.assertEqual(self.conn.execute("SELECT press_kind, press_year FROM vinyl_holdings WHERE id = ?", (hid,)).fetchone(), ("repress", None))
        with self.assertRaises(Exception):
            vinyl.set_pressing(Req({}, {"vinylId": hid, "kind": "bootleg"}))
        undo(Req({}, {"kind": "edit", "id": out["editId"]}))
        self.assertEqual(checksum(self.conn, ["vinyl_holdings"]), before)


if __name__ == "__main__":
    unittest.main()
