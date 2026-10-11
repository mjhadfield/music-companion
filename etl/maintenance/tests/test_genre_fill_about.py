"""The MusicBrainz genre fill (pick rules, the plan from cache, apply + batch undo, the artist_genres
fallback) and the stored About (Last.fm cleaning, edits undoing exactly, a re-fetch never touching
an edit, artist merges carrying both). Uses the merge tests' throwaway database."""
import json
import unittest

import test_merge_undo as base  # first: sets the test DB path and import paths

import about  # noqa: E402
import genre_fill  # noqa: E402
import genres as g  # noqa: E402
import merge  # noqa: E402
from api import batch  # noqa: E402

G = lambda **kw: [{"name": n, "id": None, "count": c} for n, c in kw.items()]


def names(picked):
    return [n for n, _, _ in picked]


def cache(conn, key, payload):
    conn.execute("INSERT OR REPLACE INTO mb_cache (key, payload_json, fetched_at) VALUES (?, ?, datetime('now'))", (key, json.dumps(payload)))


class PickTests(unittest.TestCase):
    def test_tie_goes_to_the_artists_votes_not_list_order(self):
        # Insomnium's Anno 1696: the edition has melodic death metal 1 / metal 1; the artist 13 / 3
        edition = [{"name": "metal", "count": 1}, {"name": "melodic death metal", "count": 1}]
        artist = [{"name": "melodic death metal", "count": 13}, {"name": "death metal", "count": 6}, {"name": "metal", "count": 3}]
        self.assertEqual(names(g.pick_musicbrainz(edition, artist)), ["melodic death metal"])
        self.assertEqual(names(g.pick_for_fill(edition, artist)), ["melodic death metal"])
        self.assertEqual(names(g.pick_musicbrainz(edition)), ["melodic death metal"])  # no artist: by name, never list order

    def test_umbrella_gives_way_to_specific_and_max_three(self):
        is_this_it = [{"name": "rock", "count": 8}, {"name": "indie rock", "count": 6}, {"name": "garage rock revival", "count": 3},
                      {"name": "garage rock", "count": 2}, {"name": "post-punk revival", "count": 1}]
        self.assertEqual(names(g.pick_for_fill(is_this_it)), ["indie rock", "garage rock revival", "garage rock"])
        self.assertEqual(names(g.pick_for_fill([{"name": "rock", "count": 5}, {"name": "pop", "count": 1}])), ["rock"])  # nothing more specific
        self.assertEqual(names(g.pick_for_fill([{"name": "thrash metal", "count": 17}, {"name": "metal", "count": 5}])), ["thrash metal"])
        self.assertEqual(g.pick_for_fill([]), [])


class FillTests(unittest.TestCase):
    setUp = base.MergeUndoTests.setUp
    tearDown = base.MergeUndoTests.tearDown

    def _gather(self):
        # what the overnight run would have cached: Black Sabbath's browse (Paranoid's group), the
        # Remastered album's id is an edition whose group has no genres -> its own edition genres
        cache(self.conn, "browse:rg-genres:aaaaaaaa-0000-0000-0000-000000000001",
              [{"mbid": "bbbbbbbb-0000-0000-0000-000000000010", "genres": [{"name": "heavy metal", "id": "h", "count": 9},
                                                                          {"name": "metal", "id": "m", "count": 4}]}])
        cache(self.conn, "resolve:release-group:bbbbbbbb-0000-0000-0000-000000000011", "rg-remaster")
        cache(self.conn, "lookup:rg-genres:rg-remaster", {"group": [], "releases": [], "releaseCount": 1})
        cache(self.conn, "lookup:release-genres:bbbbbbbb-0000-0000-0000-000000000011", [{"name": "hard rock", "id": "r", "count": 1}])
        cache(self.conn, "lookup:artist-genres:aaaaaaaa-0000-0000-0000-000000000001", [{"name": "heavy metal", "id": "h", "count": 20},
                                                                                        {"name": "doom metal", "id": "d", "count": 8}])
        self.conn.commit()

    def test_plan_from_cache(self):
        self._gather()
        albums, artists = genre_fill.plan(self.conn)
        by = {a["albumId"]: a for a in albums}
        self.assertEqual((by[10]["status"], by[10]["from"], names(by[10]["genres"])), ("tagged", "album", ["heavy metal"]))
        self.assertEqual((by[11]["status"], by[11]["from"], names(by[11]["genres"])), ("tagged", "edition", ["hard rock"]))
        self.assertEqual(by[12]["status"], "nomb")
        sab = next(a for a in artists if a["artistId"] == 1)
        self.assertEqual(names(sab["genres"]), ["heavy metal", "doom metal"])

    def test_apply_and_undo_as_one_batch(self):
        self._gather()
        before = (sorted(self.conn.execute("SELECT * FROM album_genres").fetchall()), sorted(self.conn.execute("SELECT * FROM artist_mb_genres").fetchall()))
        self.conn.execute("BEGIN IMMEDIATE")
        handles, summary = batch.run_action(self.conn, {"type": "genreFill"})
        self.conn.commit()
        self.assertEqual(summary["rowsMoved"], {"albums": 2, "artists": 1})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM artist_mb_genres WHERE artist_id = 1").fetchone()[0], 2)
        # artist 1 has tagged albums now: its own genres stay out of artist_genres (a fallback only)
        self.assertEqual({r[0] for r in self.conn.execute("SELECT albums FROM artist_genres WHERE artist_id = 1")}, {1})
        self.conn.execute("BEGIN IMMEDIATE")
        for h in reversed(handles):
            batch.undo_handle(self.conn, h)
        self.conn.commit()
        after = (sorted(self.conn.execute("SELECT * FROM album_genres").fetchall()), sorted(self.conn.execute("SELECT * FROM artist_mb_genres").fetchall()))
        self.assertEqual(before, after)

    def test_artist_genres_falls_back_to_the_artists_own(self):
        gid = g.genre_id(self.conn, "doom metal")
        self.conn.execute("INSERT INTO artist_mb_genres (artist_id, genre_id, votes) VALUES (3, ?, 7)", (gid,))
        self.assertEqual(self.conn.execute("SELECT genre_id, albums, votes FROM artist_genres WHERE artist_id = 3").fetchall(), [(gid, 0, 7)])
        g.apply(self.conn, 12, "manual", [("heavy metal", None, None)])  # Ozzy is credited on album 12
        self.assertNotIn(gid, [r[0] for r in self.conn.execute("SELECT genre_id FROM artist_genres WHERE artist_id = 3")])


