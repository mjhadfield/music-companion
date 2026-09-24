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
    id                INTEGER PRIMARY KEY,
    mbid              TEXT UNIQUE,              -- MusicBrainz release-group id
    artist_id         INTEGER NOT NULL REFERENCES artists(id),  -- primary/display artist (convenience; see album_artists for the full credit)
    title             TEXT NOT NULL,
    year              INTEGER,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    -- Cover art is fetched once (from the Cover Art Archive, via mbid, or a manually pasted
    -- URL) and stored locally as data/covers/{id}.jpg -- these two columns just track that
    -- fetch's outcome; the image bytes themselves live on disk, not in this table. NULL means
    -- never attempted. 'ok' means data/covers/{id}.jpg exists and is real usable art. 'none'
    -- means a fetch was tried and came back with nothing (a real, recorded outcome, not the
    -- same as "haven't checked yet" -- keeps a completeness sweep from re-trying it forever).
    -- Column order matters here: appended after created_at, not grouped with the rest of an
    -- album's identity above, because build_public_db.py's `INSERT ... SELECT *` is
    -- positional -- these must land in the same order ALTER TABLE actually put them in on the
    -- live database (new columns always append at the end), or that insert quietly shuffles
    -- values into the wrong columns instead of failing loudly.
    cover_status      TEXT CHECK (cover_status IN ('ok', 'none')),
    cover_updated_at  TEXT
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
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    -- The exact MusicBrainz *release* this pressing is (albums.mbid is the release *group*, i.e.
    -- the work across every pressing). Set when a human accepts MusicBrainz's own Discogs-URL
    -- link for this discogs_release_id; its release group should equal the album's mbid.
    -- After created_at because ALTER TABLE ADD COLUMN appends (see migrations.py).
    mb_release_id           TEXT
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
    merged_at        TEXT NOT NULL DEFAULT (datetime('now')),
    -- Everything needed to reverse this merge row-for-row (see etl/maintenance/merge.py's
    -- undo_merge); NULL for merges made before the undo journal existed.
    undo_json        TEXT,
    undone_at        TEXT
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
-- Maintenance review state (internal housekeeping only: not public)
-- ---------------------------------------------------------------------
-- Kept in sync with etl/migrations.py, which adds these to databases that predate them.

-- Cached MusicBrainz responses, keyed by request ("lookup:artist:<mbid>", "search:..."), so a
-- lookup is only ever made once per expiry window -- see etl/maintenance/mbcache.py.
CREATE TABLE IF NOT EXISTS mb_cache (
    key          TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    fetched_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A proposed mbid for an entity, from an automated sweep -- never applied until a human
-- accepts it. Rejected rows stay, so the same guess isn't proposed again.
CREATE TABLE IF NOT EXISTS suggestions (
    id            INTEGER PRIMARY KEY,
    entity_type   TEXT NOT NULL CHECK (entity_type IN ('artist','album','song','vinyl')),
    entity_id     INTEGER NOT NULL,
    mbid          TEXT NOT NULL,
    label         TEXT,                 -- display name of what the mbid points at
    confidence    REAL NOT NULL,        -- 0-100
    tier          TEXT NOT NULL CHECK (tier IN ('high','medium','low')),
    source        TEXT NOT NULL,        -- 'mb-search' | 'discogs-link' | ...
    evidence_json TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at    TEXT,
    UNIQUE(entity_type, entity_id, mbid)
);
CREATE INDEX IF NOT EXISTS idx_suggestions_entity ON suggestions(entity_type, entity_id);

-- "A human looked at this and it's fine" markers -- e.g. a verify flag marked "looks right"
-- ('verified:<check>') or an album whose songs have been reviewed ('songs-reviewed').
CREATE TABLE IF NOT EXISTS review_marks (
    id           INTEGER PRIMARY KEY,
    entity_type  TEXT NOT NULL CHECK (entity_type IN ('artist','album','song','vinyl')),
    entity_id    INTEGER NOT NULL,
    mark         TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(entity_type, entity_id, mark)
);

-- Extra MusicBrainz artist ids a local artist also releases under (e.g. "Jimi Hendrix" also owns
-- "The Jimi Hendrix Experience"), so checks, searches, song lookups and imports treat that credit
-- as the same artist. override_ids: the alias_overrides rows created with it (removed with it).
CREATE TABLE IF NOT EXISTS artist_mb_aliases (
    id            INTEGER PRIMARY KEY,
    artist_id     INTEGER NOT NULL REFERENCES artists(id),
    mbid          TEXT NOT NULL UNIQUE,
    name          TEXT,
    override_ids  TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_artist_mb_aliases_artist ON artist_mb_aliases(artist_id);

-- Editions: MusicBrainz *release* ids (the exact edition) -> the album they belong to. The album's
-- own mbid is the release GROUP (what stats and matching use); this keeps the edition detail
-- without letting it split albums, and is the importer's instant lookup table.
CREATE TABLE IF NOT EXISTS album_releases (
    id            INTEGER PRIMARY KEY,
    release_mbid  TEXT NOT NULL UNIQUE,
    album_id      INTEGER NOT NULL REFERENCES albums(id),
    title         TEXT,
    source        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_album_releases_album ON album_releases(album_id);

-- The exact release each scrobble was played from, as Last.fm reported it.
CREATE TABLE IF NOT EXISTS scrobble_releases (
    scrobble_id   INTEGER PRIMARY KEY REFERENCES scrobbles(id),
    release_mbid  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scrobble_releases_release ON scrobble_releases(release_mbid);

-- Tracks: MusicBrainz *track* ids (a recording's slot on one specific release) -> the song. A
-- song's own mbid is the RECORDING; Last.fm sends either kind in the same field, so track ids
-- are kept here instead -- and a track id seen once routes future imports straight to the song.
CREATE TABLE IF NOT EXISTS song_tracks (
    id            INTEGER PRIMARY KEY,
    track_mbid    TEXT NOT NULL UNIQUE,
    song_id       INTEGER NOT NULL REFERENCES songs(id),
    release_mbid  TEXT,
    source        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_song_tracks_song ON song_tracks(song_id);

-- The raw id Last.fm sent for each scrobble's track (a recording OR a track id -- unverified).
CREATE TABLE IF NOT EXISTS scrobble_tracks (
    scrobble_id   INTEGER PRIMARY KEY REFERENCES scrobbles(id),
    lastfm_mbid   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scrobble_tracks_mbid ON scrobble_tracks(lastfm_mbid);

-- The Import inbox: what each import run created or couldn't place, for review.
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
    kind          TEXT NOT NULL,        -- new_artist | new_album | new_song | edition_linked | mbid_clash | unresolved_release | suspect
    entity_type   TEXT,
    entity_id     INTEGER,
    detail_json   TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_import_events_open ON import_events(reviewed_at, kind);

-- Every non-merge identity edit (mbid/title/year) with its previous values, so it can be undone.
CREATE TABLE IF NOT EXISTS edit_log (
    id            INTEGER PRIMARY KEY,
    entity_type   TEXT NOT NULL CHECK (entity_type IN ('artist','album','song','vinyl')),
    entity_id     INTEGER NOT NULL,
    entity_name   TEXT NOT NULL,
    changes_json  TEXT NOT NULL,        -- {"mbid": [old, new], "title": [old, new]}
    reason        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    undone_at     TEXT
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

-- Genres (migration 007): a first-class tag on albums (release groups), from MusicBrainz genres
-- and, for vinyl, Discogs styles. Artists inherit theirs through the artist_genres view.
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
    GROUP BY aa.artist_id, ag.genre_id;
