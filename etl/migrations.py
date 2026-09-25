"""
Ordered, idempotent schema migrations for an *existing* data/music.sqlite.

schema.sql is only ever applied with CREATE ... IF NOT EXISTS, which can add a brand-new
table to an old database but can never add a column to an existing one -- so until now every
column addition was a one-off hand-run ALTER. This is the replacement: each migration runs
exactly once per database, tracked in schema_migrations, and they only ever *add* structure
(tables, columns, indexes) -- never touch existing rows.

schema.sql is kept in sync by hand so a fresh `init_db.py --fresh` build ends up identical;
init_db.py then runs these too, and each one is written to be a no-op when its change is
already present (so a fresh database just gets every id recorded as applied).

Run automatically at maintenance-server startup and by init_db.py; can also be run directly:
    python etl/migrations.py
"""
import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _run_statements(conn: sqlite3.Connection, sql: str) -> None:
    # Not executescript(): that silently COMMITs first, which would break migrate()'s
    # one-transaction-per-migration guarantee. None of these statements contain a literal ';'.
    for stmt in sql.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def _m001_maintenance_review_tables(conn: sqlite3.Connection) -> None:
    _run_statements(
        conn,
        """
        CREATE TABLE IF NOT EXISTS mb_cache (
            key          TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            fetched_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS suggestions (
            id            INTEGER PRIMARY KEY,
            entity_type   TEXT NOT NULL CHECK (entity_type IN ('artist','album','song')),
            entity_id     INTEGER NOT NULL,
            mbid          TEXT NOT NULL,
            label         TEXT,
            confidence    REAL NOT NULL,
            tier          TEXT NOT NULL CHECK (tier IN ('high','medium','low')),
            source        TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            decided_at    TEXT,
            UNIQUE(entity_type, entity_id, mbid)
        );
        CREATE INDEX IF NOT EXISTS idx_suggestions_entity ON suggestions(entity_type, entity_id);

        CREATE TABLE IF NOT EXISTS review_marks (
            id           INTEGER PRIMARY KEY,
            entity_type  TEXT NOT NULL CHECK (entity_type IN ('artist','album','song')),
            entity_id    INTEGER NOT NULL,
            mark         TEXT NOT NULL,
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(entity_type, entity_id, mark)
        );

        CREATE TABLE IF NOT EXISTS edit_log (
            id            INTEGER PRIMARY KEY,
            entity_type   TEXT NOT NULL CHECK (entity_type IN ('artist','album','song')),
            entity_id     INTEGER NOT NULL,
            entity_name   TEXT NOT NULL,
            changes_json  TEXT NOT NULL,
            reason        TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            undone_at     TEXT
        );
        """
    )
    cols = _columns(conn, "merge_log")
    if "undo_json" not in cols:
        conn.execute("ALTER TABLE merge_log ADD COLUMN undo_json TEXT")
    if "undone_at" not in cols:
        conn.execute("ALTER TABLE merge_log ADD COLUMN undone_at TEXT")


def _m002_vinyl_pressings(conn: sqlite3.Connection) -> None:
    """Each vinyl holding can be linked to its exact MusicBrainz *release* (the pressing), not
    just its album's release group; and the review tables learn a 'vinyl' entity type (for
    per-holding suggestions/edits). The CHECK constraints can't be altered in place, so those
    three small internal tables are rebuilt -- same columns, same rows, one extra allowed value."""
    if "mb_release_id" not in _columns(conn, "vinyl_holdings"):
        conn.execute("ALTER TABLE vinyl_holdings ADD COLUMN mb_release_id TEXT")
    for table in ("suggestions", "review_marks", "edit_log"):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
        if "'vinyl'" in sql:
            continue
        old_check = "CHECK (entity_type IN ('artist','album','song'))"
        if old_check not in sql:
            raise RuntimeError(f"{table}: unexpected CHECK constraint, refusing to rebuild: {sql}")
        new_sql = sql.replace(old_check, "CHECK (entity_type IN ('artist','album','song','vinyl'))")
        new_sql = new_sql.replace(f"CREATE TABLE {table}", f"CREATE TABLE {table}__new", 1)
        cols = ", ".join(r[1] for r in conn.execute(f"PRAGMA table_info({table})"))
        before = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        conn.execute(new_sql)
        conn.execute(f"INSERT INTO {table}__new ({cols}) SELECT {cols} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}__new RENAME TO {table}")
        after = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if before != after:
            raise RuntimeError(f"{table}: row count changed during rebuild ({before} -> {after})")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_suggestions_entity ON suggestions(entity_type, entity_id)")


