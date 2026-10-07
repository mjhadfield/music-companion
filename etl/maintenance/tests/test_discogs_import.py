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

    def test_accept_ticked_applies_a_discogs_change_rather_than_discarding_it(self):
        # what went wrong on 3 Oct: ticking everything and "Accept ticked" marked Discogs changes reviewed without applying them
        regraded = list(STAR_WARS)
        regraded[5] = "4"
        discogs_import.import_csv(self._csv("second", [regraded, DESTROYER, DETONATOR]))
        before = checksum(self.conn, ["vinyl_holdings"])
        ids = [r[0] for r in self.conn.execute("SELECT id FROM import_events WHERE reviewed_at IS NULL")]
        out = imports.accept(Req({}, {"ids": ids}))
        self.assertEqual((out["reviewed"], out["applied"]), (len(ids), 1))
        self.assertEqual(self.conn.execute("SELECT rating FROM vinyl_holdings WHERE discogs_release_id = 1010548").fetchone()[0], 4)
        self.assertEqual(self._open(), [])
        from api.general import undo
        undo(Req({}, {"kind": "batch", "items": out["undo"]["id"]}))
        self.assertEqual(self.conn.execute("SELECT rating FROM vinyl_holdings WHERE discogs_release_id = 1010548").fetchone()[0], 5)
        self.assertEqual(len(self._open()), len(ids))                                          # all open again
        self.assertEqual(checksum(self.conn, ["vinyl_holdings"]), before)                      # undo puts every record back exactly

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


# ---- the same import straight from Discogs (--api): recorded API shapes, no network ----------------------

FIELDS = {"fields": [{"id": 7, "name": "Notes"}, {"id": 1, "name": "Media Condition"}, {"id": 2, "name": "Sleeve Condition"}]}  # ids are yours, names fixed


def instance(release_id, artists, title, *, labels, formats, rating, added, media, sleeve, notes=""):
    return {"id": release_id, "instance_id": release_id * 10, "rating": rating, "date_added": added, "folder_id": 1,
            "notes": [{"field_id": 1, "value": media}, {"field_id": 2, "value": sleeve}] + ([{"field_id": 7, "value": notes}] if notes else []),
            "basic_information": {"id": release_id, "title": title, "year": 1977, "artists": [{"name": a} for a in artists],
                                  "labels": labels, "formats": formats}}


# Star Wars and Destroyer as the API describes them: formats spelled out, times in Discogs' own (Pacific) zone, as the export
API_STAR_WARS = instance(1010548, ["John Williams (4)", "London Symphony Orchestra"], "Star Wars",
                         labels=[{"name": "20th Century Records", "catno": "BTD 541"}],
                         formats=[{"name": "Vinyl", "qty": "2", "descriptions": ["LP", "Album", "Stereo"], "text": "Gatefold"}],
                         rating=5, added="2026-09-05T07:56:29-07:00", media="Near Mint (NM or M-)", sleeve="Very Good Plus (VG+)", notes="No poster.")
API_DESTROYER = instance(2000, ["Kiss"], "Destroyer", labels=[{"name": "Casablanca Records", "catno": "NBLP 7025"}],  # label spelled differently: not compared
                         formats=[{"name": "Vinyl", "qty": "1", "descriptions": ["LP", "Album", "Reissue"], "text": None}],
                         rating=0, added="2026-09-05T08:00:00-07:00", media="Very Good Plus (VG+)", sleeve="Very Good (VG)")
API_DETONATOR = instance(6645593, ["Ratt"], "Detonator", labels=[{"name": "Atlantic", "catno": "7 91342-1"}],
                         formats=[{"name": "Vinyl", "qty": "1", "descriptions": ["LP", "Album", "Limited Edition"], "text": "Red Translucent"}],
                         rating=0, added="2026-10-03T20:00:00-07:00", media="Near Mint (NM or M-)", sleeve="Near Mint (NM or M-)")