class AboutTests(unittest.TestCase):
    setUp = base.MergeUndoTests.setUp
    tearDown = base.MergeUndoTests.tearDown

    FETCHED = {"text": "Black Sabbath were an English rock band.\n\nThey formed in Birmingham in 1968. More text here.", "source": "wikipedia",
               "wiki_url": "https://en.wikipedia.org/wiki/Black_Sabbath", "wiki_title": "Black Sabbath", "thumbnail_url": None,
               "tagline": "English heavy metal band", "type": "Group", "area": "United Kingdom", "begin_area": "Birmingham",
               "formed": "1968", "ended": "2025", "links_json": None}

    def test_lastfm_cleaning(self):
        self.assertIsNone(about._clean_lastfm("There are at least six artists with this name: 1) ..."))
        self.assertEqual(about._clean_lastfm('Plaza are a band.\n\nFrom Newcastle. <a href="https://www.last.fm/music/Plaza">Read more on Last.fm</a>. '
                                             'User-contributed text is available under the Creative Commons By-SA License; additional terms may apply.'),
                         "Plaza are a band.\n\nFrom Newcastle.")

    def test_edit_undo_and_refetch_keeps_edit(self):
        about.store_fetched(self.conn, 1, self.FETCHED)
        e = about.edit(self.conn, 1, {"text": "Black Sabbath were an English rock band."})
        self.assertEqual(about.row(self.conn, 1)["edited"], 1)
        changed = {**self.FETCHED, "text": "Black Sabbath were an English rock band formed in Birmingham."}
        self.assertIn("kept your edit", about.store_fetched(self.conn, 1, changed))
        r = about.row(self.conn, 1)
        self.assertEqual((r["text"], r["changed_upstream"]), ("Black Sabbath were an English rock band.", 1))
        merge.undo_edit(self.conn, e)
        r = about.row(self.conn, 1)
        self.assertEqual((r["text"], r["edited"]), (self.FETCHED["text"], 0))

    def test_artist_merge_carries_about_and_genres(self):
        about.store_fetched(self.conn, 2, {**self.FETCHED, "text": "the dup's text"})
        gid = g.genre_id(self.conn, "doom metal")
        self.conn.execute("INSERT INTO artist_mb_genres (artist_id, genre_id, votes) VALUES (2, ?, 5)", (gid,))
        r = merge.merge_artists(self.conn, 2, 1)
        self.assertEqual(about.row(self.conn, 1)["text"], "the dup's text")
        self.assertEqual(self.conn.execute("SELECT artist_id FROM artist_mb_genres").fetchall(), [(1,)])
        merge.undo_merge(self.conn, r["logId"])
        self.assertEqual(about.row(self.conn, 2)["text"], "the dup's text")
        self.assertIsNone(about.row(self.conn, 1))
        self.assertEqual(self.conn.execute("SELECT artist_id FROM artist_mb_genres").fetchall(), [(2,)])


if __name__ == "__main__":
    unittest.main()
