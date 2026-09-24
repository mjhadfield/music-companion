"""Genres: automatic application (MusicBrainz votes, Discogs styles), what survives a refresh
(removals, manual tags, hide/merge rules), hand edits undoing exactly, and genres following
album merges. Uses the merge tests' throwaway database."""
import unittest

import test_merge_undo as base  # first: sets the test DB path and import paths

import genres as g  # noqa: E402
import merge  # noqa: E402


def tags(conn, album_id):
    return {name: src for name, src in conn.execute(
        "SELECT ge.name, ag.source FROM album_genres ag JOIN genres ge ON ge.id = ag.genre_id WHERE ag.album_id = ?", (album_id,))}


def snapshot(conn):
    return (sorted(conn.execute("SELECT * FROM album_genres").fetchall()), sorted(conn.execute("SELECT album_id, genre_id FROM genre_hidden").fetchall()),
            sorted(conn.execute("SELECT genre_id, action, target_genre_id FROM genre_rules").fetchall()))


MB = [{"name": "heavy metal", "id": "gm-1", "count": 12}, {"name": "hard rock", "id": "gm-2", "count": 4},
      {"name": "doom metal", "id": "gm-3", "count": 1}]


class GenreTests(unittest.TestCase):
    setUp = base.MergeUndoTests.setUp
    tearDown = base.MergeUndoTests.tearDown

    def test_musicbrainz_vote_threshold(self):
        self.assertEqual([n for n, _, _ in g.pick_musicbrainz(MB)], ["heavy metal", "hard rock"])
        self.assertEqual([n for n, _, _ in g.pick_musicbrainz([{"name": "a", "count": 1}, {"name": "b", "count": 0}])], ["a"])
        self.assertEqual(g.pick_musicbrainz([]), [])

    def test_apply_from_artist_browse_exact_release_group_only(self):
        groups = [{"mbid": "bbbbbbbb-0000-0000-0000-000000000010", "genres": MB}, {"mbid": "not-yours", "genres": MB}]
        self.assertEqual(g.apply_musicbrainz_groups(self.conn, groups, [1]), 1)
        self.assertEqual(tags(self.conn, 10), {"heavy metal": "musicbrainz", "hard rock": "musicbrainz"})

    def test_legacy_edition_album_via_resolved_release_group(self):
        self.conn.execute("UPDATE albums SET mbid = 'dddddddd-0000-0000-0000-000000000077' WHERE id = 10")
        self.conn.execute("INSERT INTO mb_cache (key, payload_json, fetched_at) VALUES "
                          "('resolve:release-group:dddddddd-0000-0000-0000-000000000077', '\"rg-x\"', datetime('now'))")
        self.assertEqual(g.apply_musicbrainz_groups(self.conn, [{"mbid": "rg-x", "genres": MB}], [1]), 1)
        self.assertIn("heavy metal", tags(self.conn, 10))

    def test_refresh_drops_what_the_source_dropped_but_never_manual_or_hidden(self):
        g.apply(self.conn, 10, "musicbrainz", g.pick_musicbrainz(MB))
        g.edit_album(self.conn, 10, add=["NWOBHM"])  # manual; the synonym map names it properly
        hr = self.conn.execute("SELECT id FROM genres WHERE name = 'hard rock'").fetchone()[0]
        g.edit_album(self.conn, 10, remove=[hr])
        g.apply(self.conn, 10, "musicbrainz", g.pick_musicbrainz(MB))  # the refresh offers hard rock again
        self.assertEqual(tags(self.conn, 10), {"heavy metal": "musicbrainz", "new wave of british heavy metal": "manual"})
        g.apply(self.conn, 10, "musicbrainz", [])  # MusicBrainz no longer lists any
        self.assertEqual(tags(self.conn, 10), {"new wave of british heavy metal": "manual"})

    def test_priority_manual_over_musicbrainz_over_discogs(self):
        g.apply(self.conn, 10, "discogs", [("Heavy Metal", None, None)])
        self.assertEqual(tags(self.conn, 10), {"heavy metal": "discogs"})
        g.apply(self.conn, 10, "musicbrainz", g.pick_musicbrainz(MB))
        self.assertEqual(tags(self.conn, 10)["heavy metal"], "musicbrainz")
        g.apply(self.conn, 10, "discogs", [("Heavy Metal", None, None)])  # a lower source can't downgrade it
        self.assertEqual(tags(self.conn, 10)["heavy metal"], "musicbrainz")

    def test_rules_hide_and_merge_apply_now_and_on_refresh(self):
        g.apply(self.conn, 10, "musicbrainz", [("metal", None, 5), ("rock", None, 3)])
        g.apply(self.conn, 11, "musicbrainz", [("heavy metal", None, 5)])
        gid = lambda n: self.conn.execute("SELECT id FROM genres WHERE name = ?", (n,)).fetchone()[0]  # noqa: E731
        g.set_rule(self.conn, gid("rock"), "hide")
        g.set_rule(self.conn, gid("metal"), "merge", gid("heavy metal"))
        self.assertEqual(tags(self.conn, 10), {"heavy metal": "musicbrainz"})
        g.apply(self.conn, 10, "musicbrainz", [("metal", None, 5), ("rock", None, 3)])  # refresh honours the rules
        self.assertEqual(tags(self.conn, 10), {"heavy metal": "musicbrainz"})

    def test_edits_and_rules_undo_exactly(self):
        g.apply(self.conn, 10, "musicbrainz", g.pick_musicbrainz(MB))
        self.conn.commit()
        before = snapshot(self.conn)
        hm = self.conn.execute("SELECT id FROM genres WHERE name = 'heavy metal'").fetchone()[0]
        e1 = g.edit_album(self.conn, 10, add=["thrash", "hard rock"], remove=[hm])
        e2 = g.set_rule(self.conn, self.conn.execute("SELECT id FROM genres WHERE name = 'hard rock'").fetchone()[0], "hide")
        self.assertNotEqual(snapshot(self.conn), before)
        merge.undo_edit(self.conn, e2)
        merge.undo_edit(self.conn, e1)
        self.assertEqual(snapshot(self.conn), before)

    def test_album_merge_carries_genres_and_undoes(self):
        g.apply(self.conn, 11, "musicbrainz", [("heavy metal", None, 3), ("doom metal", None, 2)])
        g.apply(self.conn, 10, "musicbrainz", [("heavy metal", None, 9)])
        g.edit_album(self.conn, 11, remove=[self.conn.execute("SELECT id FROM genres WHERE name = 'doom metal'").fetchone()[0]])
        g.apply(self.conn, 11, "discogs", [("Stoner Rock", None, None)])
        self.conn.commit()
        before = snapshot(self.conn)
        r = merge.merge_albums(self.conn, 11, 10)
        self.assertEqual(tags(self.conn, 10), {"heavy metal": "musicbrainz", "stoner rock": "discogs"})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM genre_hidden WHERE album_id = 10").fetchone()[0], 1)
        merge.undo_merge(self.conn, r["logId"])
        self.assertEqual(snapshot(self.conn), before)


if __name__ == "__main__":
    unittest.main()