def _m003_alias_renamed_titles(conn: sqlite3.Connection) -> None:
    """Renames made before merge.alias_old_titles existed left their old spellings unrouted --
    the next import of one would recreate a duplicate. Add the aliases now (insert-only; an
    existing override for the same key is left alone)."""
    import json
    rows = conn.execute("SELECT entity_type, entity_id, changes_json FROM edit_log WHERE undone_at IS NULL AND changes_json LIKE '%\"title\"%'").fetchall()
    for entity_type, entity_id, changes in rows:
        if entity_type not in ("album", "song"):
            continue
        old_title = (json.loads(changes).get("title") or [None])[0]
        table = "albums" if entity_type == "album" else "songs"
        row = conn.execute(f"SELECT id, artist_id, title FROM {table} WHERE id = ?", (entity_id,)).fetchone()
        if not old_title or not row:
            continue
        raw_queries = {
            "album": [("lastfm", "SELECT DISTINCT raw_album_text FROM scrobbles WHERE album_id = ?"),
                      ("discogs", "SELECT DISTINCT raw_title_text FROM vinyl_holdings WHERE album_id = ?")],
            "song": [("lastfm", "SELECT DISTINCT raw_track_text FROM scrobbles WHERE song_id = ?"),
                     ("setlistfm", "SELECT DISTINCT raw_song_text FROM setlist_songs WHERE song_id = ?")],
        }[entity_type]
        for source, query in raw_queries:
            raws = {old_title} | {r[0] for r in conn.execute(query, (row[0],)) if r[0]}
            for raw in raws:
                if raw.strip().lower() == row[2].strip().lower():
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO alias_overrides (source, source_key, canonical_type, canonical_id, note) VALUES (?, ?, ?, ?, ?)",
                    (source, f"{row[1]}:{raw.strip().lower()}", entity_type, row[0], "backfilled alias for a rename"))


def _m004_artist_mb_aliases(conn: sqlite3.Connection) -> None:
    """Extra MusicBrainz artist ids a local artist also releases under -- e.g. local "Jimi
    Hendrix" also owns "The Jimi Hendrix Experience" (a separate artist on MusicBrainz), so
    identity checks, searches, song lookups and imports treat that credit as the same artist.
    One MusicBrainz artist can belong to only one local artist (UNIQUE mbid)."""
    _run_statements(conn, """
        CREATE TABLE IF NOT EXISTS artist_mb_aliases (
            id            INTEGER PRIMARY KEY,
            artist_id     INTEGER NOT NULL REFERENCES artists(id),
            mbid          TEXT NOT NULL UNIQUE,
            name          TEXT,
            override_ids  TEXT NOT NULL DEFAULT '[]',
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_artist_mb_aliases_artist ON artist_mb_aliases(artist_id)
    """)


