"""
Shared helpers used by every ETL script: env loading and the
get-or-create-by-name matching logic for artists/albums/songs.

This is deliberately the *naive* first-pass matcher (case-insensitive
exact name match, optionally backfilling an MBID when a source hands us
one). It's what gets data in the door from each source independently.
The later cross-source entity-resolution pass is what reconciles the
inevitable near-misses (e.g. "Beatles, The" vs "The Beatles") using
MusicBrainz IDs and etl/common's alias_overrides table -- it is not
this module's job.
"""
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "music.sqlite"
ENV_PATH = ROOT / ".env"


def load_env() -> None:
    """Minimal .env loader (no external dependency needed for `KEY=value` lines)."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"Missing {name} -- set it in .env (see .env.example) or the environment."
        )
    return value


def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise SystemExit(f"{DB_PATH} doesn't exist yet -- run `python etl/init_db.py` first.")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _set_mbid_if_free(conn: sqlite3.Connection, table: str, row_id: int, mbid: str) -> None:
    """Best-effort: claim this mbid for this row, unless some other row
    already has it (a source can hand us the same mbid under two different
    text spellings -- e.g. a reissue). When that happens we just leave it
    unset rather than erroring; the entity-resolution pass reconciles it."""
    try:
        conn.execute(f"UPDATE {table} SET mbid = ? WHERE id = ? AND mbid IS NULL", (mbid, row_id))
    except sqlite3.IntegrityError:
        pass


def get_or_create_artist(
    conn: sqlite3.Connection,
    cache: dict,
    name: str,
    mbid: str | None = None,
    source: str | None = None,
) -> int:
    # A manual merge decision beats everything else -- check alias_overrides
    # before either mbid or name matching, exactly as schema.sql's docstring
    # always promised. `source` identifies which importer is asking
    # ('lastfm' | 'setlistfm' | 'discogs'); callers that don't pass one
    # (or a name with no override on file) fall straight through to the
    # matching below, unchanged.
    if source:
        override_key = ("override", source, name.strip().lower())
        if override_key in cache:
            return cache[override_key]
        row = conn.execute(
            "SELECT canonical_id FROM alias_overrides WHERE source = ? AND source_key = ? AND canonical_type = 'artist'",
            (source, name.strip().lower()),
        ).fetchone()
        if row:
            artist_id = row[0]
            cache[override_key] = artist_id
            if mbid:
                _set_mbid_if_free(conn, "artists", artist_id, mbid)
            return artist_id

    # mbid is the stronger signal -- check it first so two different text
    # spellings of the same mbid (e.g. from different sources) resolve to
    # one row instead of colliding on artists.mbid's UNIQUE constraint.
    if mbid:
        mbid_key = ("mbid", mbid)
        if mbid_key in cache:
            return cache[mbid_key]
        row = conn.execute("SELECT id FROM artists WHERE mbid = ?", (mbid,)).fetchone()
        if row:
            cache[mbid_key] = cache[name.lower()] = row[0]
            return row[0]

    key = name.lower()
    if key in cache:
        artist_id = cache[key]
    else:
        row = conn.execute("SELECT id FROM artists WHERE lower(name) = ?", (key,)).fetchone()
        if row:
            artist_id = row[0]
        else:
            cur = conn.execute(
                "INSERT INTO artists (name, sort_name, mbid) VALUES (?, ?, ?)",
                (name, name, mbid or None),
            )
            artist_id = cur.lastrowid
        cache[key] = artist_id
    if mbid:
        _set_mbid_if_free(conn, "artists", artist_id, mbid)
        cache[("mbid", mbid)] = artist_id
    return artist_id


def get_or_create_album(
    conn: sqlite3.Connection,
    cache: dict,
    artist_ids: list[int],
    title: str,
    year: int | None = None,
    mbid: str | None = None,
    source: str | None = None,
) -> int:
    """artist_ids[0] is the primary/display artist; the full credit list
    (relevant when a source hands us more than one artist, e.g. Discogs'
    "Kiss, Ace Frehley") is written to album_artists on first insert.

    `source` mirrors get_or_create_artist's override check: a manual album
    merge closes the loop by writing an alias_overrides row keyed on
    (source, "<primary artist id>:<raw title, lowercased>") so the same
    raw title from that source resolves straight to the merge's survivor
    next time, instead of recreating the duplicate. Scoped by artist id
    (not just title) since two different artists sharing a generic title
    like "Greatest Hits" must not collide on one override. Note this key
    goes stale if that artist row is *itself* later merged into another --
    unlike artist-level overrides, nothing currently repoints it."""
    primary_artist_id = artist_ids[0]
    if source:
        source_key = f"{primary_artist_id}:{title.strip().lower()}"
        override_key = ("override", source, source_key)
        if override_key in cache:
            return cache[override_key]
        row = conn.execute(
            "SELECT canonical_id FROM alias_overrides WHERE source = ? AND source_key = ? AND canonical_type = 'album'",
            (source, source_key),
        ).fetchone()
        if row:
            album_id = row[0]
            cache[override_key] = album_id
            if mbid:
                _set_mbid_if_free(conn, "albums", album_id, mbid)
            return album_id

    if mbid:
        mbid_key = ("mbid", mbid)
        if mbid_key in cache:
            return cache[mbid_key]
        row = conn.execute("SELECT id FROM albums WHERE mbid = ?", (mbid,)).fetchone()
        if row:
            cache[mbid_key] = cache[(artist_ids[0], title.lower())] = row[0]
            return row[0]

    key = (primary_artist_id, title.lower())
    if key in cache:
        album_id = cache[key]
    else:
        row = conn.execute(
            "SELECT id FROM albums WHERE artist_id = ? AND lower(title) = ?",
            (primary_artist_id, title.lower()),
        ).fetchone()
        if row:
            album_id = row[0]
        else:
            cur = conn.execute(
                "INSERT INTO albums (artist_id, title, year, mbid) VALUES (?, ?, ?, ?)",
                (primary_artist_id, title, year, mbid or None),
            )
            album_id = cur.lastrowid
            for position, artist_id in enumerate(artist_ids):
                conn.execute(
                    "INSERT OR IGNORE INTO album_artists (album_id, artist_id, position) VALUES (?, ?, ?)",
                    (album_id, artist_id, position),
                )
        cache[key] = album_id
    if mbid:
        _set_mbid_if_free(conn, "albums", album_id, mbid)
        cache[("mbid", mbid)] = album_id
    return album_id


def get_or_create_song(
    conn: sqlite3.Connection,
    cache: dict,
    artist_id: int,
    title: str,
    album_id: int | None = None,
    mbid: str | None = None,
) -> int:
    if mbid:
        mbid_key = ("mbid", mbid)
        if mbid_key in cache:
            return cache[mbid_key]
        row = conn.execute("SELECT id FROM songs WHERE mbid = ?", (mbid,)).fetchone()
        if row:
            cache[mbid_key] = cache[(artist_id, title.lower())] = row[0]
            return row[0]

    key = (artist_id, title.lower())
    if key in cache:
        song_id = cache[key]
    else:
        row = conn.execute(
            "SELECT id FROM songs WHERE artist_id = ? AND lower(title) = ?",
            (artist_id, title.lower()),
        ).fetchone()
        if row:
            song_id = row[0]
        else:
            cur = conn.execute(
                "INSERT INTO songs (artist_id, album_id, title, mbid) VALUES (?, ?, ?, ?)",
                (artist_id, album_id, title, mbid or None),
            )
            song_id = cur.lastrowid
        cache[key] = song_id
    if mbid:
        _set_mbid_if_free(conn, "songs", song_id, mbid)
        cache[("mbid", mbid)] = song_id
    if album_id:
        conn.execute("UPDATE songs SET album_id = ? WHERE id = ? AND album_id IS NULL", (album_id, song_id))
    return song_id


def get_or_create_venue(
    conn: sqlite3.Connection,
    cache: dict,
    setlistfm_id: str | None,
    name: str,
    city: str | None = None,
    state: str | None = None,
    country: str | None = None,
) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    key = setlistfm_id or ("name", name.lower(), city)
    if key in cache:
        return cache[key]
    row = None
    if setlistfm_id:
        row = conn.execute("SELECT id FROM venues WHERE setlistfm_id = ?", (setlistfm_id,)).fetchone()
    if row:
        venue_id = row[0]
    else:
        cur = conn.execute(
            "INSERT INTO venues (setlistfm_id, name, city, state, country) VALUES (?, ?, ?, ?, ?)",
            (setlistfm_id, name, city, state, country),
        )
        venue_id = cur.lastrowid
    cache[key] = venue_id
    return venue_id