class DiscogsApiSyncTests(unittest.TestCase):
    # the same throwaway library as the CSV tests
    setUp = DiscogsImportTests.setUp
    tearDown = DiscogsImportTests.tearDown
    _csv = DiscogsImportTests._csv
    _open = DiscogsImportTests._open

    def _sync(self, instances, dry_run=False):
        from unittest import mock
        import discogs_api
        with mock.patch.object(discogs_api, "identity", return_value="mike"), \
                mock.patch.object(discogs_api, "collection_fields", return_value={f["id"]: f["name"] for f in FIELDS["fields"]}), \
                mock.patch.object(discogs_api, "collection_instances", return_value=instances):
            discogs_import.import_api(dry_run=dry_run)

    def test_the_same_collection_from_the_api_changes_nothing(self):
        before = checksum(self.conn, ["vinyl_holdings"])
        self._sync([API_STAR_WARS, API_DESTROYER])
        self.assertEqual(checksum(self.conn, ["vinyl_holdings"]), before)
        self.assertEqual(self._open(), [])        # spelled-out formats, another label spelling, Discogs' own times: no false "changed"

    def test_a_regrade_on_discogs_is_raised_with_only_that_field(self):
        regraded = dict(API_STAR_WARS, notes=[{"field_id": 1, "value": "Near Mint (NM or M-)"}, {"field_id": 2, "value": "Very Good (VG)"},
                                              {"field_id": 7, "value": "No poster."}])
        self._sync([regraded, API_DESTROYER])
        [(kind, hid, title)] = self._open()
        self.assertEqual((kind, title), ("holding_changed", "Star Wars"))
        detail = self.conn.execute("SELECT json_extract(detail_json, '$.changes') FROM import_events WHERE kind = 'holding_changed'").fetchone()[0]
        self.assertEqual(set(__import__("json").loads(detail)), {"sleeve_condition"})

    def test_a_new_record_goes_in_with_an_export_style_format(self):
        self._sync([API_STAR_WARS, API_DESTROYER, API_DETONATOR])
        row = self.conn.execute("SELECT format, date_added, rating, label, catalog_number FROM vinyl_holdings WHERE discogs_release_id = 6645593").fetchone()
        self.assertEqual(row, ("LP, Album, Ltd, Red", "2026-10-03 20:00:00", None, "Atlantic", "7 91342-1"))
        self.assertEqual({k for k, *_ in self._open()}, {"new_holding", "new_album", "new_artist"})

    def test_a_record_gone_from_the_collection_is_flagged_never_deleted(self):
        self._sync([API_STAR_WARS])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vinyl_holdings").fetchone()[0], 2)
        self.assertEqual([(k, t) for k, _, t in self._open()], [("holding_missing", "Destroyer")])

    def test_a_dry_run_writes_nothing(self):
        tables = ["vinyl_holdings", "albums", "artists", "import_events", "import_runs", "staging_discogs_rows"]
        before = checksum(self.conn, tables)
        regraded = dict(API_DESTROYER, rating=3)
        self._sync([API_STAR_WARS, regraded, API_DETONATOR], dry_run=True)
        self.assertEqual(checksum(self.conn, tables), before)


class CsvStyleFormatTests(unittest.TestCase):
    def test_formats_read_like_the_export(self):
        f = discogs_import.csv_style_format
        self.assertEqual(f([{"name": "Vinyl", "qty": "2", "descriptions": ["LP", "Album", "Stereo"], "text": "Gatefold"}]), "2xLP, Album, Gat")
        self.assertEqual(f([{"name": "Vinyl", "qty": "1", "descriptions": ["LP", "Album", "Reissue", "Stereo"], "text": "Black Labels, EMI Pressing"}]),
                         "LP, Album, RE, Bla")
        self.assertEqual(f([{"name": "Vinyl", "qty": "2", "descriptions": ["LP", "45 RPM", "Album", "Misprint"], "text": "180gr. "}]), "2xLP, Album, M/Print, 180")
        self.assertEqual(f([{"name": "Vinyl", "qty": "1", "descriptions": ["LP", "Album"], "text": None},
                            {"name": "All Media", "qty": "1", "descriptions": ["Deluxe Edition"], "text": None}]), "LP, Album + Dlx")
        self.assertEqual(f([{"name": "Box Set", "qty": "1", "descriptions": ["Compilation"], "text": None}]), "Box, Comp")


if __name__ == "__main__":
    unittest.main()
