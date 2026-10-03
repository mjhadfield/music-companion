"""Maintenance > Genres review (By album / By artist / By vinyl): suggestions from MusicBrainz (the
album's own genres, else its editions'), Discogs styles and artist hints; saving a reviewed set as a
batch (albumGenres) and undoing it exactly; the queues. MusicBrainz is stubbed. Throwaway database."""
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
import genres as genre_tags  # noqa: E402
import mbcache  # noqa: E402
from api import batch  # noqa: E402
from api import genres as genres_api  # noqa: E402
from api.core import Req  # noqa: E402
from common import DB_PATH, connect  # noqa: E402
from migrations import migrate  # noqa: E402
from tests.test_batch import checksum  # noqa: E402

RG = "cccccccc-0000-0000-0000-0000000000{:02d}"
TABLES = ["album_genres", "genre_hidden", "genres", "review_marks"]


class GenreReviewTests(unittest.TestCase):
    def setUp(self):
        if DB_PATH.exists():
            DB_PATH.unlink()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        conn.executescript(f"""
            INSERT INTO artists (id, mbid, name) VALUES (1, 'aaaaaaaa-0000-0000-0000-000000000001', 'Band');
            INSERT INTO albums (id, artist_id, title, mbid) VALUES (10, 1, 'Played Most', '{RG.format(10)}'), (11, 1, 'Vinyl One', '{RG.format(11)}'),
                (12, 1, 'Edition Only', '{RG.format(12)}'), (13, 1, 'No Id', NULL);
            INSERT INTO album_artists VALUES (10, 1, 0), (11, 1, 0), (12, 1, 0), (13, 1, 0);
            INSERT INTO songs (id, artist_id, album_id, title) VALUES (100, 1, 10, 'A');
            INSERT INTO scrobbles (song_id, artist_id, album_id, played_at, raw_artist_text, raw_track_text) VALUES
                (100, 1, 10, '2020-01-01', 'Band', 'A'), (100, 1, 10, '2020-01-02', 'Band', 'A');
            INSERT INTO vinyl_holdings (id, album_id, discogs_release_id, raw_title_text, raw_artist_text) VALUES (5, 11, 777, 'Vinyl One', 'Band');
            INSERT INTO vinyl_details (holding_id, styles) VALUES (5, '["Thrash"]');
            INSERT INTO genres (id, name) VALUES (1, 'heavy metal'), (2, 'speed metal');
            INSERT INTO album_genres (album_id, genre_id, source, votes) VALUES (11, 1, 'discogs', NULL);
            INSERT INTO genre_hidden (album_id, genre_id) VALUES (10, 2);
        """)
        conn.commit()
        conn.close()
        self.conn = connect()
        self.real = (mbcache.release_group_genres, mbcache.release_genres, mbcache.artist_genres)
        groups = {RG.format(10): {"group": [], "releases": [{"name": "thrash metal", "id": "t", "count": 4}, {"name": "speed metal", "id": "s", "count": 3},
                                                                {"name": "crossover", "id": "x", "count": 1}], "releaseCount": 6},
                  RG.format(11): {"group": [{"name": "heavy metal", "id": "h", "count": 5}, {"name": "nwobhm", "id": "n", "count": 1}], "releases": [], "releaseCount": 2}}
        mbcache.release_group_genres = lambda m: groups.get(m)          # RG 12 isn't a release group: an edition id
        mbcache.release_genres = lambda m: [{"name": "power metal", "id": "p", "count": 2}]
        mbcache.artist_genres = lambda m: [{"name": "metal", "id": "m", "count": 9}]

    def tearDown(self):
        mbcache.release_group_genres, mbcache.release_genres, mbcache.artist_genres = self.real
        self.conn.close()

    def _suggest(self, album_id):
        out = genres_api.suggest(Req({"albumId": str(album_id)}, {}))
        return {s["name"]: s for s in out["suggestions"]}, out["notes"]

    def test_editions_genres_when_the_album_has_none(self):
        s, notes = self._suggest(10)
        self.assertTrue(s["thrash metal"]["tick"])                       # releases' genres, >= 2 votes
        self.assertEqual(s["thrash metal"]["from"], "editions")
        self.assertNotIn("speed metal", s)                                # removed from this album before: never again
        self.assertFalse(s["crossover"]["tick"])                         # a single vote: a hint
        self.assertFalse(s["metal"]["tick"])                             # the artist's genre: a hint
        self.assertIn("none on the album itself", notes[0])

    def test_album_genres_discogs_styles_and_what_is_already_there(self):
        s, _ = self._suggest(11)
        self.assertNotIn("heavy metal", s)                                # already on the album
        self.assertTrue(s["thrash metal"]["tick"])                       # the pressing's Discogs style (Thrash -> thrash metal)
        self.assertEqual(s["thrash metal"]["source"], "discogs")
        self.assertFalse(s["new wave of british heavy metal"]["tick"])   # "nwobhm" (a synonym): one vote, others have more

    def test_with_nothing_of_its_own_the_artists_top_genres_are_ticked(self):
        s, notes = self._suggest(13)                                      # no MusicBrainz id, no pressing
        self.assertTrue(s["metal"]["tick"])
        self.assertIn("artist's top genres", notes[-1])
        s, _ = self._suggest(10)                                          # has editions' genres: the artist's stay hints
        self.assertFalse(s["metal"]["tick"])

    def test_an_edition_id_uses_that_editions_genres(self):
        s, notes = self._suggest(12)
        self.assertTrue(s["power metal"]["tick"])
        self.assertIn("edition", notes[0])

    def test_saving_a_review_and_undoing_it(self):
        before = checksum(self.conn, TABLES)
        out = batch.apply(Req({}, {"actions": [{"type": "albumGenres", "albumId": 11,
                                                "keep": [{"name": "thrash metal", "source": "discogs"}, {"name": "my own genre", "source": "manual"}],
                                                "drop": [1], "reject": ["nwobhm"]}]}))
        q = self.conn.execute
        rows = dict(q("SELECT ge.name, ag.source FROM album_genres ag JOIN genres ge ON ge.id = ag.genre_id WHERE ag.album_id = 11").fetchall())
        self.assertEqual(rows, {"thrash metal": "discogs", "my own genre": "manual"})
        hidden = {n for (n,) in q("SELECT ge.name FROM genre_hidden h JOIN genres ge ON ge.id = h.genre_id WHERE h.album_id = 11")}
        self.assertEqual(hidden, {"heavy metal"})                         # dropped -> stays removed (nwobhm was never a genre row)
        self.assertTrue(q("SELECT 1 FROM review_marks WHERE entity_id = 11 AND mark = ?", (genre_tags.REVIEWED_MARK,)).fetchone())
        batch.undo_batch(out["batchId"])
        after = checksum(self.conn, TABLES)
        self.assertEqual({k: v for k, v in after.items() if k != "genres"}, {k: v for k, v in before.items() if k != "genres"})

    def test_the_queues(self):
        page = lambda mode: [a["albumId"] for a in genres_api.queue(Req({"mode": mode}, {}))["albums"]]
        self.assertEqual(page("untagged")[0], 10)                         # most played first; 11 has a genre
        self.assertNotIn(11, page("untagged"))
        self.assertEqual(page("vinyl"), [11])                             # records, tagged or not
        batch.apply(Req({}, {"actions": [{"type": "albumGenres", "albumId": 11, "keep": [], "drop": [], "reject": []}]}))
        self.assertEqual(page("vinyl"), [])                               # reviewed: leaves the list...
        self.assertEqual(page("vinyl-all"), [11])                         # ...but "show reviewed" still has it
        batch.apply(Req({}, {"actions": [{"type": "albumGenres", "albumId": 13, "keep": [], "drop": [], "reject": []}]}))
        self.assertNotIn(13, page("untagged"))                            # reviewed with nothing to add: gone too


if __name__ == "__main__":
    unittest.main()
