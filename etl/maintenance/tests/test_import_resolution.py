"""Import-time identity resolution: a Last.fm release id (one edition) must land on the album
(release group) exactly, never become an album of its own, and anything new or doubtful must be
recorded for the Import inbox. Runs against a throwaway database built from schema.sql, with
MusicBrainz and Last.fm stubbed out -- no network."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("MUSIC_DB_PATH", str(Path(tempfile.mkdtemp()) / "test.sqlite"))  # before common is imported

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))
import common  # noqa: E402
import lastfm_pull  # noqa: E402
import releases  # noqa: E402
from common import (connect, finish_import_run, get_or_create_album, get_or_create_artist, get_or_create_song,  # noqa: E402
                    start_import_run)
from migrations import migrate  # noqa: E402

SABBATH = "aaaaaaaa-0000-0000-0000-000000000001"
OTHER_ARTIST = "aaaaaaaa-0000-0000-0000-000000000099"
PARANOID_RG = "bbbbbbbb-0000-0000-0000-000000000010"
MASTER_RG = "bbbbbbbb-0000-0000-0000-000000000012"
VOL4_RG = "bbbbbbbb-0000-0000-0000-000000000014"
REL = {n: f"dddddddd-0000-0000-0000-{n:012d}" for n in range(1, 10)}
TRK_WARPIGS = "ffffffff-0000-0000-0000-00000000000a"
REC_WARPIGS = "eeeeeeee-0000-0000-0000-00000000000a"
REC_CARAVAN = "eeeeeeee-0000-0000-0000-00000000000c"
TRK_UNSORTED = "ffffffff-0000-0000-0000-00000000000f"


def cache_release(conn, release, rg, artists=(SABBATH,), title="x"):
    conn.execute("INSERT INTO mb_cache (key, payload_json, fetched_at) VALUES (?, ?, datetime('now'))",
                 (common.RESOLVE_KEY.format(release), json.dumps(rg)))
    if rg:
        conn.execute("INSERT INTO mb_cache (key, payload_json, fetched_at) VALUES (?, ?, datetime('now'))",
                     (common.RELEASE_PARENT_KEY.format(release), json.dumps({"mbid": rg, "title": title, "artistMbids": list(artists)})))


class ImportResolutionTests(unittest.TestCase):
    def setUp(self):
        if common.DB_PATH.exists():
            common.DB_PATH.unlink()
        conn = sqlite3.connect(common.DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        conn.executescript(f"""
            INSERT INTO artists (id, mbid, name) VALUES (1, '{SABBATH}', 'Black Sabbath'), (2, NULL, 'Nobody Yet');
            INSERT INTO albums (id, mbid, artist_id, title) VALUES (10, '{PARANOID_RG}', 1, 'Paranoid'),
                (12, NULL, 1, 'Master of Reality'), (13, '{REL[9]}', 1, 'Sabotage');
            INSERT INTO album_artists VALUES (10, 1, 0), (12, 1, 0), (13, 1, 0);
            INSERT INTO album_releases (release_mbid, album_id, source) VALUES ('{REL[1]}', 10, 'lastfm');
            INSERT INTO songs (id, mbid, artist_id, album_id, title) VALUES (100, 'cccccccc-0000-0000-0000-000000000100', 1, 10, 'Paranoid');
        """)
        conn.commit()
        conn.close()
        self.conn = connect()
        self.run_id = start_import_run(self.conn, "lastfm")
        self.cache = {}

    def tearDown(self):
        common._import_run["id"] = None
        self.conn.close()

    def album(self, title, release):
        return get_or_create_album(self.conn, self.cache, [1], title, release_mbid=release, source="lastfm")

    def events(self, kind=None):
        rows = self.conn.execute("SELECT kind, entity_id, detail_json, reviewed_at FROM import_events WHERE run_id = ?"
                                 + (" AND kind = ?" if kind else ""), (self.run_id, kind) if kind else (self.run_id,)).fetchall()
        return [(k, e, json.loads(d), r) for k, e, d, r in rows]

    def album_count(self):
        return self.conn.execute("SELECT count(*) FROM albums").fetchone()[0]

    def test_known_edition(self):
        self.assertEqual(self.album("Paranoid (Remastered)", REL[1]), 10)
        self.assertEqual(self.events(), [])

    def test_legacy_release_id_as_album_mbid(self):
        self.assertEqual(self.album("Sabotage", REL[9]), 13)
        self.assertEqual(self.conn.execute("SELECT album_id FROM album_releases WHERE release_mbid = ?", (REL[9],)).fetchone()[0], 13)

    def test_new_edition_of_existing_album_links_exactly(self):
        cache_release(self.conn, REL[2], PARANOID_RG)
        self.assertEqual(self.album("Paranoid (Deluxe Edition)", REL[2]), 10)  # different title, same release group
        self.assertEqual(self.album_count(), 3)
        (ev,) = self.events()
        self.assertEqual((ev[0], ev[2]["how"]), ("edition_linked", "release group"))
        self.assertIsNotNone(ev[3], "an exact link is informational, not for review")

    def test_two_editions_one_album(self):
        cache_release(self.conn, REL[3], VOL4_RG)
        cache_release(self.conn, REL[4], VOL4_RG)
        a = self.album("Vol. 4", REL[3])
        b = self.album("Vol 4 (2021 Remaster)", REL[4])
        self.assertEqual(a, b)
        self.assertEqual(self.conn.execute("SELECT mbid FROM albums WHERE id = ?", (a,)).fetchone()[0], VOL4_RG,
                         "a new album is identified by its release group, never a release")
        self.assertEqual([e[0] for e in self.events()], ["new_album", "edition_linked"])

    def test_title_match_takes_group_and_is_flagged(self):
        cache_release(self.conn, REL[5], MASTER_RG)
        self.assertEqual(self.album("Master of Reality", REL[5]), 12)
        self.assertEqual(self.conn.execute("SELECT mbid FROM albums WHERE id = 12").fetchone()[0], MASTER_RG)
        (ev,) = self.events()
        self.assertEqual((ev[0], ev[2]["how"], ev[3]), ("edition_linked", "title", None))

    def test_not_looked_up(self):
        self.assertEqual(self.album("Master of Reality", REL[6]), 12)
        (ev,) = self.events()
        self.assertEqual((ev[0], ev[2]["why"]), ("unresolved_release", "not looked up yet"))
        self.assertIsNone(self.conn.execute("SELECT mbid FROM albums WHERE id = 12").fetchone()[0])
        # next run: it's looked up, lands on the album, and the old inbox item closes itself
        finish_import_run(self.conn, {})
        start_import_run(self.conn, "lastfm")
        self.assertEqual(releases.unknown_releases(self.conn, [REL[6]]), [REL[6]])
        cache_release(self.conn, REL[6], MASTER_RG)
        self.assertEqual(get_or_create_album(self.conn, {}, [1], "Master of Reality", release_mbid=REL[6], source="lastfm"), 12)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM import_events WHERE kind = 'unresolved_release' AND reviewed_at IS NULL").fetchone()[0], 0)

    def test_not_on_musicbrainz(self):
        cache_release(self.conn, REL[6], None)
        self.album("Master of Reality", REL[6])
        self.assertEqual(self.events()[0][2]["why"], "not on MusicBrainz")

    def test_release_credited_to_someone_else(self):
        cache_release(self.conn, REL[7], VOL4_RG, artists=(OTHER_ARTIST,))
        self.assertEqual(self.album("Master of Reality", REL[7]), 12)  # title match still applies
        self.assertIsNone(self.conn.execute("SELECT mbid FROM albums WHERE id = 12").fetchone()[0])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM album_releases WHERE release_mbid = ?", (REL[7],)).fetchone())
        (ev,) = self.events()
        self.assertEqual((ev[0], ev[2]["reason"]), ("suspect", "release-credited-elsewhere"))
        # flagged once ever, not every run
        finish_import_run(self.conn, {})
        start_import_run(self.conn, "lastfm")
        get_or_create_album(self.conn, {}, [1], "Master of Reality", release_mbid=REL[7], source="lastfm")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM import_events WHERE kind = 'suspect'").fetchone()[0], 1)

    def test_various_artists_credit_is_fine(self):
        cache_release(self.conn, REL[8], VOL4_RG, artists=(common.VARIOUS_ARTISTS_MBID,))
        self.album("Some Compilation", REL[8])
        self.assertEqual([e[0] for e in self.events()], ["new_album"])

    def test_song_mbid_clash_is_recorded_once(self):
        # a human routed this title to song 101; Last.fm says its recording is song 100's
        self.conn.execute("INSERT INTO songs (id, artist_id, album_id, title) VALUES (101, 1, 10, 'Paranoid (Live)')")
        self.conn.execute("INSERT INTO alias_overrides (source, source_key, canonical_type, canonical_id) VALUES ('lastfm', '1:paranoid - live', 'song', 101)")
        for _ in range(3):
            self.assertEqual(get_or_create_song(self.conn, {}, 1, "Paranoid - Live", mbid="cccccccc-0000-0000-0000-000000000100", source="lastfm"), 101)
        (ev,) = self.events()
        self.assertEqual((ev[0], ev[1], ev[2]["holderId"]), ("mbid_clash", 101, 100))

    def test_legacy_song_id_routes(self):
        """A song still carrying an unsorted Last.fm id as its mbid keeps receiving that id."""
        self.assertEqual(get_or_create_song(self.conn, {}, 1, "Paranoid (Live)", lastfm_id="cccccccc-0000-0000-0000-000000000100", source="lastfm"), 100)

    def test_known_track_routes_without_lookup(self):
        self.conn.execute("INSERT INTO song_tracks (track_mbid, song_id) VALUES (?, 100)", (TRK_WARPIGS,))
        self.assertEqual(get_or_create_song(self.conn, {}, 1, "Totally Different Title", lastfm_id=TRK_WARPIGS, source="lastfm"), 100)

    def test_new_song(self):
        get_or_create_song(self.conn, {}, 1, "Planet Caravan", album_id=10, source="lastfm")
        self.assertEqual([e[0] for e in self.events()], ["new_song"])

    def test_artist_mbid_differs(self):
        self.assertEqual(get_or_create_artist(self.conn, {}, "Black Sabbath", mbid=OTHER_ARTIST, source="lastfm"), 1)
        (ev,) = self.events()
        self.assertEqual((ev[0], ev[2]["reason"]), ("suspect", "artist-mbid-differs"))

    def test_no_run_no_events(self):
        finish_import_run(self.conn, {})
        get_or_create_artist(self.conn, {}, "Brand New Band", source="discogs")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM import_events").fetchone()[0], 0)


def track(artist, name, album, release, uts, artist_mbid="", mbid=""):
    return {"artist": {"#text": artist, "mbid": artist_mbid}, "name": name, "mbid": mbid,
            "album": {"#text": album, "mbid": release}, "date": {"uts": str(uts)}}


class LastfmPullTests(unittest.TestCase):
    """The whole pull, end to end, with Last.fm and MusicBrainz stubbed."""

    def setUp(self):
        ImportResolutionTests.setUp(self)
        finish_import_run(self.conn, {})
        self.conn.close()

    def test_pull(self):
        now = int(datetime.now(timezone.utc).timestamp())
        page = {"recenttracks": {"@attr": {"totalPages": "1", "total": "6"}, "track": [
            track("Black Sabbath", "War Pigs", "Paranoid (50th Anniversary)", REL[2], now - 100, SABBATH, mbid=TRK_WARPIGS),
            track("Black Sabbath", "War Pigs", "Paranoid (50th Anniversary)", REL[2], now - 50, SABBATH, mbid=TRK_WARPIGS),
            track("Black Sabbath", "Planet Caravan", "Paranoid (50th Anniversary)", REL[2], now - 60, SABBATH, mbid=REC_CARAVAN),
            track("Black Sabbath", "Electric Funeral", "Mystery Comp", "", now - 70, SABBATH, mbid=TRK_UNSORTED),
            track("Black Sabbath", "Iron Man", "Paranoid", REL[1], now - 200, SABBATH),
            track("Black Sabbath", "Supernaut", "Vol. 4", REL[3], now - 300, SABBATH),
            track("Black Sabbath", "Snowblind", "Vol 4 (Remaster)", REL[4], now - 400, SABBATH),
            track("Black Sabbath", "Future", "Paranoid", REL[1], now + 86400 * 30, SABBATH),
            track("Black Sabbath", "Ancient", "Paranoid", REL[1], 86400, SABBATH),
        ]}}
        lookups = []

        def fake_resolve(mbid):
            lookups.append(mbid)
            rg = {REL[2]: PARANOID_RG, REL[3]: VOL4_RG, REL[4]: VOL4_RG}[mbid]
            tracks = {TRK_WARPIGS: REC_WARPIGS, "ffffffff-0000-0000-0000-00000000000c": REC_CARAVAN} if mbid == REL[2] else {}
            return rg, {"mbid": rg, "title": "t", "artistMbids": [SABBATH]}, tracks

        with mock.patch.object(lastfm_pull, "fetch_page", return_value=page), \
                mock.patch.object(lastfm_pull, "load_env"), mock.patch.object(lastfm_pull, "require_env", return_value="x"), \
                mock.patch.object(lastfm_pull, "most_recent_played_at", return_value=None), \
                mock.patch.object(releases.mb, "resolve_release_first", side_effect=fake_resolve), \
                mock.patch.object(releases.mb, "sort_recording_or_track", return_value={"recording": None, "kind": None}) as direct, \
                mock.patch("builtins.print"):
            lastfm_pull.pull(full=False, max_pages=None)

        conn = connect()
        self.assertEqual(sorted(lookups), sorted([REL[2], REL[3], REL[4]]), "known editions are never looked up")
        rows = conn.execute("""SELECT s.raw_track_text, s.album_id, r.release_mbid FROM scrobbles s
                               LEFT JOIN scrobble_releases r ON r.scrobble_id = s.id ORDER BY s.id""").fetchall()
        self.assertEqual(len(rows), 7, "the future- and 1970-dated scrobbles are held back")
        by = {t: (a, r) for t, a, r in rows}
        self.assertEqual(by["War Pigs"], (10, REL[2]))
        self.assertEqual(by["Iron Man"], (10, REL[1]))
        self.assertEqual(by["Supernaut"][0], by["Snowblind"][0])
        self.assertEqual(conn.execute("SELECT count(*) FROM albums WHERE mbid LIKE 'dddddddd%' AND id != 13").fetchone()[0], 0)
        summary = json.loads(conn.execute("SELECT summary_json FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual((summary["imported"], summary["invalid"], summary["releasesLookedUp"]), (7, 2, 4))
        direct.assert_called_once_with(TRK_UNSORTED)  # the one id with no edition is asked about directly
        # song ids: a track id becomes the recording (and is kept as a track); a recording id is used as is;
        # an id with no tracklist to sort it is never put on the song, only kept on the scrobble
        song = lambda t: conn.execute("SELECT id, mbid FROM songs WHERE title = ?", (t,)).fetchone()  # noqa: E731
        self.assertEqual(song("War Pigs")[1], REC_WARPIGS)
        self.assertEqual(conn.execute("SELECT song_id, release_mbid FROM song_tracks WHERE track_mbid = ?", (TRK_WARPIGS,)).fetchone(),
                         (song("War Pigs")[0], REL[2]))
        self.assertEqual(conn.execute("SELECT count(*) FROM songs WHERE title = 'War Pigs'").fetchone()[0], 1)
        self.assertEqual(song("Planet Caravan")[1], REC_CARAVAN)
        self.assertIsNone(song("Electric Funeral")[1])
        self.assertEqual(conn.execute("SELECT count(*) FROM scrobble_tracks WHERE lastfm_mbid = ?", (TRK_UNSORTED,)).fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM scrobble_tracks").fetchone()[0], 4)
        self.assertEqual(summary["events"].get("suspect"), 2)
        conn.close()

    def test_budget(self):
        conn = connect()
        with mock.patch.object(releases.mb, "resolve_release_first", return_value=(None, None, None)) as m:
            n = releases.resolve_new(conn, [REL[2], REL[3], REL[2], REL[1]], {"left": 1})
        self.assertEqual((n, m.call_count), (1, 1))  # REL[1] is already known; REL[2] only once
        conn.close()


if __name__ == "__main__":
    unittest.main()
