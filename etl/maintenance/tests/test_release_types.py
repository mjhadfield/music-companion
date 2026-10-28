"""Release types (Maintenance > Albums > Types): MusicBrainz's types -> kinds, the plan from cache (edition
ids, the too-many-songs check, hand-set types left alone), apply + batch undo, and setting by hand.
Uses the merge tests' throwaway database."""
import json
import unittest

import test_merge_undo as base  # first: sets the test DB path and import paths

import release_types as rt  # noqa: E402
from api import batch  # noqa: E402

RG = "bbbbbbbb-0000-0000-0000-000000000010"       # album 10 (Paranoid): a release group
EDITION = "bbbbbbbb-0000-0000-0000-000000000011"  # album 11: a Last.fm edition id


def cache(conn, key, payload):
    conn.execute("INSERT OR REPLACE INTO mb_cache (key, payload_json, fetched_at) VALUES (?, ?, datetime('now'))", (key, json.dumps(payload)))


def group(mbid, primary, *secondary):
    return {"mbid": mbid, "title": "x", "primaryType": primary, "secondaryTypes": list(secondary)}


class ClassifyTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(rt.classify("Album", []), "album")
        self.assertEqual(rt.classify("EP", []), "ep")
        self.assertEqual(rt.classify("Album", ["Live"]), "live")                   # No Sleep 'til Hammersmith
        self.assertEqual(rt.classify("EP", ["Live"]), "live")
        self.assertEqual(rt.classify("Album", ["Live", "Soundtrack"]), "live")     # Rite Here Rite Now
        self.assertEqual(rt.classify("Single", ["Live"]), "single")                # a live single folds away
        self.assertEqual(rt.classify("Single", ["Remix"]), "single")
        self.assertEqual(rt.classify("Album", ["Compilation"]), "compilation")
        self.assertEqual(rt.classify("Album", ["Compilation", "Live"]), "compilation")
        self.assertEqual(rt.classify("Album", ["Soundtrack"]), "other")
        self.assertEqual(rt.classify("Album", ["Remix"]), "other")
        self.assertEqual(rt.classify("Broadcast", []), "other")
        self.assertEqual(rt.classify(None, []), "other")


