"""Song lengths (etl/song_lengths.py): from MusicBrainz tracklists (by recording id, else the title on the song's own
album), from your pressings' Discogs tracklists, then Last.fm; a live version never takes the studio track's length;
tracklist lines with none -- even tracks never played ("Embryo") -- are looked up too. Throwaway database, no network."""
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
import song_lengths  # noqa: E402
from common import DB_PATH, connect  # noqa: E402
from migrations import migrate  # noqa: E402


class SongLengthTests(unittest.TestCase):
    def setUp(self):
        if DB_PATH.exists():
            DB_PATH.unlink()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript((HERE.parent.parent.parent / "schema.sql").read_text())
        migrate(conn)
        conn.executescript("""
            INSERT INTO artists (id, name) VALUES (1, 'Black Sabbath'), (2, 'Gojira');
            INSERT INTO albums (id, artist_id, title) VALUES (10, 1, 'Master Of Reality'), (20, 2, 'Magma');
            INSERT INTO songs (id, artist_id, album_id, title, mbid) VALUES
                (100, 1, 10, 'Sweet Leaf', NULL), (101, 1, 10, 'Into The Void - 2009 Remaster', NULL),
                (102, 1, 10, 'Sweet Leaf (Live)', NULL), (200, 2, 20, 'Silvera', 'rec-silvera'), (201, 2, 20, 'Stranded', NULL);
            INSERT INTO scrobbles (song_id, artist_id, album_id, played_at, raw_artist_text, raw_track_text) VALUES
                (100, 1, 10, '2026-01-01T10:00:00+00:00', 'Black Sabbath', 'Sweet Leaf'), (102, 1, 10, '2026-01-02T10:00:00+00:00', 'Black Sabbath', 'Sweet Leaf (Live)');
            INSERT INTO album_tracklists (album_id, position, number, title, recording_mbid, length_ms) VALUES
                (20, 1, '1', 'The Shooting Star', 'rec-star', 342000), (20, 2, '2', 'Silver-a', 'rec-silvera', 213000), (20, 3, '3', 'Stranded', NULL, 269000);
            INSERT INTO vinyl_holdings (id, album_id, discogs_release_id, raw_artist_text, raw_title_text) VALUES (1, 10, 555, 'Black Sabbath', 'Master Of Reality');
        """)
        conn.execute("INSERT INTO vinyl_details (holding_id, tracklist) VALUES (1, ?)", (json.dumps([
            {"position": "A1", "title": "Sweet Leaf", "duration": "5:05", "type": "track"},
            {"position": "A3", "title": "Embryo", "duration": "", "type": "track"},              # never played, no length on the pressing
            {"position": "B4", "title": "Into The Void", "duration": "6:12", "type": "track"}]),))
        conn.commit()
        conn.close()
        self.conn = connect()

    def tearDown(self):
        self.conn.close()

    def length(self, song_id):
        return self.conn.execute("SELECT length_ms, length_source FROM songs WHERE id = ?", (song_id,)).fetchone()

    def test_tracklists_first_and_a_live_version_keeps_its_own(self):
        song_lengths.from_tracklists(self.conn)
        self.assertEqual(self.length(200), (213000, "tracklist"))          # the same recording, whatever it's called
        self.assertEqual(self.length(201), (269000, "tracklist"))          # the same title on its album
        self.assertEqual(self.length(100), (305000, "pressing"))           # your pressing's tracklist
        self.assertEqual(self.length(101), (372000, "pressing"))           # a remaster is the same track
        self.assertEqual(self.length(102), (None, None))                   # a live version: not the studio length

    def test_lastfm_for_the_rest_and_lines_never_played(self):
        song_lengths.from_tracklists(self.conn)
        os.environ["LASTFM_API_KEY"] = "test-key"
        answers = {"Sweet Leaf (Live)": 331000, "Embryo": 23000}
        with mock.patch.object(song_lengths, "lastfm_length", lambda s, k, artist, title: answers.get(title)), \
                mock.patch.object(song_lengths, "LASTFM_GAP", 0):
            song_lengths.run(100, log=lambda *_: None)
        self.assertEqual(self.length(102), (331000, "lastfm"))
        rows = dict(self.conn.execute("SELECT title, length_ms FROM track_lengths WHERE album_id = 10"))
        self.assertEqual(rows, {"Embryo": 23000})                          # Embryo, never played, has a length now
        # nothing asked twice: a second run has nothing left to look up
        with mock.patch.object(song_lengths, "lastfm_length", side_effect=AssertionError("asked again")):
            song_lengths.run(100, log=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
