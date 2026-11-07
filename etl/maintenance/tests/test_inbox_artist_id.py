"""Inbox › Artists "same name, different MusicBrainz artist" checks: repeats shown once, the evidence
(which plays / gigs carried the other id), the two explicit decisions (keep mine / use theirs) and that
imports stop raising a decided id. Plus Inbox › Songs merging either way. Throwaway database, no network."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("MUSIC_DB_PATH", str(Path(tempfile.mkdtemp()) / "test.sqlite"))  # before common is imported

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))
import common  # noqa: E402
import mbcache  # noqa: E402
from api import general, imports  # noqa: E402
from api.core import ApiError, Req  # noqa: E402
from common import connect, finish_import_run, get_or_create_artist, start_import_run  # noqa: E402
from migrations import migrate  # noqa: E402

MINE = "aaaaaaaa-0000-0000-0000-000000000001"     # Sion, the band
DJ = "aaaaaaaa-0000-0000-0000-000000000002"       # Sion, a UK DJ (what Last.fm sends)
GENX_WRONG = "aaaaaaaa-0000-0000-0000-000000000003"
GENX_RIGHT = "aaaaaaaa-0000-0000-0000-000000000004"
MB_ARTISTS = {MINE: {"mbid": MINE, "name": "Sion", "disambiguation": "US metalcore band", "type": "Group", "country": "US"},
              DJ: {"mbid": DJ, "name": "Sion", "disambiguation": "DJ and producer based in UK", "type": "Person", "country": "GB"}}


def lastfm_row(artist, mbid, track, album, uts):
    return json.dumps({"artist": {"#text": artist, "mbid": mbid}, "name": track, "album": {"#text": album}, "date": {"uts": str(uts)}})


class ArtistIdCheckTests(unittest.TestCase):
    def setUp(self):
        if common.DB_PATH.exists():
            common.DB_PATH.unlink()
        conn = sqlite3.connect(common.DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        conn.executescript(f"""
            INSERT INTO artists (id, mbid, name) VALUES (1, '{MINE}', 'Sion'), (2, '{GENX_WRONG}', 'Generation X'), (3, NULL, 'Cradle of Filth');
            INSERT INTO albums (id, artist_id, title) VALUES (10, 3, 'Cruelty and the Beast');
            INSERT INTO songs (id, artist_id, album_id, title, mbid) VALUES (100, 3, 10, 'Bathory Aria - Remixed and Remastered', NULL);
            INSERT INTO scrobbles (song_id, artist_id, album_id, played_at, raw_artist_text, raw_track_text) VALUES
                (NULL, 1, NULL, '2026-10-01T10:00:00Z', 'Sion', 'The Blade'), (100, 3, 10, '2026-10-02T10:00:00Z', 'Cradle of Filth', 'Bathory Aria');
        """)
        # the same play seen by two full re-pulls -- shown once
        conn.executemany("INSERT INTO staging_lastfm_rows (raw_json) VALUES (?)",
                         [(lastfm_row("Sion", DJ, "The Blade", "The Blade", 1616425000),), (lastfm_row("Sion", DJ, "The Blade", "The Blade", 1616425000),),
                          (lastfm_row("Sion", DJ, "Drown", "SION", 1700000000),), (lastfm_row("Sion", MINE, "Skyfall", "SION", 1700000500),)])
        conn.execute("INSERT INTO staging_setlistfm_rows (raw_json) VALUES (?)", (json.dumps({
            "id": "s1", "eventDate": "14-06-2015", "artist": {"name": "Billy Idol", "mbid": "x"},
            "venue": {"name": "Donington Park", "city": {"name": "Castle Donington"}}, "url": "https://www.setlist.fm/s1",
            "sets": {"set": [{"song": [{"name": "Dancing With Myself", "cover": {"name": "Generation X", "mbid": GENX_RIGHT, "disambiguation": "English post-punk band"}},
                                       {"name": "White Wedding"}]}]}}),))
        conn.commit()
        conn.close()
        self.conn = connect()

    def tearDown(self):
        common._import_run["id"] = None
        self.conn.close()

    def _import(self, name, mbid, source="lastfm"):
        start_import_run(self.conn, source)
        get_or_create_artist(self.conn, {}, name, mbid=mbid, source=source)
        finish_import_run(self.conn, {})
        self.conn.commit()

    def _open(self):
        return self.conn.execute("SELECT id, entity_id, detail_json FROM import_events WHERE reviewed_at IS NULL AND kind = 'suspect'").fetchall()

    def _items(self):
        return [it for it in imports.inbox(Req({"tab": "artists"}, {}))["items"] if it["kind"] == "suspect"]

    def test_repeat_check_is_not_stacked_and_is_listed_once(self):
        self._import("Sion", DJ)
        self._import("Sion", DJ)
        self.assertEqual(len(self._open()), 1)
        # copies left by older imports (before the guard) collapse into one item that settles them all
        self.conn.execute("INSERT INTO import_events (run_id, kind, entity_type, entity_id, detail_json) SELECT run_id, kind, entity_type, entity_id, detail_json FROM import_events")
        self.conn.commit()
        (it,) = self._items()
        self.assertEqual(len(it["sameIds"]), 2)
        self.assertEqual(imports.inbox(Req({"tab": "artists"}, {}))["pending"]["artists"], 1)

    def test_evidence_lists_plays_and_gigs_that_carried_the_id(self):
        self._import("Sion", DJ)
        (it,) = self._items()
        with mock.patch.object(mbcache, "artist", side_effect=lambda m: MB_ARTISTS.get(m)):
            ev = imports.artist_id_evidence(Req({"eventId": str(it["id"])}, {}))
        self.assertEqual(ev["theirs"]["disambiguation"], "DJ and producer based in UK")
        self.assertEqual(ev["yours"]["disambiguation"], "US metalcore band")
        self.assertEqual([p["track"] for p in ev["plays"]], ["Drown", "The Blade"])   # deduped, newest first, not MINE's row
        self.assertEqual(ev["yourPlays"], 1)

        self._import("Generation X", GENX_RIGHT, source="setlistfm")
        genx = next(i for i in self._items() if i["entityId"] == 2)
        with mock.patch.object(mbcache, "artist", return_value=None):
            ev = imports.artist_id_evidence(Req({"eventId": str(genx["id"])}, {}))
        (gig,) = ev["gigs"]
        self.assertEqual((gig["date"], gig["headliner"], gig["songs"], gig["setlistfmSays"]),
                         ("2015-06-14", "Billy Idol", ["Dancing With Myself"], "English post-punk band"))

    def test_keep_mine_settles_every_copy_and_stops_the_flag(self):
        self._import("Sion", DJ)
        self.conn.execute("INSERT INTO import_events (run_id, kind, entity_type, entity_id, detail_json) SELECT run_id, kind, entity_type, entity_id, detail_json FROM import_events")
        self.conn.commit()
        (it,) = self._items()
        out = imports.decide_artist_id(Req({}, {"eventId": it["id"], "decision": "keep"}))
        self.assertEqual(self._open(), [])
        self.assertEqual(self.conn.execute("SELECT mbid FROM artists WHERE id = 1").fetchone()[0], MINE)
        self._import("Sion", DJ)
        self.assertEqual(self._open(), [], "a decided id isn't raised again")
        with self.assertRaises(ApiError):
            imports.decide_artist_id(Req({}, {"eventId": it["id"], "decision": "keep"}))
        general.undo(Req({}, {"kind": "batch", "items": out["undo"]["id"]}))
        self.assertEqual(len(self._open()), 2)
        self.assertFalse(common.is_wrong_artist_mbid(self.conn, 1, DJ))

    def test_use_theirs_switches_the_id_and_rules_out_the_old_one(self):
        self._import("Generation X", GENX_RIGHT, source="setlistfm")
        (it,) = self._items()
        out = imports.decide_artist_id(Req({}, {"eventId": it["id"], "decision": "switch"}))
        self.assertEqual(self.conn.execute("SELECT mbid FROM artists WHERE id = 2").fetchone()[0], GENX_RIGHT)
        self._import("Generation X", GENX_WRONG)   # Last.fm still sends the old id
        self._import("Generation X", GENX_RIGHT, source="setlistfm")
        self.assertEqual(self._open(), [])
        general.undo(Req({}, {"kind": "batch", "items": out["undo"]["id"]}))
        self.assertEqual(self.conn.execute("SELECT mbid FROM artists WHERE id = 2").fetchone()[0], GENX_WRONG)
        self.assertEqual(len(self._open()), 1)

    def test_merge_either_way_keeps_the_new_song(self):
        start_import_run(self.conn, "lastfm")
        new_id = common.get_or_create_song(self.conn, {}, 3, "Bathory Aria", album_id=10, mbid="cccccccc-0000-0000-0000-000000000001", source="lastfm")
        finish_import_run(self.conn, {})
        self.conn.commit()
        it = next(i for i in imports.inbox(Req({"tab": "songs"}, {}))["items"] if i["entityId"] == new_id)
        self.assertEqual([h["songId"] for h in it["hints"]], [100])
        out = imports.merge_into(Req({}, {"eventId": it["id"], "otherId": 100}))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM songs WHERE id = 100").fetchone())
        self.assertEqual(self.conn.execute("SELECT title, mbid FROM songs WHERE id = ?", (new_id,)).fetchone(),
                         ("Bathory Aria", "cccccccc-0000-0000-0000-000000000001"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM scrobbles WHERE song_id = ?", (new_id,)).fetchone()[0], 1)
        self.assertIsNotNone(self.conn.execute("SELECT reviewed_at FROM import_events WHERE id = ?", (it["id"],)).fetchone()[0])
        general.undo(Req({}, {"kind": "batch", "items": out["undo"]["id"]}))
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM songs WHERE id = 100").fetchone())


if __name__ == "__main__":
    unittest.main()