def _m005_releases_and_import_inbox(conn: sqlite3.Connection) -> None:
    """Keep MusicBrainz *release* ids (the exact edition) without letting them split albums:
      album_releases     release id -> the album it's an edition of (the album's own mbid is the
                         release GROUP). Also the importer's instant lookup table: an edition seen
                         once is never looked up again.
      scrobble_releases  the exact release a scrobble was played from (as Last.fm reported it).
      import_runs / import_events  what each import created or couldn't place -- the Import inbox.
    Backfills scrobble_releases from the raw Last.fm rows we keep, and seeds album_releases from
    that history (and linked vinyl pressings) wherever a release maps to exactly one album.
    Internal tables -- not part of the public build."""
    import json
    from datetime import datetime, timezone

    _run_statements(conn, """
        CREATE TABLE IF NOT EXISTS album_releases (
            id            INTEGER PRIMARY KEY,
            release_mbid  TEXT NOT NULL UNIQUE,
            album_id      INTEGER NOT NULL REFERENCES albums(id),
            title         TEXT,
            source        TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_album_releases_album ON album_releases(album_id);
        CREATE TABLE IF NOT EXISTS scrobble_releases (
            scrobble_id   INTEGER PRIMARY KEY REFERENCES scrobbles(id),
            release_mbid  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scrobble_releases_release ON scrobble_releases(release_mbid);
        CREATE TABLE IF NOT EXISTS import_runs (
            id            INTEGER PRIMARY KEY,
            source        TEXT NOT NULL,
            started_at    TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at   TEXT,
            summary_json  TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS import_events (
            id            INTEGER PRIMARY KEY,
            run_id        INTEGER REFERENCES import_runs(id),
            kind          TEXT NOT NULL,
            entity_type   TEXT,
            entity_id     INTEGER,
            detail_json   TEXT NOT NULL DEFAULT '{}',
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            reviewed_at   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_import_events_open ON import_events(reviewed_at, kind)
    """)

    # scrobble -> release, from the raw Last.fm rows (matched on the same natural key the importer
    # dedupes on: artist text + track text + timestamp)
    if not conn.execute("SELECT 1 FROM scrobble_releases LIMIT 1").fetchone():
        release_of = {}
        for (raw,) in conn.execute("SELECT raw_json FROM staging_lastfm_rows"):
            t = json.loads(raw)
            rel = (t.get("album") or {}).get("mbid")
            uts = (t.get("date") or {}).get("uts")
            if not rel or not uts:
                continue
            played = datetime.fromtimestamp(int(uts), tz=timezone.utc).isoformat()
            release_of[((t.get("artist") or {}).get("#text", "").strip(), (t.get("name") or "").strip(), played)] = rel
        rows = [(sid, release_of[(a, tr, p)]) for sid, a, tr, p in
                conn.execute("SELECT id, raw_artist_text, raw_track_text, played_at FROM scrobbles WHERE source = 'lastfm'")
                if (a, tr, p) in release_of]
        conn.executemany("INSERT OR IGNORE INTO scrobble_releases (scrobble_id, release_mbid) VALUES (?, ?)", rows)

    # release -> album, wherever the history is unambiguous (one album per release)
    if not conn.execute("SELECT 1 FROM album_releases LIMIT 1").fetchone():
        conn.execute("""
            INSERT OR IGNORE INTO album_releases (release_mbid, album_id, source)
            SELECT sr.release_mbid, min(s.album_id), 'lastfm history'
            FROM scrobble_releases sr JOIN scrobbles s ON s.id = sr.scrobble_id
            WHERE s.album_id IS NOT NULL
            GROUP BY sr.release_mbid HAVING count(DISTINCT s.album_id) = 1
        """)
        conn.execute("""
            INSERT OR IGNORE INTO album_releases (release_mbid, album_id, source)
            SELECT mb_release_id, min(album_id), 'vinyl pressing' FROM vinyl_holdings
            WHERE mb_release_id IS NOT NULL GROUP BY mb_release_id HAVING count(DISTINCT album_id) = 1
        """)


