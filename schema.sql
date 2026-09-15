-- Music Companion schema
-- SQLite. This file is the single source of truth for structure;
-- etl/init_db.py applies it to build/rebuild data/music.sqlite.
--
-- Design notes:
--   * Canonical entities (artists/albums/songs) are keyed by our own
--     integer id, but carry a MusicBrainz ID (mbid) where known -- that's
--     the glue used to match the same artist/album/song across Discogs,
--     Last.fm and Setlist.fm, which each spell names slightly differently.
--   * Raw pulls from each source land in staging_* tables untouched, so we
--     can re-run entity resolution without re-hitting the APIs.
--   * alias_overrides is a manual escape hatch for anything automatic
--     matching gets wrong (or can't resolve at all).

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- Canonical entities
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS artists (
    id          INTEGER PRIMARY KEY,
    mbid        TEXT UNIQUE,              -- MusicBrainz artist id, when known
    name        TEXT NOT NULL,
    sort_name   TEXT,                     -- e.g. "Beatles, The"
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS albums (
    id          INTEGER PRIMARY KEY,
    mbid        TEXT UNIQUE,              -- MusicBrainz release-group id
    artist_id   INTEGER NOT NULL REFERENCES artists(id),  -- primary/display artist (convenience; see album_artists for the full credit)
    title       TEXT NOT NULL,
    year        INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Full artist credit per album (many-to-many). Real releases are often
-- credited to more than one artist -- e.g. "Kiss, Ace Frehley" (the 1978
-- KISS solo album series) or "John Williams, London Symphony Orchestra".
-- albums.artist_id holds just the primary/display artist for convenient
-- joins; this table is the authoritative full credit list, so stats like
-- "how many Kiss records do I own" can find every album Kiss is credited
-- on, not just the ones where Kiss is listed first.
CREATE TABLE IF NOT EXISTS album_artists (
    album_id    INTEGER NOT NULL REFERENCES albums(id),
    artist_id   INTEGER NOT NULL REFERENCES artists(id),
    position    INTEGER NOT NULL DEFAULT 0,   -- order as credited, 0-based
    PRIMARY KEY (album_id, artist_id)
);

CREATE INDEX IF NOT EXISTS idx_album_artists_artist ON album_artists(artist_id);

CREATE TABLE IF NOT EXISTS songs (
    id          INTEGER PRIMARY KEY,
    mbid        TEXT UNIQUE,              -- MusicBrainz recording id
    artist_id   INTEGER NOT NULL REFERENCES artists(id),
    album_id    INTEGER REFERENCES albums(id),   -- primary/first-seen album, if any
    title       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums(artist_id);
CREATE INDEX IF NOT EXISTS idx_songs_artist  ON songs(artist_id);
CREATE INDEX IF NOT EXISTS idx_songs_album   ON songs(album_id);

-- ---------------------------------------------------------------------
-- Physical collection (Discogs)
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS vinyl_holdings (
    id                      INTEGER PRIMARY KEY,
    album_id                INTEGER REFERENCES albums(id),
    discogs_release_id      INTEGER UNIQUE,
    discogs_instance_id     INTEGER,
    catalog_number          TEXT,
    label                   TEXT,
    format                  TEXT,          -- e.g. "LP, Album, Reissue"
    media_condition         TEXT,
    sleeve_condition        TEXT,
    date_added              TEXT,
    rating                  INTEGER,
    notes                   TEXT,
    raw_artist_text         TEXT NOT NULL, -- artist string as Discogs had it (pre-match)
    raw_title_text          TEXT NOT NULL,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_vinyl_album ON vinyl_holdings(album_id);

-- ---------------------------------------------------------------------
-- Listening history (Last.fm)
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS scrobbles (
    id              INTEGER PRIMARY KEY,
    song_id         INTEGER REFERENCES songs(id),
    artist_id       INTEGER REFERENCES artists(id),
    album_id        INTEGER REFERENCES albums(id),
    played_at       TEXT NOT NULL,          -- ISO8601 UTC timestamp
    raw_artist_text TEXT NOT NULL,
    raw_track_text  TEXT NOT NULL,
    raw_album_text  TEXT,
    source          TEXT NOT NULL DEFAULT 'lastfm',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_scrobbles_song    ON scrobbles(song_id);
CREATE INDEX IF NOT EXISTS idx_scrobbles_artist  ON scrobbles(artist_id);
CREATE INDEX IF NOT EXISTS idx_scrobbles_album   ON scrobbles(album_id);
CREATE INDEX IF NOT EXISTS idx_scrobbles_played  ON scrobbles(played_at);

-- ---------------------------------------------------------------------
-- Live history (Setlist.fm)
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS venues (
    id          INTEGER PRIMARY KEY,
    setlistfm_id TEXT UNIQUE,
    name        TEXT NOT NULL,
    city        TEXT,
    state       TEXT,
    country     TEXT
);

CREATE TABLE IF NOT EXISTS setlists (
    id              INTEGER PRIMARY KEY,
    setlistfm_id    TEXT UNIQUE NOT NULL,
    artist_id       INTEGER REFERENCES artists(id),
    venue_id        INTEGER REFERENCES venues(id),
    event_date      TEXT NOT NULL,      -- YYYY-MM-DD
    tour_name       TEXT,
    raw_artist_text TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS setlist_songs (
    id          INTEGER PRIMARY KEY,
    setlist_id  INTEGER NOT NULL REFERENCES setlists(id),
    song_id     INTEGER REFERENCES songs(id),
    position    INTEGER NOT NULL,
    set_name    TEXT,                   -- e.g. "Encore", "Set 1"
    is_cover    INTEGER NOT NULL DEFAULT 0,
    cover_of_artist_text TEXT,
    raw_song_text TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_setlists_artist   ON setlists(artist_id);
CREATE INDEX IF NOT EXISTS idx_setlist_songs_set  ON setlist_songs(setlist_id);
CREATE INDEX IF NOT EXISTS idx_setlist_songs_song ON setlist_songs(song_id);

-- ---------------------------------------------------------------------
-- Reviews / journal (your own writing)
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS notes (
    id            INTEGER PRIMARY KEY,
    entity_type   TEXT NOT NULL CHECK (entity_type IN ('artist','album','song','setlist')),
    entity_id     INTEGER NOT NULL,
    title         TEXT,
    body_markdown TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_notes_entity ON notes(entity_type, entity_id);

-- ---------------------------------------------------------------------
-- Entity resolution overrides
-- ---------------------------------------------------------------------

-- Manual fixups for when automatic matching (name/MBID lookup) gets it
-- wrong or can't resolve at all. ETL/entity-resolution code should always
-- check here first before falling back to fuzzy matching.
CREATE TABLE IF NOT EXISTS alias_overrides (
    id                 INTEGER PRIMARY KEY,
    source             TEXT NOT NULL,        -- 'discogs' | 'lastfm' | 'setlistfm'
    source_key         TEXT NOT NULL,        -- raw text or id from that source
    canonical_type     TEXT NOT NULL CHECK (canonical_type IN ('artist','album','song')),
    canonical_id       INTEGER NOT NULL,
    note               TEXT,
    UNIQUE(source, source_key, canonical_type)
);

-- ---------------------------------------------------------------------
-- Merge audit log
-- ---------------------------------------------------------------------

-- Merging a duplicate entity (e.g. two artist rows for the same act)
-- reassigns every FK reference off the loser and deletes it -- this is
-- the only record left of what that merge did. Internal housekeeping
-- only: not part of the public build (see etl/build_public_db.py).
CREATE TABLE IF NOT EXISTS merge_log (
    id               INTEGER PRIMARY KEY,
    entity_type      TEXT NOT NULL CHECK (entity_type IN ('artist','album','song')),
    absorbed_id      INTEGER NOT NULL,     -- former id of the deleted row -- no FK, the row is gone
    absorbed_name    TEXT NOT NULL,
    absorbed_mbid    TEXT,
    canonical_id     INTEGER NOT NULL,     -- polymorphic target (same pattern as notes.entity_id): no FK
    canonical_name   TEXT NOT NULL,
    rows_moved_json  TEXT NOT NULL,        -- {"songs": 4, "scrobbles": 812, ...}
    merged_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A fuzzy duplicate-name scan (e.g. across all artists) can't tell "Bush"
-- vs "Kate Bush" (different artists, coincidentally one is a substring of
-- the other) from "Bob Marley" vs "Bob Marley & The Wailers" (genuinely
-- the same act) by text alone -- that's a judgment call for a human. This
-- records "I looked, it's not a duplicate" so the same pair doesn't keep
-- resurfacing on every re-scan. Pair order is normalized (a < b) so either
-- direction matches. Internal housekeeping only: not part of the public
-- build.
CREATE TABLE IF NOT EXISTS duplicate_dismissals (
    id            INTEGER PRIMARY KEY,
    entity_type   TEXT NOT NULL CHECK (entity_type IN ('artist','album','song')),
    entity_id_a   INTEGER NOT NULL,     -- the smaller of the two ids
    entity_id_b   INTEGER NOT NULL,     -- the larger of the two ids
    dismissed_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(entity_type, entity_id_a, entity_id_b)
);

-- ---------------------------------------------------------------------
-- Raw staging tables (untouched API/CSV pulls, re-populated on each ETL run)
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS staging_discogs_rows (
    id          INTEGER PRIMARY KEY,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    raw_json    TEXT NOT NULL   -- one row of the Discogs CSV export, as JSON
);

CREATE TABLE IF NOT EXISTS staging_lastfm_rows (
    id          INTEGER PRIMARY KEY,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    raw_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS staging_setlistfm_rows (
    id          INTEGER PRIMARY KEY,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    raw_json    TEXT NOT NULL
);
