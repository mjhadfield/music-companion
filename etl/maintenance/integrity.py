"""
Integrity checks for data/music.sqlite -- what the maintenance tool runs after every reviewed batch
(inside the batch's own transaction, before it commits) and shows on the Overview.

Two kinds of finding:
  hard  something points at nothing -- a foreign key, an import alias, a note. A batch that would
        make ANY hard count go up is rolled back instead of committed (api/batch.py), so the tool
        can never leave the database worse than it found it. Pre-existing hard findings don't
        block anything; they're listed with a reviewed fix where one is safe.
  soft  worth a look, not wrong as such (a song filed under an album that doesn't credit its
        artist, two songs with one title...). Shown, never blocking.

Stale "not the same" decisions and review marks that point at merged-away rows are deliberately
NOT cleaned up: undoing that merge brings the row back, and its decisions with it.
"""
import sqlite3

# name -> (severity, description, count SQL, sample SQL returning (id, text))
CHECKS = {
    "fk_violations": ("hard", "Rows whose foreign key points at nothing", None, None),
    "alias_missing_target": (
        "hard", "Import aliases routing to an artist, album or song that no longer exists",
        """SELECT count(*) FROM alias_overrides a WHERE
             (a.canonical_type = 'artist' AND NOT EXISTS (SELECT 1 FROM artists x WHERE x.id = a.canonical_id)) OR
             (a.canonical_type = 'album'  AND NOT EXISTS (SELECT 1 FROM albums  x WHERE x.id = a.canonical_id)) OR
             (a.canonical_type = 'song'   AND NOT EXISTS (SELECT 1 FROM songs   x WHERE x.id = a.canonical_id))""",
        """SELECT a.id, a.source || ' "' || a.source_key || '" -> ' || a.canonical_type || ' #' || a.canonical_id || coalesce(' (' || a.note || ')', '')
           FROM alias_overrides a WHERE
             (a.canonical_type = 'artist' AND NOT EXISTS (SELECT 1 FROM artists x WHERE x.id = a.canonical_id)) OR
             (a.canonical_type = 'album'  AND NOT EXISTS (SELECT 1 FROM albums  x WHERE x.id = a.canonical_id)) OR
             (a.canonical_type = 'song'   AND NOT EXISTS (SELECT 1 FROM songs   x WHERE x.id = a.canonical_id)) LIMIT 50"""),
    "alias_key_missing_artist": (
        "hard", "Album/song import aliases keyed on an artist that no longer exists",
        """SELECT count(*) FROM alias_overrides a WHERE a.canonical_type IN ('album', 'song') AND a.source_key GLOB '[0-9]*:*'
             AND NOT EXISTS (SELECT 1 FROM artists x WHERE x.id = CAST(substr(a.source_key, 1, instr(a.source_key, ':') - 1) AS INTEGER))""",
        """SELECT a.id, a.source || ' "' || a.source_key || '"' FROM alias_overrides a WHERE a.canonical_type IN ('album', 'song')
             AND a.source_key GLOB '[0-9]*:*'
             AND NOT EXISTS (SELECT 1 FROM artists x WHERE x.id = CAST(substr(a.source_key, 1, instr(a.source_key, ':') - 1) AS INTEGER)) LIMIT 50"""),
    "notes_missing_entity": (
        "hard", "Notes attached to something that no longer exists",
        """SELECT count(*) FROM notes n WHERE
             (n.entity_type = 'artist'  AND NOT EXISTS (SELECT 1 FROM artists  x WHERE x.id = n.entity_id)) OR
             (n.entity_type = 'album'   AND NOT EXISTS (SELECT 1 FROM albums   x WHERE x.id = n.entity_id)) OR
             (n.entity_type = 'song'    AND NOT EXISTS (SELECT 1 FROM songs    x WHERE x.id = n.entity_id)) OR
             (n.entity_type = 'setlist' AND NOT EXISTS (SELECT 1 FROM setlists x WHERE x.id = n.entity_id))""",
        """SELECT n.id, n.entity_type || ' #' || n.entity_id || ': ' || substr(n.body_markdown, 1, 60) FROM notes n WHERE
             (n.entity_type = 'artist'  AND NOT EXISTS (SELECT 1 FROM artists  x WHERE x.id = n.entity_id)) OR
             (n.entity_type = 'album'   AND NOT EXISTS (SELECT 1 FROM albums   x WHERE x.id = n.entity_id)) OR
             (n.entity_type = 'song'    AND NOT EXISTS (SELECT 1 FROM songs    x WHERE x.id = n.entity_id)) OR
             (n.entity_type = 'setlist' AND NOT EXISTS (SELECT 1 FROM setlists x WHERE x.id = n.entity_id)) LIMIT 50"""),
    "album_without_credit": (
        "hard", "Albums with no artist credit at all",
        "SELECT count(*) FROM albums al WHERE NOT EXISTS (SELECT 1 FROM album_artists aa WHERE aa.album_id = al.id)",
        """SELECT al.id, al.title FROM albums al WHERE NOT EXISTS (SELECT 1 FROM album_artists aa WHERE aa.album_id = al.id) LIMIT 50"""),
    "play_song_artist_mismatch": (
        "hard", "Plays credited to one artist but filed under another artist's song",
        "SELECT count(*) FROM scrobbles sc JOIN songs s ON s.id = sc.song_id WHERE s.artist_id != sc.artist_id",
        """SELECT sc.id, sc.raw_artist_text || ' — ' || sc.raw_track_text FROM scrobbles sc JOIN songs s ON s.id = sc.song_id
           WHERE s.artist_id != sc.artist_id LIMIT 50"""),
    "song_on_uncredited_album": (
        "soft", "Songs filed under an album that doesn't credit their artist",
        """SELECT count(*) FROM songs s JOIN albums al ON al.id = s.album_id WHERE al.artist_id != s.artist_id
             AND NOT EXISTS (SELECT 1 FROM album_artists aa WHERE aa.album_id = al.id AND aa.artist_id = s.artist_id)""",
        """SELECT s.id, ar.name || ' — ' || s.title || '  (on ' || al.title || ')' FROM songs s JOIN albums al ON al.id = s.album_id
           JOIN artists ar ON ar.id = s.artist_id WHERE al.artist_id != s.artist_id
             AND NOT EXISTS (SELECT 1 FROM album_artists aa WHERE aa.album_id = al.id AND aa.artist_id = s.artist_id) LIMIT 50"""),
    "same_title_songs": (
        "soft", "One artist with two songs of exactly the same title (imports can only find one of them)",
        "SELECT count(*) FROM (SELECT 1 FROM songs GROUP BY artist_id, lower(title) HAVING count(*) > 1)",
        """SELECT min(s.id), ar.name || ' — ' || s.title || ' (' || count(*) || ' rows)' FROM songs s JOIN artists ar ON ar.id = s.artist_id
           GROUP BY s.artist_id, lower(s.title) HAVING count(*) > 1 LIMIT 50"""),
    "orphan_songs": (
        "soft", "Songs with no plays, live shows or tracklist links",
        """SELECT count(*) FROM songs s WHERE NOT EXISTS (SELECT 1 FROM scrobbles x WHERE x.song_id = s.id)
             AND NOT EXISTS (SELECT 1 FROM setlist_songs y WHERE y.song_id = s.id) AND NOT EXISTS (SELECT 1 FROM track_links t WHERE t.song_id = s.id)""",
        """SELECT s.id, ar.name || ' — ' || s.title FROM songs s JOIN artists ar ON ar.id = s.artist_id
           WHERE NOT EXISTS (SELECT 1 FROM scrobbles x WHERE x.song_id = s.id)
             AND NOT EXISTS (SELECT 1 FROM setlist_songs y WHERE y.song_id = s.id) AND NOT EXISTS (SELECT 1 FROM track_links t WHERE t.song_id = s.id) LIMIT 50"""),
    "stale_decisions": (
        "info", "\"Not the same\" decisions and review marks about merged-away rows (kept: an undo brings them back into use)",
        """SELECT (SELECT count(*) FROM duplicate_dismissals d WHERE
                    (d.entity_type = 'album'  AND (NOT EXISTS (SELECT 1 FROM albums  x WHERE x.id = d.entity_id_a) OR NOT EXISTS (SELECT 1 FROM albums  x WHERE x.id = d.entity_id_b))) OR
                    (d.entity_type = 'song'   AND (NOT EXISTS (SELECT 1 FROM songs   x WHERE x.id = d.entity_id_a) OR NOT EXISTS (SELECT 1 FROM songs   x WHERE x.id = d.entity_id_b))) OR
                    (d.entity_type = 'artist' AND (NOT EXISTS (SELECT 1 FROM artists x WHERE x.id = d.entity_id_a) OR NOT EXISTS (SELECT 1 FROM artists x WHERE x.id = d.entity_id_b))))
              + (SELECT count(*) FROM review_marks m WHERE
                    (m.entity_type = 'album'  AND NOT EXISTS (SELECT 1 FROM albums  x WHERE x.id = m.entity_id)) OR
                    (m.entity_type = 'song'   AND NOT EXISTS (SELECT 1 FROM songs   x WHERE x.id = m.entity_id)) OR
                    (m.entity_type = 'artist' AND NOT EXISTS (SELECT 1 FROM artists x WHERE x.id = m.entity_id)))""",
        None),
}