def _m006_song_tracks(conn: sqlite3.Connection) -> None:
    """Last.fm's per-track "mbid" is sometimes a MusicBrainz *recording* id (the song) and
    sometimes a *track* id (that recording's slot on one specific release) -- both used to be
    stored as songs.mbid. Now songs.mbid is only ever a recording id, and:
      song_tracks      track id -> the song it's a track of (with its release). Also the
                       importer's instant lookup: a track id seen once needs no lookup again.
      scrobble_tracks  the raw id Last.fm sent for a scrobble's track, whatever kind it was.
    Backfills scrobble_tracks from the raw Last.fm rows; song_tracks fills as track ids are
    resolved (at import, or by Songs > Recording ids). Internal -- not part of the public build."""
    import json
    from datetime import datetime, timezone

    _run_statements(conn, """
        CREATE TABLE IF NOT EXISTS song_tracks (
            id            INTEGER PRIMARY KEY,
            track_mbid    TEXT NOT NULL UNIQUE,
            song_id       INTEGER NOT NULL REFERENCES songs(id),
            release_mbid  TEXT,
            source        TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_song_tracks_song ON song_tracks(song_id);
        CREATE TABLE IF NOT EXISTS scrobble_tracks (
            scrobble_id   INTEGER PRIMARY KEY REFERENCES scrobbles(id),
            lastfm_mbid   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scrobble_tracks_mbid ON scrobble_tracks(lastfm_mbid)
    """)
    if not conn.execute("SELECT 1 FROM scrobble_tracks LIMIT 1").fetchone():
        id_of = {}
        for (raw,) in conn.execute("SELECT raw_json FROM staging_lastfm_rows"):
            t = json.loads(raw)
            mbid, uts = t.get("mbid"), (t.get("date") or {}).get("uts")
            if not mbid or not uts:
                continue
            played = datetime.fromtimestamp(int(uts), tz=timezone.utc).isoformat()
            id_of[((t.get("artist") or {}).get("#text", "").strip(), (t.get("name") or "").strip(), played)] = mbid
        rows = [(sid, id_of[(a, tr, p)]) for sid, a, tr, p in
                conn.execute("SELECT id, raw_artist_text, raw_track_text, played_at FROM scrobbles WHERE source = 'lastfm'")
                if (a, tr, p) in id_of]
        conn.executemany("INSERT OR IGNORE INTO scrobble_tracks (scrobble_id, lastfm_mbid) VALUES (?, ?)", rows)


def _m007_genres_and_pressings(conn: sqlite3.Connection) -> None:
    """Genres as a first-class tag, plus full Discogs pressing detail for the vinyl collection.
      genres          one row per genre name (MusicBrainz's vocabulary; Discogs styles join it).
                      parent_id is for a later genre map (MusicBrainz genres have "subgenre of").
      album_genres    album (release group) -> genre, with its source and MusicBrainz vote count.
                      Artists never get their own rows: they inherit through artist_genres (a view).
      vinyl_details   one pressing's Discogs detail: country, date, format descriptions, identifiers
                      (barcode, matrix/runout), companies (pressing plant...), tracklist, notes.
      genre_hidden    a genre removed from an album by hand -- refreshes never bring it back.
      genre_rules     a genre hidden everywhere, or merged into another (synonyms).
    genres/album_genres/vinyl_details/artist_genres are public; the rest are internal."""
    _run_statements(conn, """
        CREATE TABLE IF NOT EXISTS genres (
            id            INTEGER PRIMARY KEY,
            name          TEXT NOT NULL UNIQUE,
            mbid          TEXT UNIQUE,
            parent_id     INTEGER REFERENCES genres(id),
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS album_genres (
            album_id      INTEGER NOT NULL REFERENCES albums(id),
            genre_id      INTEGER NOT NULL REFERENCES genres(id),
            source        TEXT NOT NULL CHECK (source IN ('musicbrainz', 'discogs', 'manual')),
            votes         INTEGER,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (album_id, genre_id)
        );
        CREATE INDEX IF NOT EXISTS idx_album_genres_genre ON album_genres(genre_id);
        CREATE TABLE IF NOT EXISTS vinyl_details (
            holding_id          INTEGER PRIMARY KEY REFERENCES vinyl_holdings(id),
            country             TEXT,
            released            TEXT,
            year                INTEGER,
            format_descriptions TEXT,
            format_text         TEXT,
            identifiers         TEXT,
            companies           TEXT,
            tracklist           TEXT,
            discogs_notes       TEXT,
            genres              TEXT,
            styles              TEXT,
            fetched_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS genre_hidden (
            album_id      INTEGER NOT NULL REFERENCES albums(id),
            genre_id      INTEGER NOT NULL REFERENCES genres(id),
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (album_id, genre_id)
        );
        CREATE TABLE IF NOT EXISTS genre_rules (
            genre_id         INTEGER PRIMARY KEY REFERENCES genres(id),
            action           TEXT NOT NULL CHECK (action IN ('hide', 'merge')),
            target_genre_id  INTEGER REFERENCES genres(id),
            created_at       TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE VIEW IF NOT EXISTS artist_genres AS
            SELECT aa.artist_id, ag.genre_id,
                   count(DISTINCT ag.album_id) AS albums,
                   count(DISTINCT CASE WHEN EXISTS (SELECT 1 FROM vinyl_holdings v WHERE v.album_id = ag.album_id) THEN ag.album_id END) AS vinyl_albums
            FROM album_genres ag JOIN album_artists aa ON aa.album_id = ag.album_id
            GROUP BY aa.artist_id, ag.genre_id
    """)