class FillTests(unittest.TestCase):
    setUp = base.MergeUndoTests.setUp
    tearDown = base.MergeUndoTests.tearDown

    def _plan(self):
        return {r["albumId"]: r for r in rt.plan(self.conn)}

    def test_plan_from_group_and_edition_ids(self):
        cache(self.conn, f"lookup:release-group:{RG}", group(RG, "Album"))
        cache(self.conn, f"lookup:release-parent:{EDITION}", group("rg-single", "Single"))
        # the genre browse has the primary type only -- not enough to tell an album from a compilation
        cache(self.conn, "browse:rg-genres:aaaaaaaa-0000-0000-0000-000000000001", [{"mbid": "other-rg", "primaryType": "Album"}])
        self.conn.commit()
        p = self._plan()
        self.assertEqual((p[10]["kind"], p[10]["status"]), ("album", "set"))
        self.assertEqual((p[11]["kind"], p[11]["status"]), ("single", "set"))
        self.assertEqual(p[12]["status"], "nomb")

    def test_edition_resolved_to_a_cached_group(self):
        cache(self.conn, f"resolve:release-group:{EDITION}", "rg-ep")
        cache(self.conn, "lookup:release-group:rg-ep", group("rg-ep", "EP"))
        self.conn.commit()
        self.assertEqual(self._plan()[11]["kind"], "ep")

    def test_uncached_id_is_pending(self):
        self.assertEqual(self._plan()[10]["status"], "pending")

    def test_too_many_songs_for_a_single_is_left_to_check(self):
        cache(self.conn, f"lookup:release-group:{RG}", group(RG, "Single"))  # Lateralus filed under the single's id
        self.conn.executemany("INSERT INTO songs (id, artist_id, album_id, title) VALUES (?, 1, 10, ?)",
                              [(200 + i, f"Track {i}") for i in range(4)])  # 5 with Paranoid
        self.conn.commit()
        p = self._plan()[10]
        self.assertEqual((p["kind"], p["status"], p["songs"]), ("single", "check", 5))
        handles, _ = batch.run_action(self.conn, {"type": "releaseTypeFill"})
        self.assertIsNone(self.conn.execute("SELECT release_type FROM albums WHERE id = 10").fetchone()[0])

    def test_apply_and_undo_leave_hand_set_alone(self):
        cache(self.conn, f"lookup:release-group:{RG}", group(RG, "Album", "Live"))
        cache(self.conn, f"lookup:release-parent:{EDITION}", group("rg-comp", "Album", "Compilation"))
        self.conn.commit()
        batch.run_action(self.conn, {"type": "setReleaseType", "albumId": 11, "kind": "album"})  # by hand first
        handles, summary = batch.run_action(self.conn, {"type": "releaseTypeFill"})
        self.conn.commit()
        rows = dict((r[0], r[1:]) for r in self.conn.execute("SELECT id, release_type, release_type_source FROM albums WHERE id IN (10, 11)"))
        self.assertEqual(rows[10], ("live", "musicbrainz"))
        self.assertEqual(rows[11], ("album", "manual"))                  # MusicBrainz says compilation; the hand wins
        self.assertEqual(len(handles), 1)
        self.assertIn("1 live", summary["text"])
        self.assertEqual(self._plan()[10]["status"], "same")
        for h in reversed(handles):
            batch.undo_handle(self.conn, h)
        self.assertEqual(self.conn.execute("SELECT release_type, release_type_source FROM albums WHERE id = 10").fetchone(), (None, None))

    def test_changed_id_is_corrected(self):
        cache(self.conn, f"lookup:release-group:{RG}", group(RG, "Single"))
        self.conn.commit()
        batch.run_action(self.conn, {"type": "releaseTypeFill"})
        cache(self.conn, f"lookup:release-group:{RG}", group(RG, "Album"))  # e.g. its id was fixed since
        p = self._plan()[10]
        self.assertEqual((p["current"], p["kind"], p["status"]), ("single", "album", "change"))

    def test_by_hand_and_back_to_musicbrainz(self):
        cache(self.conn, f"lookup:release-group:{RG}", group(RG, "EP"))
        self.conn.commit()
        h1, _ = batch.run_action(self.conn, {"type": "setReleaseType", "albumId": 10, "kind": "album"})
        self.assertEqual(self.conn.execute("SELECT release_type, release_type_source FROM albums WHERE id = 10").fetchone(), ("album", "manual"))
        batch.run_action(self.conn, {"type": "setReleaseType", "albumId": 10, "kind": None})
        self.assertEqual(self.conn.execute("SELECT release_type, release_type_source FROM albums WHERE id = 10").fetchone(), ("ep", "musicbrainz"))
        batch.run_action(self.conn, {"type": "setReleaseType", "albumId": 12, "kind": None})  # no id: just untyped
        self.assertEqual(self.conn.execute("SELECT release_type FROM albums WHERE id = 12").fetchone()[0], None)
        with self.assertRaises(batch.ApiError):
            batch.run_action(self.conn, {"type": "setReleaseType", "albumId": 10, "kind": "bootleg"})

    def test_merge_undo_keeps_the_type(self):
        import merge
        batch.run_action(self.conn, {"type": "setReleaseType", "albumId": 11, "kind": "ep"})
        r = merge.merge_albums(self.conn, 11, 10)
        merge.undo_merge(self.conn, r["logId"])
        self.assertEqual(self.conn.execute("SELECT release_type, release_type_source FROM albums WHERE id = 11").fetchone(), ("ep", "manual"))


if __name__ == "__main__":
    unittest.main()