def counts(conn: sqlite3.Connection, *, hard_only: bool = False) -> dict[str, int]:
    """Every check's current count (cheap -- runs inside each batch transaction)."""
    out = {}
    for name, (severity, _desc, count_sql, _sample) in CHECKS.items():
        if hard_only and severity != "hard":
            continue
        if name == "fk_violations":
            out[name] = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        else:
            out[name] = conn.execute(count_sql).fetchone()[0]
    return out


def worsened(before: dict[str, int], after: dict[str, int]) -> list[str]:
    """Hard checks whose count went up -- a batch that does this is rolled back."""
    return [k for k, v in after.items() if CHECKS[k][0] == "hard" and v > before.get(k, 0)]


def report(conn: sqlite3.Connection, *, full: bool = False) -> dict:
    """For the Overview: each check with its count and up to 50 examples. `full` adds SQLite's
    own page-level check (quick_check -- a few seconds on a big database)."""
    found = counts(conn)
    items = []
    for name, (severity, desc, _count, sample_sql) in CHECKS.items():
        n = found[name]
        examples = []
        if n and name == "fk_violations":
            examples = [{"id": r[1], "text": f"{r[0]} row {r[1]} -> {r[2]}"} for r in conn.execute("PRAGMA foreign_key_check").fetchall()[:50]]
        elif n and sample_sql:
            examples = [{"id": r[0], "text": r[1]} for r in conn.execute(sample_sql).fetchall()]
        items.append({"check": name, "severity": severity, "description": desc, "count": n, "examples": examples,
                      "fixable": name == "alias_missing_target"})
    out = {"items": items, "hard": sum(i["count"] for i in items if i["severity"] == "hard")}
    if full:
        out["quickCheck"] = [r[0] for r in conn.execute("PRAGMA quick_check").fetchall()]
    return out