def _m008_pressing_year_and_disc_colour(conn: sqlite3.Connection) -> None:
    """Two public columns on vinyl_holdings:
      pressing_year  the year THIS pressing came out (Discogs' "Released"), as distinct from the
                     album's original year (albums.year) -- an anniversary reissue is a 2021
                     pressing of a 1986 album. Backfilled from the raw Discogs export rows.
      disc_colour    the record's colour set by hand when the format text doesn't say (or says it
                     wrong): JSON {"colours": ["red", "black"], "effect": "marbled"}; NULL = as detected."""
    import json
    import re

    cols = {r[1] for r in conn.execute("PRAGMA table_info(vinyl_holdings)")}
    if "pressing_year" not in cols:
        conn.execute("ALTER TABLE vinyl_holdings ADD COLUMN pressing_year INTEGER")
    if "disc_colour" not in cols:
        conn.execute("ALTER TABLE vinyl_holdings ADD COLUMN disc_colour TEXT")
    released = {}
    for (raw,) in conn.execute("SELECT raw_json FROM staging_discogs_rows"):
        row = json.loads(raw)
        rel, when = str(row.get("release_id") or "").strip(), str(row.get("Released") or "")
        m = re.search(r"\b(1[89]\d\d|20\d\d)\b", when)
        if rel.isdigit() and m:
            released[int(rel)] = int(m.group(1))
    conn.executemany("UPDATE vinyl_holdings SET pressing_year = ? WHERE discogs_release_id = ? AND pressing_year IS NULL",
                     [(y, rid) for rid, y in released.items()])


