"""Song duplicate review groups (dupes.py) and the workbench's read side (api/workbench.py):
what pairs as a possible duplicate and what never may, what arrives pre-ticked, and that the
done-signature changes when something new turns up. Throwaway database built from schema.sql."""
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
import dupes  # noqa: E402
from api import workbench  # noqa: E402
from common import DB_PATH, connect  # noqa: E402
from migrations import migrate  # noqa: E402

SONGS = [
    # id, album, title
    (100, 10, "Total Hate"), (101, 11, "Total Hate '95"),                 # suffix -> possible
    (102, 10, "Vermilion"), (103, 11, "Vermilion Pt. 2"),                 # sequel -> never
    (104, 10, "Harvest"), (105, 11, "Harvest Moon"),                      # extra word -> never
    (106, 10, "Gold Dust"), (107, 11, "Golddust"),                        # spacing -> possible
    (108, 10, "A Song for the Dead"), (109, 11, "Song for the Dead"),     # article -> possible
    (110, 10, "Still Loving You"), (111, 10, "Still Loving You - 2015 Remaster"),   # edition, same album -> pre-ticked
    (112, 10, "Holiday"), (113, 11, "Holiday - Remastered"),             # edition, different albums -> not ticked
    (114, 10, "Paranoid"), (115, 10, "Paranoid - Live"),                  # variant -> never ticked
]


class DupesTests(unittest.TestCase):
    def setUp(self):
        if DB_PATH.exists():
            DB_PATH.unlink()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        conn.executescript("""
            INSERT INTO artists (id, name) VALUES (1, 'Band');
            INSERT INTO albums (id, artist_id, title) VALUES (10, 1, 'First'), (11, 1, 'Second');
            INSERT INTO album_artists VALUES (10, 1, 0), (11, 1, 0);
        """)
        conn.executemany("INSERT INTO songs (id, artist_id, album_id, title) VALUES (?, 1, ?, ?)", SONGS)
        conn.executemany("INSERT INTO scrobbles (song_id, artist_id, album_id, played_at, raw_artist_text, raw_track_text) VALUES (?, 1, ?, ?, 'Band', ?)",
                         [(sid, alb, f"2020-01-{1 + i % 28:02d}", t) for i, (sid, alb, t) in enumerate(SONGS)])
        conn.commit()
        conn.close()
        self.conn = connect()

    def tearDown(self):
        self.conn.close()

    def _groups(self):
        return {tuple(sorted(m["songId"] for m in g["members"])): g for g in dupes.review_groups(self.conn, [1])}

    def test_possible_pairs_only_for_small_clear_differences(self):
        g = self._groups()
        self.assertEqual(g[(100, 101)]["kind"], "possible")
        self.assertEqual(g[(100, 101)]["reason"], "suffix")
        self.assertEqual(g[(106, 107)]["reason"], "spacing")
        self.assertEqual(g[(108, 109)]["reason"], "article")
        for never in [(102, 103), (104, 105)]:
            self.assertNotIn(never, g)
        for pair in [(100, 101), (106, 107), (108, 109)]:
            self.assertEqual(g[pair]["include"], [])                     # never pre-ticked

    def test_only_same_album_editions_arrive_ticked(self):
        g = self._groups()
        self.assertEqual(g[(110, 111)]["kind"], "edition")
        self.assertEqual(g[(110, 111)]["include"], [111])
        self.assertEqual(g[(112, 113)]["kind"], "edition")
        self.assertEqual(g[(112, 113)]["include"], [])
        self.assertEqual(g[(114, 115)]["kind"], "variant")
        self.assertEqual(g[(114, 115)]["include"], [])

    def test_inbox_near_titles(self):
        self.assertEqual(dupes.near_titles("Total Hate '95", [(1, "Total Hate"), (2, "Total Hatred"), (3, "Vermilion")]), [(1, "suffix")])
        self.assertEqual(dupes.near_titles("Vermilion Pt. 2", [(3, "Vermilion")]), [])
        self.assertEqual(dupes.near_titles("Gold Dust", [(4, "Golddust")]), [(4, "spacing")])

    def test_dismissed_pairs_stay_gone(self):
        self.conn.execute("INSERT INTO duplicate_dismissals (entity_type, entity_id_a, entity_id_b) VALUES ('song', 100, 101), ('song', 114, 115)")
        g = self._groups()
        self.assertNotIn((100, 101), g)
        self.assertNotIn((114, 115), g)

    def test_signature_changes_when_something_new_turns_up(self):
        before = workbench.signature(workbench.outstanding(self.conn, [1])[1])
        self.assertEqual(before, workbench.signature(workbench.outstanding(self.conn, [1])[1]))   # stable
        self.conn.execute("INSERT INTO songs (id, artist_id, album_id, title) VALUES (116, 1, 10, 'Holiday (Mono)')")
        self.assertNotEqual(before, workbench.signature(workbench.outstanding(self.conn, [1])[1]))

    def test_placement_proposes_the_credited_album(self):
        self.conn.executescript("""
            INSERT INTO artists (id, name) VALUES (2, 'Other');
            INSERT INTO albums (id, artist_id, title) VALUES (20, 2, 'Their Album');
            INSERT INTO album_artists VALUES (20, 2, 0);
            UPDATE songs SET album_id = 20 WHERE id = 104;
        """)
        issues = workbench.placement_issues(self.conn, [1])[1]
        self.assertEqual([(i["songId"], i["reason"], i["suggested"]["albumId"]) for i in issues], [(104, "uncredited", 10)])
        self.conn.execute("INSERT INTO review_marks (entity_type, entity_id, mark) VALUES ('song', 104, ?)", (workbench.PLACEMENT_OK,))
        self.assertEqual(workbench.placement_issues(self.conn, [1]).get(1, []), [])


if __name__ == "__main__":
    unittest.main()