def _m009_album_tracklists(conn: sqlite3.Connection) -> None:
    """The album's own tracklist for albums not on vinyl (vinyl albums use the pressing you own):
    MusicBrainz's earliest official release of the release group -- the album as first released,
    so deluxe / remaster extras are recognisably bonus tracks. Filled by the album-tracklists
    sweep. Public: the site splits an album page into "Tracklist" and "Bonus & other tracks"."""
    _run_statements(conn, """
        CREATE TABLE IF NOT EXISTS album_tracklists (
            album_id        INTEGER NOT NULL REFERENCES albums(id),
            position        INTEGER NOT NULL,
            number          TEXT,
            disc            INTEGER,
            title           TEXT NOT NULL,
            recording_mbid  TEXT,
            length_ms       INTEGER,
            PRIMARY KEY (album_id, position)
        );
        CREATE TABLE IF NOT EXISTS album_tracklist_sources (
            album_id        INTEGER PRIMARY KEY REFERENCES albums(id),
            source          TEXT NOT NULL,
            release_mbid    TEXT,
            release_title   TEXT,
            release_date    TEXT,
            country         TEXT,
            format          TEXT,
            fetched_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)


def _m010_copy_look(conn: sqlite3.Connection) -> None:
    """A copy's own look, for when one album has records that are really distinct releases
    (Electric Ladyland Part 1 / Part 2 / the double; a picture disc beside the original). All
    public, all NULL = as the album:
      cover_file     its own cover image in the covers dir (h<id>-<hash>.jpg -- a new name per
                     image, so undo can point back at the previous one and browsers never cache stale)
      display_title  what the site calls this record ("Electric Ladyland Part 1")
      release_year   the year this release first came out, where it differs from the album's"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(vinyl_holdings)")}
    for col, typ in (("cover_file", "TEXT"), ("display_title", "TEXT"), ("release_year", "INTEGER")):
        if col not in cols:
            conn.execute(f"ALTER TABLE vinyl_holdings ADD COLUMN {col} {typ}")


def _m011_album_parts(conn: sqlite3.Connection) -> None:
    """A record that's a SET of albums -- a 2-on-1 / double pack like 1976's "Rainbow Rising /
    Ritchie Blackmore's Rainbow" gatefold -- has no release group of its own on MusicBrainz. Its
    album row lists the albums it contains here, so it counts as identified (by its parts), and
    the site shows their plays / songs on the record and the record on their pages. Public."""
    _run_statements(conn, """
        CREATE TABLE IF NOT EXISTS album_parts (
            album_id        INTEGER NOT NULL REFERENCES albums(id),  -- the set
            part_album_id   INTEGER NOT NULL REFERENCES albums(id),  -- an album it contains
            position        INTEGER NOT NULL,
            PRIMARY KEY (album_id, part_album_id)
        );
        CREATE INDEX IF NOT EXISTS idx_album_parts_part ON album_parts(part_album_id)
    """)


def _m012_track_links(conn: sqlite3.Connection) -> None:
    """A tracklist line you've matched to a song by hand, where the titles don't say so (the UK
    pressing's "Come On (Part 1)" = the scrobbled "Come On (Let the Good Times Roll)"). Keyed by
    the album and the track's title as the tracklist spells it, so any pressing's tracklist, or
    MusicBrainz's, listing that title picks it up. Rows have ids so song / album merges carry them
    (merge._move, journaled). Public: the site's tracklists honour them too."""
    _run_statements(conn, """
        CREATE TABLE IF NOT EXISTS track_links (
            id              INTEGER PRIMARY KEY,
            album_id        INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
            track_title     TEXT NOT NULL,
            song_id         INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_track_links_album ON track_links(album_id);
        CREATE INDEX IF NOT EXISTS idx_track_links_song ON track_links(song_id)
    """)


MIGRATIONS = [
    ("001_maintenance_review_tables", _m001_maintenance_review_tables),
    ("002_vinyl_pressings", _m002_vinyl_pressings),
    ("003_alias_renamed_titles", _m003_alias_renamed_titles),
    ("004_artist_mb_aliases", _m004_artist_mb_aliases),
    ("005_releases_and_import_inbox", _m005_releases_and_import_inbox),
    ("006_song_tracks", _m006_song_tracks),
    ("007_genres_and_pressings", _m007_genres_and_pressings),
    ("008_pressing_year_and_disc_colour", _m008_pressing_year_and_disc_colour),
    ("009_album_tracklists", _m009_album_tracklists),
    ("010_copy_look", _m010_copy_look),
    ("011_album_parts", _m011_album_parts),
    ("012_track_links", _m012_track_links),
]


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Applies every not-yet-applied migration, each in its own transaction. Returns the ids
    applied this call (empty when already up to date)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " id TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.commit()
    done = {r[0] for r in conn.execute("SELECT id FROM schema_migrations")}
    applied = []
    for mig_id, fn in MIGRATIONS:
        if mig_id in done:
            continue
        try:
            conn.execute("BEGIN IMMEDIATE")
            fn(conn)
            conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (mig_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        applied.append(mig_id)
    return applied


if __name__ == "__main__":
    from common import connect

    c = connect()
    try:
        ids = migrate(c)
    finally:
        c.close()
    print("Applied: " + ", ".join(ids) if ids else "Already up to date.")
